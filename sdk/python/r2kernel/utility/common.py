"""Unit conversion and pose helpers for the SDK's coordinate and manipulator datatypes."""

from __future__ import annotations

import math
from collections.abc import Sequence

from r2kernel.datatype.coordinate import Orientation, Position, Pose
from r2kernel.datatype.manipulator import JointValues, Payload, TaskWayPoint, TCP, ToolSettings
from r2kernel.datatype.physics import Inertia


def _validate_joint_types(jointvector: JointValues, jointtype: Sequence[str] | None):
    if jointtype is None or len(jointtype) != len(jointvector):
        raise ValueError("jointtype must contain one R or P for each joint")
    if any(kind not in {"R", "P"} for kind in jointtype):
        raise ValueError("jointtype entries must be R (revolute) or P (prismatic)")


def _to_meter_radian_jointvector(jointvector: JointValues, jointtype: Sequence[str] | None):
    _validate_joint_types(jointvector, jointtype)
    return JointValues(
        [math.radians(value) if kind == "R" else value * 0.001
         for value, kind in zip(jointvector, jointtype)],
        names=jointvector._names,
    )


def _to_millimeter_degree_jointvector(jointvector: JointValues, jointtype: Sequence[str] | None):
    _validate_joint_types(jointvector, jointtype)
    return JointValues(
        [math.degrees(value) if kind == "R" else value * 1000.0
         for value, kind in zip(jointvector, jointtype)],
        names=jointvector._names,
    )


def _to_meter_position(position: Position):
    return Position(x=position[0] / 1000.0, y=position[1] / 1000.0, z=position[2] / 1000.0)


def _to_radian_orientation(orientation: Orientation):
    return Orientation(rx=math.radians(orientation[0]), ry=math.radians(orientation[1]),
                       rz=math.radians(orientation[2]))


def _to_meter_radian_pose(pose: Pose):
    return Pose(position=_to_meter_position(pose.position), orientation=_to_radian_orientation(pose.orientation))


def _to_meter_radian_waypoint(waypoint: TaskWayPoint):
    return TaskWayPoint(
        pose=_to_meter_radian_pose(waypoint.pose),
        radius=waypoint.radius / 1000.0,
        vel=waypoint.vel,
        acc=waypoint.acc,
        cornering_vel=waypoint.cornering_vel,
    )


def _to_meter_radian_tcp(tcp: TCP):
    return TCP(position=_to_meter_position(tcp.position), orientation=_to_radian_orientation(tcp.orientation))


def _to_meter_radian_kg_payload(payload: Payload):
    return Payload(
        mass=payload.mass / 1000.0,
        center_of_mass=_to_meter_position(payload.center_of_mass),
        inertia=Inertia([value * 1e-9 for value in payload.inertia], names=payload.inertia._names),
    )


def _to_meter_radian_kg_tool(tool: ToolSettings):
    return ToolSettings(
        tcp=_to_meter_radian_tcp(tool.tcp),
        payload=_to_meter_radian_kg_payload(tool.payload),
    )


def _to_millimeter_position(position: Position):
    return Position(x=position[0] * 1000.0, y=position[1] * 1000.0, z=position[2] * 1000.0)


def _to_degree_orientation(orientation: Orientation):
    return Orientation(
        rx=math.degrees(orientation[0]),
        ry=math.degrees(orientation[1]),
        rz=math.degrees(orientation[2])
    )


def _to_millimeter_degree_pose(pose: Pose):
    return Pose(position=_to_millimeter_position(pose.position), orientation=_to_degree_orientation(pose.orientation))


def _to_millimeter_degree_waypoint(waypoint: TaskWayPoint):
    return TaskWayPoint(
        pose=_to_millimeter_degree_pose(waypoint.pose),
        radius=waypoint.radius * 1000.0,
        vel=waypoint.vel,
        acc=waypoint.acc,
        cornering_vel=waypoint.cornering_vel,
    )


def _to_millimeter_degree_tcp(tcp: TCP):
    return TCP(position=_to_millimeter_position(tcp.position), orientation=_to_degree_orientation(tcp.orientation))


def _to_millimeter_degree_g_payload(payload: Payload):
    return Payload(
        mass=payload.mass * 1000.0,
        center_of_mass=_to_millimeter_position(payload.center_of_mass),
        inertia=Inertia([value * 1e9 for value in payload.inertia], names=payload.inertia._names),
    )


def _to_millimeter_degree_g_tool(tool: ToolSettings):
    return ToolSettings(
        tcp=_to_millimeter_degree_tcp(tool.tcp),
        payload=_to_millimeter_degree_g_payload(tool.payload),
    )


_TO_METER_RADIAN_KG = {
    JointValues: _to_meter_radian_jointvector,
    Position: _to_meter_position,
    Orientation: _to_radian_orientation,
    Pose: _to_meter_radian_pose,
    TaskWayPoint: _to_meter_radian_waypoint,
    TCP: _to_meter_radian_tcp,
    Payload: _to_meter_radian_kg_payload,
    ToolSettings: _to_meter_radian_kg_tool,
}

_TO_MILLIMETER_DEGREE_G = {
    JointValues: _to_millimeter_degree_jointvector,
    Position: _to_millimeter_position,
    Orientation: _to_degree_orientation,
    Pose: _to_millimeter_degree_pose,
    TaskWayPoint: _to_millimeter_degree_waypoint,
    TCP: _to_millimeter_degree_tcp,
    Payload: _to_millimeter_degree_g_payload,
    ToolSettings: _to_millimeter_degree_g_tool,
}


def to_meter_radian_kg(target, jointtype: Sequence[str] | None = None):
    """Convert mm/degree/g to m/radian/kg without modifying ``target``.

    JointValues require one ``R`` (revolute) or ``P`` (prismatic) per joint.
    TaskWayPoint motion tuning values are retained; only pose and radius convert.
    Payload inertia converts from g*mm² to kg*m².
    """
    convert = _TO_METER_RADIAN_KG[type(target)]
    if isinstance(target, JointValues):
        return convert(target, jointtype)
    if jointtype:
        raise ValueError("jointtype is only supported for JointValues")
    return convert(target)


def to_millimeter_degree_g(target, jointtype: Sequence[str] | None = None):
    """Inverse of :func:`to_meter_radian_kg`, returning a new datatype value."""
    convert = _TO_MILLIMETER_DEGREE_G[type(target)]
    if isinstance(target, JointValues):
        return convert(target, jointtype)
    if jointtype:
        raise ValueError("jointtype is only supported for JointValues")
    return convert(target)


def offset_pose(
    source_pose: Pose,
    x: float | None = None,
    y: float | None = None,
    z: float | None = None,
    rx: float | None = None,
    ry: float | None = None,
    rz: float | None = None
):
    offset_position = source_pose.position
    offset_orientation = source_pose.orientation
    if x is not None:
        offset_position.x += x
    if y is not None:
        offset_position.y += y
    if z is not None:
        offset_position.z += z
    if rx is not None:
        offset_orientation.rx += rx
    if ry is not None:
        offset_orientation.ry += ry
    if rz is not None:
        offset_orientation.rz += rz

    return Pose(position=offset_position, orientation=offset_orientation)


def blend_pose(
    source_pose: Pose,
    x: float | None = None,
    y: float | None = None,
    z: float | None = None,
    rx: float | None = None,
    ry: float | None = None,
    rz: float | None = None
):
    blend_position = source_pose.position
    blend_orientation = source_pose.orientation
    if x is not None:
        blend_position.x = x
    if y is not None:
        blend_position.y = y
    if z is not None:
        blend_position.z = z
    if rx is not None:
        blend_orientation.rx = rx
    if ry is not None:
        blend_orientation.ry = ry
    if rz is not None:
        blend_orientation.rz = rz

    return Pose(position=blend_position, orientation=blend_orientation)
