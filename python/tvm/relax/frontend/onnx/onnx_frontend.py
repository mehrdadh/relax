# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""ONNX: Open Neural Network Exchange importer for Relax.

This module implemnets the required functionality to read ONNX models
and convert them into equivalent Relax functions. The entry point that encapsulates
this functionality is the function from_onnx.

In order to extend the functionality of the importer, you can add new
operators to the operator registry. The operator registry is a dictionary
that maps operator names to operator converters. The registry is defined
in the _get_converter_map function. To add a new operator, you can define
a new class that inherits from the OnnxOpConverter class and implement
the _impl method.

By default, ONNX defines models in terms of dynamic shapes. The ONNX importer
retains dynamic shapes upon import, and when possible, the compiler attempts to
convert the model to use static shapes at compile time.
If this fails, there may still be dynamic operations in the model.
Not all TVM kernels currently support dynamic shapes, please file an issue on
github.com/apache/tvm/issues if you hit an error with dynamic kernels.
"""
import warnings
from typing import Union, Tuple, Optional, List, Dict, Any

import numpy as _np

import tvm
from tvm import relax, topi
from tvm.ir import IRModule
from tvm.ir.supply import NameSupply
from tvm.relax import testing
from tvm.relax.frontend.common import attach_span, emit_te_with_span

import onnx.onnx_ml_pb2


def get_type(elem_type: Union[str, int]) -> str:
    """Converts onnx integer datatype to numpy datatype"""
    # If a string was passed instead of a tensor type, it does not need
    # conversion and can be returned.
    if isinstance(elem_type, str):
        return elem_type

    try:
        from onnx.mapping import TENSOR_TYPE_TO_NP_TYPE  # pylint: disable=import-outside-toplevel
    except ImportError as exception:
        raise ImportError("Unable to import onnx which is required {}".format(exception))

    return str(TENSOR_TYPE_TO_NP_TYPE[elem_type])


def get_info(info_proto: onnx.onnx_ml_pb2.ValueInfoProto) -> Tuple[str, List, str, List]:
    """Extract the shape from a ValueInfoProto.

    Parameters
    ----------
    info_proto: onnx.onnx_ml_pb2.ValueInfoProto
        The ValueInfoProto to extract the info from.

    Returns
    -------
    Tuple[str, List, str, List]
        The name, shape, type, and shape name of the ValueInfoProto.
    """
    shape = []
    shape_name = []
    for dim in info_proto.type.tensor_type.shape.dim:
        name = dim.dim_param
        value = dim.dim_value
        if value is None or value == 0:
            value = tvm.tir.Var("dyn", "int64")
            shape_name.append(name)
        else:
            shape_name.append(value)
        shape.append(value)

    name = info_proto.name
    if info_proto.type.tensor_type.elem_type:
        dtype = get_type(info_proto.type.tensor_type.elem_type)
    else:
        dtype = None
    return name, shape, dtype, shape_name


def get_numpy(tensor_proto: onnx.onnx_ml_pb2.TensorProto) -> _np.ndarray:
    """Grab data in TensorProto and convert to numpy array."""
    try:
        from onnx.numpy_helper import to_array  # pylint: disable=import-outside-toplevel
    except ImportError as exception:
        raise ImportError("Unable to import onnx which is required {}".format(exception))
    return to_array(tensor_proto)


class onnx_input(list):  # pylint: disable=invalid-name
    """A list that returns None when out-of-bounds indices are accessed."""

    def __getitem__(self, item):
        if isinstance(item, slice):
            if item.stop is None:
                stop = len(self)
            else:
                stop = item.stop
            indices = list(range(stop)[item])
            return [self[i] for i in indices]
        if isinstance(item, int):
            return list(self)[item] if item < len(self) else None
        raise TypeError("list indices must be integers or slices, not %s" % type(item).__name__)


# pylint: disable=invalid-name, len-as-condition, unused-argument, too-many-lines, redefined-builtin
class OnnxOpConverter(object):
    """A helper class for holding the common logic for ONNX op converters.
    Each converter maps to a single ONNX op and defines the equivalent
    functionality using Relax expressions. The converter can define multiple versions
    of the op and the version is selected based on the opset version of the model.
    """

    @classmethod
    def get_converter(cls, opset):
        """Get converter matches given opset.

        Parameters
        ----------
        opset: int
            opset from model.

        Returns
        -------
        converter, which should be `_impl_vx`. Number x is the biggest
            number smaller than or equal to opset belongs to all support versions.
        """
        versions = [int(d.replace("_impl_v", "")) for d in dir(cls) if "_impl_v" in d]
        versions = sorted(versions + [opset])
        version = versions[max([i for i, v in enumerate(versions) if v == opset]) - 1]
        if hasattr(cls, "_impl_v{}".format(version)):
            return getattr(cls, "_impl_v{}".format(version))
        raise NotImplementedError(
            "opset version {} of {} not implemented".format(version, cls.__name__)
        )


class MatMul(OnnxOpConverter):
    """Converts an onnx MatMul node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return attach_span(relax.op.matmul(inputs[0], inputs[1]))


class Div(OnnxOpConverter):
    """Converts an onnx Div node into an equivalent Relax expression."""

    @classmethod
    def _impl_v14(cls, bb, inputs, attr):
        if all([isinstance(inp, relax.Constant) for inp in inputs]):
            output = inputs[0].data.numpy() / inputs[1].data.numpy()
            return relax.const(output, inputs[0].struct_info.dtype)
        return attach_span(relax.op.divide(inputs[0], inputs[1]))


class Sigmoid(OnnxOpConverter):
    """Converts an onnx Sigmoid node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return attach_span(relax.op.sigmoid(inputs[0]))


class Softmax(OnnxOpConverter):
    """Converts an onnx Softmax node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        axis = attr.get("axis", -1)
        return attach_span(relax.op.nn.softmax(inputs[0], axis=axis))


class Transpose(OnnxOpConverter):
    """Converts an onnx Transpose node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        axes = attr.get("perm", None)
        if isinstance(inputs[0], relax.Constant):
            output = _np.transpose(inputs[0].data.numpy(), axes)
            return relax.const(output, output.dtype)
        return attach_span(relax.op.permute_dims(inputs[0], axes))


class Unsqueeze(OnnxOpConverter):
    """Converts an onnx Unsqueeze node into an equivalent Relax expression."""

    @classmethod
    def _impl_v11(cls, bb, inputs, attr):
        axes = list(attr.get("axes"))
        inputs = inputs + [relax.const(axes, "int64")]
        return cls._impl_v13(bb, inputs, attr)

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        data = inputs[0]
        axes = inputs[1]

        # If input is a constant, compute directly
        if isinstance(data, relax.Constant) and isinstance(axes, relax.Constant):
            axes = axes.data.numpy().tolist()
            expanded = data.data.numpy()
            if len(expanded.shape) == 0:
                # Special case implying input is a scalar, wrap it as a list.
                if 0 in axes:
                    axes.remove(0)
                expanded = [expanded]
            for axis in axes:
                expanded = _np.expand_dims(expanded, axis=axis)
            return relax.const(expanded, data.struct_info.dtype)

        if isinstance(axes, relax.Constant):
            constant_axes = list(axes.data.numpy())
            constant_axes = list(map(int, constant_axes))
            constant_axes = sorted(constant_axes)
            for axis in constant_axes:
                data = attach_span(relax.op.expand_dims(data, axis=axis))
            return data

        raise NotImplementedError("Unsqueeze with dynamic axes is not supported.")


class Concat(OnnxOpConverter):
    """Convert an onnx Concat node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        axis = attr.get("axis", 0)
        # If all inputs are constant, perform computation directly.
        if all([isinstance(inp, relax.Constant) for inp in inputs]):
            const_inputs = []
            for inp in inputs:
                const_inputs.append(inp.data.numpy())
            out = _np.concatenate(const_inputs, axis=axis)
            dtype = inputs[0].struct_info.dtype
            return relax.const(out, dtype)
        return attach_span(relax.op.concat(inputs, axis=axis))


class Add(OnnxOpConverter):
    """Convert an onnx Add node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        if all([isinstance(inp, relax.Constant) for inp in inputs]):
            output = inputs[0].data.numpy() + inputs[1].data.numpy()
            return relax.const(output, output.dtype)
        return attach_span(relax.op.add(inputs[0], inputs[1]))


class Mul(OnnxOpConverter):
    """Convert an onnx Mul node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        if all([isinstance(inp, relax.Constant) for inp in inputs]):
            output = inputs[0].data.numpy() * inputs[1].data.numpy()
            return relax.const(output, output.dtype)
        return attach_span(relax.op.multiply(inputs[0], inputs[1]))


class Cast(OnnxOpConverter):
    """Convert an onnx Cast node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        to_type = get_type(attr["to"])
        if isinstance(inputs[0], relax.Constant):
            output = inputs[0].data.numpy().astype(to_type)
            return relax.const(output, to_type)
        return attach_span(relax.op.astype(inputs[0], to_type))


class Gather(OnnxOpConverter):
    """Convert an onnx Gather node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        # Unpack inputs
        data = inputs[0]
        indices = inputs[1]
        axis = attr.get("axis", 0)

        # If all inputs are constant, we can compute directly.
        if all([isinstance(inp, relax.Constant) for inp in [data, indices]]):
            output = _np.take(data.data.numpy(), indices.data.numpy(), axis=axis)
            return relax.const(output, output.dtype)

        # If input is a shape expression, take a value from that shape and return it as a constant.
        if isinstance(data, relax.ShapeExpr):
            assert isinstance(
                indices, relax.Constant
            ), "Only constant indices supported for shape gather."
            np_index = indices.data.numpy()
            if len(np_index.shape) == 1:
                np_index = np_index[0]
            np_index = int(np_index)
            shape_val = data[np_index]
            if hasattr(shape_val, "value"):
                return relax.const(shape_val.value, dtype="int64")
            else:
                raise ValueError("Need to fix this case.")

        # TODO(jwfromm) Make relax.take work with other indices shape.
        return emit_te_with_span(bb, topi.take, data, indices, axis)


class Gemm(OnnxOpConverter):
    """Convert an onnx Gemm node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        alpha = attr.get("alpha", None)
        beta = attr.get("beta", None)
        transA = attr.get("transA", False)
        transB = attr.get("transB", False)
        A = inputs[0]
        B = inputs[1]
        C = inputs[2]
        dtype = A.checked_type.dtype

        # Compute Y = alpha * A X B + beta * C

        if alpha is not None:
            A = bb.normalize(attach_span(relax.op.multiply(A, relax.const(alpha, dtype=dtype))))

        if transA:
            A = attach_span(relax.op.permute_dims(A, [1, 0]))
        if transB:
            B = attach_span(relax.op.permute_dims(B, [1, 0]))
        Y = bb.normalize(attach_span(relax.op.matmul(A, B)))

        if C is not None:
            if beta is not None:
                C = bb.normalize(attach_span(relax.op.multiply(C, relax.const(beta, dtype=dtype))))
            Y = attach_span(relax.op.add(Y, C))

        return Y


class Reshape(OnnxOpConverter):
    """Convert an onnx Reshape node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        data = inputs[0]
        new_shape = inputs[1]
        if isinstance(data, relax.Constant) and isinstance(new_shape, relax.Constant):
            out = _np.reshape(data.data.numpy(), new_shape.data.numpy().tolist())
            return relax.const(out, out.dtype)
        if isinstance(inputs[1], relax.Constant):
            new_shape = inputs[1].data.numpy().tolist()
        out = relax.op.reshape(data, new_shape)
        return attach_span(out)


class Gelu(OnnxOpConverter):
    """Operator converter for Gelu from Microsoft onnxruntime contrib opset.

    gelu(x) = 0.5x(1 + erf(x/sqrt(2)))
    """

    @classmethod
    def _impl_v1(cls, bb, inputs, attr):
        return attach_span(relax.op.nn.gelu(inputs[0]))


class BiasGelu(OnnxOpConverter):
    """Operator converter for BiasGelu from Microsoft onnxruntime contrib opset.

    bias_gelu(x, b) = 0.5(x + b)(1 + erf((x + b)/sqrt(2)))
    """

    @classmethod
    def _impl_v1(cls, bb, inputs, attr):
        inp = attach_span(relax.op.add(inputs[0], inputs[1]))
        return attach_span(relax.op.nn.gelu(inp))


class Where(OnnxOpConverter):
    """Convert an onnx Where node into an equivalent Relax expression."""

    @classmethod
    def _impl_v16(cls, bb, inputs, attr):
        if all([isinstance(inp, relax.Constant) for inp in inputs]):
            np_inputs = [inp.data.numpy() for inp in inputs]
            output = _np.where(*np_inputs)
            return relax.const(output, output.dtype)
        return attach_span(relax.op.where(inputs[0], inputs[1], inputs[2]))


class Clip(OnnxOpConverter):
    """Converts an onnx Clip node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        results = inputs[0]
        if inputs[1] is not None:
            results = emit_te_with_span(bb, topi.maximum, results, inputs[1])
        if inputs[2] is not None:
            results = emit_te_with_span(bb, topi.minimum, results, inputs[2])
        return results


class Equal(OnnxOpConverter):
    """Converts an onnx Equal node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        if all([isinstance(inp, relax.Constant) for inp in inputs]):
            output = inputs[0].data.numpy() == inputs[1].data.numpy()
            return relax.const(output, output.dtype)
        return attach_span(relax.op.equal(inputs[0], inputs[1]))


class Shape(OnnxOpConverter):
    """Converts an onnx Equal node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        data_info = inputs[0].struct_info

        # If no shape is defined in the struct info, it must be computed at runtime.
        if not data_info.shape:
            data_shape = bb.normalize(relax.op.shape_of(inputs[0]))
            return data_shape

        return data_info.shape


class Not(OnnxOpConverter):
    """Converts an onnx Not node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return attach_span(relax.op.bitwise_not(inputs[0]))


class Tanh(OnnxOpConverter):
    """Converts an onnx Tanh node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return attach_span(relax.op.tanh(inputs[0]))


class Sqrt(OnnxOpConverter):
    """Converts an onnx Sqrt node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return attach_span(relax.op.sqrt(inputs[0]))


class Relu(OnnxOpConverter):
    """Converts an onnx Relu node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return attach_span(relax.op.nn.relu(inputs[0]))


class Pow(OnnxOpConverter):
    """Converts an onnx Pow node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return attach_span(relax.op.power(inputs[0], inputs[1]))


class Conv(OnnxOpConverter):
    """Convert an onnx Conv node into an equivalent Relax expression."""

    @classmethod
    def _impl_v11(cls, bb, inputs, attr):
        if hasattr(inputs[0].struct_info, "ndim"):
            ndim = inputs[0].struct_info.ndim
        else:
            ndim = len(inputs[0].struct_info.shape)

        if ndim == 3:
            conv_out = emit_te_with_span(
                bb,
                topi.nn.conv1d,
                inputs[0],
                inputs[1],
                attr.get("strides", 1),
                attr.get("pads", 0),
                attr.get("dilation", 1),
                "NCHW",
                "OIHW",
            )
        elif ndim == 4:
            conv_out = bb.normalize(
                attach_span(
                    relax.op.nn.conv2d(
                        data=inputs[0],
                        weight=inputs[1],
                        strides=attr.get("strides", 1),
                        padding=attr.get("pads", 0),
                        dilation=attr.get("dilation", 1),
                        groups=attr.get("group", 1),
                        data_layout="NCHW",
                        kernel_layout="OIHW",
                    )
                )
            )
        else:
            raise NotImplementedError("Only 2d conv currently supported.")

        if inputs[2] is not None:
            bias = attach_span(
                relax.op.reshape(
                    inputs[2],
                    [1, -1]
                    + [
                        1,
                    ]
                    * (ndim - 2),
                )
            )
            conv_out = attach_span(relax.op.add(conv_out, bias))

        return conv_out


class Erf(OnnxOpConverter):
    """Converts an onnx Erf node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        x = inputs[0]
        sqrt2 = relax.const(_np.sqrt(2), x.struct_info.dtype)
        # TODO: replace with erf operator once it is implemented
        mul = attach_span(relax.op.multiply(x, sqrt2))
        gelu = attach_span(relax.op.nn.gelu(mul))
        mul_2 = attach_span(relax.op.multiply(gelu, sqrt2))
        return bb.normalize(
            attach_span(
                relax.op.add(
                    attach_span(relax.op.divide(mul_2, x)),
                    relax.const(-1, x.struct_info.dtype),
                )
            )
        )


class CumSum(OnnxOpConverter):
    """Converts an onnx CumSum node into an equivalent Relax expression."""

    @classmethod
    def _impl_v14(cls, bb, inputs, attr):
        data = inputs[0]
        assert not attr.get("exclusive", False), "Exclusive option not yet supported."
        if len(inputs) > 1:
            axis = int(inputs[1].data.numpy())
        else:
            axis = None
        data = attach_span(relax.op.cumsum(data, axis))
        if attr.get("reverse", 0) != 0:
            data = emit_te_with_span(bb, topi.flip, data, axis=axis if axis else 0)
        return data


class Squeeze(OnnxOpConverter):
    """Converts an onnx Squeeze node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        axis = inputs[1]
        if axis is not None:
            axis = [int(x) for x in inputs[1].data.numpy()]
        # If data is constant, perform computation directly.
        if isinstance(inputs[0], relax.Constant):
            out_data = _np.squeeze(inputs[0].data.numpy(), axis)
            return relax.const(out_data, inputs[0].struct_info.dtype)
        return attach_span(relax.op.squeeze(inputs[0], axis))


class Constant(OnnxOpConverter):
    """Converts an onnx Constant node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        if "value" not in attr:
            raise ValueError("no value in Constant")
        value = attr.pop("value")
        # Constants may rarely have string types. These are likely exported
        # from other frameworks and not actually used in TVM. We'll just use
        # a zero valued constant for compatibility.
        if isinstance(value, bytes):
            np_value = _np.asarray([0]).astype("int64")
        else:
            np_value = get_numpy(value)
        dtype = np_value.dtype.name
        value = relax.const(np_value, dtype)
        return value


class ConstantOfShape(OnnxOpConverter):
    """Converts an onnx ConstantOfShape node into an equivalent Relax expression."""

    @classmethod
    def _impl_v9(cls, bb, inputs, attr):
        shape = inputs[0]
        value = get_numpy(attr.get("value", 0))
        if isinstance(value, _np.ndarray):
            dtype = str(value.dtype)
        else:
            dtype = "float32"

        # If shape is a constant, we can directly create a relax constant.
        if isinstance(shape, relax.Constant):
            np_array = _np.zeros(shape=shape.data.numpy()) + value
            return relax.const(np_array, dtype=dtype)
        elif isinstance(shape, relax.ShapeExpr):
            np_array = _np.zeros(shape=[dim.value for dim in shape]) + value
            return relax.const(np_array, dtype)

        # Otherwise we have to use the value of shape at runtime.
        # Create a constant for the new value.
        const_value = relax.const(value, dtype)

        # Convert to shape expression if needed.
        if not isinstance(shape.struct_info, relax.ShapeStructInfo):
            shape_ndim = [dim.value for dim in shape.struct_info.shape.values][0]
            # Broadcast the constant to the input shape.
            shape_dataflow_var = bb.emit(
                relax.Call(
                    relax.ExternFunc("vm.builtin.tensor_to_shape"),
                    [shape],
                    sinfo_args=[relax.ShapeStructInfo(ndim=shape_ndim)],
                )
            )
            shape_vars = []
            for i in range(shape_ndim):
                shape_vars.append(tvm.tir.Var("x_%d" % i, "int64"))
            bb.match_cast(shape_dataflow_var, relax.ShapeStructInfo(shape_vars))
            shape = relax.ShapeExpr(shape_vars)

        return attach_span(relax.op.broadcast_to(const_value, shape))


class Sub(OnnxOpConverter):
    """Converts an onnx Sub node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        if all([isinstance(inp, relax.Constant) for inp in inputs]):
            output = inputs[0].data.numpy() - inputs[1].data.numpy()
            return relax.const(output, output.dtype)
        return attach_span(relax.op.subtract(inputs[0], inputs[1]))


class Sin(OnnxOpConverter):
    """Converts an onnx Sin node into an equivalent Relax expression."""

    @classmethod
    def _impl_v7(cls, bb, inputs, attr):
        return attach_span(relax.op.sin(inputs[0]))


class Cos(OnnxOpConverter):
    """Converts an onnx Cos node into an equivalent Relax expression."""

    @classmethod
    def _impl_v7(cls, bb, inputs, attr):
        return attach_span(relax.op.cos(inputs[0]))


class Neg(OnnxOpConverter):
    """Converts an onnx Neg node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        if isinstance(inputs[0], relax.Constant):
            data_np = inputs[0].data.numpy()
            return relax.const(_np.negative(data_np), inputs[0].struct_info.dtype)
        return attach_span(relax.op.negative(inputs[0]))


class Abs(OnnxOpConverter):
    """Converts an onnx Abs node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        if isinstance(inputs[0], relax.Constant):
            output = _np.abs(inputs[0].data.numpy())
            return relax.const(output, output.dtype)
        return attach_span(relax.op.abs(inputs[0]))


class Min(OnnxOpConverter):
    """Converts an onnx Min node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        if all([isinstance(inp, relax.Constant) for inp in inputs]):
            np_inputs = [inp.data.numpy() for inp in inputs]
            output = _np.minimum(*np_inputs)
            return relax.const(output, output.dtype)

        # Expand inputs, stack them, then perform minimum over the new axis.
        inputs = [bb.normalize(attach_span(relax.op.expand_dims(i, axis=0))) for i in inputs]
        stacked_tensor = attach_span(relax.op.concat(inputs, axis=0))
        return attach_span(relax.op.min(stacked_tensor, axis=0))


class Max(OnnxOpConverter):
    """Converts an onnx Max node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        if all([isinstance(inp, relax.Constant) for inp in inputs]):
            np_inputs = [inp.data.numpy() for inp in inputs]
            output = _np.maximum(*np_inputs)
            return relax.const(output, output.dtype)

        # Expand inputs, stack them, then perform maximum over the new axis.
        inputs = [bb.normalize(attach_span(relax.op.expand_dims(i, axis=0))) for i in inputs]
        stacked_tensor = attach_span(relax.op.concat(inputs, axis=0))
        return attach_span(relax.op.max(stacked_tensor, axis=0))


class Log(OnnxOpConverter):
    """Converts an onnx Log node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        if isinstance(inputs[0], relax.Constant):
            return relax.const(_np.log(inputs[0].data.numpy()), inputs[0].struct_info.dtype)
        return attach_span(relax.op.log(inputs[0]))


class Exp(OnnxOpConverter):
    """Converts an onnx Exp node into an equivalent Relax expression."""

    @classmethod
    def _check_type(cls, dtype, valid_types):
        assert dtype in valid_types, "Types {} are supported only, but {} is given".format(
            valid_types, dtype
        )

    @classmethod
    def _impl_v1(cls, bb, inputs, attr):
        data = inputs[0]
        valid_types = ["float", "float32", "double", "float64", "float16"]
        cls._check_type(data.checked_type.dtype, valid_types)

        return attach_span(relax.op.exp(data))

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        data = inputs[0]
        valid_types = ["float", "float32", "double", "float64", "float16", "bfloat16"]
        cls._check_type(data.checked_type.dtype, valid_types)

        return attach_span(relax.op.exp(data))


class Less(OnnxOpConverter):
    """Converts an onnx Less node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        if all([isinstance(inp, relax.Constant) for inp in inputs]):
            output = _np.less(inputs[0].data.numpy(), inputs[1].data.numpy())
            return relax.const(output, output.dtype)
        return attach_span(relax.op.less(inputs[0], inputs[1]))


class LessOrEqual(OnnxOpConverter):
    """Converts an onnx LessOrEqual node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        if all([isinstance(inp, relax.Constant) for inp in inputs]):
            output = _np.less_equal(inputs[0].data.numpy(), inputs[1].data.numpy())
            return relax.const(output, output.dtype)
        return attach_span(relax.op.less_equal(inputs[0], inputs[1]))


class Split(OnnxOpConverter):
    """Converts an onnx Split node into an equivalent Relax expression."""

    @classmethod
    def _impl_v1(cls, bb, inputs, attr):
        splits = attr.get("split", None)
        if splits is not None and len(splits) > 1:
            indices = []
            index = 0
            for i in splits[:-1]:
                index += i
                indices.append(index)
        # When splits isnt specified divide evenly over axis.
        else:
            indices = attr["tvm_custom"]["num_outputs"]
        return emit_te_with_span(bb, topi.split, inputs[0], indices, attr.get("axis", 0))

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        splits = inputs[1]
        splits_rank = None
        if splits is not None:
            splits_rank = splits.checked_type.ndim
        if splits is not None and splits_rank > 0:
            if isinstance(splits, relax.Constant):
                splits = splits.data.asnumpy()
                indices = []
                index = 0
                for i in splits[:-1]:
                    index += i
                    indices.append(index)
            else:
                raise ValueError("Dynamic Split not yet supported")
        # When splits isnt specified divide evenly over axis.
        else:
            indices = attr["tvm_custom"]["num_outputs"]
        return emit_te_with_span(bb, topi.split, inputs[0], indices, axis=attr.get("axis", 0))


class Slice(OnnxOpConverter):
    """Converts an onnx Splice node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        # TODO (jwfromm) currently only supports constant parameters.
        data = inputs[0]
        starts = inputs[1]
        ends = inputs[2]
        axes = inputs[3]
        steps = inputs[4]
        if not all(
            [
                (isinstance(param, relax.Constant) or param is None)
                for param in [starts, ends, axes, steps]
            ]
        ):
            raise ValueError("Only constant Slice parameters are currently supported.")
        # Convert parameters to constant lists.
        starts = starts.data.numpy().tolist()
        ends = ends.data.numpy().tolist()
        if axes is not None:
            axes = axes.data.numpy().tolist()
        else:
            axes = list(range(len(starts)))
        # Convert negative axis to positive if needed.
        for i, axis in enumerate(axes):
            if axis < 0:
                axes[i] = axis + len(data.struct_info.shape)
        if steps is not None:
            steps = steps.data.numpy().tolist()
        else:
            steps = [1] * len(axes)
        # If input is a shape tensor, we can directly extract it.
        if isinstance(data, relax.ShapeExpr):
            shape_data = [dim.value for dim in data]
            # Starts, ends, and steps must be 1-d for shape operation.
            assert all(len(i) == 1 for i in [starts, ends, steps])
            sliced_values = shape_data[starts[0] : ends[0] : steps[0]]
            return relax.const(sliced_values, "int64")
        return attach_span(relax.op.strided_slice(data, axes, starts, ends, steps))


class Pad(OnnxOpConverter):
    """Converts an onnx Pad node into an equivalent Relax expression."""

    @classmethod
    def _impl_v11(cls, bb, inputs, attr):
        pads = inputs[1]
        if len(inputs) == 3 and inputs[2] is not None:
            constant_value = inputs[2].data.numpy().item()
        else:
            constant_value = 0.0

        if isinstance(pads, relax.Constant):
            pad_before, pad_after = _np.split(pads.data.numpy(), 2)
            pad_before = _np.ndarray.tolist(pad_before)
            pad_after = _np.ndarray.tolist(pad_after)
        else:
            raise ValueError("Dynamic pads are not supported yet.")

        pad_mode = attr.get("mode", b"constant").decode("utf-8")
        if not pad_mode in ["constant", "edge", "reflect"]:
            raise tvm.error.OpAttributeInvalid(
                "Value " + pad_mode + ' in attribute "mode" is invalid for operator Pad.'
            )

        if pad_mode == "constant":
            return emit_te_with_span(
                bb, topi.nn.pad, inputs[0], pad_before, pad_after, constant_value
            )
        elif pad_mode == "reflect":
            return emit_te_with_span(
                bb, topi.nn.mirror_pad, inputs[0], pad_before, pad_after, "REFLECT"
            )
        else:
            # TODO(gigiblender) Support edge mode.
            raise NotImplementedError("Pad mode {} not implemented".format(pad_mode))


class Tile(OnnxOpConverter):
    """Converts an onnx Tile node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        reps = inputs[1]
        if isinstance(reps, relax.Constant):
            reps = reps.data.numpy().tolist()
        else:
            raise ValueError("Dynamic reps for Tile are supported yet.")
        return emit_te_with_span(bb, topi.tile, inputs[0], reps)


class Expand(OnnxOpConverter):
    """Converts an onnx Expand node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        data = inputs[0]
        shape = inputs[1]

        if isinstance(shape, relax.ShapeExpr):
            return relax.op.broadcast_to(data, shape)

        # If possible, directly expand to constant shape.
        if isinstance(shape, relax.Constant):
            new_shape = shape.data.numpy().tolist()
            # For some reason, onnx allows target shapes to be smaller than input shapes.
            # We need to go correct it.
            data_shape = [dim.value for dim in data.struct_info.shape]
            for i, s in enumerate(new_shape):
                if s < data_shape[i]:
                    new_shape[i] = data_shape[i]
            # If the new shape matches the input shape, no transformation is needed.
            if new_shape == data_shape:
                return data
            return relax.op.broadcast_to(data, relax.ShapeExpr(new_shape))

        # Otherwise handle dynamic shapes.
        shape_ndim = [dim.value for dim in shape.struct_info.shape.values][0]
        shape_dataflow_var = bb.emit(
            relax.Call(
                relax.ExternFunc("vm.builtin.tensor_to_shape"),
                [shape],
                sinfo_args=[relax.ShapeStructInfo(ndim=shape_ndim)],
            )
        )

        shape_vars = []
        for i in range(shape_ndim):
            shape_vars.append(tvm.tir.Var("x_%d" % i, "int64"))
        bb.match_cast(shape_dataflow_var, relax.ShapeStructInfo(shape_vars))
        return bb.normalize(attach_span(relax.op.broadcast_to(data, relax.ShapeExpr(shape_vars))))


class Attention(OnnxOpConverter):
    """Converts an onnx.microsoft Attention node into an equivalent Relax expression."""

    @classmethod
    def _impl_v1(cls, bb, inputs, attr):
        num_heads = attr["num_heads"]

        assert "do_rotary" not in attr, "rotary position embedding is not currently supported"
        assert (
            "past_present_share_buffer" not in attr
        ), "past state for key and value is not currently supported"
        assert "scale" not in attr, "custom scale is not currently supported"
        assert "unidirectional" not in attr, "unidirectional attention is not currently supported"

        if "mask_filter_value" in attr:
            mask_filter_value = attr["mask_filter_value"]
        else:
            mask_filter_value = -10000.0

        # (batch_size, sequence_length, input_hidden_size)
        input_emb = bb.normalize(inputs[0])

        # (input_hidden_size, hidden_size + hidden_size + v_hidden_size)
        weight = bb.normalize(inputs[1])

        def optional_input(k: int):
            if inputs[k] is not None:
                return bb.normalize(inputs[k])
            else:
                return None

        # (hidden_size + hidden_size + v_hidden_size)
        bias = optional_input(2)

        # 1. (    batch_size,             1,   max_seq_len, max_seq_len,)
        # 2. (    batch_size, total_seq_len,)
        # 3. (    batch_size,       seq_len, total_seq_len,)
        # 4. (    batch_size,)
        # 5. (2 * batch_size,)
        # For now, we only support case 2 & 3.
        mask_index = optional_input(3)

        # (2, batch_size, num_heads, past_sequence_length, head_size)
        assert inputs[4] is None, "past state for key and value is not currently supported"

        # (batch_size, num_heads, sequence_length, total_sequence_length)
        qk_bias = optional_input(5)

        assert inputs[6] is None, "past_sequence_length is not currently supported"

        (batch_size, seq_len, input_hidden_size) = [
            val.value for val in input_emb.struct_info.shape.values
        ]
        weight_shape = [val.value for val in weight.struct_info.shape.values]

        assert (
            weight_shape[0] == input_hidden_size
        ), "input and weight should share the same input hiden size"

        if "qkv_hidden_sizes" in attr:
            assert (
                attr["qkv_hidden_sizes"][0] == attr["qkv_hidden_sizes"][1]
            ), "Q and K should share the same hidden sizes"
            hidden_size, _, hidden_size_v = attr["qkv_hidden_sizes"]
        else:
            hidden_size = hidden_size_v = weight_shape[1] // 3

        assert (
            hidden_size % num_heads == 0
        ), "hidden size should be divisible by number of attention heads"
        head_size = hidden_size // num_heads
        head_size_v = hidden_size_v // num_heads

        if mask_index is not None:
            mask_index_shape = [val.value for val in mask_index.struct_info.shape.values]
            assert mask_index_shape in (
                [batch_size, seq_len],
                [
                    batch_size,
                    seq_len,
                    seq_len,
                ],
            ), """mask index should be in shape of (batch_size, seq_len),
            or (batch_size, seq_len, seq_len)"""
            mask_bias = attach_span(
                relax.op.subtract(relax.const(1, dtype=mask_index.struct_info.dtype), mask_index)
            )
            mask_bias = attach_span(relax.op.astype(mask_bias, dtype=input_emb.struct_info.dtype))
            mask_bias = bb.normalize(
                attach_span(
                    relax.op.multiply(
                        mask_bias,
                        relax.const(mask_filter_value, dtype=input_emb.struct_info.dtype),
                    )
                )
            )
            if qk_bias is None:
                qk_bias = mask_bias
            else:
                if len(mask_index_shape) == 2:
                    mask_bias = bb.normalize(
                        attach_span(relax.op.reshape(mask_bias, [batch_size, 1, 1, seq_len]))
                    )
                elif len(mask_index_shape) == 3:
                    mask_bias = bb.normalize(
                        attach_span(relax.op.reshape(mask_bias, [batch_size, 1, seq_len, seq_len]))
                    )
                qk_bias = bb.normalize(attach_span(relax.op.add(qk_bias, mask_bias)))

        QKV = attach_span(relax.op.matmul(input_emb, weight))

        if bias:
            bias_shape = [val.value for val in bias.struct_info.shape.values]
            assert (
                bias_shape[0] == weight_shape[1]
            ), "bias and weight should share the same hidden size sum"
            QKV = attach_span(relax.op.add(QKV, bias))

        QKV = attach_span(relax.op.split(QKV, [hidden_size, hidden_size * 2], 2))
        Q, K, V = QKV[0], QKV[1], QKV[2]

        Q = bb.normalize(
            attach_span(relax.op.reshape(Q, (batch_size, seq_len, num_heads, head_size)))
        )
        K = bb.normalize(
            attach_span(relax.op.reshape(K, (batch_size, seq_len, num_heads, head_size)))
        )
        V = bb.normalize(
            attach_span(relax.op.reshape(V, (batch_size, seq_len, num_heads, head_size_v)))
        )
        output = attach_span(relax.op.nn.attention(Q, K, V, qk_bias))
        output = bb.normalize(
            attach_span(relax.op.reshape(output, (batch_size, seq_len, num_heads * head_size_v)))
        )
        # add placeholder for optional present state supported in the future
        placeholder = relax.const(0, dtype="float32")
        return relax.Tuple([output, placeholder])


class Identity(OnnxOpConverter):
    """Converts an onnx Identity node into an equivalent Relax expression."""

    @classmethod
    def _impl_v1(cls, bb, inputs, attr):
        return inputs[0]


class Resize(OnnxOpConverter):
    """Converts an onnx Resize node into an equivalent Relax expression."""

    @classmethod
    def _impl_v18(cls, bb, inputs, attr):
        # Extract the many attributes of resize.
        coord_mode = attr.get("coordinate_transformation_mode", b"half_pixel").decode("ascii")
        cubic_coeff_a = attr.get("cubic_coeff_a", -0.75)
        exclude_outside = attr.get("exclude_outside", 0)
        extrapolation_value = attr.get("extrapolation_value", 0.0)
        mode = attr.get("mode", b"nearest").decode("ascii")
        rounding_method = attr.get("nearest_mode", b"round_prefer_floor").decode("ascii")

        # Adapt attributes to fit TVM definition.
        if mode == "nearest":
            mode = "nearest_neighbor"

        # Unpack inputs.
        x = inputs[0]
        roi = inputs[1]
        scales = inputs[2]
        sizes = inputs[3]
        ndims = len(x.struct_info.shape)
        assert ndims == 4, "Only resize2d is currently supported."

        assert (
            scales is None or sizes is None
        ), "Only one of scales and sizes can be provided in Resize."

        # Define relax implementation.
        if roi is not None:
            roi = attach_span(
                relax.op.concat(
                    [
                        attach_span(relax.op.strided_slice(roi, axes=[0], begin=[2], end=[ndims])),
                        attach_span(
                            relax.op.strided_slice(
                                roi, axes=[0], begin=[ndims + 2], end=[2 * ndims]
                            )
                        ),
                    ],
                    axis=0,
                )
            )
        else:
            roi = [0.0] * 4

        # Convert scales to sizes if needed.
        if scales is not None:
            assert isinstance(scales, relax.Constant), "Only constant scales currently supported."
            scales = scales.data.numpy()
            sizes_shape = [dim.value for dim in x.struct_info.shape]
            sizes = (sizes_shape * scales)[2:].astype("int64").tolist()
        else:
            assert isinstance(
                sizes, relax.Constant
            ), "Only constant output size currently supported."
            sizes = sizes.data.numpy().astype("int64").tolist()[2:]

        # TODO(jwfromm) relax.image.resize2d runs into some issues with dynamism.
        return emit_te_with_span(
            bb,
            topi.image.resize2d,
            x,
            roi,
            sizes,
            layout="NCHW",
            method=mode,
            coordinate_transformation_mode=coord_mode,
            rounding_method=rounding_method,
            bicubic_alpha=cubic_coeff_a,
            bicubic_exclude=exclude_outside,
            extrapolation_value=extrapolation_value,
        )


class Einsum(OnnxOpConverter):
    """Converts an onnx Einsum node into an equivalent Relax expression."""

    @classmethod
    def _impl_v12(cls, bb, inputs, attr):
        equation = attr["equation"].decode("utf-8")
        return emit_te_with_span(bb, topi.einsum, equation, *inputs)


class Range(OnnxOpConverter):
    """Converts an onnx Range node into an equivalent Relax expression."""

    @classmethod
    def _impl_v12(cls, bb, inputs, attr):
        start = inputs[0]
        limit = inputs[1]
        delta = inputs[2]
        out_dtype = start.struct_info.dtype

        if isinstance(start, relax.Constant):
            start = start.data.numpy().tolist()

        if isinstance(limit, relax.Constant):
            limit = limit.data.numpy().tolist()

        assert isinstance(delta, relax.Constant), "Constant delta required for Range."
        step = delta.data.numpy().tolist()

        # If all inputs are constant, compute directly.
        if isinstance(start, int) and isinstance(limit, int):
            out_range = _np.arange(start=start, stop=limit, step=step)
            return relax.const(out_range, out_dtype)

        # Otherwise compute in graph.
        return emit_te_with_span(bb, topi.arange, start, limit, step, out_dtype)


class InstanceNormalization(OnnxOpConverter):
    """Converts an onnx InstanceNormalization node into an equivalent Relax expression."""

    @classmethod
    def _impl_v6(cls, bb, inputs, attr):
        data = inputs[0]
        scale = inputs[1]
        B = inputs[2]
        epsilon = attr.get("epsilon", 1e-05)
        epsilon = relax.const(epsilon, dtype=data.struct_info.dtype)

        ndim = len(data.struct_info.shape)
        redux_axes = list(range(2, ndim))

        mean = attach_span(relax.op.mean(data, axis=redux_axes, keepdims=True))
        var = attach_span(relax.op.variance(data, axis=redux_axes, keepdims=True))
        sqrt = attach_span(relax.op.sqrt(attach_span(relax.op.add(var, epsilon))))
        out = attach_span(relax.op.divide(attach_span(relax.op.subtract(data, mean)), sqrt))
        broadcast_shape = [-1] + [
            1,
        ] * (ndim - 2)
        if scale is not None:
            scale = attach_span(relax.op.reshape(scale, broadcast_shape))
            out = attach_span(relax.op.multiply(out, scale))
        if B is not None:
            B = attach_span(relax.op.reshape(B, broadcast_shape))
            out = attach_span(relax.op.add(out, B))
        return out


class BatchNormalization(OnnxOpConverter):
    """Converts an onnx BatchNormalization node into an equivalent Relax expression."""

    @classmethod
    def _impl_v16(cls, bb, inputs, attr):
        # Unpack inputs
        data = inputs[0]
        scale = inputs[1]
        bias = inputs[2]
        mean = inputs[3]
        var = inputs[4]
        epsilon = attr.get("epsilon", 1e-05)
        return attach_span(
            relax.op.nn.batch_norm(data, scale, bias, mean, var, axis=1, epsilon=epsilon)
        )


class MaxPool(OnnxOpConverter):
    """Converts an onnx MaxPool node into an equivalent Relax expression."""

    @classmethod
    def _impl_v12(cls, bb, inputs, attr):
        # Unpack inputs and attributes.
        data = inputs[0]
        auto_pad = attr.get("auto_pad", b"NOTSET").decode("utf-8")
        ceil_mode = attr.get("ceil_mode", 0)
        dilations = attr.get("dilations", [1, 1])
        kernel_shape = attr.get("kernel_shape")
        pads = attr.get("pads", 0)
        strides = attr.get("strides", 1)

        assert len(kernel_shape) == 2, "Currently only 2D pooling is supported."
        assert auto_pad in [
            "NOTSET",
            "SAME_UPPER",
            "SAME_LOWER",
            "VALID",
        ], f"Value {auto_pad} in attribute auto_pad is invalid."

        if auto_pad in ("SAME_UPPER", "SAME_LOWER"):
            input_spatial_shape = cls._get_input_spatial_shape(data)
            output_spatial_shape = [0 for _ in input_spatial_shape]

            pads = _np.array([(0, 0) for _ in range(len(kernel_shape))])

            for i, _ in enumerate(input_spatial_shape):
                if auto_pad == "SAME_UPPER":
                    output_spatial_shape[i] = int(_np.ceil(input_spatial_shape[i] / strides[i]))
                else:
                    output_spatial_shape[i] = int(_np.floor(input_spatial_shape[i] / strides[i]))
                pad_i = (
                    (output_spatial_shape[i] - 1) * strides[i]
                    + ((kernel_shape[i] - 1) * dilations[i] + 1)
                    - input_spatial_shape[i]
                )
                if auto_pad == "SAME_UPPER":
                    pads[i, 0] = pad_i // 2
                    pads[i, 1] = pad_i - pads[i, 0]
                else:
                    pads[i, 1] = pad_i // 2
                    pads[i, 0] = pad_i - pads[i, 1]

            # TODO(agladyshev): for now we support only 2D kernel
            # (top, left, bottom, right)
            flatten_pads = [pads[0][0], pads[1][0], pads[0][1], pads[1][1]]
            pads = tuple(flatten_pads)

        return attach_span(
            relax.op.nn.max_pool2d(data, kernel_shape, strides, pads, dilations, ceil_mode)
        )

    @classmethod
    def _get_input_spatial_shape(cls, tensor):
        # shape is (N x C x D1 x D2 ... Dn)
        return _np.array([int(d) for d in tensor.struct_info.shape], dtype="int64")[2:]


class GlobalAveragePool(OnnxOpConverter):
    """Converts an onnx GlobalAveragePool node into an equivalent Relax expression."""

    @classmethod
    def _impl_v1(cls, bb, inputs, attr):
        return attach_span(relax.op.nn.adaptive_avg_pool2d(inputs[0], 1))


class Flatten(OnnxOpConverter):
    """Converts an onnx Flatten node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        axis = attr.get("axis", 1)
        data_shape = [i.value for i in inputs[0].struct_info.shape]
        new_shape = (1, -1) if axis == 0 else (_np.prod(data_shape[0:axis]).astype("int64"), -1)
        return attach_span(relax.op.reshape(inputs[0], new_shape))


class LayerNormalization(OnnxOpConverter):
    """Converts an onnx LayerNormalization node into an equivalent Relax expression."""

    @classmethod
    def _impl_v17(cls, bb, inputs, attr):
        data = inputs[0]
        scale = inputs[1]
        bias = inputs[2]
        axis = attr.get("axis", -1)
        epsilon = attr.get("epsilon", 1e-05)

        output = attach_span(relax.op.nn.layer_norm(data, scale, bias, axis, epsilon))
        # Onnx layernorm has 3 outputs but only the first is used.
        # We construct two empty constants for this.
        placeholder = relax.const(0, dtype="float32")
        return relax.Tuple([output, placeholder, placeholder])


class ReduceMax(OnnxOpConverter):
    """Converts an onnx ReduceMax node into an equivalent Relax expression."""

    @classmethod
    def _impl_v11(cls, bb, inputs, attr):
        data = inputs[0]
        axes = attr.get("axes", None)
        keepdims = attr.get("keepdims", 1)
        return attach_span(relax.op.max(data, axes, keepdims))


class ReduceMin(OnnxOpConverter):
    """Converts an onnx ReduceMin node into an equivalent Relax expression."""

    @classmethod
    def _impl_v11(cls, bb, inputs, attr):
        data = inputs[0]
        axes = attr.get("axes", None)
        keepdims = attr.get("keepdims", 1)
        return attach_span(relax.op.min(data, axes, keepdims))


class ReduceSum(OnnxOpConverter):
    """Converts an onnx ReduceSum node into an equivalent Relax expression."""

    @classmethod
    def _impl_v11(cls, bb, inputs, attr):
        data = inputs[0]
        axes = attr.get("axes", None)
        keepdims = attr.get("keepdims", 1)
        return attach_span(relax.op.sum(data, axes, keepdims))

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        data = inputs[0]
        axes = inputs[1]
        keepdims = attr.get("keepdims", 1)
        assert isinstance(axes, relax.Constant), "Only constant axes currently supported."
        axes = axes.data.numpy().tolist()
        return attach_span(relax.op.sum(data, axes, keepdims))


class ReduceMean(OnnxOpConverter):
    """Converts an onnx ReduceMean node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        data = inputs[0]
        axes = attr.get("axes", None)
        keepdims = attr.get("keepdims", 1)
        return attach_span(relax.op.mean(data, axes, keepdims))


class ReduceProd(OnnxOpConverter):
    """Converts an onnx ReduceProd node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        data = inputs[0]
        axes = attr.get("axes", None)
        keepdims = attr.get("keepdims", 1)
        return attach_span(relax.op.prod(data, axes, keepdims))


class ReduceLogSumExp(OnnxOpConverter):
    """Converts an onnx ReduceLogSumExp node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        x = inputs[0]
        axes = attr.get("axes", None)
        keepdims = attr.get("keepdims", 1)
        max_x = attach_span(relax.op.max(x, axes, True))
        exp_x = attach_span(relax.op.exp(attach_span(relax.op.subtract(x, max_x))))
        sum_x = attach_span(relax.op.sum(exp_x, axes, True))
        out_x = attach_span(relax.op.add(attach_span(relax.op.log(sum_x)), max_x))
        if not keepdims:
            out_x = attach_span(relax.op.squeeze(out_x, axes))
        return out_x


class ReduceLogSum(OnnxOpConverter):
    """Converts an onnx ReduceLogSum node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        data = inputs[0]
        axes = attr.get("axes", None)
        keepdims = attr.get("keepdims", 1)
        return attach_span(relax.op.log(attach_span(relax.op.sum(data, axes, keepdims))))


class ReduceSumSquare(OnnxOpConverter):
    """Converts an onnx ReduceSumSquare node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        data = inputs[0]
        axes = attr.get("axes", None)
        keepdims = attr.get("keepdims", 1)
        return attach_span(relax.op.sum(attach_span(relax.op.multiply(data, data)), axes, keepdims))


class ReduceL1(OnnxOpConverter):
    """Converts an onnx ReduceL1 node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        data = inputs[0]
        axes = attr.get("axes", None)
        keepdims = attr.get("keepdims", 1)
        return attach_span(relax.op.sum(attach_span(relax.op.abs(data)), axes, keepdims))


class ReduceL2(OnnxOpConverter):
    """Converts an onnx ReduceL2 node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        data = inputs[0]
        axes = attr.get("axes", None)
        keepdims = attr.get("keepdims", 1)
        return attach_span(
            relax.op.sqrt(
                attach_span(
                    relax.op.sum(attach_span(relax.op.multiply(data, data)), axes, keepdims)
                )
            )
        )


class ArgMax(OnnxOpConverter):
    """Converts an onnx ArgMax node into an equivalent Relax expression."""

    @classmethod
    def _check_attrs(cls, data, attr, shift_axis=True):
        dims_num = len(data.struct_info.shape)
        axis = attr.get("axis", 0)
        if shift_axis and axis < 0:
            axis += dims_num
        assert 0 <= axis < dims_num, "Axis is out of bounds"
        keepdims = attr.get("keepdims", True)
        return axis, keepdims

    @classmethod
    def _impl_v1(cls, bb, inputs, attr):
        data = inputs[0]
        axis, keepdims = cls._check_attrs(data, attr, False)
        return attach_span(relax.op.argmax(data, axis, keepdims))

    @classmethod
    def _impl_v11(cls, bb, inputs, attr):
        data = inputs[0]
        axis, keepdims = cls._check_attrs(data, attr)
        return attach_span(relax.op.argmax(data, axis, keepdims))

    @classmethod
    def _impl_v12(cls, bb, inputs, attr):
        data = inputs[0]
        axis, keepdims = cls._check_attrs(data, attr)
        select_last_index = attr.get("select_last_index", False)
        if select_last_index:
            # TODO(vvchernov): support attr
            raise tvm.error.OpAttributeUnImplemented(
                "'select_last_index' attribute has not been supported yet"
            )
        return attach_span(relax.op.argmax(data, axis, keepdims))


class ArgMin(OnnxOpConverter):
    """Converts an onnx ArgMin node into an equivalent Relax expression."""

    @classmethod
    def _check_attrs(cls, data, attr, shift_axis=True):
        dims_num = len(data.struct_info.shape)
        axis = attr.get("axis", 0)
        if shift_axis and axis < 0:
            axis += dims_num
        assert 0 <= axis < dims_num, "Axis is out of bounds"
        keepdims = attr.get("keepdims", True)
        return axis, keepdims

    @classmethod
    def _impl_v1(cls, bb, inputs, attr):
        data = inputs[0]
        axis, keepdims = cls._check_attrs(data, attr, False)
        return attach_span(relax.op.argmin(data, axis, keepdims))

    @classmethod
    def _impl_v11(cls, bb, inputs, attr):
        data = inputs[0]
        axis, keepdims = cls._check_attrs(data, attr)
        return attach_span(relax.op.argmin(data, axis, keepdims))

    @classmethod
    def _impl_v12(cls, bb, inputs, attr):
        data = inputs[0]
        axis, keepdims = cls._check_attrs(data, attr)
        select_last_index = attr.get("select_last_index", False)
        if select_last_index:
            # TODO(vvchernov): support attr
            raise tvm.error.OpAttributeUnImplemented(
                "'select_last_index' attribute has not been supported yet"
            )
        return attach_span(relax.op.argmin(data, axis, keepdims))


class SkipLayerNormalization(OnnxOpConverter):
    """Converts a microsoft contrib SkipLayerNormalization node into a Relax expression."""

    @classmethod
    def _impl_v1(cls, bb, inputs, attr):
        data = inputs[0]
        skip = inputs[1]
        gamma = inputs[2]
        beta = inputs[3]
        bias = inputs[4]

        assert (
            beta is not None and bias is not None
        ), "SkipLayerNormalization import currently only supports required beta and bias"

        epsilon = attr.get("epsilon", 1e-12)

        data = attach_span(relax.op.add(data, skip))
        if bias is not None:
            data = attach_span(relax.op.add(data, bias))

        output = attach_span(relax.op.nn.layer_norm(data, gamma, beta, axes=-1, epsilon=epsilon))

        # Expects three outputs though only the first is used. Construct a placeholder for others.
        placeholder = relax.const(0, dtype="float32")
        return relax.Tuple([output, placeholder, placeholder])


class EmbedLayerNormalization(OnnxOpConverter):
    """Converts a microsoft contrib EmbedLayerNormalization node into a Relax expression."""

    @classmethod
    def _impl_v1(cls, bb, inputs, attr):
        input_ids = inputs[0]
        segment_ids = inputs[1]
        word_emb = inputs[2]
        pos_emb = inputs[3]
        segment_emb = inputs[4]
        gamma = inputs[5]
        beta = inputs[6]
        mask = inputs[7]
        pos_ids = inputs[8]

        epsilon = attr.get("epsilon", 1e-12)

        (batch_size, seq_len) = [dim.value for dim in input_ids.struct_info.shape]

        if segment_ids:
            assert segment_emb

        if pos_ids is None:
            pos_ids = relax.const([list(range(seq_len))] * batch_size, dtype="int64")
        # TODO(jwfromm) Replace with relax ops once take has better support.
        word_vec = emit_te_with_span(bb, topi.take, word_emb, input_ids, 0)
        if segment_ids:
            segment_vec = emit_te_with_span(bb, topi.take, segment_emb, segment_ids, 0)
        pos_vec = emit_te_with_span(bb, topi.take, pos_emb, pos_ids, 0)

        vec_sum = attach_span(relax.op.add(word_vec, pos_vec))
        if segment_ids:
            vec_sum = attach_span(relax.op.add(vec_sum, segment_vec))

        ln = attach_span(relax.op.nn.layer_norm(vec_sum, gamma, beta, axes=-1, epsilon=epsilon))

        mask_index = relax.const(_np.zeros((batch_size,), dtype="int64"))
        if mask:
            # Caculate number of words per sentence.
            mask_index = attach_span(relax.op.sum(mask, axis=1))

        return relax.Tuple([ln, mask_index])


class Greater(OnnxOpConverter):
    """Converts an onnx Greater node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        if all([isinstance(inp, relax.Constant) for inp in inputs]):
            output = _np.greater(inputs[0].data.numpy(), inputs[1].data.numpy())
            return relax.const(output, output.dtype)
        return attach_span(relax.op.greater(inputs[0], inputs[1]))


class Reciprocal(OnnxOpConverter):
    """Converts an onnx Reciprocal node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        input_dtype = inputs[0].struct_info.dtype
        return attach_span(relax.op.divide(relax.const(1, dtype=input_dtype), inputs[0]))


class OneHot(OnnxOpConverter):
    """Converts an onnx OneHot node into an equivalent Relax expression."""

    @classmethod
    def _impl_v11(cls, bb, inputs, attr):
        indices = inputs[0]
        depth = inputs[1]
        values = inputs[2]
        axis = attr.get("axis", -1)
        dtype = values.struct_info.dtype
        assert isinstance(depth, relax.Constant), "Only constant depth currently supported."
        depth = depth.data.numpy().tolist()
        assert isinstance(values, relax.Constant), "Only constant values currently supported."
        values = values.data.numpy().tolist()
        off_value, on_value = values
        return emit_te_with_span(bb, topi.one_hot, indices, on_value, off_value, depth, axis, dtype)


def _get_convert_map():
    return {
        "MatMul": MatMul,
        "Concat": Concat,
        "Add": Add,
        "Mul": Mul,
        "Cast": Cast,
        "Gather": Gather,
        "Gemm": Gemm,
        "Reshape": Reshape,
        "Div": Div,
        "Sigmoid": Sigmoid,
        "Softmax": Softmax,
        "Transpose": Transpose,
        "Unsqueeze": Unsqueeze,
        "Gelu": Gelu,
        "BiasGelu": BiasGelu,
        "Where": Where,
        "Clip": Clip,
        "Equal": Equal,
        "Shape": Shape,
        "Not": Not,
        "Tanh": Tanh,
        "Sqrt": Sqrt,
        "Relu": Relu,
        "Conv": Conv,
        "Pow": Pow,
        "Erf": Erf,
        "CumSum": CumSum,
        "Squeeze": Squeeze,
        "Constant": Constant,
        "Sub": Sub,
        "Sin": Sin,
        "Cos": Cos,
        "Neg": Neg,
        "Abs": Abs,
        "Min": Min,
        "Max": Max,
        "Log": Log,
        "Exp": Exp,
        "Less": Less,
        "LessOrEqual": LessOrEqual,
        "LayerNormalization": LayerNormalization,
        "SkipLayerNormalization": SkipLayerNormalization,
        "EmbedLayerNormalization": EmbedLayerNormalization,
        "InstanceNormalization": InstanceNormalization,
        # defs/reduction
        "ReduceMax": ReduceMax,
        "ReduceMin": ReduceMin,
        "ReduceSum": ReduceSum,
        "ReduceMean": ReduceMean,
        "ReduceProd": ReduceProd,
        "ReduceLogSumExp": ReduceLogSumExp,
        "ReduceLogSum": ReduceLogSum,
        "ReduceSumSquare": ReduceSumSquare,
        "ReduceL1": ReduceL1,
        "ReduceL2": ReduceL2,
        "ArgMax": ArgMax,
        "ArgMin": ArgMin,
        "Expand": Expand,
        "ConstantOfShape": ConstantOfShape,
        "Slice": Slice,
        "Attention": Attention,
        "Pad": Pad,
        "Split": Split,
        "Tile": Tile,
        "BatchNormalization": BatchNormalization,
        "GlobalAveragePool": GlobalAveragePool,
        "Flatten": Flatten,
        "MaxPool": MaxPool,
        "Identity": Identity,
        "Resize": Resize,
        "Einsum": Einsum,
        "Range": Range,
        "Greater": Greater,
        "Reciprocal": Reciprocal,
        "OneHot": OneHot,
    }


class ONNXGraphImporter:
    """A helper class for handling Relax expression copying from pb2.GraphProto.
    Definition: https://github.com/onnx/onnx/blob/main/onnx/onnx.proto

    Parameters
    ----------
    shape_dict : dict of str to tuple, optional
        The input shape to the graph
    dtype_dict : str or dict of str to str
        The input types to the graph
    sanitize : bool
        Whether to sanitize the input names to be valid Relax identifiers.
    """

    current = None

    def __init__(
        self,
        shape_dict: Dict[str, List],
        dtype_dict: Union[str, Dict[str, str]],
        sanitize: bool = True,
    ):
        self._nodes: Dict[str, relax.Expr] = {}
        self._inputs: Dict[str, relax.Var] = {}
        self._num_input: int = 0
        self._shape = shape_dict.copy() if shape_dict else {}
        self._input_names: List[str] = []
        self._dtype = dtype_dict
        self.opset: int = None
        self._name_supply = NameSupply()
        self._sanitize: bool = sanitize
        self.bb: relax.BlockBuilder = relax.BlockBuilder()  # pylint: disable=invalid-name

    def from_onnx(self, graph: onnx.onnx_ml_pb2.ModelProto, opset: int) -> IRModule:
        """Construct Relax expressions from the ONNX graph.
        Onnx graph is a python protobuf object.

        Parameters
        ----------
        graph : onnx protobuf object
            The loaded onnx graph
        opset : opset version
        Returns
        -------
        mod : tvm.IRModule
            The returned relax module
        """
        with self.bb.function("main"):
            with self.bb.dataflow() as df:  # pylint: disable=invalid-name, unused-variable
                self.opset = opset
                self._parse_graph_initializers(graph)
                self._parse_graph_input(graph)
                self._check_for_unsupported_ops(graph)
                self._construct_nodes(graph)

                # now return the outputs
                outputs = [self._nodes[self._parse_value_proto(i)] for i in graph.output]
                outputs = outputs[0] if len(outputs) == 1 else relax.Tuple(outputs)

                # Create a function from our output expression and all input variables.
                param_list = [v for k, v in self._inputs.items() if isinstance(v, relax.Var)]
                output_var = self.bb.emit_output(outputs)
            self.bb.emit_func_output(output_var, params=param_list)
        relax_mod = self.bb.get()
        return relax_mod

    def _parse_graph_initializers(self, graph: onnx.onnx_ml_pb2.GraphProto):
        """Parse network inputs to relax, aka parameters."""
        for init_tensor in graph.initializer:
            if not init_tensor.name.strip():
                raise ValueError("Tensor's name is required.")
            array = self._parse_array(init_tensor)
            self._nodes[init_tensor.name] = relax.const(array)

    def _sanitize_name(self, name: str) -> str:
        """Sanitize a name to make it a valid identifier.
        If the name is None, returns a string input_0, input_1, etc.
        If the input is an empty string, returns empty_0, empty_1, etc.
        If the input is a string that does not start with a letter or underscore,
        returns input_<name>. Otherwise, returns an unique input name.

        Parameters
        ----------
        name : str
            The name to sanitize
        Returns
        -------
        new_name : str
        """

        if name == "":
            return self._name_supply.fresh_name("empty_")

        new_name = name.replace(".", "_")
        if not new_name[0].isalpha() and new_name[0] != "_":
            new_name = str(self._name_supply.fresh_name("input_" + new_name))
        else:
            new_name = str(self._name_supply.fresh_name(new_name))

        if new_name != name:
            warnings.warn(("Renaming name %s to %s" % (name, new_name)))
        return new_name

    def _new_var(self, var_name: str, shape: List, dtype: str = "float32"):
        """Creates a new Relax variable."""
        return testing.nn.Parameter(shape=shape, dtype=dtype, name=var_name)

    def _parse_graph_input(self, graph: onnx.onnx_ml_pb2.GraphProto):
        """Parse model inputs to Relax parameters."""
        for i in graph.input:
            # from onnx v0.2, GraphProto.input has type ValueInfoProto,
            #  and the name is 'i.name'
            i_name, i_shape, d_type, i_shape_name = get_info(i)
            if i_name not in self._nodes:
                self._num_input += 1
                self._input_names.append(i_name)
                if i_name in self._shape:
                    i_shape = self._shape[i_name]
                else:
                    if "?" in str(i_shape):
                        warning_msg = (
                            "Input %s has unknown dimension shapes: %s. "
                            "Specifying static values may improve performance"
                            % (i_name, str(i_shape_name))
                        )
                        warnings.warn(warning_msg)
                if isinstance(self._dtype, dict):
                    dtype = self._dtype[i_name] if i_name in self._dtype else d_type
                else:
                    dtype = d_type
                var_name = self._sanitize_name(i_name) if self._sanitize else i_name
                self._nodes[i_name] = self._new_var(var_name, shape=i_shape, dtype=dtype)
            self._inputs[i_name] = self._nodes[i_name]

    def _check_for_unsupported_ops(self, graph: onnx.onnx_ml_pb2.GraphProto):
        convert_map = _get_convert_map()
        unsupported_ops = set()
        for node in graph.node:
            op_name = node.op_type
            if (
                op_name not in convert_map
                and op_name != "Constant"
                # and op_name not in _identity_list
            ):
                unsupported_ops.add(op_name)
        if unsupported_ops:
            msg = "The following operators are not supported for frontend ONNX: "
            msg += ", ".join(unsupported_ops)
            raise tvm.error.OpNotImplemented(msg)

    def _construct_nodes(self, graph: onnx.onnx_ml_pb2.GraphProto):
        """Nodes are stored as directed acyclic graph."""
        for node_index, node in enumerate(graph.node):
            op_name = node.op_type
            attr = self._parse_attr(node.attribute)
            # Create and populate input list.
            inputs = onnx_input()
            for i in node.input:
                if i != "":
                    inputs.append(self._nodes[i])
                else:
                    inputs.append(None)
            i_name = self._parse_value_proto(node)
            outputs = node.output
            attr["tvm_custom"] = {}
            attr["tvm_custom"]["name"] = i_name
            attr["tvm_custom"]["num_outputs"] = len(outputs)

            # Perform special handling for shape expressions. If an input is a
            # shape expr, make sure the current op can handle it, otherwise
            # convert it to a tensor.
            shape_compatible_ops = ["Reshape", "ConstantOfShape", "Gather", "Slice", "Expand"]
            for i, inp in enumerate(inputs):
                if (
                    inp is not None
                    and isinstance(inp.struct_info, relax.ShapeStructInfo)
                    and op_name not in shape_compatible_ops
                ):
                    raise ValueError(f"Node {node.name} cannot handle ShapeExpr inputs.")

            op = self._convert_operator(op_name, node_index, inputs, attr, self.opset)
            # Create struct information for the new operator.
            op = self.bb.normalize(op)

            if not isinstance(op, relax.Tuple):
                if isinstance(op.checked_type, tvm.ir.type.TupleType):
                    # This is a var bound to a tuple. We need to unpack it and create
                    # a new tuple.
                    tuple_items = []
                    for i in range(len(op.checked_type.fields)):
                        tuple_items.append(self.bb.emit(relax.TupleGetItem(op, i)))
                    op = relax.Tuple(tuple_items)
                    outputs_num = len(tuple_items)
                else:
                    outputs_num = 1
            else:
                outputs_num = len(op)
            assert (
                len(outputs) <= outputs_num
            ), "Missing outputs during conversion. Expected {} but Got {} in {}.".format(
                len(outputs), outputs_num, op_name
            )

            if outputs_num == 1:
                self._nodes[outputs[0]] = op
            else:
                for k, i in zip(list(outputs), range(len(outputs))):
                    self._nodes[k] = op[i]

    def _parse_value_proto(self, value_proto: onnx.onnx_ml_pb2.GraphProto):
        """Parse ValueProto or raw str."""
        try:
            name = value_proto.name
        except AttributeError:
            name = value_proto
        return name

    def _parse_array(self, tensor_proto: onnx.onnx_ml_pb2.TensorProto) -> tvm.nd.array:
        np_array = get_numpy(tensor_proto).reshape(tuple(tensor_proto.dims))
        return tvm.nd.array(np_array)

    def _parse_attr(self, attr_proto: onnx.onnx_ml_pb2.AttributeProto) -> Dict[str, Any]:
        """Convert a list of AttributeProto to a dict, with names as keys."""
        attrs = {}
        for a in attr_proto:
            for f in ["f", "i", "s", "g"]:
                if a.HasField(f):
                    attrs[a.name] = getattr(a, f)
            for f in ["floats", "ints", "strings"]:
                if list(getattr(a, f)):
                    assert a.name not in attrs, "Only one type of attr is allowed"
                    attrs[a.name] = tuple(getattr(a, f))
            for f in ["t"]:
                if a.HasField(f):
                    attrs[a.name] = getattr(a, f)
            for f in ["tensors"]:
                if list(getattr(a, f)):
                    assert a.name not in attrs, "Only one type of attr is allowed"
                    attrs[a.name] = tuple(getattr(a, f))
            for f in ["graphs"]:
                if list(getattr(a, f)):
                    raise NotImplementedError("Field {} is not supported in relax.".format(f))
            if a.name not in attrs:
                raise ValueError("Cannot parse attribute: \n{}\n.".format(a))
        return attrs

    def _convert_operator(
        self, op_name: str, node_index: int, inputs: List[relax.Function], attrs: Dict, opset: int
    ) -> relax.Function:
        """Convert ONNX operator into a Relax operator.
        The converter must specify conversions explicitly for incompatible name, and
        apply handlers to operator attributes.

        Parameters
        ----------
        op_name : str
            Operator name, such as Convolution, FullyConnected
        node_index : int
            Index of the node in the ONNX graph.
        inputs : list of tvm.relax.function.Function
            List of inputs.
        attrs : dict
            Dict of operator attributes
        opset : int
            Opset version
        Returns
        -------
        sym : tvm.relax.function.Function
            Converted relax function
        """
        convert_map = _get_convert_map()
        if op_name in convert_map:
            convert_class = convert_map[op_name]
            op_function = convert_class.get_converter(opset)
            span = tvm.ir.Span(tvm.ir.SourceName(op_name), node_index, node_index, 0, 0)
            with relax.frontend.SpanContext(span):
                sym = op_function(self.bb, inputs, attrs)
        else:
            raise NotImplementedError("Operator {} not implemented.".format(op_name))
        return sym


def from_onnx(
    model: onnx.onnx_ml_pb2.GraphProto,
    shape_dict: Optional[Dict[str, List]] = None,
    dtype_dict: Optional[Union[str, Dict[str, str]]] = "float32",
    opset: int = None,
    sanitize_input_names: bool = True,
) -> Tuple[IRModule, Dict]:
    """Convert a ONNX model into an equivalent Relax Function.
    ONNX graphs are represented as Python Protobuf objects.

    The current implementation assumes that the input model is after ONNX v1.1.0.

    Parameters
    ----------
    model : protobuf object
        ONNX ModelProto after ONNX v1.1.0
    shape_dict : dict of str to tuple, optional
        The input shape to the graph
    dtype_dict : str or dict of str to str, optional
        The input types to the graph
    opset : int, optional
        Override to autodetected opset.
        This can be helpful for some testing.
    sanitize_input_names : bool, optional
        Whether to sanitize the input names to ensure they are valid Relax identifiers.

    Returns
    -------
    mod : tvm.IRModule
        The relax module for compilation
    params : dict of str to tvm.nd.NDArray
        The parameter dict to be used by relax
    """
    # Error if the model version is below 1.1.0
    if model.ir_version < 3:
        raise ValueError(
            "Model IR version {} not supported. Must be at least after 1.1.0.".format(
                model.ir_version
            )
        )

    try:
        import onnx  # pylint: disable=import-outside-toplevel, redefined-outer-name

        if hasattr(onnx.checker, "check_model"):
            # try use onnx's own model checker before converting any model
            try:
                onnx.checker.check_model(model)
            except Exception as exception:  # pylint: disable=c-extension-no-member, broad-except
                # the checker is a bit violent about errors, so simply print warnings here
                warnings.warn(str(exception))
    except ImportError as error:
        raise ImportError("Unable to import onnx which is required {}".format(error))

    g = ONNXGraphImporter(shape_dict, dtype_dict, sanitize_input_names)
    graph = model.graph

    try:
        opset_in_model = 1
        if model.opset_import:
            # TODO: for now we only really support ai.onnx op set
            # TODO: handle other namespaces well see https://github.com/apache/tvm/issues/10950
            for opset_identifier in model.opset_import:
                # As per https://github.com/onnx/onnx/blob/main/docs/IR.md
                # All operator sets except the default one must specify the operator version
                if str(opset_identifier.domain) in ["ai.onnx", ""]:
                    opset_in_model = opset_identifier.version
                    break
    except AttributeError:
        opset_in_model = 1

    if opset is None:
        opset = opset_in_model
    elif opset < opset_in_model:
        warnings.warn(
            ""
            f"You are overwritting original opset ver = {opset_in_model} by lower ver = {opset}. "
            f"That might cause model conversion errors."
        )

    # Use the graph proto as a scope so that ops can access other nodes if needed.
    return g.from_onnx(graph, opset)
