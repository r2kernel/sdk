from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gnss_profile import validate_position_covariance_m2


@dataclass(frozen=True)
class GeographicOrigin:
    frame_id: str
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    altitude_reference: str


@dataclass(frozen=True)
class GnssFixtureConfiguration:
    geographic_origin: GeographicOrigin
    clock_status: str
    source_clock: str | None
    target_clock: str | None
    clock_offset_ns: int | None
    clock_alignment_tolerance_ns: int | None
    simulator_clock_id: str
    simulator_sample_period_ns: int
    simulator_position_covariance_m2_enu: tuple[float, ...]
    maximum_measurement_age_ns: int
    future_timestamp_tolerance_ns: int
    map_from_odometry_yaw_rad: float
    map_from_odometry_yaw_variance_rad2: float
    odometry_heading_variance_rad2: float
    planar_roll_pitch_variance_rad2: float
    planar_body_height_m: float
    planar_vertical_variance_m2: float


def load_gnss_fixture(path: Path) -> GnssFixtureConfiguration:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != "tutorial.gnss_fixture.v1":
        raise ValueError(f"GNSS fixture must use schema tutorial.gnss_fixture.v1: {path}")

    origin = _mapping(document, "geographic_origin")
    geographic_origin = GeographicOrigin(
        frame_id=_required_text(origin, "frame_id", "geographic_origin"),
        latitude_deg=_bounded_number(origin, "latitude_deg", "geographic_origin", -90.0, 90.0),
        longitude_deg=_bounded_number(origin, "longitude_deg", "geographic_origin", -180.0, 180.0),
        altitude_m=_finite_number(origin, "altitude_m", "geographic_origin"),
        altitude_reference=_required_choice(
            origin,
            "altitude_reference",
            "geographic_origin",
            {"wgs84_ellipsoid", "mean_sea_level"},
        ),
    )
    if not geographic_origin.frame_id.strip():
        raise ValueError("geographic_origin.frame_id must not be empty")
    if geographic_origin.altitude_reference != "wgs84_ellipsoid":
        raise ValueError("geographic_origin altitude must use wgs84_ellipsoid without a configured geoid model")

    clock = _mapping(document, "clock_mapping")
    clock_status = _required_choice(clock, "status", "clock_mapping", {"known", "unknown"})
    if clock_status == "known":
        source_clock = _required_text(clock, "source_clock", "clock_mapping")
        target_clock = _required_text(clock, "target_clock", "clock_mapping")
        if target_clock != "r2kernel_sync":
            raise ValueError("known clock_mapping.target_clock must be r2kernel_sync")
        clock_offset_ns = _integer(clock, "offset_ns", "clock_mapping")
        alignment_tolerance_ns = _positive_integer(clock, "alignment_tolerance_ns", "clock_mapping")
    else:
        forbidden_clock_fields = {"source_clock", "target_clock", "offset_ns", "alignment_tolerance_ns"}
        if forbidden_clock_fields.intersection(clock):
            raise ValueError("unknown clock mapping must omit source, target, offset, and alignment tolerance")
        source_clock = None
        target_clock = None
        clock_offset_ns = None
        alignment_tolerance_ns = None

    simulator = _mapping(document, "simulator")
    simulator_clock_id = _required_text(simulator, "clock_id", "simulator")
    sample_period_ns = _positive_duration_ns(simulator, "sample_period_ms", "simulator")
    covariance = _finite_vector(
        simulator.get("position_covariance_m2_enu"),
        "simulator.position_covariance_m2_enu",
        9,
    )
    validate_position_covariance_m2(
        covariance, name="simulator.position_covariance_m2_enu"
    )

    localizer = _mapping(document, "localizer")
    maximum_age_ns = _positive_duration_ns(localizer, "maximum_measurement_age_ms", "localizer")
    future_tolerance_ns = _positive_duration_ns(localizer, "future_timestamp_tolerance_ms", "localizer")
    map_from_odometry_yaw = _finite_number(localizer, "map_from_odometry_yaw_rad", "localizer")
    map_alignment_variance = _nonnegative_number(
        localizer, "map_from_odometry_yaw_variance_rad2", "localizer"
    )
    heading_variance = _nonnegative_number(localizer, "odometry_heading_variance_rad2", "localizer")
    roll_pitch_variance = _positive_number(localizer, "planar_roll_pitch_variance_rad2", "localizer")
    planar_body_height_m = _finite_number(localizer, "planar_body_height_m", "localizer")
    planar_vertical_variance = _positive_number(localizer, "planar_vertical_variance_m2", "localizer")

    return GnssFixtureConfiguration(
        geographic_origin=geographic_origin,
        clock_status=clock_status,
        source_clock=source_clock,
        target_clock=target_clock,
        clock_offset_ns=clock_offset_ns,
        clock_alignment_tolerance_ns=alignment_tolerance_ns,
        simulator_clock_id=simulator_clock_id,
        simulator_sample_period_ns=sample_period_ns,
        simulator_position_covariance_m2_enu=covariance,
        maximum_measurement_age_ns=maximum_age_ns,
        future_timestamp_tolerance_ns=future_tolerance_ns,
        map_from_odometry_yaw_rad=map_from_odometry_yaw,
        map_from_odometry_yaw_variance_rad2=map_alignment_variance,
        odometry_heading_variance_rad2=heading_variance,
        planar_roll_pitch_variance_rad2=roll_pitch_variance,
        planar_body_height_m=planar_body_height_m,
        planar_vertical_variance_m2=planar_vertical_variance,
    )


def _mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _required_text(parent: dict[str, Any], key: str, prefix: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{prefix}.{key} must be non-empty text")
    return value.strip()


def _required_choice(parent: dict[str, Any], key: str, prefix: str, choices: set[str]) -> str:
    value = _required_text(parent, key, prefix)
    if value not in choices:
        raise ValueError(f"{prefix}.{key} must be one of {sorted(choices)}")
    return value


def _finite_number(parent: dict[str, Any], key: str, prefix: str) -> float:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{prefix}.{key} must be a finite number")
    return float(value)


def _bounded_number(parent: dict[str, Any], key: str, prefix: str, minimum: float, maximum: float) -> float:
    value = _finite_number(parent, key, prefix)
    if value < minimum or value > maximum:
        raise ValueError(f"{prefix}.{key} must be in [{minimum}, {maximum}]")
    return value


def _positive_number(parent: dict[str, Any], key: str, prefix: str) -> float:
    value = _finite_number(parent, key, prefix)
    if value <= 0.0:
        raise ValueError(f"{prefix}.{key} must be positive")
    return value


def _nonnegative_number(parent: dict[str, Any], key: str, prefix: str) -> float:
    value = _finite_number(parent, key, prefix)
    if value < 0.0:
        raise ValueError(f"{prefix}.{key} must be non-negative")
    return value


def _integer(parent: dict[str, Any], key: str, prefix: str) -> int:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{prefix}.{key} must be an integer")
    return value


def _positive_integer(parent: dict[str, Any], key: str, prefix: str) -> int:
    value = _integer(parent, key, prefix)
    if value <= 0:
        raise ValueError(f"{prefix}.{key} must be positive")
    return value


def _positive_duration_ns(parent: dict[str, Any], key: str, prefix: str) -> int:
    nanoseconds = _positive_number(parent, key, prefix) * 1_000_000
    if not nanoseconds.is_integer():
        raise ValueError(f"{prefix}.{key} must resolve to an integer number of nanoseconds")
    return int(nanoseconds)


def _finite_vector(value: Any, field_name: str, length: int) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{field_name} must contain exactly {length} values")
    output: list[float] = []
    for index, component in enumerate(value):
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise ValueError(f"{field_name}[{index}] must be a number")
        numeric = float(component)
        if not math.isfinite(numeric):
            raise ValueError(f"{field_name}[{index}] must be finite")
        output.append(numeric)
    return tuple(output)
