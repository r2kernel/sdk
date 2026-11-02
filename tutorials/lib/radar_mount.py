from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class RadarMount:
    """Planar radar mount transform expressed in the robot body frame."""

    translation_x_m: float
    translation_y_m: float
    yaw_rad: float


def load_radar_mount(manifest_path: Path) -> RadarMount:
    """Read and validate the cleaner compound's radar mount from its manifest."""
    document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"compound manifest must contain an object: {manifest_path}")
    spatial = document.get("spatial")
    if not isinstance(spatial, dict):
        raise ValueError(f"compound manifest has no spatial object: {manifest_path}")
    mounts = spatial.get("mounts")
    if not isinstance(mounts, list):
        raise ValueError(f"compound manifest spatial.mounts must be a list: {manifest_path}")
    matching_mounts = [mount for mount in mounts if isinstance(mount, dict) and mount.get("name") == "radar_on_body"]
    if len(matching_mounts) != 1:
        raise ValueError(f"compound manifest must define exactly one radar_on_body mount: {manifest_path}")

    mount = matching_mounts[0]
    if mount.get("parent") != {"requirement": "mobility", "port": "body"}:
        raise ValueError("radar_on_body must be parented to mobility.body")
    if mount.get("child") != {"requirement": "perception", "port": "sensor"}:
        raise ValueError("radar_on_body must attach perception.sensor")
    transform = mount.get("transform")
    if not isinstance(transform, dict):
        raise ValueError("radar_on_body must define a transform object")

    translation = _finite_vector(transform.get("translation"), "radar_on_body.transform.translation", 3)
    quaternion = _finite_vector(transform.get("rotation_xyzw"), "radar_on_body.transform.rotation_xyzw", 4)
    quaternion_norm = math.sqrt(sum(value * value for value in quaternion))
    if not math.isclose(quaternion_norm, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("radar_on_body rotation quaternion must be normalized")
    if any(abs(value) > 1e-9 for value in quaternion[:2]):
        raise ValueError("planar radar mount cannot contain roll or pitch")
    if abs(translation[2]) > 1e-9:
        raise ValueError("planar radar mount cannot have a vertical offset")

    quaternion_x, quaternion_y, quaternion_z, quaternion_w = quaternion
    yaw_rad = math.atan2(
        2.0 * (quaternion_w * quaternion_z + quaternion_x * quaternion_y),
        1.0 - 2.0 * (quaternion_y * quaternion_y + quaternion_z * quaternion_z),
    )
    return RadarMount(
        translation_x_m=translation[0],
        translation_y_m=translation[1],
        yaw_rad=yaw_rad,
    )


def _finite_vector(value: Any, field_name: str, length: int) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{field_name} must contain exactly {length} numbers")
    values: list[float] = []
    for index, component in enumerate(value):
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise ValueError(f"{field_name}[{index}] must be a number")
        number = float(component)
        if not math.isfinite(number):
            raise ValueError(f"{field_name}[{index}] must be finite")
        values.append(number)
    return tuple(values)
