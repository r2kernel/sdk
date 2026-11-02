from __future__ import annotations

from typing import Literal

# Supported dtype strings for encoder/decoder registration and code completion.
DTypeLiteral = Literal[
    # base primitives
    "int32",
    "uint8",
    "uint16",
    "uint32",
    "int64",
    "float32",
    "float64",
    "double",
    "bool",
    "boolean",
    "string",
    "str",
    "bytes",
    # arrays of primitives
    "int32_array",
    "uint8_array",
    "uint16_array",
    "uint32_array",
    "int64_array",
    "float32_array",
    "float64_array",
    "bool_array",
    # compound/location/manipulator aliases
    "position",
    "orientation",
    "pose",
    "jointvector",
    "jointeffort",
    "inertia",
    "taskforce",
    "tasktorque",
    "wrench",
    "payload",
]

# Units for device configurations
UnitType = Literal["DEGREE_MM", "RADIAN_M"]

def get_fixed_size(dtype: DTypeLiteral | str) -> int | None:
    from r2kernel.datatype.basetype import get_fixed_size as _get_fixed_size

    return _get_fixed_size(dtype)


def get_max_size(dtype: DTypeLiteral | str) -> int | None:
    from r2kernel.datatype.basetype import get_max_size as _get_max_size

    return _get_max_size(dtype)


def get_type_size(dtype: DTypeLiteral | str) -> int | None:
    from r2kernel.datatype.basetype import get_type_size as _get_type_size

    return _get_type_size(dtype)


def get_array_size(dtype: DTypeLiteral | str, count: int) -> int | None:
    from r2kernel.datatype.basetype import get_array_size as _get_array_size

    return _get_array_size(dtype, count)


def get_schema_size(schema: list[tuple[str, object]], *, max_size: bool = False) -> int | None:
    from r2kernel.datatype.basetype import get_schema_size as _get_schema_size

    return _get_schema_size(schema, max_size=max_size)


from r2kernel.datatype.basetype import DEFAULT_TOPIC_SIZE

__all__ = [
    "DTypeLiteral",
    "UnitType",
    "DEFAULT_TOPIC_SIZE",
    "get_fixed_size",
    "get_max_size",
    "get_type_size",
    "get_array_size",
    "get_schema_size",
]
