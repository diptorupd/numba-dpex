# SPDX-FileCopyrightText: 2020 - 2024 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0

import copy
import logging

import dpnp
import numpy as np
from numba.core import types

from numba_dpex.core.typing import dpnpdecl
from numba_dpex.kernel_api_impl.spirv.math import mathimpl

# A global instance of dpnp ufuncs that are supported by numba-dpex
_dpnp_ufunc_db = None


def _lazy_init_dpnp_db():
    global _dpnp_ufunc_db

    if _dpnp_ufunc_db is None:
        _dpnp_ufunc_db = {}
        _fill_ufunc_db_with_dpnp_ufuncs(_dpnp_ufunc_db)


def get_ufuncs():
    """Returns the list of supported dpnp ufuncs in the _dpnp_ufunc_db"""

    _lazy_init_dpnp_db()

    return _dpnp_ufunc_db.keys()


def get_ufunc_info(ufunc_key):
    """get the lowering information for the ufunc with key ufunc_key.

    The lowering information is a dictionary that maps from a numpy
    loop string (as given by the ufunc types attribute) to a function
    that handles code generation for a scalar version of the ufunc
    (that is, generates the "per element" operation").

    raises a KeyError if the ufunc is not in the ufunc_db
    """
    _lazy_init_dpnp_db()
    return _dpnp_ufunc_db[ufunc_key]


def _fill_ufunc_db_with_dpnp_ufuncs(ufunc_db):
    """Populates the _dpnp_ufunc_db from Numba's NumPy ufunc_db"""

    from numba.np.ufunc_db import _lazy_init_db

    _lazy_init_db()

    # we need to import it after, because before init it is None and
    # variable is passed by value
    from numba.np.ufunc_db import _ufunc_db

    failed_dpnpop_types_lst = []
    for ufuncop in dpnpdecl.supported_ufuncs:
        if ufuncop == "erf":
            op = getattr(dpnp, "erf")
            op.nin = 1
            op.nout = 1
            op.nargs = 2
            op.types = ["f->f", "d->d"]
            op.is_dpnp_ufunc = True

            _unary_d_d = types.float64(types.float64)
            _unary_f_f = types.float32(types.float32)
            ufunc_db[op] = {
                "f->f": mathimpl.lower_ocl_impl[("erf", (_unary_f_f))],
                "d->d": mathimpl.lower_ocl_impl[("erf", (_unary_d_d))],
            }
        else:
            dpnpop = getattr(dpnp, ufuncop)
            npop = getattr(np, ufuncop)
            if not hasattr(dpnpop, "nin"):
                dpnpop.nin = npop.nin
            if not hasattr(dpnpop, "nout"):
                dpnpop.nout = npop.nout
            if not hasattr(dpnpop, "nargs"):
                dpnpop.nargs = dpnpop.nin + dpnpop.nout

            # Check if the dpnp operation has a `types` attribute and if an
            # AttributeError gets raised then "monkey patch" the attribute from
            # numpy. If the attribute lookup raised a ValueError, it indicates
            # that dpnp could not be resolve the supported types for the
            # operation. Dpnp will fail to resolve the `types` if no SYCL
            # devices are available on the system. For such a scenario, we log
            # dpnp operations for which the ValueError happened and print them
            # as a user-level warning. It is done this way so that the failure
            # to load the dpnpdecl registry due to the ValueError does not
            # impede a user from importing numba-dpex.
            try:
                dpnpop.types
            except ValueError:
                failed_dpnpop_types_lst.append(ufuncop)
            except AttributeError:
                dpnpop.types = npop.types

            dpnpop.is_dpnp_ufunc = True
            cp = copy.copy(_ufunc_db[npop])
            ufunc_db.update({dpnpop: cp})
            for key in list(ufunc_db[dpnpop].keys()):
                if (
                    "FF->" in key
                    or "DD->" in key
                    or "F->" in key
                    or "D->" in key
                ):
                    ufunc_db[dpnpop].pop(key)

    if failed_dpnpop_types_lst:
        try:
            getattr(dpnp, failed_dpnpop_types_lst[0]).types
        except ValueError:
            ops = " ".join(failed_dpnpop_types_lst)
            logging.exception(
                "The types attribute for the following dpnp ops could not be "
                f"determined: {ops}"
            )
