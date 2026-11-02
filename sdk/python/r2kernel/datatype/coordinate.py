"""Location datatypes: Position, Orientation, Pose."""

from __future__ import annotations

import struct
import numpy as np

from r2kernel.datatype.basetype import BaseType, CompoundType, NamedFloatVector, get_schema_size


class Position(NamedFloatVector):
    SCHEMA = [("x", "float32"), ("y", "float32"), ("z", "float32")]
    NAMES = ("x", "y", "z")

    def __init__(self, x: float | list[float] = 0, y: float | list[float] = 0, z: float | list[float] = 0):
        vals = x if isinstance(x, list) else [x, y, z]
        super().__init__(vals, names=self.NAMES)

    @classmethod
    def encode(cls, pos: "Position") -> bytes:
        """Encode a Position to bytes using CompoundType helper."""
        vals = {"x": float(pos.x), "y": float(pos.y), "z": float(pos.z)}
        return CompoundType(cls.SCHEMA, vals).to_bytes()

    @classmethod
    def decode(cls, data: bytes) -> "Position":
        vals = CompoundType.from_bytes(cls.SCHEMA, data).values
        return cls(vals["x"], vals["y"], vals["z"])

    @classmethod
    def fixed_size(cls) -> int | None:
        return get_schema_size(cls.SCHEMA)

    @classmethod
    def max_size(cls) -> int | None:
        return cls.fixed_size()


class Orientation(NamedFloatVector):
    SCHEMA = [("rx", "float32"), ("ry", "float32"), ("rz", "float32")]
    NAMES = ("rx", "ry", "rz")

    def __init__(self, rx: float | list[float] = 0, ry: float | list[float] = 0, rz: float | list[float] = 0):
        vals = rx if isinstance(rx, list) else [rx, ry, rz]
        super().__init__(vals, names=self.NAMES)

    @classmethod
    def encode(cls, ori: "Orientation") -> bytes:
        vals = {"rx": float(ori.rx), "ry": float(ori.ry), "rz": float(ori.rz)}
        return CompoundType(cls.SCHEMA, vals).to_bytes()

    @classmethod
    def decode(cls, data: bytes) -> "Orientation":
        vals = CompoundType.from_bytes(cls.SCHEMA, data).values
        return cls(vals["rx"], vals["ry"], vals["rz"])

    @classmethod
    def fixed_size(cls) -> int | None:
        return get_schema_size(cls.SCHEMA)

    @classmethod
    def max_size(cls) -> int | None:
        return cls.fixed_size()


class Pose(BaseType):
    """Compound pose consisting of position and orientation."""

    SCHEMA = [
        ("position", Position),
        ("orientation", Orientation),
    ]

    def __init__(self, position: Position | None = None, orientation: Orientation | None = None):
        position = position or Position()
        orientation = orientation or Orientation()
        self._data = np.asarray(position.tolist() + orientation.tolist(), dtype=np.float32)

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

    @property
    def position(self) -> Position:
        return Position(self._data[0:3].tolist())

    @property
    def orientation(self) -> Orientation:
        return Orientation(self._data[3:6].tolist())

    @classmethod
    def encode(cls, pose: "Pose") -> bytes:
        buf = bytearray()
        for field, dtype in cls.SCHEMA:
            value = getattr(pose, field)
            if hasattr(dtype, "encode"):
                encoded = dtype.encode(value)
            else:
                encoded = CompoundType.encode(value)
            buf += struct.pack(">I", len(encoded))
            buf += encoded
        return bytes(buf)

    @classmethod
    def decode(cls, data: bytes) -> "Pose":
        mv = memoryview(data)
        offset = 0
        parts = {}
        for field, dtype in cls.SCHEMA:
            if offset + 4 > len(mv):
                raise ValueError("Insufficient data for Pose decode")
            length = struct.unpack_from(">I", mv, offset)[0]
            offset += 4
            if offset + length > len(mv):
                raise ValueError(f"Insufficient data for field '{field}'")
            chunk = mv[offset:offset + length]
            offset += length
            if hasattr(dtype, "decode"):
                parts[field] = dtype.decode(chunk)
            else:
                parts[field] = CompoundType.decode(chunk)
        pos = parts.get("position") or Position()
        ori = parts.get("orientation") or Orientation()
        return cls(pos, ori)

    @classmethod
    def fixed_size(cls) -> int | None:
        return get_schema_size(cls.SCHEMA)

    @classmethod
    def max_size(cls) -> int | None:
        return cls.fixed_size()
