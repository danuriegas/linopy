#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Linopy module for solving lp files with different solvers."""
import io
import logging
import os
import re
import subprocess as sub
from pathlib import Path

import pandas as pd

available_solvers = []


if sub.run(["which", "glpsol"], stdout=sub.DEVNULL).returncode == 0:
    available_solvers.append("glpk")

if sub.run(["which", "cbc"], stdout=sub.DEVNULL).returncode == 0:
    available_solvers.append("cbc")

if sub.run(["which", "highs"], stdout=sub.DEVNULL).returncode == 0:
    available_solvers.append("highs")

try:
    import gurobipy

    available_solvers.append("gurobi")
except ModuleNotFoundError:
    None


try:
    import cplex

    available_solvers.append("cplex")
except ModuleNotFoundError:
    None

try:
    import xpress

    available_solvers.append("xpress")
except ModuleNotFoundError:
    None

logger = logging.getLogger(__name__)


def set_int_index(series):
    """Convert string index to int index."""
    series.index = series.index.str[1:].astype(int)
    return series


def maybe_convert_path(path):
    """Convert a pathlib.Path to a string."""
    return str(path.resolve()) if isinstance(path, Path) else path


def run_cbc(
    problem_fn,
    log_fn,
    solution_fn=None,
    warmstart_fn=None,
    basis_fn=None,
    **solver_options,
):
    """
    Solve a linear problem using the cbc solver.

    The function reads the linear problem file and passes it to the cbc
    solver. If the solution is successful it returns variable solutions and
    constraint dual values.
    For more information on the solver options, run 'cbc' in your shell
    """
    # printingOptions is about what goes in solution file
    command = f"cbc -printingOptions all -import {problem_fn} "

    if warmstart_fn:
        command += f"-basisI {warmstart_fn} "

    command += " ".join("-" + " ".join([k, str(v)]) for k, v in solver_options.items())
    command += f"-solve -solu {solution_fn} "

    if basis_fn:
        command += f"-basisO {basis_fn} "

    if not os.path.exists(solution_fn):
        os.mknod(solution_fn)

    if log_fn is None:
        p = sub.Popen(command.split(" "), stdout=sub.PIPE, stderr=sub.PIPE)
        for line in iter(p.stdout.readline, b""):
            print(line.decode(), end="")
        p.stdout.close()
        p.wait()
    else:
        log_f = open(log_fn, "w")
        p = sub.Popen(command.split(" "), stdout=log_f, stderr=log_f)
        p.wait()

    with open(solution_fn, "r") as f:
        data = f.readline()

    if data.startswith("Optimal - objective value"):
        status = "ok"
        termination_condition = "optimal"
    elif "Infeasible" in data:
        status = "warning"
        termination_condition = "infeasible"
    else:
        status = "warning"
        termination_condition = "other"

    if termination_condition != "optimal":
        return dict(status=status, termination_condition=termination_condition)

    objective = float(data[len("Optimal - objective value ") :])

    with open(solution_fn, "rb") as f:
        trimmed_sol_fn = re.sub(rb"\*\*\s+", b"", f.read())

    data = pd.read_csv(
        io.BytesIO(trimmed_sol_fn),
        header=None,
        skiprows=[0],
        sep=r"\s+",
        usecols=[1, 2, 3],
        index_col=0,
    )
    variables_b = data.index.str[0] == "x"

    solution = data[variables_b][2].pipe(set_int_index)
    dual = data[~variables_b][3].pipe(set_int_index)

    return dict(
        status=status,
        termination_condition=termination_condition,
        solution=solution,
        dual=dual,
        objective=objective,
    )


def run_glpk(
    problem_fn,
    log_fn,
    solution_fn=None,
    warmstart_fn=None,
    basis_fn=None,
    **solver_options,
):
    """
    Solve a linear problem using the glpk solver.

    This function reads the linear problem file and passes it to the glpk
    solver. If the solution is successful it returns variable solutions and
    constraint dual values.

    For more information on the glpk solver options:
    https://kam.mff.cuni.cz/~elias/glpk.pdf
    """
    # TODO use --nopresol argument for non-optimal solution output
    command = f"glpsol --lp {problem_fn} --output {solution_fn}"
    if log_fn is not None:
        command += f" --log {log_fn}"
    if warmstart_fn:
        command += f" --ini {warmstart_fn}"
    if basis_fn:
        command += f" -w {basis_fn}"
    command += " ".join("-" + " ".join([k, str(v)]) for k, v in solver_options.items())

    p = sub.Popen(command.split(" "), stdout=sub.PIPE, stderr=sub.PIPE)
    if log_fn is None:
        for line in iter(p.stdout.readline, b""):
            print(line.decode(), end="")
        p.stdout.close()
        p.wait()
    else:
        p.wait()

    f = open(solution_fn)

    def read_until_break(f):
        linebreak = False
        while not linebreak:
            line = f.readline()
            linebreak = line == "\n"
            yield line

    info = io.StringIO("".join(read_until_break(f))[:-2])
    info = pd.read_csv(info, sep=":", index_col=0, header=None)[1]
    termination_condition = info.Status.lower().strip()
    objective = float(re.sub(r"[^0-9\.\+\-e]+", "", info.Objective))

    if termination_condition in ["optimal", "integer optimal"]:
        status = "ok"
        termination_condition = "optimal"
    elif termination_condition == "undefined":
        status = "warning"
        termination_condition = "infeasible"
    else:
        status = "warning"

    if termination_condition != "optimal":
        return dict(status=status, termination_condition=termination_condition)

    dual_ = io.StringIO("".join(read_until_break(f))[:-2])
    dual_ = pd.read_fwf(dual_)[1:].set_index("Row name")
    if "Marginal" in dual_:
        dual = pd.to_numeric(dual_["Marginal"], "coerce").fillna(0).pipe(set_int_index)
    else:
        logger.warning("Shadow prices of MILP couldn't be parsed")
        dual = pd.Series(index=dual_.index, dtype=float).pipe(set_int_index)

    solution = io.StringIO("".join(read_until_break(f))[:-2])
    solution = (
        pd.read_fwf(solution)[1:]
        .set_index("Column name")["Activity"]
        .astype(float)
        .pipe(set_int_index)
    )
    f.close()

    return dict(
        status=status,
        termination_condition=termination_condition,
        solution=solution,
        dual=dual,
        objective=objective,
    )


def run_highs(
    problem_fn,
    log_fn,
    solution_fn=None,
    warmstart_fn=None,
    basis_fn=None,
    **solver_options
): 
    """
    Highs solver function. Reads a linear problem file and passes it to the highs
    solver. If the solution is feasible the function returns the objective,
    solution and dual constraint variables. Highs must be installed for usage.

    Installation
    -------------
    Installation manual: https://www.maths.ed.ac.uk/hall/HiGHS/
    After the "make" run, the 'highs' executables are in highs/build/bin/run/highs
    Once the "ctest" runs make sure you set the path. Path setting notes:
    >>> highs lib folder must be in "LD_LIBRARY_PATH" environment variable
    >>> To do this, insert the below path in the .bashrc (hidden in the home folder in Linux/Ubuntu)
    >>> LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/<your_path>/HiGHS/build/lib

    Architecture
    -------------
    The function reads and execute (i.e. subprocess.Popen,...) terminal
    commands of the solver. Meaning the command can be also executed at your
    command window/terminal if HiGHs is installed. Executing the commands on
    your local terminal helps to identify the raw outputs that are useful for
    developing the interface further.

    All functions below the "process = ..." do only read and save the outputs
    generated from the HiGHS solver. These parts are solver specific and
    depends on the solver output.

    Solver options
    ---------------
    Solver options are read by the 1) command window and the 2) option_file.txt

    1) An example list of solver options executable by the command window is given here:
    Examples:
    --model_file arg 	File of model to solve.
    --presolve arg 	    Presolve: "choose" by default - "on"/"off" are alternatives.
    --solver arg 	    Solver: "choose" by default - "simplex"/"ipm" are alternatives.
    --parallel arg 	    Parallel solve: "choose" by default - "on"/"off" are alternatives.
    --time_limit arg 	Run time limit (double).
    --options_file arg 	File containing HiGHS options.
    -h, --help 	        Print help.

    2) The options_file.txt gives some more options, see a full list here: 
    https://www.maths.ed.ac.uk/hall/HiGHS/HighsOptions.set 
    By default, we insert a couple of options for the ipm solver. The dictionary
    can be overwritten by simply giving the new values. For instance in PyPSA-Eur,
    you could write a dictionary replacing some of the default values or adding new
    options:
    ```
    solving:
        solver:
            name: highs,
            method: ipm,
            parallel: "on",
            <option_name>: <value>,
    ```
    Note, the <option_name> and <value> must be equivalent to the name convention
    of HiGHS.

    Output
    ------
    status : string,
        "ok" or "warning"
    termination_condition : string,
        Contains "optimal", "infeasible", 
    variables_sol : series
    constraints_dual : series
    objective : float
    """
    import logging, re, io, subprocess, sys, os

    default_dict = {
    "method" : "ipm",
    "primal_feasibility_tolerance" : 1e-04,
    "dual_feasibility_tolerance" : 1e-05,
    "ipm_optimality_tolerance" : 1e-6,
    "presolve" : "on",
    "run_crossover" : True,
    "parallel" : "off",
    "highs_min_threads" : 1,
    "highs_max_threads" : 8,
    "solution_file" : solution_fn,
    "write_solution_to_file" : True,
    "write_solution_pretty" : True,
    }
    # update default_dict by solver_dic (i.e. from PyPSA-Eur config.yaml)
    default_dict.update(solver_options)
    method = str(default_dict.pop("method", "ipm"))
    logger.info(f"Options: \"{default_dict}\". Docstring of function explains how to add/change options.")
    f1 = open("highs_options.txt","w")
    # write dict to a text file with options in each row
    f1.write(
        ' \n '.join([f"{str(k)} = {str(v)}" for k, v in default_dict.items()])
    )
    f1.close()

    # write (terminal) commands
    command = f"highs --model_file {problem_fn} "
    if warmstart_fn:
        logger.warning("Warmstart, probably not available at HiGHS yet")
    command += f"--solver {method} --options_file {os.getcwd()}/highs_options.txt"
    logger.info(f"Solver command: \"{command}\"")
    # execute command and store command window output
    process = subprocess.Popen(command.split(' '), stdout=subprocess.PIPE, universal_newlines=True)

    def read_until_break():
        # Function that reads line by line the command window
        while True:
            out = process.stdout.readline(1)
            if out == '' and process.poll() != None:
                break
            if out != '':
                yield out

    # converts stdout (standard terminal output) to pandas dataframe
    info = io.StringIO(''.join(read_until_break())[:])
    info = pd.read_csv(info, sep=':',  index_col=0, header=None)[1]
    # remove options.txt
    os.remove("highs_options.txt")

    # save raw solver output (info) as log
    with open(log_fn, 'w') as f:
        pd.options.display.max_colwidth = 100  # Otherwise trunctunated
        info_as_txt = info.to_string(header=False, index=True)
        f.write(info_as_txt)

    # read out termination_condition from `info`
    model_status = info[-15].lstrip()  # string with model_status
    if "optimal" in model_status:
        status = "ok"
        termination_condition = model_status
    elif "infeasible" in model_status:
        status = "warning"
        termination_condition = model_status
    else:
        status = 'warning'
        termination_condition = model_status
    objective = float(re.sub(r'[^0-9\.\+\-e]+', '', info[-2].lstrip()))

    # read out solution file (.sol)
    f = open(solution_fn,"rb")
    trimed_sol_fn = re.sub(rb'\*\*\s+', b'', f.read())
    f.close()
    sol = pd.read_csv(io.BytesIO(trimed_sol_fn), header=None, skiprows=[0],
                      sep=r'\s+', usecols=[0,1,2])
    sol = sol.rename(columns={0: "primal", 1: "dual", 2: "basis"})
    # filter primal and dual variables for "Rows" and "Columns"
    col_no = sol[(sol["primal"] == "Columns")].index[0]
    row_no = sol[(sol["primal"] == "Rows")].index[0]
    sol_rows = sol[sol.index > row_no]
    sol_cols = sol[(sol.index > col_no)&(sol.index < row_no)]
    constraints_dual = pd.to_numeric(sol_rows["dual"], errors="raise")
    variables_sol = pd.to_numeric(sol_cols["primal"], errors="raise")

    return dict(
        status=status,
        termination_condition=termination_condition,
        solution=variables_sol,
        dual=constraints_dual,
        objective=objective)


def run_cplex(
    problem_fn,
    log_fn,
    solution_fn=None,
    warmstart_fn=None,
    basis_fn=None,
    **solver_options,
):
    """
    Solve a linear problem using the cplex solver.

    This function reads the linear problem file and passes it to the cplex
    solver. If the solution is successful it returns variable solutions and
    constraint dual values. Cplex must be installed for using this function.

    Note if you pass additional solver_options, the key can specify deeper
    layered parameters, use a dot as a separator here,
    i.e. `**{'aa.bb.cc' : x}`.
    """
    m = cplex.Cplex()

    problem_fn = maybe_convert_path(problem_fn)
    log_fn = maybe_convert_path(log_fn)
    warmstart_fn = maybe_convert_path(warmstart_fn)
    basis_fn = maybe_convert_path(basis_fn)

    if log_fn is not None:
        log_f = open(log_fn, "w")
        m.set_results_stream(log_f)
        m.set_warning_stream(log_f)
        m.set_error_stream(log_f)
        m.set_log_stream(log_f)

    if solver_options is not None:
        for key, value in solver_options.items():
            param = m.parameters
            for key_layer in key.split("."):
                param = getattr(param, key_layer)
            param.set(value)

    m.read(problem_fn)

    if warmstart_fn:
        m.start.read_basis(warmstart_fn)
    m.solve()
    is_lp = m.problem_type[m.get_problem_type()] == "LP"

    if log_fn is not None:
        log_f.close()

    termination_condition = m.solution.get_status_string()
    if "optimal" in termination_condition:
        status = "ok"
        termination_condition = "optimal"
    else:
        status = "warning"
        return dict(status=status, termination_condition=termination_condition)

    if (status == "ok") and basis_fn and is_lp:
        try:
            m.solution.basis.write(basis_fn)
        except cplex.exceptions.errors.CplexSolverError:
            logger.info("No model basis stored")

    objective = m.solution.get_objective_value()

    solution = pd.Series(m.solution.get_values(), m.variables.get_names())
    solution = set_int_index(solution)

    if is_lp:
        dual = pd.Series(m.solution.get_dual_values(), m.linear_constraints.get_names())
    else:
        logger.warning("Shadow prices of MILP couldn't be parsed")
        dual = pd.Series(index=m.linear_constraints.get_names(), dtype=float)
    dual = set_int_index(dual)

    return dict(
        status=status,
        termination_condition=termination_condition,
        solution=solution,
        dual=dual,
        objective=objective,
        model=m,
    )


def run_gurobi(
    problem_fn,
    log_fn,
    solution_fn=None,
    warmstart_fn=None,
    basis_fn=None,
    **solver_options,
):
    """
    Solve a linear problem using the gurobi solver.

    This function reads the linear problem file and passes it to the gurobi
    solver. If the solution is successful it returns variable solutions and
    constraint dual values. Gurobipy must be installed for using this function
    For more information on solver options:
    https://www.gurobi.com/documentation/{gurobi_version}/refman/parameter_descriptions.html
    """
    # disable logging for this part, as gurobi output is doubled otherwise
    logging.disable(50)

    problem_fn = maybe_convert_path(problem_fn)
    log_fn = maybe_convert_path(log_fn)
    warmstart_fn = maybe_convert_path(warmstart_fn)
    basis_fn = maybe_convert_path(basis_fn)

    m = gurobipy.read(problem_fn)
    if solver_options is not None:
        for key, value in solver_options.items():
            m.setParam(key, value)
    if log_fn is not None:
        m.setParam("logfile", log_fn)

    if warmstart_fn:
        m.read(warmstart_fn)
    m.optimize()
    logging.disable(1)

    if basis_fn:
        try:
            m.write(basis_fn)
        except gurobipy.GurobiError as err:
            logger.info("No model basis stored. Raised error: ", err)

    Status = gurobipy.GRB.Status
    statusmap = {
        getattr(Status, s): s.lower() for s in Status.__dir__() if not s.startswith("_")
    }
    termination_condition = statusmap[m.status]

    if termination_condition == "optimal":
        status = "ok"
    elif termination_condition == "suboptimal":
        status = "warning"
    elif termination_condition == "inf_or_unbd":
        status = "warning"
        termination_condition = "infeasible or unbounded"
    else:
        status = "warning"

    if termination_condition not in ["optimal", "suboptimal"]:
        return dict(status=status, termination_condition=termination_condition)

    objective = m.ObjVal

    solution = pd.Series({v.VarName: v.x for v in m.getVars()})
    solution = set_int_index(solution)

    try:
        dual = pd.Series({c.ConstrName: c.Pi for c in m.getConstrs()})
    except AttributeError:
        logger.warning("Shadow prices of MILP couldn't be parsed")
        dual = pd.Series(index=[c.ConstrName for c in m.getConstrs()], dtype=float)
    dual = set_int_index(dual)

    return dict(
        status=status,
        termination_condition=termination_condition,
        solution=solution,
        dual=dual,
        objective=objective,
        model=m,
    )


def run_xpress(
    problem_fn,
    log_fn,
    solution_fn=None,
    warmstart_fn=None,
    basis_fn=None,
    **solver_options,
):
    """
    Solve a linear problem using the xpress solver.

    This function reads the linear problem file and passes it to
    the Xpress solver. If the solution is successful it returns
    variable solutions and constraint dual values. The xpress module
    must be installed for using this function.

    For more information on solver options:
    https://www.fico.com/fico-xpress-optimization/docs/latest/solver/GUID-ACD7E60C-7852-36B7-A78A-CED0EA291CDD.html
    """
    m = xpress.problem()

    problem_fn = maybe_convert_path(problem_fn)
    log_fn = maybe_convert_path(log_fn)
    warmstart_fn = maybe_convert_path(warmstart_fn)
    basis_fn = maybe_convert_path(basis_fn)

    m.read(problem_fn)
    m.setControl(solver_options)

    if log_fn is not None:
        m.setlogfile(log_fn)

    if warmstart_fn:
        m.readbasis(warmstart_fn)

    m.solve()

    if basis_fn:
        try:
            m.writebasis(basis_fn)
        except Exception as err:
            logger.info("No model basis stored. Raised error: ", err)

    termination_condition = m.getProbStatusString()

    if termination_condition == "mip_optimal" or termination_condition == "lp_optimal":
        status = "ok"
        termination_condition = "optimal"
    elif (
        termination_condition == "mip_unbounded"
        or termination_condition == "mip_infeasible"
        or termination_condition == "lp_unbounded"
        or termination_condition == "lp_infeasible"
        or termination_condition == "lp_infeas"
    ):
        status = "warning"
        termination_condition = "infeasible or unbounded"
    else:
        status = "warning"

    if termination_condition not in ["optimal"]:
        return dict(status=status, termination_condition=termination_condition)

    objective = m.getObjVal()

    var = [str(v) for v in m.getVariable()]
    solution = pd.Series(m.getSolution(var), index=var)
    solution = set_int_index(solution)

    try:
        dual = [str(d) for d in m.getConstraint()]
        dual = pd.Series(m.getDual(dual), index=dual)
    except xpress.SolverError:
        logger.warning("Shadow prices of MILP couldn't be parsed")
        dual = pd.Series(index=dual)
    dual = set_int_index(dual)

    return dict(
        status=status,
        termination_condition=termination_condition,
        solution=solution,
        dual=dual,
        objective=objective,
        model=m,
    )
