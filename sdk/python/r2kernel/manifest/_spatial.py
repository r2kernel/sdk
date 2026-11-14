"""Spatial declaration parsing, status and conformance/topology validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from r2kernel.contract.loader import ContractSpecError, load_api_contract_spec, load_component_spec
from r2kernel.interface import InterfaceSpec, spatial_frame_ports
from ._fields import _float_tuple
from .model import (
    ManifestError,
    SPATIAL_PARTICIPATION,
    SPATIAL_PORT_DIRECTIONS,
    SpatialDeclaration,
    SpatialMountDeclaration,
    SpatialPortDeclaration,
    SpatialPortReference,
)

if TYPE_CHECKING:
    from .model import DeploymentManifest, SubsystemManifest


def _spatial_declaration_status(spatial: SpatialDeclaration) -> dict[str, Any]:
    return {
        "participation": spatial.participation,
        "ports": {
            name: {
                "direction": port.direction,
                "semantic": port.semantic,
                "frame": port.frame,
                "required": port.required,
                **(
                    {
                        "source": {
                            "requirement": port.source.requirement,
                            "port": port.source.port,
                        }
                    }
                    if port.source is not None
                    else {}
                ),
            }
            for name, port in spatial.ports.items()
        },
        "mounts": [
            {
                "name": mount.name,
                "parent": {
                    "requirement": mount.parent.requirement,
                    "port": mount.parent.port,
                },
                "child": {
                    "requirement": mount.child.requirement,
                    "port": mount.child.port,
                },
                "transform": {
                    "translation": list(mount.translation),
                    "rotation_xyzw": list(mount.rotation_xyzw),
                },
            }
            for mount in spatial.mounts
        ],
    }


def _parse_spatial_declaration(value: Any, *, manifest_path: Path) -> SpatialDeclaration:
    if value is None:
        return SpatialDeclaration()
    if not isinstance(value, dict):
        raise ManifestError(f"spatial must be a mapping: {manifest_path}")
    participation = str(value.get("participation") or "").strip()
    if participation not in SPATIAL_PARTICIPATION:
        raise ManifestError(
            f"spatial.participation must be one of {', '.join(SPATIAL_PARTICIPATION)}: {manifest_path}"
        )
    raw_ports = value.get("ports") or {}
    if not isinstance(raw_ports, dict):
        raise ManifestError(f"spatial.ports must be a mapping: {manifest_path}")
    ports: dict[str, SpatialPortDeclaration] = {}
    for raw_name, raw_port in raw_ports.items():
        name = str(raw_name or "").strip()
        if not name or not isinstance(raw_port, dict):
            raise ManifestError(f"spatial.ports contains an invalid port: {manifest_path}")
        direction = str(raw_port.get("direction") or "").strip()
        if direction not in SPATIAL_PORT_DIRECTIONS:
            raise ManifestError(f"spatial.ports.{name}.direction must be import or export")
        semantic = str(raw_port.get("semantic") or "").strip()
        if not semantic:
            raise ManifestError(f"spatial.ports.{name} must define semantic")
        frame = str(raw_port.get("frame") or "").strip() or None
        source = _parse_spatial_port_reference(
            raw_port.get("source"),
            field_name=f"spatial.ports.{name}.source",
            required=False,
        )
        if frame is None and source is None:
            raise ManifestError(f"spatial.ports.{name} must define frame or source")
        if frame is not None and source is not None:
            raise ManifestError(f"spatial.ports.{name} cannot define both frame and source")
        ports[name] = SpatialPortDeclaration(
            name=name,
            direction=direction,
            semantic=semantic,
            frame=frame,
            required=bool(raw_port.get("required", True)),
            source=source,
        )

    raw_mounts = value.get("mounts") or []
    if not isinstance(raw_mounts, list):
        raise ManifestError(f"spatial.mounts must be a list: {manifest_path}")
    mounts: list[SpatialMountDeclaration] = []
    names: set[str] = set()
    for index, raw_mount in enumerate(raw_mounts):
        if not isinstance(raw_mount, dict):
            raise ManifestError(f"spatial.mounts[{index}] must be a mapping")
        name = str(raw_mount.get("name") or f"mount_{index}").strip()
        if not name or name in names:
            raise ManifestError(f"spatial.mounts contains an empty or duplicate name: {name!r}")
        names.add(name)
        parent = _parse_spatial_port_reference(
            raw_mount.get("parent"), field_name=f"spatial.mounts.{name}.parent", required=True
        )
        child = _parse_spatial_port_reference(
            raw_mount.get("child"), field_name=f"spatial.mounts.{name}.child", required=True
        )
        transform = raw_mount.get("transform") or {}
        if not isinstance(transform, dict):
            raise ManifestError(f"spatial.mounts.{name}.transform must be a mapping")
        translation = _float_tuple(
            transform.get("translation") or (0.0, 0.0, 0.0),
            length=3,
            field_name=f"spatial.mounts.{name}.transform.translation",
        )
        rotation = _float_tuple(
            transform.get("rotation_xyzw") or (0.0, 0.0, 0.0, 1.0),
            length=4,
            field_name=f"spatial.mounts.{name}.transform.rotation_xyzw",
        )
        if math.sqrt(sum(item * item for item in rotation)) < 1e-9:
            raise ManifestError(f"spatial.mounts.{name} rotation quaternion must be non-zero")
        mounts.append(
            SpatialMountDeclaration(
                name=name,
                parent=parent,
                child=child,
                translation=translation,
                rotation_xyzw=rotation,
            )
        )
    if participation == "none" and (ports or mounts):
        raise ManifestError("spatial.participation none cannot declare ports or mounts")
    if participation == "required" and not ports and not mounts:
        raise ManifestError("spatial.participation required must declare at least one port or mount")
    return SpatialDeclaration(participation=participation, ports=ports, mounts=tuple(mounts))


def _parse_spatial_port_reference(value: Any, *, field_name: str, required: bool) -> SpatialPortReference | None:
    if value is None:
        if required:
            raise ManifestError(f"{field_name} must be a mapping")
        return None
    if not isinstance(value, dict):
        raise ManifestError(f"{field_name} must be a mapping")
    requirement = str(value.get("requirement") or "").strip()
    port = str(value.get("port") or "").strip()
    if not requirement or not port:
        raise ManifestError(f"{field_name} must define requirement and port")
    return SpatialPortReference(requirement=requirement, port=port)


def _validate_subsystem_spatial_conformance(manifest: SubsystemManifest) -> None:
    for declaration in manifest.provides.values():
        if declaration.visibility != "public" or not declaration.contract.startswith("component."):
            continue
        try:
            component = load_component_spec(declaration.contract)
        except ContractSpecError as exc:
            raise ManifestError(str(exc)) from exc
        expected = component.spatial
        if expected.participation == "none":
            continue
        if manifest.spatial.participation != "required":
            raise ManifestError(
                f"Subsystem {manifest.name} implements {component.id}, which requires spatial participation"
            )
        for name, expected_port in expected.ports.items():
            if not expected_port.required:
                continue
            actual = manifest.spatial.ports.get(name)
            if actual is None:
                raise ManifestError(
                    f"Subsystem {manifest.name} is missing spatial port {name!r} required by {component.id}"
                )
            if actual.direction != expected_port.direction:
                raise ManifestError(
                    f"Subsystem {manifest.name} spatial port {name!r} direction {actual.direction!r} "
                    f"does not match {component.id} direction {expected_port.direction!r}"
                )


@dataclass(frozen=True)
class ProvidedFramePort:
    """A field of a published sample that must carry the frame of one of the provider's spatial ports."""

    field: str
    port: str
    frame: str
    contract: str


def _provided_frame_ports(manifest: SubsystemManifest) -> dict[str, tuple[ProvidedFramePort, ...]]:
    """Published topic -> frame checks of the public provided APIs whose frames.policy is spatial_ports.

    Each mapped port must be an export port with a literal frame, so the
    provider and its consumers read the frame from the provider's own manifest.
    """
    checks: dict[str, list[ProvidedFramePort]] = {}
    for interface_name, declaration in manifest.provides.items():
        if declaration.visibility != "public":
            continue
        for api in _provided_api_contracts(declaration.contract, field_name=f"provides.{interface_name}"):
            for field_name, port_name in spatial_frame_ports(api).items():
                check = ProvidedFramePort(
                    field=field_name,
                    port=port_name,
                    frame=_provided_port_frame(manifest, api.id, interface_name, field_name, port_name),
                    contract=api.id,
                )
                for event in api.published_events():
                    topic_checks = checks.setdefault(event.topic, [])
                    if check not in topic_checks:
                        topic_checks.append(check)
    return {topic: tuple(topic_checks) for topic, topic_checks in checks.items()}


def _provided_api_contracts(contract: str, *, field_name: str) -> tuple[InterfaceSpec, ...]:
    """The API contracts a public contract provides; specs that do not exist are reported elsewhere."""
    try:
        if contract.startswith("api."):
            api_ids: tuple[str, ...] = (contract,)
        elif contract.startswith("component."):
            api_ids = load_component_spec(contract).conformance_apis
        else:
            return ()
        apis = []
        for api_id in api_ids:
            try:
                apis.append(load_api_contract_spec(api_id))
            except ContractSpecError:
                continue
        return tuple(apis)
    except ContractSpecError:
        return ()
    except Exception as exc:
        raise ManifestError(f"Cannot inspect contract {contract!r} for {field_name}: {exc}") from exc


def _provided_port_frame(
    manifest: SubsystemManifest, api_id: str, interface_name: str, field_name: str, port_name: str
) -> str:
    subject = (
        f"Subsystem {manifest.name} provides {api_id} through provides.{interface_name}; "
        f"its frames.ports.{field_name} names spatial port {port_name!r}"
    )
    port = manifest.spatial.ports.get(port_name)
    if port is None:
        raise ManifestError(f"{subject}, which the subsystem does not declare")
    if port.direction != "export":
        raise ManifestError(f"{subject}, which must be an export port, not {port.direction!r}")
    if port.frame is None:
        raise ManifestError(f"{subject}, which must declare a literal frame instead of a source alias")
    return port.frame


def _validate_deployment_spatial_topology(deployment: DeploymentManifest) -> None:
    for subsystem in deployment.subsystems.values():
        spatial = subsystem.spatial
        if spatial.participation == "none":
            continue
        for port in spatial.ports.values():
            if port.source is None:
                continue
            target = _resolve_spatial_target_port(deployment, subsystem, port.source)
            same_direction_alias = target.direction == port.direction
            imported_export = port.direction == "import" and target.direction == "export"
            if not same_direction_alias and not imported_export:
                raise ManifestError(
                    f"Spatial port {subsystem.name}.{port.name} cannot source {target.direction} port "
                    f"{port.source.requirement}.{port.source.port} as {port.direction}"
                )
        for mount in spatial.mounts:
            parent = _resolve_spatial_target_port(deployment, subsystem, mount.parent)
            child = _resolve_spatial_target_port(deployment, subsystem, mount.child)
            if parent.direction != "export":
                raise ManifestError(
                    f"Spatial mount {subsystem.name}.{mount.name} parent must reference an export port"
                )
            if child.direction != "import":
                raise ManifestError(
                    f"Spatial mount {subsystem.name}.{mount.name} child must reference an import port"
                )


def _resolve_spatial_target_port(
    deployment: DeploymentManifest,
    owner: SubsystemManifest,
    reference: SpatialPortReference,
) -> SpatialPortDeclaration:
    bound = deployment.resolve(owner.name, reference.requirement)
    target = deployment.subsystems[bound.target_subsystem]
    port = target.spatial.ports.get(reference.port)
    if port is None:
        raise ManifestError(
            f"Spatial reference {owner.name}.{reference.requirement}.{reference.port} targets "
            f"{target.name}, which does not declare that port"
        )
    return port
