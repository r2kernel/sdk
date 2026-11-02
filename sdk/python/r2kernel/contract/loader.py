from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from r2kernel.spec_paths import relative_spec_path, resolve_spec_path, read_spec_text, spec_path_exists
from r2kernel.interface import InterfaceSpec, load_interface_spec


class ContractSpecError(ValueError):
    pass


_CAPABILITY_CONTRACT_PREFIX = "capability."


def _capability_contract_error(contract: str, context: str = "contract") -> str:
    """Capability contracts were removed for 1.0; say so instead of reporting a missing spec."""

    return (
        f"{context} {contract!r} is not supported; capability contracts were removed, "
        "declare an api.* or component.* contract"
    )


@dataclass(frozen=True)
class SpatialPortSpec:
    name: str
    direction: str
    semantic: str
    required: bool = True


@dataclass(frozen=True)
class SpatialConformance:
    participation: str = "none"
    ports: dict[str, SpatialPortSpec] = field(default_factory=dict)


@dataclass(frozen=True)
class ComponentSpec:
    id: str
    name: str
    version: int = 1
    description: str = ""
    conformance_apis: tuple[str, ...] = field(default_factory=tuple)
    spatial: SpatialConformance = field(default_factory=SpatialConformance)


def load_api_contract_spec(ref: str | Path | InterfaceSpec) -> InterfaceSpec:
    if isinstance(ref, InterfaceSpec):
        return ref
    if isinstance(ref, Path):
        return load_interface_spec(ref)
    if str(ref).startswith(_CAPABILITY_CONTRACT_PREFIX):
        raise ContractSpecError(_capability_contract_error(str(ref)))
    try:
        relative = relative_spec_path("apis", str(ref))
    except ValueError:
        relative = None
    if relative is not None:
        path = resolve_spec_path(relative)
        if path is not None:
            return load_interface_spec(path)
    if str(ref).startswith("api."):
        try:
            relative = relative_spec_path("apis", str(ref))
        except ValueError as exc:
            raise ContractSpecError(str(exc)) from exc
        path = resolve_spec_path(relative)
        if path is None:
            raise ContractSpecError(f"Spec not found for id: {ref}")
        return load_interface_spec(path)
    return load_interface_spec(ref)


def load_api_contract_specs(refs: Iterable[str | Path | InterfaceSpec]) -> list[InterfaceSpec]:
    return [load_api_contract_spec(ref) for ref in refs]


def load_component_spec(ref: str | Path | ComponentSpec) -> ComponentSpec:
    if isinstance(ref, ComponentSpec):
        return ref
    if isinstance(ref, Path):
        return _load_component_spec_from_path(ref)
    if str(ref).startswith(_CAPABILITY_CONTRACT_PREFIX):
        raise ContractSpecError(_capability_contract_error(str(ref)))
    try:
        relative = relative_spec_path("components", str(ref))
    except ValueError as exc:
        raise ContractSpecError(str(exc)) from exc
    path = resolve_spec_path(relative)
    if path is None:
        raise ContractSpecError(f"Spec not found for id: {ref}")
    return _load_component_spec_from_path(path)


def load_component_specs(refs: Iterable[str | Path | ComponentSpec]) -> list[ComponentSpec]:
    return [load_component_spec(ref) for ref in refs]


def _load_component_spec_from_path(path: Path) -> ComponentSpec:
    if not spec_path_exists(path):
        raise ContractSpecError(f"Component spec file not found: {path}")
    document = yaml.safe_load(read_spec_text(path)) or {}
    if not isinstance(document, dict):
        raise ContractSpecError(f"Component spec must be a mapping: {path}")
    spec_id = str(document.get("id") or "").strip()
    if not spec_id:
        raise ContractSpecError(f"Component spec is missing id: {path}")
    conformance_apis = tuple(str(item).strip() for item in (document.get("conformance_apis") or []) if str(item).strip())
    if not conformance_apis:
        raise ContractSpecError(f"Component spec must define conformance_apis: {path}")
    spatial = _parse_spatial_conformance(document.get("spatial"), spec_id=spec_id, path=path)
    return ComponentSpec(
        id=spec_id,
        name=str(document.get("name") or spec_id).strip(),
        version=int(document.get("version") or 1),
        description=str(document.get("description") or ""),
        conformance_apis=conformance_apis,
        spatial=spatial,
    )


def _parse_spatial_conformance(raw: Any, *, spec_id: str, path: Path) -> SpatialConformance:
    if raw is None:
        raise ContractSpecError(
            f"Component spec {spec_id} must explicitly define spatial.participation: {path}"
        )
    if not isinstance(raw, dict):
        raise ContractSpecError(f"Component spec {spec_id} spatial must be a mapping: {path}")
    participation = str(raw.get("participation") or "").strip()
    if participation not in {"none", "required"}:
        raise ContractSpecError(
            f"Component spec {spec_id} spatial.participation must be 'none' or 'required'"
        )
    raw_ports = raw.get("ports") or {}
    if not isinstance(raw_ports, dict):
        raise ContractSpecError(f"Component spec {spec_id} spatial.ports must be a mapping")
    ports: dict[str, SpatialPortSpec] = {}
    for raw_name, raw_port in raw_ports.items():
        name = str(raw_name or "").strip()
        if not name or not isinstance(raw_port, dict):
            raise ContractSpecError(f"Component spec {spec_id} has an invalid spatial port")
        direction = str(raw_port.get("direction") or "").strip()
        if direction not in {"import", "export"}:
            raise ContractSpecError(
                f"Component spec {spec_id} spatial port {name} direction must be import or export"
            )
        semantic = str(raw_port.get("semantic") or "").strip()
        if not semantic:
            raise ContractSpecError(
                f"Component spec {spec_id} spatial port {name} must define semantic"
            )
        ports[name] = SpatialPortSpec(
            name=name,
            direction=direction,
            semantic=semantic,
            required=bool(raw_port.get("required", True)),
        )
    if participation == "none" and ports:
        raise ContractSpecError(f"Component spec {spec_id} cannot declare ports with spatial.participation none")
    if participation == "required" and not ports:
        raise ContractSpecError(f"Component spec {spec_id} must declare at least one spatial port")
    return SpatialConformance(participation=participation, ports=ports)


def expand_declared_api_ids(
    *,
    declared_apis: Iterable[str | Path | InterfaceSpec] = (),
    declared_components: Iterable[str | Path | ComponentSpec] = (),
) -> tuple[str, ...]:
    expanded: list[str] = []
    seen: set[str] = set()

    def add_api(api_ref: str) -> None:
        if api_ref not in seen:
            seen.add(api_ref)
            expanded.append(api_ref)

    for api_contract in declared_apis:
        if isinstance(api_contract, InterfaceSpec):
            add_api(api_contract.id)
        else:
            add_api(str(api_contract))
    for component in load_component_specs(declared_components):
        for api_ref in component.conformance_apis:
            add_api(api_ref)
    return tuple(expanded)
