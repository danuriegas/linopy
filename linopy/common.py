#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Linopy common module.

This module contains commonly used functions.
"""

from functools import partialmethod, update_wrapper, wraps

import numpy as np
from xarray import DataArray, apply_ufunc, merge


def _merge_inplace(self, attr, da, name, **kwargs):
    """
    Assign a new dataarray to the dataset `attr` by merging.

    This takes care of all coordinate alignments, instead of a direct
    assignment like self.variables[name] = var
    """
    ds = merge([getattr(self, attr), da.rename(name)], **kwargs)
    setattr(self, attr, ds)


def as_dataarray(arr):
    """
    Convert an object to a DataArray if it is not already a DataArray.
    """
    if not isinstance(arr, DataArray):
        return DataArray(arr)
    return arr


def _remap(array, mapping):
    return mapping[array.ravel()].reshape(array.shape)


def replace_by_map(ds, mapping):
    """
    Replace values in a DataArray by a one-dimensional mapping.
    """
    return apply_ufunc(
        _remap,
        ds,
        kwargs=dict(mapping=mapping),
        dask="parallelized",
        output_dtypes=[mapping.dtype],
    )


def best_int(max_value):
    """
    Get the minimal int dtype for storing values <= max_value.
    """
    for t in (np.int8, np.int16, np.int32, np.int64):
        if max_value <= np.iinfo(t).max:
            return t


def has_assigned_model(func):
    """
    Check if a reference model is set.
    """

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if self.model is None:
            raise AttributeError("No reference model set.")
        return func(self, *args, **kwargs)

    return wrapper


def has_optimized_model(func):
    """
    Check if a reference model is set.
    """

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if self.model is None:
            raise AttributeError("No reference model set.")
        if self.model.status != "ok":
            raise AttributeError("Underlying model not optimized.")
        return func(self, *args, **kwargs)

    return wrapper


def is_constant(func):
    from linopy import expressions, variables

    @wraps(func)
    def wrapper(self, arg):
        if isinstance(arg, (variables.Variable, expressions.LinearExpression)):
            raise TypeError(f"Assigned rhs must be a constant, got {type()}).")
        return func(self, arg)

    return wrapper


def forward_as_properties(**routes):
    def add_accessor(cls, item, attr):
        @property
        def get(self):
            return getattr(getattr(self, item), attr)

        setattr(cls, attr, get)

    def deco(cls):
        for item, attrs in routes.items():
            for attr in attrs:
                add_accessor(cls, item, attr)
        return cls

    return deco


def monkey_patch(cls, pass_unpatched_method=False):
    def deco(func):
        wrapped = getattr(cls, func.__name__)
        if pass_unpatched_method:
            func = partialmethod(func, unpatched_method=wrapped)
        update_wrapper(func, wrapped)
        setattr(cls, func.__name__, func)
        return func

    return deco
