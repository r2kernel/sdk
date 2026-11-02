from __future__ import annotations

import math
from typing import Any


def yaw_from_pose(pose: dict[str, Any]) -> float:
    orientation = pose.get("orientation") if isinstance(pose, dict) else {}
    if not isinstance(orientation, dict):
        return 0.0
    x = float(orientation.get("x") or 0.0)
    y = float(orientation.get("y") or 0.0)
    z = float(orientation.get("z") or 0.0)
    w = float(orientation.get("w") if orientation.get("w") is not None else 1.0)
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def planar_pose(value: dict[str, Any]) -> tuple[float, float, float]:
    pose = value.get("pose") if isinstance(value.get("pose"), dict) else value
    position = pose.get("position") if isinstance(pose, dict) else {}
    if not isinstance(position, dict):
        position = {}
    return float(position.get("x") or 0.0), float(position.get("y") or 0.0), yaw_from_pose(pose)


def map_to_odom(
    map_body: tuple[float, float, float],
    odom_body: tuple[float, float, float],
) -> tuple[float, float, float]:
    map_x, map_y, map_yaw = map_body
    odom_x, odom_y, odom_yaw = odom_body
    yaw = normalize_angle(map_yaw - odom_yaw)
    cos_yaw = math.cos(yaw)
    sin_yaw = math.sin(yaw)
    return (
        map_x - (cos_yaw * odom_x - sin_yaw * odom_y),
        map_y - (sin_yaw * odom_x + cos_yaw * odom_y),
        yaw,
    )


def compose_planar(
    parent_child: tuple[float, float, float],
    child_body: tuple[float, float, float],
) -> tuple[float, float, float]:
    px, py, pyaw = parent_child
    cx, cy, cyaw = child_body
    cos_yaw = math.cos(pyaw)
    sin_yaw = math.sin(pyaw)
    return (
        px + cos_yaw * cx - sin_yaw * cy,
        py + sin_yaw * cx + cos_yaw * cy,
        normalize_angle(pyaw + cyaw),
    )


def normalize_angle(value: float) -> float:
    return math.atan2(math.sin(value), math.cos(value))
