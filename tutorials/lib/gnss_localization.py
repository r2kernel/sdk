from __future__ import annotations

import math
from typing import Any

from gnss_fixture import GnssFixtureConfiguration
from gnss_math import antenna_enu_to_body_pose, body_pose_covariance6, geodetic_to_local_enu
from gnss_mount import GnssMount
from localization_math import compose_planar, map_to_odom, planar_pose
from standard_types import pose


def build_absolute_pose_fix(
    measurement: Any,
    odometry: dict[str, Any],
    *,
    fixture: GnssFixtureConfiguration,
    mount: GnssMount,
) -> dict[str, Any]:
    antenna_up_m: float | None = None
    if measurement.altitude_m is not None:
        antenna_enu_m = geodetic_to_local_enu(
            measurement.latitude_deg,
            measurement.longitude_deg,
            measurement.altitude_m,
            fixture.geographic_origin,
        )
        antenna_up_m = antenna_enu_m[2]
    else:
        antenna_enu_m = geodetic_to_local_enu(
            measurement.latitude_deg,
            measurement.longitude_deg,
            fixture.geographic_origin.altitude_m,
            fixture.geographic_origin,
        )

    body_x, body_y, body_z, body_yaw = antenna_enu_to_body_pose(
        (antenna_enu_m[0], antenna_enu_m[1], antenna_up_m),
        planar_pose(odometry)[2],
        mount,
        map_from_odometry_yaw_rad=fixture.map_from_odometry_yaw_rad,
        planar_body_height_m=fixture.planar_body_height_m,
    )
    covariance = body_pose_covariance6(
        antenna_covariance_enu_m2=measurement.position_covariance_m2,
        odometry_yaw_rad=planar_pose(odometry)[2],
        map_from_odometry_yaw_rad=fixture.map_from_odometry_yaw_rad,
        mount=mount,
        heading_variance_rad2=(
            fixture.odometry_heading_variance_rad2
            + fixture.map_from_odometry_yaw_variance_rad2
        ),
        roll_pitch_variance_rad2=fixture.planar_roll_pitch_variance_rad2,
        planar_vertical_variance_m2=(
            fixture.planar_vertical_variance_m2
            if measurement.altitude_m is None
            else None
        ),
    )
    return {
        "frame_id": fixture.geographic_origin.frame_id,
        "child_frame_id": "cleaner_base",
        "pose": pose(x=body_x, y=body_y, z=body_z, yaw=body_yaw),
        "covariance": covariance,
        "source": "component.localizer",
    }


def absolute_fix_to_map_odom(
    position_fix: dict[str, Any], odometry: dict[str, Any]
) -> tuple[float, float, float]:
    return map_to_odom(planar_pose(position_fix), planar_pose(odometry))


def absolute_fix_to_map_odom_height(
    position_fix: dict[str, Any], odometry: dict[str, Any]
) -> float:
    fix_z = _finite(position_fix["pose"]["position"].get("z"), "position_fix.pose.position.z")
    odometry_z = _finite(odometry["pose"]["position"].get("z"), "odometry.pose.position.z")
    return fix_z - odometry_z


def build_pose_estimate(
    odometry: dict[str, Any],
    map_odom: tuple[float, float, float],
    covariance: dict[str, list[float]],
    *,
    frame_id: str,
    map_odom_height_m: float,
) -> dict[str, Any]:
    map_pose = compose_planar(map_odom, planar_pose(odometry))
    odometry_height_m = _finite(odometry["pose"]["position"].get("z"), "odometry.pose.position.z")
    return {
        "frame_id": frame_id,
        "pose": pose(
            x=map_pose[0],
            y=map_pose[1],
            z=map_odom_height_m + odometry_height_m,
            yaw=map_pose[2],
        ),
        "covariance": {"values": list(covariance["values"])},
        "source": "component.localizer",
    }


def validate_odometry_pose(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("odometry payload must be an object")
    pose_value = value.get("pose")
    if not isinstance(pose_value, dict):
        raise ValueError("odometry payload must contain pose")
    position = pose_value.get("position")
    orientation = pose_value.get("orientation")
    if not isinstance(position, dict) or not isinstance(orientation, dict):
        raise ValueError("odometry pose must contain position and orientation objects")
    for field_name in ("x", "y", "z"):
        _finite(position.get(field_name), f"odometry.pose.position.{field_name}")
    quaternion = tuple(
        _finite(orientation.get(field_name), f"odometry.pose.orientation.{field_name}")
        for field_name in ("x", "y", "z", "w")
    )
    quaternion_norm = math.sqrt(sum(component * component for component in quaternion))
    if not math.isclose(quaternion_norm, 1.0, rel_tol=0.0, abs_tol=1e-5):
        raise ValueError("odometry orientation quaternion must be normalized")
    return value


def covariance_values(value: Any) -> list[float]:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("values"), (list, tuple))
    ):
        raise ValueError("pose covariance must contain a values sequence")
    values = list(value["values"])
    if len(values) != 36:
        raise ValueError("pose covariance must contain exactly 36 values")
    return [_finite(component, f"pose covariance[{index}]") for index, component in enumerate(values)]


def _finite(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be finite")
    return float(value)
