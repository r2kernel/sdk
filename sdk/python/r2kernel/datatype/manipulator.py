"""Manipulator-related datatypes and helpers."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from r2kernel.datatype.basetype import BaseType, NamedFloatVector, get_decoder, get_encoder
from r2kernel.datatype.coordinate import Orientation, Position, Pose
from r2kernel.datatype.physics import Force, Inertia, Torque


class JointValues(NamedFloatVector):
    """Generic joint vector (position, velocity, torque, etc.)."""

    NAMES = tuple(f"j{i}" for i in range(7))

    def __init__(self, values: list[float] | None = None, *, names: tuple[str, ...] | None = None):
        super().__init__(values, names=names or self.NAMES)


class Wrench(NamedFloatVector):
    NAMES = ("fx", "fy", "fz", "tx", "ty", "tz")

    def __init__(
        self,
        values: list[float] | None = None,
        *,
        force: Force | None = None,
        torque: Torque | None = None,
    ):
        if values is None and (force is not None or torque is not None):
            values = []
            values.extend(force or [0.0, 0.0, 0.0])
            values.extend(torque or [0.0, 0.0, 0.0])
        super().__init__(values, names=self.NAMES)

    @property
    def force(self) -> Force:
        return Force(self[0:3])

    @property
    def torque(self) -> Torque:
        return Torque(self[3:6])


def _encode_fields(schema: list[tuple[str, object]], values: dict) -> bytes:
    """Encode a schema of fields with length prefixes. Supports dtype strings or classes with encode()."""
    buf = bytearray()
    for name, dtype in schema:
        val = values.get(name)
        if isinstance(dtype, str):
            encoder = get_encoder(dtype)
            encoded = encoder(val)
        elif hasattr(dtype, "encode"):
            encoded = dtype.encode(val)
        else:
            raise ValueError(f"Unsupported dtype for field {name}: {dtype}")
        buf += struct.pack(">I", len(encoded))
        buf += encoded
    return bytes(buf)


def _decode_fields(schema: list[tuple[str, object]], data: bytes) -> dict:
    mv = memoryview(data)
    offset = 0
    out = {}
    for name, dtype in schema:
        if offset + 4 > len(mv):
            raise ValueError("Insufficient data for compound field length")
        length = struct.unpack_from(">I", mv, offset)[0]
        offset += 4
        if offset + length > len(mv):
            raise ValueError(f"Insufficient data for field '{name}'")
        chunk = mv[offset:offset + length]
        offset += length
        if isinstance(dtype, str):
            decoder = get_decoder(dtype)
            out[name] = decoder(chunk)
        elif hasattr(dtype, "decode"):
            out[name] = dtype.decode(chunk)
        else:
            raise ValueError(f"Unsupported dtype for field {name}: {dtype}")
    return out


@dataclass
class TCP(BaseType):
    position: Position = field(default_factory=Position)
    orientation: Orientation = field(default_factory=Orientation)
    SCHEMA = [("position", Position), ("orientation", Orientation)]

    @classmethod
    def encode(cls, tcp: "TCP") -> bytes:
        vals = {"position": tcp.position, "orientation": tcp.orientation}
        return _encode_fields(cls.SCHEMA, vals)

    @classmethod
    def decode(cls, data: bytes) -> "TCP":
        vals = _decode_fields(cls.SCHEMA, data)
        return cls(position=vals["position"], orientation=vals["orientation"])


@dataclass
class Payload(BaseType):
    mass: float = 0
    center_of_mass: Position = field(default_factory=Position)
    inertia: Inertia = field(default_factory=Inertia)
    SCHEMA = [("mass", "float32"), ("center_of_mass", Position), ("inertia", Inertia)]

    @classmethod
    def encode(cls, payload: "Payload") -> bytes:
        vals = {
            "mass": payload.mass,
            "center_of_mass": payload.center_of_mass,
            "inertia": payload.inertia,
        }
        return _encode_fields(cls.SCHEMA, vals)

    @classmethod
    def decode(cls, data: bytes) -> "Payload":
        vals = _decode_fields(cls.SCHEMA, data)
        return cls(mass=vals["mass"], center_of_mass=vals["center_of_mass"], inertia=vals["inertia"])


@dataclass
class TaskWayPoint(BaseType):
    pose: Pose
    radius: float = -1
    vel: float = -1
    cornering_vel: float = -1
    acc: float = -1
    SCHEMA = [
        ("pose", Pose),
        ("radius", "float32"),
        ("vel", "float32"),
        ("cornering_vel", "float32"),
        ("acc", "float32"),
    ]

    @classmethod
    def encode(cls, wp: "TaskWayPoint") -> bytes:
        vals = {
            "pose": wp.pose,
            "radius": wp.radius,
            "vel": wp.vel,
            "cornering_vel": wp.cornering_vel,
            "acc": wp.acc,
        }
        return _encode_fields(cls.SCHEMA, vals)

    @classmethod
    def decode(cls, data: bytes) -> "TaskWayPoint":
        vals = _decode_fields(cls.SCHEMA, data)
        return cls(**vals)


@dataclass
class JointWayPoint(BaseType):
    q: JointValues
    radius: float = -1
    vel: float = -1
    cornering_vel: float = -1
    acc: float = -1
    SCHEMA = [
        ("q", JointValues),
        ("radius", "float32"),
        ("vel", "float32"),
        ("cornering_vel", "float32"),
        ("acc", "float32"),
    ]

    @classmethod
    def encode(cls, wp: "JointWayPoint") -> bytes:
        vals = {
            "q": wp.q,
            "radius": wp.radius,
            "vel": wp.vel,
            "cornering_vel": wp.cornering_vel,
            "acc": wp.acc,
        }
        return _encode_fields(cls.SCHEMA, vals)

    @classmethod
    def decode(cls, data: bytes) -> "JointWayPoint":
        vals = _decode_fields(cls.SCHEMA, data)
        return cls(**vals)


@dataclass
class ToolSettings(BaseType):
    payload: Payload
    tcp: TCP
    SCHEMA = [("payload", Payload), ("tcp", TCP)]

    @classmethod
    def encode(cls, tool: "ToolSettings") -> bytes:
        vals = {"payload": tool.payload, "tcp": tool.tcp}
        return _encode_fields(cls.SCHEMA, vals)

    @classmethod
    def decode(cls, data: bytes) -> "ToolSettings":
        vals = _decode_fields(cls.SCHEMA, data)
        return cls(payload=vals["payload"], tcp=vals["tcp"])


@dataclass
class ServoCommand(BaseType):
    joint: JointValues | None = None
    task: TaskWayPoint | None = None
    duration: float | None = None
    SCHEMA = [
        ("joint", JointValues),
        ("task", TaskWayPoint),
        ("duration", "float32"),
    ]

    @classmethod
    def encode(cls, cmd: "ServoCommand") -> bytes:
        vals = {
            "joint": cmd.joint,
            "task": cmd.task,
            "duration": cmd.duration if cmd.duration is not None else -1,
        }
        return _encode_fields(cls.SCHEMA, vals)

    @classmethod
    def decode(cls, data: bytes) -> "ServoCommand":
        vals = _decode_fields(cls.SCHEMA, data)
        return cls(**vals)


@dataclass
class ManipulatorOperationState(BaseType):
    flags: int = 0

    _AUTO = 1 << 0
    _MANUAL = 1 << 1
    _REMOTE = 1 << 2
    _STRICT = 1 << 3
    _PAUSED = 1 << 4

    @property
    def auto_mode(self) -> bool:
        return bool(self.flags & self._AUTO)

    @property
    def manual_mode(self) -> bool:
        return bool(self.flags & self._MANUAL)

    @property
    def remote_mode(self) -> bool:
        return bool(self.flags & self._REMOTE)

    @property
    def strict_operation_policy(self) -> bool:
        return bool(self.flags & self._STRICT)

    @property
    def paused(self) -> bool:
        return bool(self.flags & self._PAUSED)

    @classmethod
    def encode(cls, state: "ManipulatorOperationState") -> bytes:
        return int(state.flags).to_bytes(1, byteorder="big", signed=False)

    @classmethod
    def decode(cls, data: bytes) -> "ManipulatorOperationState":
        flags = int.from_bytes(data[:1], byteorder="big", signed=False) if data else 0
        return cls(flags=flags)


@dataclass
class ManipulatorErrorStatus(BaseType):
    flags: int = 0

    _HAS_ERROR = 1 << 0
    _RECOVERABLE = 1 << 1
    _MOTOR_FAULT = 1 << 2

    @property
    def has_error(self) -> bool:
        return bool(self.flags & self._HAS_ERROR)

    @property
    def recoverable(self) -> bool:
        return bool(self.flags & self._RECOVERABLE)

    @property
    def motor_fault(self) -> bool:
        return bool(self.flags & self._MOTOR_FAULT)

    @classmethod
    def encode(cls, status: "ManipulatorErrorStatus") -> bytes:
        return int(status.flags).to_bytes(1, byteorder="big", signed=False)

    @classmethod
    def decode(cls, data: bytes) -> "ManipulatorErrorStatus":
        flags = int.from_bytes(data[:1], byteorder="big", signed=False) if data else 0
        return cls(flags=flags)


@dataclass
class ManipulatorTrajectoryStatus(BaseType):
    feed_rate: float | None = None
    actual_joint_position: JointValues | None = None
    actual_joint_velocity: JointValues | None = None
    actual_joint_effort: JointValues | None = None
    residual_effort: JointValues | None = None
    joint_temperature: JointValues | None = None
    actual_pose: Pose | None = None
    actual_twist: list[float] | None = None
    actual_wrench: Wrench | None = None
    target_joint_position: JointValues | None = None
    target_joint_velocity: JointValues | None = None
    target_joint_effort: JointValues | None = None
    target_pose: Pose | None = None
    target_twist: list[float] | None = None
    target_wrench: Wrench | None = None
    collision_joint_status: list[bool] | None = None
