# -*- coding: utf-8 -*-
"""
Linopy variables module.

This module contains variable related definitions of the package.
"""

import functools
import re
from dataclasses import dataclass
from distutils.log import warn
from typing import Any, Sequence, Union
from warnings import warn

import dask
import numpy as np
import pandas as pd
from deprecation import deprecated
from numpy import floating, inf, issubdtype
from xarray import DataArray, Dataset, zeros_like

import linopy.expressions as expressions
from linopy.common import (
    _merge_inplace,
    has_assigned_model,
    has_optimized_model,
    is_constant,
)


def varwrap(method, *default_args, **new_default_kwargs):
    @functools.wraps(method)
    def _varwrap(obj, *args, **kwargs):
        for k, v in new_default_kwargs.items():
            kwargs.setdefault(k, v)
        obj = DataArray(obj)
        return Variable(method(obj, *default_args, *args, **kwargs))

    _varwrap.__doc__ = f"Wrapper for the xarray {method} function for linopy.Variable"
    if new_default_kwargs:
        _varwrap.__doc__ += f" with default arguments: {new_default_kwargs}"

    return _varwrap


class Variable(DataArray):
    """
    Variable container for storing variable labels.

    The Variable class is a subclass of xr.DataArray hence most xarray functions
    can be applied to it. However most arithmetic operations are overwritten.
    Like this one can easily combine variables into a linear expression.


    Examples
    --------
    >>> from linopy import Model
    >>> import pandas as pd
    >>> m = Model()
    >>> x = m.add_variables(pd.Series([0, 0]), 1, name="x")
    >>> y = m.add_variables(4, pd.Series([8, 10]), name="y")

    Add variable together:

    >>> x + y  # doctest: +SKIP
    Linear Expression with 2 term(s):
    ----------------------------------
    <BLANKLINE>
    Dimensions:  (dim_0: 2, _term: 2)
    Coordinates:
      * dim_0    (dim_0) int64 0 1
    Dimensions without coordinates: _term
    Data:
        coeffs   (dim_0, _term) int64 1 1 1 1
        vars     (dim_0, _term) int64 0 2 1 3

    Multiply them with a coefficient:

    >>> 3 * x  # doctest: +SKIP
    Linear Expression with 1 term(s):
    ----------------------------------
    <BLANKLINE>
    Dimensions:  (dim_0: 2, _term: 1)
    Coordinates:
      * dim_0    (dim_0) int64 0 1
    Dimensions without coordinates: _term
    Data:
        coeffs   (dim_0, _term) int64 3 3
        vars     (dim_0, _term) int64 0 1


    Further operations like taking the negative and subtracting are supported.
    """

    __slots__ = ("_cache", "_coords", "_indexes", "_name", "_variable", "model")

    def __init__(self, *args, **kwargs):

        # workaround until https://github.com/pydata/xarray/pull/5984 is merged
        if isinstance(args[0], DataArray):
            da = args[0]
            args = (da.data, da.coords)
            kwargs.update({"attrs": da.attrs, "name": da.name})

        self.model = kwargs.pop("model", None)
        super().__init__(*args, **kwargs)
        assert self.name is not None, "Variable data does not have a name."

    # We have to set the _reduce_method to None, in order to overwrite basic
    # reduction functions as `sum`. There might be a better solution (?).
    _reduce_method = None

    # Disable array function, only function defined below are supported
    # and set priority higher than pandas/xarray/numpy
    __array_ufunc__ = None
    __array_priority__ = 10000

    def __getitem__(self, keys) -> "ScalarVariable":
        keys = (keys,) if not isinstance(keys, tuple) else keys
        assert all(map(pd.api.types.is_scalar, keys)), (
            "The get function of Variable is different as of xarray.DataArray. "
            "Set single values for each dimension in order to obtain a "
            "ScalarVariable. For all other purposes, use `.sel` and `.isel`."
        )
        if not self.ndim:
            return ScalarVariable(self.data.item())
        assert self.ndim == len(keys), f"expected {self.ndim} keys, got {len(keys)}."
        key = dict(zip(self.dims, keys))
        selector = [self.get_index(k).get_loc(v) for k, v in key.items()]
        return ScalarVariable(self.data[tuple(selector)])

    def to_array(self):
        """
        Convert the variable array to a xarray.DataArray.
        """
        return DataArray(self)

    def to_linexpr(self, coefficient=1):
        """
        Create a linear exprssion from the variables.
        """
        if isinstance(coefficient, (expressions.LinearExpression, Variable)):
            raise TypeError(f"unsupported type of coefficient: {type(coefficient)}")
        return expressions.LinearExpression.from_tuples((coefficient, self))

    def __repr__(self):
        """
        Get the string representation of the variables.
        """
        data_string = (
            "Variable labels:\n" + self.to_array().__repr__().split("\n", 1)[1]
        )
        extend_line = "-" * len(self.name)
        return (
            f"Variable '{self.name}':\n"
            f"------------{extend_line}\n\n"
            f"{data_string}"
        )

    def _repr_html_(self):
        """
        Get the html representation of the variables.
        """
        # return self.__repr__()
        data_string = self.to_array()._repr_html_()
        data_string = data_string.replace("xarray.DataArray", "linopy.Variable")
        return data_string

    def __neg__(self):
        """
        Calculate the negative of the variables (converts coefficients only).
        """
        return self.to_linexpr(-1)

    def __mul__(self, other):
        """
        Multiply variables with a coefficient.
        """
        if isinstance(other, (expressions.LinearExpression, Variable)):
            raise TypeError(
                "unsupported operand type(s) for *: "
                f"{type(self)} and {type(other)}. "
                "Non-linear expressions are not yet supported."
            )
        return self.to_linexpr(other)

    def __rmul__(self, other):
        """
        Right-multiply variables with a coefficient.
        """
        return self.to_linexpr(other)

    def __div__(self, other):
        """
        Divide variables with a coefficient.
        """
        if isinstance(other, (expressions.LinearExpression, Variable)):
            raise TypeError(
                "unsupported operand type(s) for /: "
                f"{type(self)} and {type(other)}. "
                "Non-linear expressions are not yet supported."
            )
        return self.to_linexpr(1 / other)

    def __truediv__(self, coefficient):
        """
        True divide variables with a coefficient.
        """
        return self.__div__(coefficient)

    def __add__(self, other):
        """
        Add variables to linear expressions or other variables.
        """
        if isinstance(
            other, (Variable, DataArray, pd.DataFrame, pd.Series, np.ndarray)
        ):
            return expressions.LinearExpression.from_tuples((1, self), (1, other))
        elif isinstance(other, expressions.LinearExpression):
            return self.to_linexpr() + other
        else:
            raise TypeError(
                "unsupported operand type(s) for +: " f"{type(self)} and {type(other)}"
            )

    def __sub__(self, other):
        """
        Subtract linear expressions or other variables from the variables.
        """
        if isinstance(other, Variable):
            return expressions.LinearExpression.from_tuples((1, self), (-1, other))
        elif isinstance(other, expressions.LinearExpression):
            return self.to_linexpr() - other
        else:
            raise TypeError(
                "unsupported operand type(s) for -: " f"{type(self)} and {type(other)}"
            )

    def __le__(self, other):
        return self.to_linexpr().__le__(other)

    def __ge__(self, other):
        return self.to_linexpr().__ge__(other)

    def __eq__(self, other):
        return self.to_linexpr().__eq__(other)

    def groupby_sum(self, group):
        """
        Sum variable over groups.

        The function works in the same manner as the xarray.Dataset.groupby
        function, but automatically sums over all terms.

        Parameters
        ----------
        group : DataArray or IndexVariable
            Array whose unique values should be used to group the expressions.

        Returns
        -------
        Grouped linear expression.
        """
        return self.to_linexpr().groupby_sum(group)

    def group_terms(self, group):
        warn(
            'The function "group_terms" was renamed to "groupby_sum" and will be remove in v0.0.10.'
        )
        return self.groupby_sum(group)

    def rolling_sum(self, **kwargs):
        """
        Rolling sum of variable.

        Parameters
        ----------
        **kwargs :
            Keyword arguments passed to xarray.DataArray.rolling.

        Returns
        -------
        Rolling sum of variable.
        """
        return self.to_linexpr().rolling_sum(**kwargs)

    @property
    @has_assigned_model
    def upper(self):
        """
        Get the upper bounds of the variables.

        The function raises an error in case no model is set as a
        reference.
        """
        return self.model.variables.upper[self.name]

    @upper.setter
    @has_assigned_model
    @is_constant
    def upper(self, value):
        """
        Set the upper bounds of the variables.

        The function raises an error in case no model is set as a
        reference.
        """
        value = DataArray(value).broadcast_like(self)
        self.model.variables.upper[self.name] = value

    @property
    @has_assigned_model
    def lower(self):
        """
        Get the lower bounds of the variables.

        The function raises an error in case no model is set as a
        reference.
        """
        return self.model.variables.lower[self.name]

    @lower.setter
    @has_assigned_model
    @is_constant
    def lower(self, value):
        """
        Set the lower bounds of the variables.

        The function raises an error in case no model is set as a
        reference.
        """
        value = DataArray(value).broadcast_like(self)
        self.model.variables.lower[self.name] = value

    @property
    @has_optimized_model
    def sol(self):
        """
        Get the optimal values of the variable.

        The function raises an error in case no model is set as a
        reference or the model is not optimized.
        """
        if self.model.status != "ok":
            raise AttributeError("Underlying model not optimized.")
        return self.model.solution[self.name]

    def sum(self, dims=None):
        """
        Sum the variables over all or a subset of dimensions.

        This stack all terms of the dimensions, that are summed over, together.
        The function works exactly in the same way as ``LinearExpression.sum()``.

        Parameters
        ----------
        dims : str/list, optional
            Dimension(s) to sum over. The default is None which results in all
            dimensions.

        Returns
        -------
        linopy.LinearExpression
            Summed expression.
        """
        return self.to_linexpr().sum(dims)

    def where(self, cond, other=-1, **kwargs):
        """
        Filter variables based on a condition.

        This operation call ``xarray.DataArray.where`` but sets the default
        fill value to -1 and ensures preserving the linopy.Variable type.

        Parameters
        ----------
        cond : DataArray or callable
            Locations at which to preserve this object's values. dtype must be `bool`.
            If a callable, it must expect this object as its only parameter.
        other : scalar, DataArray, Variable, optional
            Value to use for locations in this object where ``cond`` is False.
            By default, these locations filled with -1.
        **kwargs :
            Keyword arguments passed to ``xarray.DataArray.where``

        Returns
        -------
        linopy.Variable
        """
        return self.__class__(DataArray.where(self, cond, other, **kwargs))

    def sanitize(self):
        """
        Sanitize variable by ensuring int dtype with fill value of -1.

        Returns
        -------
        linopy.Variable
        """
        if issubdtype(self.dtype, floating):
            return self.fillna(-1).astype(int)
        return self

    # Wrapped function which would convert variable to dataarray
    astype = varwrap(DataArray.astype)

    bfill = varwrap(DataArray.bfill)

    broadcast_like = varwrap(DataArray.broadcast_like)

    clip = varwrap(DataArray.clip)

    ffill = varwrap(DataArray.ffill)

    fillna = varwrap(DataArray.fillna)

    shift = varwrap(DataArray.shift, fill_value=-1)

    roll = varwrap(DataArray.roll)

    rolling = varwrap(DataArray.rolling)


@dataclass(repr=False)
class Variables:
    """
    A variables container used for storing multiple variable arrays.
    """

    labels: Dataset = Dataset()
    lower: Dataset = Dataset()
    upper: Dataset = Dataset()
    blocks: Dataset = Dataset()
    model: Any = None  # Model is not defined due to circular imports

    dataset_attrs = ["labels", "lower", "upper"]
    dataset_names = ["Labels", "Lower bounds", "Upper bounds"]

    def __getitem__(
        self, names: Union[str, Sequence[str]]
    ) -> Union[Variable, "Variables"]:
        if isinstance(names, str):
            return Variable(self.labels[names], model=self.model)

        return self.__class__(
            self.labels[names], self.lower[names], self.upper[names], self.model
        )

    def __repr__(self):
        """
        Return a string representation of the linopy model.
        """
        r = "linopy.model.Variables"
        line = "-" * len(r)
        r += f"\n{line}\n\n"
        # matches string between "Data variables" and "Attributes"/end of string
        coordspattern = r"(?s)(?<=\<xarray\.Dataset\>\n).*?(?=Data variables:)"
        datapattern = r"(?s)(?<=Data variables:).*?(?=($|\nAttributes))"
        for (k, K) in zip(self.dataset_attrs, self.dataset_names):
            orig = getattr(self, k).__repr__()
            if k == "labels":
                r += re.search(coordspattern, orig).group() + "\n"
            data = re.search(datapattern, orig).group()
            # drop first line which includes counter for long ds
            data = data.split("\n", 1)[1]
            r += f"{K}:\n{data}\n\n"
        return r

    def __iter__(self):
        return self.labels.__iter__()

    _merge_inplace = _merge_inplace

    def _ipython_key_completions_(self):
        """
        Provide method for the key-autocompletions in IPython.

        See http://ipython.readthedocs.io/en/stable/config/integrating.html#tab-completion
        For the details.
        """
        return list(self)

    def add(self, name, labels: DataArray, lower: DataArray, upper: DataArray):
        """
        Add variable `name`.
        """
        self._merge_inplace("labels", labels, name, fill_value=-1)
        self._merge_inplace("lower", lower, name, fill_value=-inf)
        self._merge_inplace("upper", upper, name, fill_value=inf)

    def remove(self, name):
        """
        Remove variable `name` from the variables.
        """
        for attr in self.dataset_attrs:
            ds = getattr(self, attr)
            if name in ds:
                setattr(self, attr, ds.drop_vars(name))

    @property
    def nvars(self):
        """
        Get the number all variables which were at some point added to the
        model.

        These also include variables with missing labels.
        """
        return self.ravel("labels", filter_missings=True).shape[0]

    @property
    def _binary_variables(self):
        return [v for v in self if self[v].attrs["binary"]]

    @property
    def _non_binary_variables(self):
        return [v for v in self if not self[v].attrs["binary"]]

    @property
    def binaries(self):
        """
        Get all binary variables.
        """
        return self[self._binary_variables]

    @property
    def non_binaries(self):
        """
        Get all non-binary variables.
        """
        return self[self._non_binary_variables]

    def get_name_by_label(self, label):
        """
        Get the variable name of the variable containing the passed label.

        Parameters
        ----------
        label : int
            Integer label within the range [0, MAX_LABEL] where MAX_LABEL is the last assigned
            variable label.

        Raises
        ------
        ValueError
            If label is not contained by any variable.

        Returns
        -------
        name : str
            Name of the containing variable.
        """
        if not isinstance(label, (float, int)) or label < 0:
            raise ValueError("Label must be a positive number.")
        for name, labels in self.labels.items():
            if label in labels:
                return name
        raise ValueError(f"No variable found containing the label {label}.")

    def iter_ravel(self, key, filter_missings=False):
        """
        Create an generator which iterates over all arrays in `key` and
        flattens them.

        Parameters
        ----------
        key : str/Dataset
            Key to be iterated over. Optionally pass a dataset which is
            broadcastable to the variable labels.
        filter_missings : bool, optional
            Filter out values where the variables labels are -1. This will
            raise an error if the filtered data still contains nan's.
            When enabled, the data is loaded into memory. The default is False.


        Yields
        ------
        flat : np.array/dask.array
        """
        if isinstance(key, str):
            ds = getattr(self, key)
        elif isinstance(key, Dataset):
            ds = key
        else:
            raise TypeError("Argument `key` must be of type string or xarray.Dataset")

        for name, labels in self.labels.items():

            broadcasted = ds[name].broadcast_like(labels)
            if labels.chunks is not None:
                broadcasted = broadcasted.chunk(labels.chunks)

            if filter_missings:
                flat = np.ravel(broadcasted)
                flat = flat[np.ravel(labels) != -1]
                if pd.isna(flat).any():
                    ds_name = self.dataset_names[self.dataset_attrs.index(key)]
                    err = f"{ds_name} of variable '{name}' contains nan's."
                    raise ValueError(err)
            else:
                flat = broadcasted.data.ravel()
            yield flat

    def ravel(self, key, filter_missings=False, compute=True):
        """
        Ravel and concate all arrays in `key` while aligning to
        `broadcast_like`.

        Parameters
        ----------
        key : str/Dataset
            Key to be iterated over. Optionally pass a dataset which is
            broadcastable to `broadcast_like`.
        broadcast_like : str, optional
            Name of the dataset to which the input data in `key` is aligned to.
            The default is "labels".
        filter_missings : bool, optional
            Filter out values where `broadcast_like` data is -1.
            The default is False.
        compute : bool, optional
            Whether to compute lazy data. The default is False.

        Returns
        -------
        flat
            One dimensional data with all values in `key`.
        """
        res = np.concatenate(list(self.iter_ravel(key, filter_missings)))
        if compute:
            return dask.compute(res)[0]
        else:
            return res

    def get_blocks(self, blocks: DataArray):
        """
        Get a dataset of same shape as variables.labels indicating the blocks.
        """
        dim = blocks.dims[0]
        assert dim in self.labels.dims, "Block dimension not in variables."

        block_map = zeros_like(self.labels, dtype=blocks.dtype)
        for name, variable in self.labels.items():
            if dim in variable.dims:
                block_map[name] = blocks.broadcast_like(variable)
        return block_map.where(self.labels != -1, -1)

    def blocks_to_blockmap(self, block_map, dtype=np.int8):
        """
        Get a one-dimensional array mapping the variables to blocks.
        """
        # non-assigned variables are assumed to be masked, insert -1
        res = np.full(self.model._xCounter + 1, -1, dtype=dtype)
        for name, labels in self.labels.items():
            res[np.ravel(labels)] = np.ravel(block_map[name])
        res[-1] = -1
        return res


@dataclass
class ScalarVariable:
    label: int
    coords: dict = None

    def to_scalar_linexpr(self, coeff=1):
        if not isinstance(coeff, (int, np.integer, float)):
            raise TypeError(f"Coefficient must be a numeric value, got {type(coeff)}.")
        return expressions.ScalarLinearExpression((coeff,), (self.label,))

    def to_linexpr(self, coeff=1):
        return self.to_scalar_linexpr(coeff).to_linexpr()

    def __neg__(self):
        return self.to_scalar_linexpr(-1)

    def __add__(self, other):
        return self.to_scalar_linexpr(1) + other

    def __radd__(self, other):
        # This is needed for using python's sum function
        if other == 0:
            return self

    def __sub__(self, other):
        return self.to_scalar_linexpr(1) - other

    def __mul__(self, coeff):
        return self.to_scalar_linexpr(coeff)

    def __rmul__(self, coeff):
        return self.to_scalar_linexpr(coeff)

    def __div__(self, coeff):
        return self.to_scalar_linexpr(1 / coeff)

    def __truediv__(self, coeff):
        return self.__div__(coeff)

    def __le__(self, other):
        return self.to_scalar_linexpr(1).__le__(other)

    def __ge__(self, other):
        return self.to_scalar_linexpr(1).__ge__(other)

    def __eq__(self, other):
        return self.to_scalar_linexpr(1).__eq__(other)
