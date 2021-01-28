from __future__ import absolute_import, print_function

import numpy as np

# from numba.targets.descriptors import TargetDescriptor
# from numba.targets.options import TargetOptions
# import numba_dppy, numba_dppy as dppy
from numba_dppy import kernel, autojit
from .descriptor import dppy_target

# from numba.npyufunc.deviceufunc import (UFuncMechanism, GenerializedUFunc,
#                                       GUFuncCallSteps)

from .. import dispatcher, utils, typing
from .compiler import DPPYCompiler


class DPPYDispatcher(dispatcher.Dispatcher):
    targetdescr = dppy_target

    def __init__(self, py_func, locals={}, targetoptions={}):
        assert not locals
        self.py_func = py_func
        self.targetoptions = targetoptions
        self.doc = py_func.__doc__
        self._compiled = None

    def compile(self, sig, locals={}, **targetoptions):
        assert self._compiled is None
        assert not locals
        options = self.targetoptions.copy()
        options.update(targetoptions)
        kernel = kernel(sig, **options)(self.py_func)
        self._compiled = kernel
        if hasattr(kernel, "_npm_context_"):
            self._npm_context_ = kernel._npm_context_

    @property
    def compiled(self):
        if self._compiled is None:
            self._compiled = autojit(self.py_func, **self.targetoptions)
        return self._compiled

    def __call__(self, *args, **kws):
        return self.compiled(*args, **kws)

    def disable_compile(self, val=True):
        """Disable the compilation of new signatures at call time."""
        # Do nothing
        pass

    def configure(self, *args, **kws):
        return self.compiled.configure(*args, **kws)

    def __getitem__(self, *args):
        return self.compiled.__getitem__(*args)

    def __getattr__(self, key):
        return getattr(self.compiled, key)


class DPPYUFuncDispatcher(object):
    """
    Invoke the OpenCL ufunc specialization for the given inputs.
    """

    def __init__(self, types_to_retty_kernels):
        self.functions = types_to_retty_kernels
        self._maxblocksize = 0  # ignored

    @property
    def max_blocksize(self):
        return self._maxblocksize

    @max_blocksize.setter
    def max_blocksize(self, blksz):
        self._max_blocksize = blksz

    def __call__(self, *args, **kws):
        """
        *args: numpy arrays or DeviceArrayBase (created by ocl.to_device).
               Cannot mix the two types in one call.

        **kws:
            stream -- ocl stream; when defined, asynchronous mode is used.
            out    -- output array. Can be a numpy array or DeviceArrayBase
                      depending on the input arguments.  Type must match
                      the input arguments.
        """
        return DPPYUFuncMechanism.call(self.functions, args, kws)

    def reduce(self, arg, stream=0):
        assert len(list(self.functions.keys())[0]) == 2, "must be a binary " "ufunc"
        assert arg.ndim == 1, "must use 1d array"

        n = arg.shape[0]
        gpu_mems = []

        if n == 0:
            raise TypeError("Reduction on an empty array.")
        elif n == 1:  # nothing to do
            return arg[0]

        # always use a stream
        stream = stream or ocl.stream()
        with stream.auto_synchronize():
            # transfer memory to device if necessary
            if devicearray.is_ocl_ndarray(arg):
                mem = arg
            else:
                mem = ocl.to_device(arg, stream)
            # reduce by recursively spliting and operating
            out = self.__reduce(mem, gpu_mems, stream)
            # store the resultong scalar in a [1,] buffer
            buf = np.empty(
                [
                    out.size,
                ],
                dtype=out.dtype,
            )
            # copy the result back to host
            out.copy_to_host(buf, stream=stream)

        return buf[0]

    def __reduce(self, mem, gpu_mems, stream):
        n = mem.shape[0]
        if n % 2 != 0:  # odd?
            fatcut, thincut = mem.split(n - 1)
            # prevent freeing during async mode
            gpu_mems.append(fatcut)
            gpu_mems.append(thincut)
            # execute the kernel
            out = self.__reduce(fatcut, gpu_mems, stream)
            gpu_mems.append(out)
            return self(out, thincut, out=out, stream=stream)
        else:  # even?
            left, right = mem.split(n // 2)
            # prevent freeing during async mode
            gpu_mems.append(left)
            gpu_mems.append(right)
            # execute the kernel
            self(left, right, out=left, stream=stream)
            if n // 2 > 1:
                return self.__reduce(left, gpu_mems, stream)
            else:
                return left


class _DPPYGUFuncCallSteps(GUFuncCallSteps):
    __slots__ = [
        "_stream",
    ]

    def is_device_array(self, obj):
        return devicearray.is_ocl_ndarray(obj)

    def to_device(self, hostary):
        return ocl.to_device(hostary, stream=self._stream)

    def to_host(self, devary, hostary):
        out = devary.copy_to_host(hostary, stream=self._stream)
        return out

    def device_array(self, shape, dtype):
        return ocl.device_array(shape=shape, dtype=dtype, stream=self._stream)

    def prepare_inputs(self):
        self._stream = self.kwargs.get("stream", 0)

    def launch_kernel(self, kernel, nelem, args):
        kernel.forall(nelem, queue=self._stream)(*args)


class DPPYGenerializedUFunc(GenerializedUFunc):
    @property
    def _call_steps(self):
        return _DPPYGUFuncCallSteps

    def _broadcast_scalar_input(self, ary, shape):
        return devicearray.DeviceNDArray(
            shape=shape, strides=(0,), dtype=ary.dtype, gpu_data=ary.gpu_data
        )

    def _broadcast_add_axis(self, ary, newshape):
        newax = len(newshape) - len(ary.shape)
        # Add 0 strides for missing dimension
        newstrides = (0,) * newax + ary.strides
        return devicearray.DeviceNDArray(
            shape=newshape, strides=newstrides, dtype=ary.dtype, gpu_data=ary.gpu_data
        )


class DPPYUFuncMechanism(UFuncMechanism):
    """
    Provide OpenCL specialization
    """

    DEFAULT_STREAM = 0
    ARRAY_ORDER = "A"

    def launch(self, func, count, stream, args):
        func.forall(count, queue=stream)(*args)

    def is_device_array(self, obj):
        return devicearray.is_ocl_ndarray(obj)

    def to_device(self, hostary, stream):
        return ocl.to_device(hostary, stream=stream)

    def to_host(self, devary, stream):
        return devary.copy_to_host(stream=stream)

    def device_array(self, shape, dtype, stream):
        return ocl.device_array(shape=shape, dtype=dtype, stream=stream)

    def broadcast_device(self, ary, shape):
        ax_differs = [
            ax
            for ax in range(len(shape))
            if ax >= ary.ndim or ary.shape[ax] != shape[ax]
        ]

        missingdim = len(shape) - len(ary.shape)
        strides = [0] * missingdim + list(ary.strides)

        for ax in ax_differs:
            strides[ax] = 0

        return devicearray.DeviceNDArray(
            shape=shape, strides=strides, dtype=ary.dtype, gpu_data=ary.gpu_data
        )