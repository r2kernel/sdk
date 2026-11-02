"""Robot platform parsing and deployment platform/application/control-binding validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

from ._contracts import (
    _is_known_public_contract,
    _reject_capability_contract,
    _strict_contract_control_endpoints,
)
from .model import (
    BindingReference,
    ManifestError,
    ROBOT_PLATFORM_ROLES,
    RobotPlatformSlot,
)

if TYPE_CHECKING:
    from .model import DeploymentInstance, DeploymentManifest


def _validate_control_binding_group_identity(deployment: DeploymentManifest) -> None:
    """Reject bindings whose sender and Control provider interface cannot identify one group."""

    seen_sources: dict[tuple[str, str, str], BindingReference] = {}
    for source in deployment.bindings:
        resolved = deployment.resolve(source.subsystem, source.interface)
        provider = deployment.subsystems[resolved.target_subsystem]
        declaration = provider.provides.get(resolved.provider_interface)
        if declaration is None:
            continue
        group_key = (
            source.subsystem,
            resolved.target_subsystem,
            resolved.provider_interface,
        )
        previous_source = seen_sources.get(group_key)
        if previous_source is not None:
            has_control_endpoints = bool(declaration.endpoints.get("controls")) or bool(
                _strict_contract_control_endpoints(
                    declaration.contract,
                    field_name=f"provides.{resolved.provider_interface}",
                )
            )
            if not has_control_endpoints:
                continue
            raise ManifestError(
                "Ambiguous Control binding group identity: "
                f"{previous_source.format()} and {source.format()} both bind to "
                f"{resolved.target_subsystem}.{resolved.provider_interface}; "
                "a source subsystem may bind only one requirement to a Control provider interface"
            )
        else:
            seen_sources[group_key] = source


def _parse_robot_platform_slots(value: Any, manifest_path: Path) -> dict[str, RobotPlatformSlot]:
    if not isinstance(value, dict):
        raise ManifestError(f"Robot slots must be a mapping: {manifest_path}")
    slots: dict[str, RobotPlatformSlot] = {}
    for key, raw in value.items():
        name = str(key or "").strip()
        if not name:
            raise ManifestError(f"Robot slot key is empty: {manifest_path}")
        if not isinstance(raw, dict):
            raise ManifestError(f"Robot slot {name} must be a mapping: {manifest_path}")
        role = str(raw.get("role") or "").strip()
        if role not in ROBOT_PLATFORM_ROLES:
            raise ManifestError(
                f"Robot slot {name} has unsupported role {role!r}; "
                f"robot slots must be one of {', '.join(ROBOT_PLATFORM_ROLES)}"
            )
        contract = str(raw.get("contract") or "").strip() or None
        if contract:
            _reject_capability_contract(contract, context=f"Robot slot {name} contract")
        if contract and not _is_known_public_contract(contract):
            raise ManifestError(
                f"Robot slot {name} uses unknown public contract {contract!r}; "
                "robot slots must reference R2Kernel spec contracts "
                "(component.* or api.*). Keep private/tutorial wiring inside subsystem bindings."
            )
        metadata = dict(raw)
        for owned in ("role", "contract", "part", "required"):
            metadata.pop(owned, None)
        slots[name] = RobotPlatformSlot(
            name=name,
            role=role,
            contract=contract,
            part=str(raw.get("part") or "").strip() or None,
            required=bool(raw.get("required", True)),
            metadata=metadata,
        )
    return slots


def _validate_deployment_platform_application(deployment: DeploymentManifest) -> None:
    if deployment.application is not None and deployment.robot is None:
        raise ManifestError(
            f"Deployment {deployment.path} declares an application but no robot manifest"
        )
    if deployment.application is not None:
        application_path = deployment.application.subsystem_manifest
        if application_path is not None and not any(
            subsystem.path == application_path and subsystem.role == "app"
            for subsystem in deployment.subsystems.values()
        ):
            raise ManifestError(
                f"Deployment {deployment.path} declares application {deployment.application.name!r} "
                f"but does not include its application subsystem manifest: {application_path}"
            )
    if deployment.robot is not None:
        assigned_slots: set[str] = set()
        for instance in deployment.instances.values():
            if not instance.slots:
                if instance.manifest.role not in {"app", "compound"} and instance.launch == "managed":
                    raise ManifestError(
                        f"Deployment instance {instance.name} is a managed robot subsystem "
                        "but is not assigned to any robot slot"
                    )
                continue
            if instance.manifest.role == "app":
                raise ManifestError(
                    f"Deployment instance {instance.name} is an app and cannot fill robot slots"
                )
            for slot_name, interface_name in instance.slots.items():
                if slot_name in assigned_slots:
                    raise ManifestError(f"Robot slot {slot_name!r} is assigned more than once")
                assigned_slots.add(slot_name)
                _validate_deployment_instance_slot_assignment(
                    deployment,
                    instance,
                    slot_name,
                    interface_name,
                )
        missing_slots = [
            slot.name
            for slot in deployment.robot.slots.values()
            if slot.required and slot.name not in assigned_slots
        ]
        if missing_slots:
            raise ManifestError(
                "Deployment is missing required robot slot instance(s): "
                + ", ".join(sorted(missing_slots))
            )
    else:
        slot_instances = [instance.name for instance in deployment.instances.values() if instance.slots]
        if slot_instances:
            raise ManifestError(
                "Deployment instances with `slots` require a robot manifest: "
                + ", ".join(sorted(slot_instances))
            )
    if deployment.robot is not None and deployment.application is not None:
        missing_platforms = [
            platform
            for platform in deployment.application.required_robot_platforms
            if platform not in deployment.robot.platforms
        ]
        if missing_platforms:
            raise ManifestError(
                f"Application {deployment.application.name} requires robot profile(s) "
                + ", ".join(missing_platforms)
                + f", but robot {deployment.robot.name} does not provide them"
            )


def _validate_deployment_instance_slot_assignment(
    deployment: DeploymentManifest,
    instance: DeploymentInstance,
    slot_name: str,
    interface_name: str,
) -> None:
    if deployment.robot is None:
        return
    slot = deployment.robot.slots.get(slot_name)
    if slot is None:
        raise ManifestError(
            f"Deployment instance {instance.name} references unknown robot slot {slot_name!r}"
        )
    if instance.manifest.role != slot.role:
        raise ManifestError(
            f"Deployment instance {instance.name} role {instance.manifest.role!r} "
            f"does not match robot slot {slot_name} role {slot.role!r}"
        )
    provided = instance.manifest.provides.get(interface_name)
    if provided is None:
        raise ManifestError(
            f"Deployment instance {instance.name} slot {slot_name!r} references missing "
            f"provided interface {interface_name!r}"
        )
    if provided.visibility != "public":
        raise ManifestError(
            f"Deployment instance {instance.name} slot {slot_name!r} uses interface "
            f"{interface_name!r} with visibility {provided.visibility}; robot slots require public interfaces"
        )
    if slot.contract and slot.contract != provided.contract:
        raise ManifestError(
            f"Deployment instance {instance.name} slot {slot_name!r} requires contract {slot.contract}, "
            f"but interface {interface_name!r} provides {provided.contract}"
        )
