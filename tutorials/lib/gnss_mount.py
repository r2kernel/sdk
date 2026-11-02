from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class GnssMount:
    frame_id: str
    translation_x_m: float
    translation_y_m: float
    translation_z_m: float


def load_gnss_mount(compound_manifest_path: Path, localizer_manifest_path: Path) -> GnssMount:
    compound_document = yaml.safe_load(compound_manifest_path.read_text(encoding="utf-8"))
    localizer_document = yaml.safe_load(localizer_manifest_path.read_text(encoding="utf-8"))
    if not isinstance(compound_document, dict):
        raise ValueError(f"compound manifest must contain an object: {compound_manifest_path}")
    if not isinstance(localizer_document, dict):
        raise ValueError(f"localizer manifest must contain an object: {localizer_manifest_path}")
    compound_spatial = compound_document.get("spatial")
    localizer_spatial = localizer_document.get("spatial")
    if not isinstance(compound_spatial, dict) or not isinstance(localizer_spatial, dict):
        raise ValueError("compound and localizer manifests must both define spatial objects")
    ports = localizer_spatial.get("ports")
    antenna_port = ports.get("antenna") if isinstance(ports, dict) else None
    if (
        not isinstance(antenna_port, dict)
        or antenna_port.get("direction") != "import"
        or antenna_port.get("frame") != "gnss_antenna"
    ):
        raise ValueError("component.localizer must import the gnss_antenna frame")

    mounts = compound_spatial.get("mounts")
    if not isinstance(mounts, list):
        raise ValueError(f"compound manifest spatial.mounts must be a list: {compound_manifest_path}")
    matching_mounts = [
        mount for mount in mounts
        if isinstance(mount, dict) and mount.get("name") == "gnss_antenna_on_body"
    ]
    if len(matching_mounts) != 1:
        raise ValueError(
            f"compound manifest must define exactly one gnss_antenna_on_body mount: {compound_manifest_path}"
        )

    mount = matching_mounts[0]
    if mount.get("parent") != {"requirement": "mobility", "port": "body"}:
        raise ValueError("gnss_antenna_on_body must be parented to mobility.body")
    if mount.get("child") != {"requirement": "localization", "port": "antenna"}:
        raise ValueError("gnss_antenna_on_body must attach localization.antenna")
    transform = mount.get("transform")
    if not isinstance(transform, dict):
        raise ValueError("gnss_antenna_on_body must define a transform object")

    translation = _finite_vector(
        transform.get("translation"), "gnss_antenna_on_body.transform.translation", 3
    )
    quaternion = _finite_vector(
        transform.get("rotation_xyzw"), "gnss_antenna_on_body.transform.rotation_xyzw", 4
    )
    quaternion_norm = math.sqrt(sum(value * value for value in quaternion))
    if not math.isclose(quaternion_norm, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("gnss_antenna_on_body rotation quaternion must be normalized")
    if any(abs(value) > 1e-9 for value in quaternion[:2]):
        raise ValueError("planar GNSS mount cannot contain roll or pitch")

    return GnssMount(
        frame_id="gnss_antenna",
        translation_x_m=translation[0],
        translation_y_m=translation[1],
        translation_z_m=translation[2],
    )


def _finite_vector(value: Any, field_name: str, length: int) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{field_name} must contain exactly {length} values")
    values: list[float] = []
    for index, component in enumerate(value):
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise ValueError(f"{field_name}[{index}] must be a number")
        number = float(component)
        if not math.isfinite(number):
            raise ValueError(f"{field_name}[{index}] must be finite")
        values.append(number)
    return tuple(values)
