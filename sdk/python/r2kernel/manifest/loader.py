from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from r2kernel.contract.loader import ContractSpecError, load_component_spec
from r2kernel.spec_paths import package_spec_sources
from .model import (
    ManifestError,
    SUBSYSTEM_ROLES,
    DEPLOYMENT_LAUNCH_MODES,
    INTERFACE_VISIBILITIES,
    InterfaceDeclaration,
    ExportDeclaration,
    BindingReference,
    BoundInterface,
    SubsystemManifest,
    DeploymentManifest,
    DeploymentInstance,
    RobotPlatformManifest,
    ApplicationManifest,
    RuntimeManifestIssue,
    _unique,
)
from ._fields import _load_yaml_mapping, _string_mapping, _mapping, _nested_mapping, _string_tuple
from ._spatial import (
    _spatial_declaration_status,
    _parse_spatial_declaration,
    _validate_subsystem_spatial_conformance,
    _validate_deployment_spatial_topology,
)
from ._contracts import (
    _validate_role_endpoint_policy,
    _endpoint_mapping,
    _runtime_names,
    _reject_capability_contract,
    _is_known_public_contract,
    _interface_endpoint_names,
)
from ._platform import (
    _validate_control_binding_group_identity,
    _parse_robot_platform_slots,
    _validate_deployment_platform_application,
)


def load_subsystem_manifest(path: str | Path, *, package_root: Path | None = None) -> SubsystemManifest:
    manifest_path = Path(path).expanduser().resolve()
    if package_root is None:
        package_root = next((parent for parent in manifest_path.parents
                             if (parent / "package.yaml").is_file()), None)
    if package_root is None:
        return _load_subsystem_manifest(manifest_path)
    package_root = package_root.resolve()
    if not manifest_path.is_relative_to(package_root):
        raise ManifestError(f"Subsystem manifest must remain inside package directory: {manifest_path}")
    roots = _package_contract_spec_roots(package_root)
    if not roots:
        return _load_subsystem_manifest(manifest_path)
    with package_spec_sources(roots):
        return _load_subsystem_manifest(manifest_path)


def _load_subsystem_manifest(manifest_path: Path) -> SubsystemManifest:
    document = _load_yaml_mapping(manifest_path, "subsystem manifest")
    kind = str(document.get("kind") or "").strip()
    if kind != "r2kernel.subsystem":
        raise ManifestError(f"Subsystem manifest kind must be 'r2kernel.subsystem': {manifest_path}")
    version = int(document.get("version") or 1)
    subsystem = document.get("subsystem")
    if not isinstance(subsystem, dict):
        raise ManifestError(f"Subsystem manifest must define subsystem mapping: {manifest_path}")
    name = str(subsystem.get("name") or "").strip()
    if not name:
        raise ManifestError(f"Subsystem manifest is missing subsystem.name: {manifest_path}")
    role = str(subsystem.get("role") or "").strip()
    if not role:
        raise ManifestError(f"Subsystem manifest is missing subsystem.role: {manifest_path}")
    if role not in SUBSYSTEM_ROLES:
        raise ManifestError(
            f"Subsystem {name} has unsupported role {role!r}; "
            f"expected one of {', '.join(SUBSYSTEM_ROLES)}"
        )
    requires = _parse_interface_declarations(document.get("requires"), section="requires", default_required=True)
    provides = _parse_interface_declarations(document.get("provides"), section="provides", default_required=True)
    exports = _parse_export_declarations(
        document.get("exports"),
        role=role,
        requires=requires,
        manifest_path=manifest_path,
    )
    overlap = sorted(set(provides).intersection(exports))
    if overlap:
        raise ManifestError(
            f"Subsystem {name} declares the same interface in provides and exports: {', '.join(overlap)}"
        )
    manifest = SubsystemManifest(
        path=manifest_path,
        kind=kind,
        version=version,
        name=name,
        role=role,
        provides=provides,
        requires=requires,
        exports=exports,
        spatial=_parse_spatial_declaration(document.get("spatial"), manifest_path=manifest_path),
    )
    _validate_role_endpoint_policy(manifest)
    _validate_subsystem_spatial_conformance(manifest)
    return manifest


def load_robot_platform_manifest(path: str | Path) -> RobotPlatformManifest:
    manifest_path = Path(path).expanduser().resolve()
    document = _load_yaml_mapping(manifest_path, "robot manifest")
    kind = str(document.get("kind") or "").strip()
    if kind != "r2kernel.robot":
        raise ManifestError(f"Robot manifest kind must be 'r2kernel.robot': {manifest_path}")
    version = int(document.get("version") or 1)
    robot = document.get("robot")
    if not isinstance(robot, dict):
        raise ManifestError(f"Robot manifest must define robot mapping: {manifest_path}")
    name = str(robot.get("name") or "").strip()
    if not name:
        raise ManifestError(f"Robot manifest is missing robot.name: {manifest_path}")
    if "capabilities" in document or "capabilities" in robot:
        raise ManifestError(
            "robot manifest field `capabilities` is not supported; declare api.* requirements "
            f"on subsystems and bind them in the deployment: {manifest_path}"
        )
    slots = _parse_robot_platform_slots(document.get("slots") or {}, manifest_path)
    return RobotPlatformManifest(
        path=manifest_path,
        kind=kind,
        version=version,
        name=name,
        platforms=_string_tuple(robot.get("profiles") or robot.get("platforms") or document.get("profiles") or document.get("platforms") or ()),
        kinematics=_mapping(document.get("kinematics") or {}, field_name="kinematics"),
        parts=_nested_mapping(document.get("parts") or {}, field_name="parts"),
        slots=slots,
    )


def load_application_manifest(path: str | Path) -> ApplicationManifest:
    manifest_path = Path(path).expanduser().resolve()
    document = _load_yaml_mapping(manifest_path, "application manifest")
    kind = str(document.get("kind") or "").strip()
    version = int(document.get("version") or 1)
    if kind == "r2kernel.subsystem":
        subsystem_manifest = load_subsystem_manifest(manifest_path)
        if subsystem_manifest.role != "app":
            raise ManifestError(
                f"Application subsystem manifest {manifest_path} has role {subsystem_manifest.role!r}; "
                "expected 'app'"
            )
        application = document.get("application") or {}
        if not isinstance(application, dict):
            raise ManifestError(f"Application subsystem application section must be a mapping: {manifest_path}")
        robot_requires = application.get("requires_robot") or {}
        if not isinstance(robot_requires, dict):
            raise ManifestError(f"Application subsystem application.requires_robot must be a mapping: {manifest_path}")
        if "capabilities" in robot_requires:
            raise ManifestError(
                "application robot requirement field `capabilities` is not supported; declare api.* "
                f"requirements and bind them in the deployment: {manifest_path}"
            )
        return ApplicationManifest(
            path=manifest_path,
            kind=kind,
            version=version,
            name=subsystem_manifest.name,
            subsystem_manifest=manifest_path,
            platforms=_string_tuple(application.get("profiles") or application.get("platforms") or ()),
            required_robot_platforms=_string_tuple(robot_requires.get("profiles") or robot_requires.get("platforms") or ()),
            frontend=_mapping(application.get("frontend") or {}, field_name="application.frontend"),
        )
    raise ManifestError(f"Application manifest kind must be an app-role 'r2kernel.subsystem': {manifest_path}")


def load_deployment_manifest(path: str | Path) -> DeploymentManifest:
    manifest_path = Path(path).expanduser().resolve()
    document = _load_yaml_mapping(manifest_path, "deployment manifest")
    kind = str(document.get("kind") or "").strip()
    if kind != "r2kernel.deployment":
        raise ManifestError(f"Deployment manifest kind must be 'r2kernel.deployment': {manifest_path}")
    version = int(document.get("version") or 1)
    robot = _load_optional_robot_manifest(manifest_path, document.get("robot"))
    application = _load_optional_application_manifest(manifest_path, document.get("application"))
    subsystems_doc = document.get("subsystems") or {}
    if not isinstance(subsystems_doc, dict):
        raise ManifestError(f"Deployment manifest subsystems must be a mapping: {manifest_path}")
    subsystems: dict[str, SubsystemManifest] = {}
    for subsystem_name, entry in subsystems_doc.items():
        manifest_ref = _subsystem_manifest_ref(entry)
        subsystem_manifest = load_subsystem_manifest((manifest_path.parent / manifest_ref).resolve())
        declared_name = str(subsystem_name or "").strip()
        if not declared_name:
            raise ManifestError(f"Deployment subsystem key is empty: {manifest_path}")
        if subsystem_manifest.name != declared_name:
            raise ManifestError(
                f"Deployment subsystem key {declared_name!r} does not match "
                f"manifest subsystem.name {subsystem_manifest.name!r}: {subsystem_manifest.path}"
            )
        subsystems[declared_name] = subsystem_manifest
    instances_doc = document.get("instances") or {}
    if not isinstance(instances_doc, dict):
        raise ManifestError(f"Deployment manifest instances must be a mapping: {manifest_path}")
    instances: dict[str, DeploymentInstance] = {}
    for instance_name, entry in instances_doc.items():
        declared_name = str(instance_name or "").strip()
        if not declared_name:
            raise ManifestError(f"Deployment instance key is empty: {manifest_path}")
        if declared_name in subsystems:
            raise ManifestError(f"Deployment instance duplicates subsystem name: {declared_name}")
        instance = _parse_deployment_instance(manifest_path, declared_name, entry)
        instances[declared_name] = instance
        subsystems[declared_name] = replace(instance.manifest, name=declared_name)
    bindings = _parse_bindings(document.get("bindings") or {})
    manifest = DeploymentManifest(
        path=manifest_path,
        kind=kind,
        version=version,
        robot=robot,
        application=application,
        subsystems=subsystems,
        bindings=bindings,
        instances=instances,
    )
    _validate_deployment_platform_application(manifest)
    for source in bindings:
        manifest.resolve(source.subsystem, source.interface)
    _validate_control_binding_group_identity(manifest)
    for subsystem in subsystems.values():
        validate_required_bindings_for_subsystem(subsystem, manifest)
    _validate_deployment_spatial_topology(manifest)
    return manifest


def validate_required_bindings_for_subsystem(subsystem: SubsystemManifest, deployment: DeploymentManifest) -> tuple[BoundInterface, ...]:
    resolved: list[BoundInterface] = []
    for name, requirement in subsystem.requires.items():
        if not requirement.required:
            continue
        resolved.append(deployment.resolve(subsystem.name, name))
    return tuple(resolved)


def subsystem_manifest_status_payload(
    subsystem: SubsystemManifest,
    deployment: DeploymentManifest | None = None,
    launch_bindings: dict[str, BindingReference] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "manifest": {
            "kind": subsystem.kind,
            "version": subsystem.version,
            "path": str(subsystem.path),
            "name": subsystem.name,
            "role": subsystem.role,
        },
        "interfaces": {
            "provides": {
                name: _interface_declaration_status(declaration)
                for name, declaration in subsystem.provides.items()
            },
            "requires": {
                name: _interface_declaration_status(declaration)
                for name, declaration in subsystem.requires.items()
            },
            "exports": {
                name: {
                    "contract": declaration.contract,
                    "source": {"requirement": declaration.requirement},
                    "declarative": True,
                }
                for name, declaration in subsystem.exports.items()
            },
        },
        "spatial": _spatial_declaration_status(subsystem.spatial),
    }
    launch_bindings = launch_bindings or {}

    bindings: list[dict[str, str]] = []
    binding_errors: list[str] = []
    for requirement, declaration in subsystem.requires.items():
        direct = launch_bindings.get(requirement)
        if direct is not None:
            resolved = BoundInterface(
                requirement=requirement,
                target_subsystem=direct.subsystem,
                provider_interface=direct.interface,
                contract=declaration.contract,
            )
        elif deployment is not None:
            try:
                resolved = deployment.resolve(subsystem.name, requirement)
            except ManifestError as exc:
                if declaration.required:
                    binding_errors.append(str(exc))
                continue
        else:
            if declaration.required:
                binding_errors.append(f"Required binding is missing: {subsystem.name}.{requirement}")
            continue
        bindings.append(
            {
                "requirement": resolved.requirement,
                "source": f"{subsystem.name}.{resolved.requirement}",
                "target": f"{resolved.target_subsystem}.{resolved.provider_interface}",
                "target_subsystem": resolved.target_subsystem,
                "provider_interface": resolved.provider_interface,
                "contract": resolved.contract,
            }
        )
    payload["bindings"] = bindings
    if binding_errors:
        payload["binding_errors"] = binding_errors
    return payload


def validate_runtime_endpoints(subsystem: SubsystemManifest, runtime: Any) -> tuple[RuntimeManifestIssue, ...]:
    issues: list[RuntimeManifestIssue] = []
    endpoint_sets = {
        "procedures": _runtime_names(runtime, "get_procedure_info"),
        "operations": _runtime_names(runtime, "get_operation_info"),
        "controls": _runtime_names(runtime, "get_control_info"),
        "publishes": _runtime_names(runtime, "get_publish_info"),
        "subscribes": _runtime_names(runtime, "get_subscribe_info"),
    }
    for interface_name, declaration in subsystem.provides.items():
        for endpoint_kind in declaration.endpoints:
            if endpoint_kind not in endpoint_sets:
                issues.append(
                    RuntimeManifestIssue(
                        interface=interface_name,
                        endpoint_kind=endpoint_kind,
                        endpoint="",
                        message=f"Unknown endpoint kind in manifest: {endpoint_kind}",
                    )
                )
                continue
        for endpoint_kind in endpoint_sets:
            endpoints = _interface_endpoint_names(declaration, endpoint_kind)
            registered = endpoint_sets[endpoint_kind]
            for endpoint in endpoints:
                if endpoint not in registered:
                    issues.append(
                        RuntimeManifestIssue(
                            interface=interface_name,
                            endpoint_kind=endpoint_kind,
                            endpoint=endpoint,
                            message=f"Manifest endpoint is not registered at runtime: {interface_name}.{endpoint_kind}.{endpoint}",
                        )
                    )
    allowed = allowed_runtime_endpoints(subsystem)
    for endpoint_kind, registered in endpoint_sets.items():
        allowed_endpoints = set(allowed.get(endpoint_kind, ()))
        for endpoint in registered:
            if endpoint in allowed_endpoints:
                continue
            issues.append(
                RuntimeManifestIssue(
                    interface="",
                    endpoint_kind=endpoint_kind,
                    endpoint=endpoint,
                    message=(
                        f"Runtime endpoint is not declared by subsystem manifest or R2Kernel spec contract: "
                        f"{endpoint_kind}.{endpoint}"
                    ),
                )
            )
    return tuple(issues)


def allowed_runtime_endpoints(subsystem: SubsystemManifest) -> dict[str, tuple[str, ...]]:
    """Return runtime endpoints this subsystem may register locally.

    Public endpoint sets come from R2Kernel spec contracts. Internal endpoint sets
    must be explicitly declared in the subsystem manifest. Required Event
    surfaces are inverted because the local consumer subscribes to provider
    publishes and publishes to provider subscribes.
    """

    allowed: dict[str, list[str]] = {
        "procedures": [],
        "operations": [],
        "controls": [],
        "publishes": [],
        "subscribes": [],
    }

    for declaration in subsystem.provides.values():
        for endpoint_kind in allowed:
            allowed[endpoint_kind].extend(_interface_endpoint_names(declaration, endpoint_kind))

    for declaration in subsystem.requires.values():
        endpoints = {kind: _interface_endpoint_names(declaration, kind) for kind in allowed}
        allowed["subscribes"].extend(endpoints["publishes"])
        allowed["publishes"].extend(endpoints["subscribes"])

    return {kind: tuple(_unique(values)) for kind, values in allowed.items()}


def _interface_declaration_status(declaration: InterfaceDeclaration) -> dict[str, Any]:
    payload = {
        "contract": declaration.contract,
        "visibility": declaration.visibility,
        "required": declaration.required,
        "cardinality": declaration.cardinality,
        "labels": dict(declaration.labels),
        "endpoints": {
            name: list(endpoints)
            for name in ("controls", "procedures", "operations", "publishes", "subscribes")
            if (endpoints := _interface_endpoint_names(declaration, name))
        },
    }
    return payload


def _parse_interface_declarations(value: Any, *, section: str, default_required: bool) -> dict[str, InterfaceDeclaration]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ManifestError(f"{section} must be a mapping")
    declarations: dict[str, InterfaceDeclaration] = {}
    for name, raw in value.items():
        interface_name = str(name or "").strip()
        if not interface_name:
            raise ManifestError(f"{section} contains an empty interface name")
        declarations[interface_name] = _parse_interface_declaration(
            interface_name,
            raw,
            section=section,
            default_required=default_required,
        )
    return declarations


def _parse_interface_declaration(name: str, raw: Any, *, section: str, default_required: bool) -> InterfaceDeclaration:
    if isinstance(raw, str):
        contract = raw.strip()
        raw_mapping: dict[str, Any] = {}
    elif isinstance(raw, dict):
        raw_mapping = raw
        if "capability" in raw:
            raise ManifestError(
                f"{section}.{name} field `capability` is not supported; capability contracts were removed, "
                "declare an api.* or component.* contract"
            )
        contract = str(raw.get("contract") or "").strip()
    else:
        raise ManifestError(f"{section}.{name} must be a contract string or mapping")
    if not contract:
        raise ManifestError(f"{section}.{name} is missing contract")
    _reject_capability_contract(contract, context=f"{section}.{name} contract")
    visibility = str(raw_mapping.get("visibility") or "public").strip() or "public"
    if visibility not in INTERFACE_VISIBILITIES:
        raise ManifestError(
            f"{section}.{name} has unsupported visibility {visibility!r}; "
            f"expected one of {', '.join(INTERFACE_VISIBILITIES)}"
        )
    if visibility == "public" and not _is_known_public_contract(contract):
        raise ManifestError(
            f"{section}.{name} uses unknown public contract {contract!r}; "
            "public subsystem interfaces must reference a R2Kernel spec contract "
            "(component.* or api.*). Use visibility: internal for private/tutorial wiring."
        )
    labels = _string_mapping(raw_mapping.get("labels") or {}, field_name=f"{section}.{name}.labels")
    endpoints = _endpoint_mapping(raw_mapping.get("endpoints") or {}, field_name=f"{section}.{name}.endpoints")
    if visibility == "public" and endpoints:
        raise ManifestError(
            f"{section}.{name} declares endpoints on a public interface; "
            "public interface endpoints are defined by the referenced R2Kernel spec contract"
        )
    return InterfaceDeclaration(
        name=name,
        contract=contract,
        visibility=visibility,
        required=bool(raw_mapping.get("required", default_required)),
        cardinality=str(raw_mapping.get("cardinality") or "one").strip(),
        labels=labels,
        endpoints=endpoints,
    )


def _parse_export_declarations(
    value: Any,
    *,
    role: str,
    requires: dict[str, InterfaceDeclaration],
    manifest_path: Path,
) -> dict[str, ExportDeclaration]:
    if value is None:
        return {}
    if role != "compound":
        raise ManifestError(f"Only compound subsystems may declare exports: {manifest_path}")
    if not isinstance(value, dict):
        raise ManifestError(f"exports must be a mapping: {manifest_path}")
    exports: dict[str, ExportDeclaration] = {}
    for raw_name, raw in value.items():
        name = str(raw_name or "").strip()
        if not name or not isinstance(raw, dict):
            raise ManifestError(f"exports contains an invalid declaration: {manifest_path}")
        contract = str(raw.get("contract") or "").strip()
        _reject_capability_contract(contract, context=f"exports.{name} contract")
        if not contract.startswith("api.") or not _is_known_public_contract(contract):
            raise ManifestError(
                f"exports.{name}.contract must reference a known api.* contract: {contract!r}"
            )
        source = raw.get("from") or raw.get("source")
        if not isinstance(source, dict):
            raise ManifestError(f"exports.{name}.from must be a mapping")
        requirement = str(source.get("requirement") or "").strip()
        required = requires.get(requirement)
        if required is None:
            raise ManifestError(
                f"exports.{name}.from.requirement references unknown requirement {requirement!r}"
            )
        if required.visibility != "public" or not required.contract.startswith("component."):
            raise ManifestError(
                f"exports.{name} must project a public component.* requirement, got {required.contract}"
            )
        try:
            component = load_component_spec(required.contract)
        except ContractSpecError as exc:
            raise ManifestError(str(exc)) from exc
        if contract not in component.conformance_apis:
            raise ManifestError(
                f"exports.{name} selects {contract}, but {required.contract} does not guarantee it in conformance_apis"
            )
        exports[name] = ExportDeclaration(
            name=name,
            contract=contract,
            requirement=requirement,
        )
    return exports


def _parse_bindings(value: Any) -> dict[BindingReference, BindingReference]:
    if not isinstance(value, dict):
        raise ManifestError("Deployment bindings must be a mapping")
    bindings: dict[BindingReference, BindingReference] = {}
    for source_text, raw in value.items():
        source = BindingReference.parse(str(source_text), field_name="binding key")
        if isinstance(raw, str):
            target_text = raw
        elif isinstance(raw, dict):
            if "select" in raw:
                raise ManifestError(f"Selector bindings are not supported yet: {source.format()}")
            target_text = str(raw.get("to") or "")
        else:
            raise ManifestError(f"Binding value must be a string or mapping: {source.format()}")
        target = BindingReference.parse(target_text, field_name=f"binding {source.format()}.to")
        bindings[source] = target
    return bindings


def _subsystem_manifest_ref(value: Any) -> Path:
    if isinstance(value, str):
        return Path(value)
    if isinstance(value, dict):
        manifest = str(value.get("manifest") or "").strip()
        if manifest:
            return Path(manifest)
    raise ManifestError("Deployment subsystem entry must be a manifest path or mapping with manifest")


def _parse_deployment_instance(manifest_path: Path, name: str, value: Any) -> DeploymentInstance:
    if not isinstance(value, dict):
        raise ManifestError(f"Deployment instance {name} must be a mapping")
    package_path: Path | None = None
    package_ref = str(value.get("package") or "").strip()
    if package_ref:
        package_path = (manifest_path.parent / package_ref).resolve()
    manifest_ref = str(value.get("manifest") or "").strip()
    if manifest_ref:
        subsystem_manifest_path = (manifest_path.parent / manifest_ref).resolve()
    elif package_path is not None:
        subsystem_manifest_path = _package_subsystem_manifest_ref(package_path)
    else:
        raise ManifestError(f"Deployment instance {name} must define manifest or package")
    if "slot" in value:
        raise ManifestError(f"Deployment instance {name} uses deprecated `slot`; use `slots` mapping")
    slots = _string_mapping(value.get("slots") or {}, field_name=f"instances.{name}.slots")
    for slot_name, interface_name in slots.items():
        if not interface_name:
            raise ManifestError(f"instances.{name}.slots.{slot_name} must name a provided interface")
    launch = str(value.get("launch") or "managed").strip() or "managed"
    if launch not in DEPLOYMENT_LAUNCH_MODES:
        raise ManifestError(
            f"Deployment instance {name} has unsupported launch mode {launch!r}; "
            f"expected one of {', '.join(DEPLOYMENT_LAUNCH_MODES)}"
        )
    host = str(value.get("host") or "").strip() or None
    params = _string_mapping(value.get("params") or {}, field_name=f"instances.{name}.params")
    return DeploymentInstance(
        name=name,
        manifest=load_subsystem_manifest(subsystem_manifest_path, package_root=package_path),
        package=package_path,
        slots=slots,
        launch=launch,
        host=host,
        params=params,
    )


def _package_subsystem_manifest_ref(package_path: Path) -> Path:
    package_manifest_path = package_path / "package.yaml"
    document = _load_yaml_mapping(package_manifest_path, "package manifest")
    subsystem_manifest = str(document.get("subsystem_manifest") or "subsystem.yaml").strip()
    if not subsystem_manifest:
        subsystem_manifest = "subsystem.yaml"
    return _package_relative_path(package_path, subsystem_manifest, field_name="subsystem_manifest", directory=False)


def _package_relative_path(package_root: Path, raw: str, *, field_name: str, directory: bool) -> Path:
    relative = Path(raw)
    if not raw.strip() or relative.is_absolute() or ".." in relative.parts:
        raise ManifestError(f"{field_name} must be a nonempty package-relative path without '..': {raw!r}")
    resolved = (package_root / relative).resolve()
    if not resolved.is_relative_to(package_root.resolve()):
        raise ManifestError(f"{field_name} must remain inside package directory: {raw!r}")
    if not (resolved.is_dir() if directory else resolved.is_file()):
        expected = "directory" if directory else "file"
        raise ManifestError(f"{field_name} must name an existing {expected}: {raw!r}")
    return resolved


def _package_contract_spec_roots(package_root: Path) -> tuple[Path, ...]:
    package = _load_yaml_mapping(package_root / "package.yaml", "package manifest")
    contracts = package.get("contracts", {})
    if not isinstance(contracts, dict):
        raise ManifestError("Package contracts must be a mapping")
    roots = contracts.get("spec_roots", [])
    if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
        raise ManifestError("Package contracts.spec_roots must be a list of strings")
    return tuple(dict.fromkeys(_package_relative_path(
        package_root, root, field_name="contracts.spec_roots", directory=True) for root in roots))


def _load_optional_robot_manifest(manifest_path: Path, value: Any) -> RobotPlatformManifest | None:
    ref = _optional_manifest_ref(value, field_name="deployment.robot")
    if ref is None:
        return None
    return load_robot_platform_manifest((manifest_path.parent / ref).resolve())


def _load_optional_application_manifest(manifest_path: Path, value: Any) -> ApplicationManifest | None:
    ref = _optional_manifest_ref(value, field_name="deployment.application")
    if ref is None:
        return None
    return load_application_manifest((manifest_path.parent / ref).resolve())


def _optional_manifest_ref(value: Any, *, field_name: str) -> Path | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return Path(text) if text else None
    if isinstance(value, dict):
        text = str(value.get("manifest") or "").strip()
        return Path(text) if text else None
    raise ManifestError(f"{field_name} must be a path string or mapping with manifest")
