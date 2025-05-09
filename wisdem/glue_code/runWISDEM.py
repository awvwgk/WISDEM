import os
import sys
import logging
import warnings
import time

import numpy as np
import openmdao.api as om
from openmdao.utils.mpi import MPI
if MPI:
    max_cores = MPI.COMM_WORLD.Get_size()
    
from wisdem.commonse import fileIO
from wisdem.glue_code.glue_code import WindPark
from wisdem.glue_code.gc_LoadInputs import WindTurbineOntologyPython
from wisdem.glue_code.gc_WT_InitModel import yaml2openmdao
from wisdem.glue_code.gc_PoseOptimization import PoseOptimization

# Numpy deprecation warnings
warnings.filterwarnings("ignore", category=np.exceptions.VisibleDeprecationWarning)

# Suppress the maxfev warnings is scipy _minpack_py, line:175
warnings.simplefilter("ignore", RuntimeWarning, lineno=175)
warnings.simplefilter("ignore", RuntimeWarning, lineno=177)

def run_wisdem(fname_wt_input, fname_modeling_options, fname_opt_options, overridden_values=None, run_only=False):
    t0 = time.time()

    # Load all yaml inputs and validate (also fills in defaults)
    wt_initial = WindTurbineOntologyPython(fname_wt_input, fname_modeling_options, fname_opt_options)
    wt_init, modeling_options, opt_options = wt_initial.get_input_data()

    myopt = PoseOptimization(wt_init, modeling_options, opt_options)

    folder_output = opt_options["general"]["folder_output"]

    os.makedirs(folder_output, exist_ok=True)

    # create logger
    logger = logging.getLogger("wisdem/weis")
    logger.setLevel(logging.INFO)

    # create handlers
    ht = logging.StreamHandler()
    ht.setLevel(logging.WARNING)

    flog = os.path.join(folder_output, opt_options["general"]["fname_output"] + ".log")
    hf = logging.FileHandler(flog, mode="w")
    hf.setLevel(logging.INFO)

    # create formatters
    formatter_t = logging.Formatter("%(module)s:%(funcName)s:%(lineno)d %(levelname)s:%(message)s")
    formatter_f = logging.Formatter(
        "P%(process)d %(asctime)s %(module)s:%(funcName)s:%(lineno)d %(levelname)s:%(message)s"
    )

    # add formatter to handlers
    ht.setFormatter(formatter_t)
    hf.setFormatter(formatter_f)

    # add handlers to logger
    logger.addHandler(ht)
    logger.addHandler(hf)
    logger.info("Started")

    if MPI and opt_options["opt_flag"] and not run_only:
        # Parallel settings for OpenMDAO
        wt_opt = om.Problem(model=om.Group(num_par_fd=max_cores), reports=False)
        wt_opt.model.add_subsystem(
            "comp", WindPark(modeling_options=modeling_options, opt_options=opt_options), promotes=["*"]
        )
    else:
        # Sequential finite differencing
        wt_opt = om.Problem(
            model=WindPark(modeling_options=modeling_options, opt_options=opt_options), reports=False
        )

    # If at least one of the design variables is active, setup an optimization
    if opt_options["opt_flag"] and not run_only:
        wt_opt = myopt.set_driver(wt_opt)
        wt_opt = myopt.set_objective(wt_opt)
        wt_opt = myopt.set_design_variables(wt_opt, wt_init)
        wt_opt = myopt.set_constraints(wt_opt)
        wt_opt = myopt.set_recorders(wt_opt)

    if modeling_options["General"]["verbosity"] == False:
        wt_opt.set_solver_print(level=-1)

    # Set working directory and setup openmdao problem
    wt_opt.options['work_dir'] = folder_output
    wt_opt.setup()

    # Load initial wind turbine data from wt_initial to the openmdao problem
    wt_opt = yaml2openmdao(wt_opt, modeling_options, wt_init, opt_options)
    wt_opt = myopt.set_initial(wt_opt, wt_init)

    # If the user provides values in this dict, they overwrite
    # whatever values have been set by the yaml files.
    # This is useful for performing black-box wrapped optimization without
    # needing to modify the yaml files.
    if overridden_values is not None:
        for key in overridden_values:
            wt_opt[key] = overridden_values[key]

    # Place the last design variables from a previous run into the problem.
    # This needs to occur after the above setup() and yaml2openmdao() calls
    # so these values are correctly placed in the problem.
    wt_opt = myopt.set_restart(wt_opt)

    if "check_totals" in opt_options["driver"] and not run_only:
        if opt_options["driver"]["check_totals"]:
            wt_opt.run_model()
            totals = wt_opt.compute_totals()

    if "check_partials" in opt_options["driver"] and not run_only:
        if opt_options["driver"]["check_partials"]:
            wt_opt.run_model()
            checks = wt_opt.check_partials(compact_print=True)

    sys.stdout.flush()

    if opt_options["driver"]["step_size_study"]["flag"] and not run_only:
        wt_opt.run_model()
        study_options = opt_options["driver"]["step_size_study"]
        step_sizes = study_options["step_sizes"]
        all_derivs = {}
        for idx, step_size in enumerate(step_sizes):
            wt_opt.model.approx_totals(method="fd", step=step_size, form=study_options["form"])

            if study_options["of"]:
                of = study_options["of"]
            else:
                of = None

            if study_options["wrt"]:
                wrt = study_options["wrt"]
            else:
                wrt = None

            derivs = wt_opt.compute_totals(of=of, wrt=wrt, driver_scaling=study_options["driver_scaling"])
            all_derivs[idx] = derivs
            all_derivs[idx]["step_size"] = step_size
        np.save("total_derivs.npy", all_derivs)

    # Run openmdao problem
    elif opt_options["opt_flag"] and not run_only:
        wt_opt.run_driver()
    else:
        wt_opt.run_model()

    # Save data coming from openmdao to an output yaml file
    froot_out = os.path.join(folder_output, opt_options["general"]["fname_output"])
    wt_initial.write_ontology(wt_opt, froot_out)
    wt_initial.write_options(froot_out)

    # Save data to numpy and matlab arrays
    fileIO.save_data(froot_out, wt_opt)

    t1 = time.time()
    if MPI:
        rank = MPI.COMM_WORLD.Get_rank()
    else:
        rank = 0
    if rank == 0:
        print("WISDEM run completed in,", t1-t0, "seconds")

    return wt_opt, modeling_options, opt_options


def load_wisdem(frootin):
    froot,fext = os.path.splitext(frootin)
    if fext not in ['.yaml','.pkl']:
        froot = frootin
    fgeom = froot + ".yaml"
    fmodel = froot + "-modeling.yaml"
    fopt = froot + "-analysis.yaml"
    fpkl = froot + ".pkl"

    # Load all yaml inputs and validate (also fills in defaults)
    wt_initial = WindTurbineOntologyPython(fgeom, fmodel, fopt)
    wt_init, modeling_options, opt_options = wt_initial.get_input_data()

    wt_opt = om.Problem(model=WindPark(modeling_options=modeling_options, opt_options=opt_options), reports=False)
    wt_opt.setup()

    wt_opt = fileIO.load_data(fpkl, wt_opt)

    return wt_opt, modeling_options, opt_options
