from __future__ import annotations

import struct
import numpy as np
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Type, Union

from r2kernel.datatype import DTypeLiteral


class BaseType(ABC):
    """Base class for SDR datatypes with encode/decode helpers."""

    SCHEMA: list[tuple[str, object]] = []
    MAX_SIZE: int | None = None

    @classmethod
    @abstractmethod
    def encode(cls, value: Any) -> bytes:
        pass

    @classmethod
    @abstractmethod
    def decode(cls, data: bytes) -> Any:
        pass

    @classmethod
    def fixed_size(cls) -> int | None:
        if cls.SCHEMA:
            return _schema_size(cls.SCHEMA, use_max=False)
        return None

    @classmethod
    def max_size(cls) -> int | None:
        fixed = cls.fixed_size()
        if fixed is not None:
            return fixed
        if cls.MAX_SIZE is not None:
            return cls.MAX_SIZE
        if cls.SCHEMA:
            return _schema_size(cls.SCHEMA, use_max=True)
        return None


DEFAULT_TOPIC_SIZE = 1024 * 1024


_S_INT32 = struct.Struct(">i")
_S_UINT8 = struct.Struct(">B")
_S_UINT16 = struct.Struct(">H")
_S_UINT32 = struct.Struct(">I")
_S_INT64 = struct.Struct(">q")
_S_FLOAT32 = struct.Struct(">f")
_S_FLOAT64 = struct.Struct(">d")


class _StructType(BaseType):
    STRUCT: struct.Struct
    CAST: Callable[[Any], Any] = lambda x: x

    @classmethod
    def encode(cls, value: Any) -> bytes:
        return cls.STRUCT.pack(cls.CAST(value))

    @classmethod
    def decode(cls, data: bytes) -> Any:
        mv = memoryview(data)
        return cls.STRUCT.unpack_from(mv, 0)[0]

    @classmethod
    def fixed_size(cls) -> int | None:
        return cls.STRUCT.size


class Int32(_StructType):
    STRUCT = _S_INT32
    CAST = int


class UInt8(_StructType):
    STRUCT = _S_UINT8
    CAST = int


class UInt16(_StructType):
    STRUCT = _S_UINT16
    CAST = int


class UInt32(_StructType):
    STRUCT = _S_UINT32
    CAST = int


class Int64(_StructType):
    STRUCT = _S_INT64
    CAST = int


class Float32(_StructType):
    STRUCT = _S_FLOAT32
    CAST = float


class Float64(_StructType):
    STRUCT = _S_FLOAT64
    CAST = float


class Bool(BaseType):
    @classmethod
    def encode(cls, value: bool) -> bytes:
        return b"\x01" if bool(value) else b"\x00"

    @classmethod
    def decode(cls, data: bytes) -> bool:
        mv = memoryview(data)
        return len(mv) > 0 and mv[0] != 0

    @classmethod
    def fixed_size(cls) -> int | None:
        return 1


class String(BaseType):
    MAX_SIZE: int | None = 1024

    @classmethod
    def encode(cls, value: str) -> bytes:
        if value is None:
            value = ""
        return str(value).encode("utf-8")

    @classmethod
    def decode(cls, data: bytes) -> str:
        return memoryview(data).tobytes().decode("utf-8")


class Bytes(BaseType):
    MAX_SIZE: int | None = 1024

    @classmethod
    def encode(cls, value: bytes) -> bytes:
        if isinstance(value, bytes):
            return value
        return bytes(value)

    @classmethod
    def decode(cls, data: bytes) -> bytes:
        return memoryview(data).tobytes()


@dataclass
class TypeInfo:
    cls: Type[BaseType] | None = None
    loader: Callable[[str], Type[BaseType]] | None = None


def _load_coordinate(base: str) -> Type[BaseType]:
    from r2kernel.datatype.coordinate import Orientation, Pose, Position

    coord_map = {"position": Position, "orientation": Orientation, "pose": Pose}
    return coord_map[base]


def _load_physics(base: str) -> Type[BaseType]:
    from r2kernel.datatype.physics import Force, Inertia, Torque

    phys_map = {"inertia": Inertia, "taskforce": Force, "tasktorque": Torque}
    return phys_map[base]


def _load_manipulator(base: str) -> Type[BaseType]:
    from r2kernel.datatype.manipulator import JointValues, Payload, Wrench

    manip_map = {"jointvector": JointValues, "jointeffort": JointValues, "wrench": Wrench, "payload": Payload}
    return manip_map[base]


_TYPE_INFO: dict[str, TypeInfo] = {
    "int32": TypeInfo(cls=Int32),
    "uint8": TypeInfo(cls=UInt8),
    "uint16": TypeInfo(cls=UInt16),
    "uint32": TypeInfo(cls=UInt32),
    "int64": TypeInfo(cls=Int64),
    "float32": TypeInfo(cls=Float32),
    "float64": TypeInfo(cls=Float64),
    "double": TypeInfo(cls=Float64),
    "bool": TypeInfo(cls=Bool),
    "boolean": TypeInfo(cls=Bool),
    "string": TypeInfo(cls=String),
    "str": TypeInfo(cls=String),
    "bytes": TypeInfo(cls=Bytes),
    "position": TypeInfo(loader=_load_coordinate),
    "orientation": TypeInfo(loader=_load_coordinate),
    "pose": TypeInfo(loader=_load_coordinate),
    "jointvector": TypeInfo(loader=_load_manipulator),
    "jointeffort": TypeInfo(loader=_load_manipulator),
    "wrench": TypeInfo(loader=_load_manipulator),
    "payload": TypeInfo(loader=_load_manipulator),
    "inertia": TypeInfo(loader=_load_physics),
    "taskforce": TypeInfo(loader=_load_physics),
    "tasktorque": TypeInfo(loader=_load_physics),
}

_ARRAY_FORMATS: dict[str, tuple[str, struct.Struct]] = {
    "int32": ("i", _S_INT32),
    "uint8": ("B", _S_UINT8),
    "uint16": ("H", _S_UINT16),
    "uint32": ("I", _S_UINT32),
    "int64": ("q", _S_INT64),
    "float32": ("f", _S_FLOAT32),
    "float64": ("d", _S_FLOAT64),
    "double": ("d", _S_FLOAT64),
}


def _resolve_type(base: str) -> Type[BaseType] | None:
    info = _TYPE_INFO.get(base)
    if not info:
        return None
    if info.cls is None and info.loader:
        info.cls = info.loader(base)
    return info.cls


def _array_element_size(base: str) -> int | None:
    if base == "bool":
        return 1
    fmt = _ARRAY_FORMATS.get(base)
    if not fmt:
        return None
    return fmt[1].size


def _schema_size(schema: list[tuple[str, object]], *, use_max: bool) -> int | None:
    total = 0
    size_fn = get_max_size if use_max else get_fixed_size
    for _, dtype in schema:
        if isinstance(dtype, str):
            field_size = size_fn(dtype)
        elif hasattr(dtype, "fixed_size"):
            field_size = dtype.max_size() if use_max else dtype.fixed_size()
        else:
            field_size = None
        if field_size is None:
            return None
        total += 4 + field_size
    return total


def get_schema_size(schema: list[tuple[str, object]], *, max_size: bool = False) -> int | None:
    """Return schema size using length prefixes; uses fixed sizes or max sizes."""
    return _schema_size(schema, use_max=max_size)


def get_encoder(dtype: Union[str, DTypeLiteral]) -> Callable[[Any], bytes]:
    base, is_array = _parse_type(dtype)
    if is_array:
        return lambda values: encode_array(base, values)
    cls = _resolve_type(base)
    if not cls:
        raise ValueError(f"Unsupported SDR datatype: {dtype}")
    return cls.encode


def get_decoder(dtype: Union[str, DTypeLiteral]) -> Callable[[bytes], Any]:
    base, is_array = _parse_type(dtype)
    if is_array:
        return lambda data: decode_array(base, data)
    cls = _resolve_type(base)
    if not cls:
        raise ValueError(f"Unsupported SDR datatype: {dtype}")
    return cls.decode


def get_fixed_size(dtype: Union[str, DTypeLiteral]) -> int | None:
    base, is_array = _parse_type(dtype)
    if is_array:
        return None
    cls = _resolve_type(base)
    if not cls:
        raise ValueError(f"Unsupported SDR datatype: {dtype}")
    return cls.fixed_size()


def get_max_size(dtype: Union[str, DTypeLiteral]) -> int | None:
    base, is_array = _parse_type(dtype)
    if is_array:
        return None
    fixed = get_fixed_size(base)
    if fixed is not None:
        return fixed
    cls = _resolve_type(base)
    if not cls:
        raise ValueError(f"Unsupported SDR datatype: {dtype}")
    return cls.max_size()


def get_type_size(dtype: Union[str, DTypeLiteral]) -> int | None:
    """Return fixed size if available, otherwise max size (if defined)."""
    size = get_fixed_size(dtype)
    if size is not None:
        return size
    return get_max_size(dtype)


def get_array_size(dtype: Union[str, DTypeLiteral], count: int) -> int | None:
    """Return encoded size for a fixed-size array type when count is known."""
    base, is_array = _parse_type(dtype)
    if not is_array:
        base = str(dtype).lower()
    element_size = _array_element_size(str(base).lower())
    if element_size is None:
        return None
    return 4 + count * element_size

def encode_array(dtype: str, values: list[Any]) -> bytes:
    """Encode a list of fixed-size primitives with a length prefix."""
    base = dtype.lower()
    element_size = _array_element_size(base)
    if element_size is None:
        raise ValueError(f"Array encoding not supported for type: {dtype}")
    count = len(values)
    buf = bytearray(4 + count * element_size)
    _S_UINT32.pack_into(buf, 0, count)
    if count == 0:
        return bytes(buf)

    fmt = _ARRAY_FORMATS.get(base)
    if fmt:
        fmt_char = fmt[0]
        np_dtype = f">{fmt_char}"
        arr = np.asarray(values, dtype=np_dtype)
        buf[4:] = arr.tobytes(order="C")
    elif base == "bool":
        mv = memoryview(buf)[4:]
        mv[:count] = bytes(1 if v else 0 for v in values)
    return bytes(buf)


def decode_array(dtype: str, data: bytes) -> list[Any]:
    """Decode a length-prefixed array of fixed-size primitives."""
    base = dtype.lower()
    element_size = _array_element_size(base)
    if element_size is None:
        raise ValueError(f"Array decoding not supported for type: {dtype}")
    if len(data) < 4:
        raise ValueError("Insufficient data for array length prefix")
    mv = memoryview(data)
    count = _S_UINT32.unpack_from(mv, 0)[0]
    expected_size = 4 + count * element_size
    if len(mv) < expected_size:
        raise ValueError(f"Insufficient data for {count} elements of type {dtype}")
    if count == 0:
        return []

    fmt = _ARRAY_FORMATS.get(base)
    if fmt:
        fmt_char = fmt[0]
        np_dtype = f">{fmt_char}"
        arr = np.frombuffer(mv[4:4 + count * element_size], dtype=np_dtype)
        return arr
    if base == "bool":
        payload = mv[4:4 + count]
        arr = np.frombuffer(payload, dtype=np.uint8)
        return arr.astype(bool)
    raise ValueError(f"Array decoding not supported for type: {dtype}")


def _parse_type(dtype: str) -> tuple[str, bool]:
    base = dtype.lower()
    if base.endswith("_array"):
        return base.replace("_array", ""), True
    return base, False


# ====================================================================================
# Named float vector helper
# ====================================================================================
class NamedFloatVector(BaseType):
    """Base vector with named fields and float32 array encode/decode."""

    NAMES: tuple[str, ...] = ()

    def __init__(self, values: list[float] | None = None, *, names: tuple[str, ...] | None = None):
        self._names = names or self.NAMES
        if not self._names:
            raise ValueError("NamedFloatVector requires NAMES")
        vals = values if values is not None else [0.0] * len(self._names)
        if len(vals) != len(self._names):
            raise ValueError(f"Expected {len(self._names)} values, got {len(vals)}")
        self._data = np.asarray(vals, dtype=np.float32)

    def __len__(self):
        return int(self._data.shape[0])

    def __iter__(self):
        return iter(self._data.tolist())

    def __getitem__(self, idx):
        if isinstance(idx, slice):
            return self._data[idx].tolist()
        return float(self._data[idx])

    def __setitem__(self, idx, value):
        self._data[idx] = value

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._data.tolist()})"

    def tolist(self) -> list[float]:
        return self._data.tolist()

    def __getattr__(self, name):
        if name in self._names:
            return self[self._names.index(name)]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name.startswith("_") or name not in getattr(self, "_names", ()):
            object.__setattr__(self, name, value)
        else:
            self[self._names.index(name)] = value

    @classmethod
    def fixed_size(cls) -> int | None:
        if not cls.NAMES:
            return None
        return 4 + len(cls.NAMES) * _S_FLOAT32.size

    @classmethod
    def max_size(cls) -> int | None:
        return cls.fixed_size()

    @classmethod
    def encode(cls, vec: "NamedFloatVector") -> bytes:
        return encode_array("float32", vec.tolist())

    @classmethod
    def decode(cls, data: bytes) -> "NamedFloatVector":
        vals = decode_array("float32", data)
        return cls(vals)


# ====================================================================================
# Compound datatype helper
# ====================================================================================
class CompoundType(BaseType):
    """Generic compound datatype composed of ordered base-type fields.

    schema: list of (field_name, dtype) where dtype matches basetype registry
            and may include *_array variants.
    values: mapping of field_name -> value
    """

    SCHEMA: list[tuple[str, str]] = []

    def __init__(self, schema: list[tuple[str, str]] | None = None, values: dict[str, Any] | None = None):
        self.schema = schema or self.SCHEMA
        self.values = values

    def to_bytes(self) -> bytes:
        """Serialize the compound type with length prefixes per field."""
        buf = bytearray()
        for field, dtype in self.schema:
            encoder = get_encoder(dtype)
            encoded = encoder(self.values.get(field) if self.values else None)
            buf += struct.pack(">I", len(encoded))
            buf += encoded
        return bytes(buf)

    @classmethod
    def from_bytes(cls, schema: list[tuple[str, str]], data: bytes) -> "CompoundType":
        """Deserialize bytes into a CompoundType using the provided schema."""
        mv = memoryview(data)
        offset = 0
        values: dict[str, Any] = {}
        for field, dtype in schema:
            if offset + 4 > len(mv):
                raise ValueError("Insufficient data for compound field length")
            length = struct.unpack_from(">I", mv, offset)[0]
            offset += 4
            if offset + length > len(mv):
                raise ValueError(f"Insufficient data for field '{field}'")
            chunk = mv[offset:offset + length]
            offset += length
            decoder = get_decoder(dtype)
            values[field] = decoder(chunk)
        return cls(schema=schema, values=values)

    @classmethod
    def encode(cls, value: Any) -> bytes:
        """Encode using class-level SCHEMA if provided."""
        if isinstance(value, CompoundType):
            return value.to_bytes()
        if not cls.SCHEMA:
            raise ValueError("SCHEMA not defined for CompoundType encode")
        if not isinstance(value, dict):
            raise TypeError("CompoundType.encode expects a CompoundType or dict of values")
        return CompoundType(cls.SCHEMA, value).to_bytes()

    @classmethod
    def decode(cls, data: bytes) -> "CompoundType":
        """Decode using class-level SCHEMA; returns a CompoundType instance."""
        if not cls.SCHEMA:
            raise ValueError("SCHEMA not defined for CompoundType decode")
        return CompoundType.from_bytes(cls.SCHEMA, data)

    def __getitem__(self, item):
        return self.values[item]

    def __setitem__(self, key, value):
        self.values[key] = value


__all__ = [
    "BaseType",
    "DEFAULT_TOPIC_SIZE",
    "Int32",
    "UInt32",
    "Int64",
    "Float32",
    "Float64",
    "Bool",
    "String",
    "Bytes",
    "NamedFloatVector",
    "get_encoder",
    "get_decoder",
    "get_fixed_size",
    "get_max_size",
    "get_type_size",
    "get_array_size",
    "get_schema_size",
    "encode_array",
    "decode_array",
    "CompoundType",
]
