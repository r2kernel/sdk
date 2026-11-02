"""Manifest data model: errors, enumerations and declaration/manifest dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


class ManifestError(ValueError):
    pass


SUBSYSTEM_ROLES = ("app", "component", "gateway", "compound")
ROBOT_PLATFORM_ROLES = ("component", "gateway", "compound")
DEPLOYMENT_LAUNCH_MODES = ("managed", "external", "federated")
INTERFACE_VISIBILITIES = ("public", "internal")
SPATIAL_PARTICIPATION = ("none", "required")
SPATIAL_PORT_DIRECTIONS = ("import", "export")


@dataclass(frozen=True)
class InterfaceDeclaration:
    name: str
    contract: str
    visibility: str = "public"
    required: bool = True
    cardinality: str = "one"
    labels: dict[str, str] = field(default_factory=dict)
    endpoints: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ExportDeclaration:
    """Declarative API projection from a compound requirement."""

    name: str
    contract: str
    requirement: str


@dataclass(frozen=True)
class SpatialPortReference:
    requirement: str
    port: str


@dataclass(frozen=True)
class SpatialPortDeclaration:
    name: str
    direction: str
    semantic: str
    frame: str | None = None
    required: bool = True
    source: SpatialPortReference | None = None


@dataclass(frozen=True)
class SpatialMountDeclaration:
    name: str
    parent: SpatialPortReference
    child: SpatialPortReference
    translation: tuple[float, float, float]
    rotation_xyzw: tuple[float, float, float, float]


@dataclass(frozen=True)
class SpatialDeclaration:
    participation: str = "none"
    ports: dict[str, SpatialPortDeclaration] = field(default_factory=dict)
    mounts: tuple[SpatialMountDeclaration, ...] = ()


@dataclass(frozen=True)
class BindingReference:
    subsystem: str
    interface: str

    @classmethod
    def parse(cls, value: str, *, field_name: str) -> "BindingReference":
        text = str(value or "").strip()
        if "." not in text:
            raise ManifestError(f"{field_name} must use '<subsystem>.<interface>': {value!r}")
        subsystem, interface = text.rsplit(".", 1)
        subsystem = subsystem.strip()
        interface = interface.strip()
        if not subsystem or not interface:
            raise ManifestError(f"{field_name} must use '<subsystem>.<interface>': {value!r}")
        return cls(subsystem=subsystem, interface=interface)

    def format(self) -> str:
        return f"{self.subsystem}.{self.interface}"


@dataclass(frozen=True)
class BoundInterface:
    requirement: str
    target_subsystem: str
    provider_interface: str
    contract: str

    @property
    def target(self) -> str:
        return self.target_subsystem


@dataclass(frozen=True)
class SubsystemManifest:
    path: Path
    kind: str
    version: int
    name: str
    role: str
    provides: dict[str, InterfaceDeclaration] = field(default_factory=dict)
    requires: dict[str, InterfaceDeclaration] = field(default_factory=dict)
    exports: dict[str, ExportDeclaration] = field(default_factory=dict)
    spatial: SpatialDeclaration = field(default_factory=SpatialDeclaration)

    @property
    def declared_components(self) -> tuple[str, ...]:
        return tuple(
            _unique(
                decl.contract
                for decl in self.provides.values()
                if decl.visibility == "public" and decl.contract.startswith("component.")
            )
        )

    @property
    def declared_apis(self) -> tuple[str, ...]:
        return tuple(
            _unique(
                decl.contract
                for decl in self.provides.values()
                if decl.visibility == "public" and decl.contract.startswith("api.")
            )
        )


@dataclass(frozen=True)
class DeploymentManifest:
    path: Path
    kind: str
    version: int
    robot: "RobotPlatformManifest | None" = None
    application: "ApplicationManifest | None" = None
    subsystems: dict[str, SubsystemManifest] = field(default_factory=dict)
    bindings: dict[BindingReference, BindingReference] = field(default_factory=dict)
    instances: dict[str, "DeploymentInstance"] = field(default_factory=dict)

    def resolve(
        self,
        consumer: str,
        requirement: str,
        *,
        target_override: BindingReference | None = None,
    ) -> BoundInterface:
        consumer_manifest = self.subsystems.get(consumer)
        if consumer_manifest is None:
            raise ManifestError(f"Deployment does not declare subsystem: {consumer}")
        required = consumer_manifest.requires.get(requirement)
        if required is None:
            raise ManifestError(f"Subsystem {consumer} does not declare required interface: {requirement}")
        source = BindingReference(subsystem=consumer, interface=requirement)
        target = target_override or self.bindings.get(source)
        if target is None:
            if required.required:
                raise ManifestError(f"Required binding is missing: {source.format()}")
            raise ManifestError(f"Optional binding is not configured: {source.format()}")
        return self._resolve_target(source, required, target, export_path=())

    def _resolve_target(
        self,
        source: BindingReference,
        required: InterfaceDeclaration,
        target: BindingReference,
        *,
        export_path: tuple[str, ...],
    ) -> BoundInterface:
        target_ref = target.format()
        if target_ref in export_path:
            chain = " -> ".join((*export_path, target_ref))
            raise ManifestError(f"Export binding cycle: {chain}")
        provider_manifest = self.subsystems.get(target.subsystem)
        if provider_manifest is None:
            raise ManifestError(f"Binding target subsystem is not declared: {target.subsystem}")
        consumer_manifest = self.subsystems.get(source.subsystem)
        if (
            consumer_manifest is not None
            and consumer_manifest.role in {"app", "compound"}
            and provider_manifest.role == "gateway"
        ):
            raise ManifestError(
                f"Subsystem {source.subsystem} with role {consumer_manifest.role!r} cannot bind directly "
                f"to gateway subsystem {target.subsystem}; bind through a component"
            )
        if (
            consumer_manifest is not None
            and consumer_manifest.role == "compound"
            and provider_manifest.role != "component"
        ):
            raise ManifestError(
                f"Compound subsystem {source.subsystem} may bind only to component-provided interfaces; "
                f"{target.format()} is provided by role {provider_manifest.role!r}"
            )
        provided = provider_manifest.provides.get(target.interface)
        exported = provider_manifest.exports.get(target.interface)
        declaration = provided or exported
        if declaration is None:
            raise ManifestError(f"Binding target does not provide or export interface: {target.format()}")
        if (
            consumer_manifest is not None
            and consumer_manifest.role == "app"
            and exported is None
            and provider_manifest.role != "component"
        ):
            raise ManifestError(
                f"Application subsystem {source.subsystem} may bind directly only to component-provided "
                f"interfaces or compound exports; {target.format()} is provided by role "
                f"{provider_manifest.role!r}"
            )
        if required.cardinality != "one":
            raise ManifestError(f"Unsupported binding cardinality for {source.format()}: {required.cardinality}")
        if required.contract != declaration.contract:
            raise ManifestError(
                "Binding contract mismatch: "
                f"{source.format()} requires {required.contract}, "
                f"but {target.format()} provides {declaration.contract}"
            )
        if exported is not None:
            export_source = BindingReference(
                subsystem=provider_manifest.name,
                interface=exported.requirement,
            )
            export_requirement = provider_manifest.requires[exported.requirement]
            inner_target = self.bindings.get(export_source)
            if inner_target is None:
                raise ManifestError(f"Export source binding is missing: {export_source.format()}")
            resolved = self._resolve_target(
                export_source,
                export_requirement,
                inner_target,
                export_path=(*export_path, target_ref),
            )
            physical_manifest = self.subsystems[resolved.target_subsystem]
            physical_interface = physical_manifest.provides.get(resolved.provider_interface)
            if physical_interface is None:
                raise ManifestError(
                    f"Export target must resolve to a provided interface: {resolved.target_subsystem}.{resolved.provider_interface}"
                )
            if physical_manifest.role != "component":
                raise ManifestError(
                    f"Export target {resolved.target_subsystem}.{resolved.provider_interface} "
                    f"must be provided by a component subsystem, got role {physical_manifest.role!r}"
                )
            if physical_interface.visibility != "public":
                raise ManifestError(
                    f"Export target must be public: {resolved.target_subsystem}.{resolved.provider_interface}"
                )
            return BoundInterface(
                requirement=source.interface,
                target_subsystem=resolved.target_subsystem,
                provider_interface=resolved.provider_interface,
                contract=required.contract,
            )
        return BoundInterface(
            requirement=source.interface,
            target_subsystem=target.subsystem,
            provider_interface=target.interface,
            contract=provided.contract,
        )


@dataclass(frozen=True)
class DeploymentInstance:
    name: str
    manifest: SubsystemManifest
    package: Path | None = None
    slots: dict[str, str] = field(default_factory=dict)
    launch: str = "managed"
    host: str | None = None
    params: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RobotPlatformSlot:
    name: str
    role: str
    contract: str | None = None
    part: str | None = None
    required: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RobotPlatformManifest:
    path: Path
    kind: str
    version: int
    name: str
    platforms: tuple[str, ...] = ()
    kinematics: dict[str, Any] = field(default_factory=dict)
    parts: dict[str, dict[str, Any]] = field(default_factory=dict)
    slots: dict[str, RobotPlatformSlot] = field(default_factory=dict)


@dataclass(frozen=True)
class ApplicationManifest:
    path: Path
    kind: str
    version: int
    name: str
    subsystem_manifest: Path | None = None
    platforms: tuple[str, ...] = ()
    required_robot_platforms: tuple[str, ...] = ()
    frontend: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeManifestIssue:
    interface: str
    endpoint_kind: str
    endpoint: str
    message: str


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
