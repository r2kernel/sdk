from __future__ import annotations

import math
from collections.abc import Sequence

from gnss_fixture import GeographicOrigin
from gnss_mount import GnssMount
from gnss_profile import validate_position_covariance_m2


WGS84_SEMI_MAJOR_AXIS_M = 6_378_137.0
WGS84_FLATTENING = 1.0 / 298.257223563
WGS84_ECCENTRICITY_SQUARED = WGS84_FLATTENING * (2.0 - WGS84_FLATTENING)


def geodetic_to_local_enu(
    latitude_deg: float,
    longitude_deg: float,
    altitude_m: float,
    origin: GeographicOrigin,
) -> tuple[float, float, float]:
    """Convert WGS84 geodetic coordinates into east-north-up metres at origin."""
    _validate_ellipsoid_origin(origin)
    latitude = _bounded_finite(latitude_deg, "latitude_deg", -90.0, 90.0)
    longitude = _bounded_finite(longitude_deg, "longitude_deg", -180.0, 180.0)
    altitude = _finite(altitude_m, "altitude_m")
    point_ecef = geodetic_to_ecef(latitude, longitude, altitude)
    origin_ecef = geodetic_to_ecef(origin.latitude_deg, origin.longitude_deg, origin.altitude_m)
    delta_x, delta_y, delta_z = tuple(point - reference for point, reference in zip(point_ecef, origin_ecef))
    origin_latitude = math.radians(origin.latitude_deg)
    origin_longitude = math.radians(origin.longitude_deg)
    sine_latitude = math.sin(origin_latitude)
    cosine_latitude = math.cos(origin_latitude)
    sine_longitude = math.sin(origin_longitude)
    cosine_longitude = math.cos(origin_longitude)
    east_m = -sine_longitude * delta_x + cosine_longitude * delta_y
    north_m = (
        -sine_latitude * cosine_longitude * delta_x
        - sine_latitude * sine_longitude * delta_y
        + cosine_latitude * delta_z
    )
    up_m = (
        cosine_latitude * cosine_longitude * delta_x
        + cosine_latitude * sine_longitude * delta_y
        + sine_latitude * delta_z
    )
    return east_m, north_m, up_m


def local_enu_to_geodetic(
    east_m: float,
    north_m: float,
    up_m: float,
    origin: GeographicOrigin,
) -> tuple[float, float, float]:
    """Convert local east-north-up metres to WGS84 latitude, longitude, height."""
    _validate_ellipsoid_origin(origin)
    east = _finite(east_m, "east_m")
    north = _finite(north_m, "north_m")
    up = _finite(up_m, "up_m")
    origin_x, origin_y, origin_z = geodetic_to_ecef(
        origin.latitude_deg, origin.longitude_deg, origin.altitude_m
    )
    origin_latitude = math.radians(origin.latitude_deg)
    origin_longitude = math.radians(origin.longitude_deg)
    sine_latitude = math.sin(origin_latitude)
    cosine_latitude = math.cos(origin_latitude)
    sine_longitude = math.sin(origin_longitude)
    cosine_longitude = math.cos(origin_longitude)
    point_x = (
        origin_x
        - sine_longitude * east
        - sine_latitude * cosine_longitude * north
        + cosine_latitude * cosine_longitude * up
    )
    point_y = (
        origin_y
        + cosine_longitude * east
        - sine_latitude * sine_longitude * north
        + cosine_latitude * sine_longitude * up
    )
    point_z = origin_z + cosine_latitude * north + sine_latitude * up
    return ecef_to_geodetic(point_x, point_y, point_z)


def geodetic_to_ecef(latitude_deg: float, longitude_deg: float, altitude_m: float) -> tuple[float, float, float]:
    latitude = math.radians(_bounded_finite(latitude_deg, "latitude_deg", -90.0, 90.0))
    longitude = math.radians(_bounded_finite(longitude_deg, "longitude_deg", -180.0, 180.0))
    altitude = _finite(altitude_m, "altitude_m")
    sine_latitude = math.sin(latitude)
    cosine_latitude = math.cos(latitude)
    prime_vertical_radius = WGS84_SEMI_MAJOR_AXIS_M / math.sqrt(
        1.0 - WGS84_ECCENTRICITY_SQUARED * sine_latitude * sine_latitude
    )
    x = (prime_vertical_radius + altitude) * cosine_latitude * math.cos(longitude)
    y = (prime_vertical_radius + altitude) * cosine_latitude * math.sin(longitude)
    z = (
        prime_vertical_radius * (1.0 - WGS84_ECCENTRICITY_SQUARED) + altitude
    ) * sine_latitude
    return x, y, z


def ecef_to_geodetic(x_m: float, y_m: float, z_m: float) -> tuple[float, float, float]:
    x = _finite(x_m, "x_m")
    y = _finite(y_m, "y_m")
    z = _finite(z_m, "z_m")
    longitude = math.atan2(y, x)
    horizontal_radius = math.hypot(x, y)
    if horizontal_radius < 1e-9:
        raise ValueError("geodetic conversion is undefined at the earth center and poles")

    latitude = math.atan2(z, horizontal_radius * (1.0 - WGS84_ECCENTRICITY_SQUARED))
    altitude = 0.0
    for _iteration in range(12):
        sine_latitude = math.sin(latitude)
        prime_vertical_radius = WGS84_SEMI_MAJOR_AXIS_M / math.sqrt(
            1.0 - WGS84_ECCENTRICITY_SQUARED * sine_latitude * sine_latitude
        )
        cosine_latitude = math.cos(latitude)
        if abs(cosine_latitude) < 1e-12:
            altitude = abs(z) - prime_vertical_radius * (1.0 - WGS84_ECCENTRICITY_SQUARED)
        else:
            altitude = horizontal_radius / cosine_latitude - prime_vertical_radius
        denominator = prime_vertical_radius + altitude
        if denominator <= 0.0:
            raise ValueError("ECEF coordinate is inside the WGS84 ellipsoid")
        next_latitude = math.atan2(
            z,
            horizontal_radius
            * (1.0 - WGS84_ECCENTRICITY_SQUARED * prime_vertical_radius / denominator),
        )
        if abs(next_latitude - latitude) <= 1e-14:
            latitude = next_latitude
            break
        latitude = next_latitude
    return math.degrees(latitude), math.degrees(longitude), altitude


def antenna_enu_to_body_pose(
    antenna_enu_m: Sequence[float | None],
    odometry_yaw_rad: float,
    mount: GnssMount,
    *,
    map_from_odometry_yaw_rad: float,
    planar_body_height_m: float,
) -> tuple[float, float, float, float]:
    if len(antenna_enu_m) != 3:
        raise ValueError("antenna_enu_m must contain exactly east, north, and up")
    antenna_east = _finite(antenna_enu_m[0], "antenna_enu_m[0]")
    antenna_north = _finite(antenna_enu_m[1], "antenna_enu_m[1]")
    antenna_up = (
        planar_body_height_m
        if antenna_enu_m[2] is None
        else _finite(antenna_enu_m[2], "antenna_enu_m[2]")
    )
    odometry_yaw = _finite(odometry_yaw_rad, "odometry_yaw_rad")
    map_alignment_yaw = _finite(map_from_odometry_yaw_rad, "map_from_odometry_yaw_rad")
    body_yaw = _normalize_angle(odometry_yaw + map_alignment_yaw)
    cosine_yaw = math.cos(body_yaw)
    sine_yaw = math.sin(body_yaw)
    offset_east = cosine_yaw * mount.translation_x_m - sine_yaw * mount.translation_y_m
    offset_north = sine_yaw * mount.translation_x_m + cosine_yaw * mount.translation_y_m
    body_z = (
        antenna_up - mount.translation_z_m
        if antenna_enu_m[2] is not None
        else planar_body_height_m
    )
    return antenna_east - offset_east, antenna_north - offset_north, body_z, body_yaw


def body_pose_covariance6(
    *,
    antenna_covariance_enu_m2: Sequence[float],
    odometry_yaw_rad: float,
    map_from_odometry_yaw_rad: float,
    mount: GnssMount,
    heading_variance_rad2: float,
    roll_pitch_variance_rad2: float,
    planar_vertical_variance_m2: float | None = None,
) -> dict[str, list[float]]:
    if len(antenna_covariance_enu_m2) != 9:
        raise ValueError("antenna_covariance_enu_m2 must contain exactly nine row-major values")
    covariance = tuple(
        _finite(value, f"antenna_covariance_enu_m2[{index}]")
        for index, value in enumerate(antenna_covariance_enu_m2)
    )
    validate_position_covariance_m2(covariance, name="antenna_covariance_enu_m2")
    heading_variance = _nonnegative(heading_variance_rad2, "heading_variance_rad2")
    roll_pitch_variance = _positive(roll_pitch_variance_rad2, "roll_pitch_variance_rad2")
    planar_vertical_variance = (
        None
        if planar_vertical_variance_m2 is None
        else _positive(planar_vertical_variance_m2, "planar_vertical_variance_m2")
    )
    yaw = _normalize_angle(
        _finite(odometry_yaw_rad, "odometry_yaw_rad")
        + _finite(map_from_odometry_yaw_rad, "map_from_odometry_yaw_rad")
    )
    cosine_yaw = math.cos(yaw)
    sine_yaw = math.sin(yaw)
    derivative_x = sine_yaw * mount.translation_x_m + cosine_yaw * mount.translation_y_m
    derivative_y = -cosine_yaw * mount.translation_x_m + sine_yaw * mount.translation_y_m
    values = [0.0] * 36
    for row in range(3):
        for column in range(3):
            values[row * 6 + column] = covariance[row * 3 + column]
    if planar_vertical_variance is not None:
        # A 2D fix has no measured vertical coordinate. Apply the explicit
        # configured height prior without discarding a valid zero variance or
        # pretending that horizontal/vertical position errors are correlated.
        values[2] = 0.0
        values[8] = 0.0
        values[12] = 0.0
        values[13] = 0.0
        values[14] = planar_vertical_variance

    # First-order lever-arm Jacobian at zero mean roll and pitch. The
    # configured angular priors account for translation and cross-covariance
    # induced by the nonzero antenna offset.
    lever_arm_jacobian = (
        (-sine_yaw * mount.translation_z_m, -cosine_yaw * mount.translation_z_m, derivative_x),
        (cosine_yaw * mount.translation_z_m, -sine_yaw * mount.translation_z_m, derivative_y),
        (-mount.translation_y_m, mount.translation_x_m, 0.0),
    )
    rotational_variances = (
        roll_pitch_variance,
        roll_pitch_variance,
        heading_variance,
    )
    for row in range(3):
        for column in range(3):
            values[row * 6 + column] += sum(
                lever_arm_jacobian[row][axis]
                * lever_arm_jacobian[column][axis]
                * rotational_variances[axis]
                for axis in range(3)
            )
        for axis in range(3):
            covariance_index = 3 + axis
            position_attitude_covariance = (
                lever_arm_jacobian[row][axis] * rotational_variances[axis]
            )
            values[row * 6 + covariance_index] = position_attitude_covariance
            values[covariance_index * 6 + row] = position_attitude_covariance
    values[21] = roll_pitch_variance
    values[28] = roll_pitch_variance
    values[35] = heading_variance
    return {"values": values}


def _normalize_angle(value: float) -> float:
    return math.atan2(math.sin(value), math.cos(value))


def _finite(value: float, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be a finite number")
    return float(value)


def _bounded_finite(value: float, field_name: str, minimum: float, maximum: float) -> float:
    number = _finite(value, field_name)
    if not minimum <= number <= maximum:
        raise ValueError(f"{field_name} must be in [{minimum}, {maximum}]")
    return number


def _nonnegative(value: float, field_name: str) -> float:
    number = _finite(value, field_name)
    if number < 0.0:
        raise ValueError(f"{field_name} must be non-negative")
    return number


def _positive(value: float, field_name: str) -> float:
    number = _finite(value, field_name)
    if number <= 0.0:
        raise ValueError(f"{field_name} must be positive")
    return number


def _validate_ellipsoid_origin(origin: GeographicOrigin) -> None:
    if origin.altitude_reference != "wgs84_ellipsoid":
        raise ValueError("ECEF conversion requires an origin height above the WGS84 ellipsoid")
    _bounded_finite(origin.latitude_deg, "origin.latitude_deg", -90.0, 90.0)
    _bounded_finite(origin.longitude_deg, "origin.longitude_deg", -180.0, 180.0)
    _finite(origin.altitude_m, "origin.altitude_m")
