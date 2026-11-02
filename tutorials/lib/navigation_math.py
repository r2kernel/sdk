from __future__ import annotations

import math
from typing import Any

from localization_math import planar_pose


def wrap_angle(value: float) -> float:
    return math.atan2(math.sin(float(value)), math.cos(float(value)))


def resolve_target_pose(
    *,
    frame_id: str,
    target_pose: dict[str, Any],
    current_map_pose: tuple[float, float, float],
    body_frame: str = "cleaner_base",
    radar_frame: str = "cleaner_radar",
    body_radar_transform: dict[str, Any] | None = None,
) -> tuple[float, float, float]:
    target = planar_pose(target_pose)
    normalized_frame = str(frame_id or "map").strip()
    if normalized_frame == "map":
        return target
    if normalized_frame in {body_frame, "base_link"}:
        body_target = target
    elif normalized_frame == radar_frame:
        if body_radar_transform is None:
            raise RuntimeError(
                f"navigation requires a current Spatial transform from {body_frame} to {radar_frame}"
            )
        mount_x, mount_y, mount_yaw = _planar_frame_transform(
            body_radar_transform,
            source_frame=body_frame,
            target_frame=radar_frame,
        )
        target_x, target_y, target_yaw = target
        body_x, body_y = _radar_point_in_body_for_mount(
            target_x,
            target_y,
            mount_x,
            mount_y,
            mount_yaw,
        )
        body_target = (body_x, body_y, wrap_angle(mount_yaw + target_yaw))
    else:
        raise ValueError(f"unsupported tutorial navigation frame: {normalized_frame}")

    current_x, current_y, current_yaw = current_map_pose
    target_x, target_y, target_yaw = body_target
    cos_yaw = math.cos(current_yaw)
    sin_yaw = math.sin(current_yaw)
    return (
        current_x + cos_yaw * target_x - sin_yaw * target_y,
        current_y + sin_yaw * target_x + cos_yaw * target_y,
        wrap_angle(current_yaw + target_yaw),
    )


def radar_point_in_body(
    target_x: float,
    target_y: float,
    body_radar_transform: dict[str, Any],
) -> tuple[float, float]:
    """Transform a point from cleaner_radar coordinates into cleaner_base."""
    mount_x, mount_y, mount_yaw = _planar_frame_transform(
        body_radar_transform,
        source_frame="cleaner_base",
        target_frame="cleaner_radar",
    )
    return _radar_point_in_body_for_mount(
        float(target_x),
        float(target_y),
        mount_x,
        mount_y,
        mount_yaw,
    )


def radar_detection_in_body(
    detection: dict[str, Any],
    *,
    source_frame_id: str,
    body_radar_transform: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Express one acquired observation in the body frame without changing its age."""
    if source_frame_id not in {"cleaner_base", "cleaner_radar"}:
        raise ValueError(f"unsupported radar observation frame: {source_frame_id!r}")
    distance = float(detection["distance"])
    bearing = math.radians(float(detection["bearing_deg"]))
    if not math.isfinite(distance) or distance <= 0.0 or not math.isfinite(bearing):
        raise ValueError("radar detection requires finite bearing and positive distance")
    body_x, body_y = math.cos(bearing) * distance, math.sin(bearing) * distance
    if source_frame_id == "cleaner_radar":
        if body_radar_transform is None:
            raise RuntimeError("radar detection requires a current Spatial transform from cleaner_base to cleaner_radar")
        body_x, body_y = radar_point_in_body(body_x, body_y, body_radar_transform)
    return {
        **detection,
        "frame_id": "cleaner_base",
        "relative_x": body_x,
        "relative_y": body_y,
        "distance": math.hypot(body_x, body_y),
        "bearing_deg": math.degrees(math.atan2(body_y, body_x)),
    }


def body_pose_in_radar(
    x: float,
    y: float,
    yaw: float,
    body_radar_transform: dict[str, Any],
) -> tuple[float, float, float]:
    """Express a cleaner_base pose in cleaner_radar coordinates."""
    mount_x, mount_y, mount_yaw = _planar_frame_transform(
        body_radar_transform,
        source_frame="cleaner_base",
        target_frame="cleaner_radar",
    )
    offset_x = float(x) - mount_x
    offset_y = float(y) - mount_y
    cos_mount_yaw = math.cos(mount_yaw)
    sin_mount_yaw = math.sin(mount_yaw)
    return (
        cos_mount_yaw * offset_x + sin_mount_yaw * offset_y,
        -sin_mount_yaw * offset_x + cos_mount_yaw * offset_y,
        wrap_angle(float(yaw) - mount_yaw),
    )


def _radar_point_in_body_for_mount(
    target_x: float,
    target_y: float,
    mount_x: float,
    mount_y: float,
    mount_yaw: float,
) -> tuple[float, float]:
    cos_mount_yaw = math.cos(mount_yaw)
    sin_mount_yaw = math.sin(mount_yaw)
    return (
        mount_x + cos_mount_yaw * target_x - sin_mount_yaw * target_y,
        mount_y + sin_mount_yaw * target_x + cos_mount_yaw * target_y,
    )


def _planar_frame_transform(
    transform: dict[str, Any],
    *,
    source_frame: str,
    target_frame: str,
) -> tuple[float, float, float]:
    if not isinstance(transform, dict) or transform.get("res") != "ok":
        detail = transform.get("error") if isinstance(transform, dict) else None
        if not detail:
            detail = "lookup returned no successful transform"
        raise RuntimeError(
            f"Spatial frame lookup from {source_frame} to {target_frame} failed: {detail}"
        )

    translation = transform.get("translation")
    rotation = transform.get("rotation_xyzw")
    if not isinstance(translation, (list, tuple)) or len(translation) != 3:
        raise RuntimeError(
            f"Spatial frame lookup from {source_frame} to {target_frame} returned an invalid translation"
        )
    if not isinstance(rotation, (list, tuple)) or len(rotation) != 4:
        raise RuntimeError(
            f"Spatial frame lookup from {source_frame} to {target_frame} returned an invalid rotation"
        )

    try:
        x, y, _z = (float(value) for value in translation)
        qx, qy, qz, qw = (float(value) for value in rotation)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Spatial frame lookup from {source_frame} to {target_frame} returned non-numeric data"
        ) from exc
    if not all(math.isfinite(value) for value in (x, y, qx, qy, qz, qw)):
        raise RuntimeError(
            f"Spatial frame lookup from {source_frame} to {target_frame} returned non-finite data"
        )

    quaternion_norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if quaternion_norm < 1e-12:
        raise RuntimeError(
            f"Spatial frame lookup from {source_frame} to {target_frame} returned a zero rotation"
        )
    qx /= quaternion_norm
    qy /= quaternion_norm
    qz /= quaternion_norm
    qw /= quaternion_norm
    yaw = math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))
    return x, y, yaw


def navigation_command(
    current: tuple[float, float, float],
    target: tuple[float, float, float],
    *,
    position_tolerance: float,
    yaw_tolerance: float,
    max_linear: float,
    max_angular: float,
) -> dict[str, float | bool]:
    current_x, current_y, current_yaw = current
    target_x, target_y, target_yaw = target
    dx = target_x - current_x
    dy = target_y - current_y
    distance = math.hypot(dx, dy)
    if distance > position_tolerance:
        path_yaw = math.atan2(dy, dx)
        heading_error = wrap_angle(path_yaw - current_yaw)
        linear_scale = max(0.0, 1.0 - abs(heading_error) / (math.pi / 2.0))
        linear = min(max_linear, max(0.10, distance * 2.4)) * linear_scale
        angular = max(-max_angular, min(max_angular, heading_error * 2.8))
        return {
            "reached": False,
            "linear": linear,
            "angular": angular,
            "remaining_distance": distance,
            "heading_error": heading_error,
        }
    yaw_error = wrap_angle(target_yaw - current_yaw)
    if abs(yaw_error) > yaw_tolerance:
        return {
            "reached": False,
            "linear": 0.0,
            "angular": max(-max_angular, min(max_angular, yaw_error * 2.6)),
            "remaining_distance": distance,
            "heading_error": yaw_error,
        }
    return {
        "reached": True,
        "linear": 0.0,
        "angular": 0.0,
        "remaining_distance": distance,
        "heading_error": yaw_error,
    }
