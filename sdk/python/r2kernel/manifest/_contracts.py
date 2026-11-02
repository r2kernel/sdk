"""Contract inspection: declared contract resolution, endpoint inventories and role endpoint policy."""

from __future__ import annotations

from typing import Any, Iterable, TYPE_CHECKING

from r2kernel.contract.loader import (
    ContractSpecError,
    _CAPABILITY_CONTRACT_PREFIX,
    _capability_contract_error,
    load_component_spec,
    load_api_contract_spec,
)
from .model import ManifestError, _unique

if TYPE_CHECKING:
    from .model import InterfaceDeclaration, SubsystemManifest


def _validate_role_endpoint_policy(manifest: SubsystemManifest) -> None:
    if manifest.role == "compound" and manifest.provides:
        raise ManifestError(
            f"Compound subsystem {manifest.name} is declarative and cannot declare provides interfaces"
        )

    if manifest.role == "gateway":
        for section, interfaces in (("provides", manifest.provides), ("requires", manifest.requires)):
            for interface_name, declaration in interfaces.items():
                field_name = f"{section}.{interface_name}"
                non_control = tuple(
                    endpoint_kind
                    for endpoint_kind, endpoints in declaration.endpoints.items()
                    if endpoint_kind != "controls" and endpoints
                )
                if non_control:
                    raise ManifestError(
                        f"Gateway may use only Control endpoints: {field_name} declares "
                        f"{', '.join(non_control)}"
                    )
                if declaration.visibility == "internal" and not declaration.contract.startswith(
                    ("api.", "component.")
                ):
                    controls = declaration.endpoints.get("controls", ())
                    if not controls:
                        raise ManifestError(
                            f"Gateway private contract {declaration.contract!r} in {field_name} "
                            "requires explicit internal Control endpoints"
                        )
                    continue

                endpoint_inventory = _gateway_contract_endpoint_inventory(
                    declaration.contract,
                    field_name=field_name,
                )
                if not endpoint_inventory["controls"]:
                    raise ManifestError(
                        f"Gateway public contract {declaration.contract!r} in {field_name} "
                        "must declare at least one Control endpoint"
                    )
                non_control = tuple(
                    endpoint_name
                    for endpoint_kind in ("events", "procedures", "operations")
                    for endpoint_name in endpoint_inventory[endpoint_kind]
                )
                if non_control:
                    raise ManifestError(
                        f"Gateway may use only Control endpoints: {field_name} "
                        f"({declaration.contract}) also declares {', '.join(non_control)}"
                    )

    if manifest.role == "compound":
        for interface_name, declaration in manifest.requires.items():
            if not declaration.contract.startswith("component."):
                raise ManifestError(
                    f"Compound requirement {interface_name!r} must reference a component.* contract; "
                    f"got {declaration.contract!r}"
                )

    if manifest.role not in {"app", "compound"}:
        return

    declarations = (
        ("provides", manifest.provides),
        ("requires", manifest.requires),
    )
    for section, interfaces in declarations:
        for interface_name, declaration in interfaces.items():
            explicit_controls = declaration.endpoints.get("controls", ())
            if explicit_controls:
                raise ManifestError(
                    f"Subsystem {manifest.name} with role {manifest.role!r} cannot declare or consume "
                    f"internal Control endpoints in {section}.{interface_name}"
                )

            controls = _strict_contract_control_endpoints(
                declaration.contract,
                field_name=f"{section}.{interface_name}",
            )
            if not controls:
                continue
            if (
                manifest.role == "compound"
                and section == "requires"
                and declaration.contract.startswith("component.")
            ):
                continue
            raise ManifestError(
                f"Subsystem {manifest.name} with role {manifest.role!r} cannot declare or consume "
                f"Control endpoints through {section}.{interface_name} ({declaration.contract}): "
                f"{', '.join(controls)}"
            )

    if manifest.role == "compound":
        for export_name, export in manifest.exports.items():
            controls = _strict_contract_control_endpoints(
                export.contract,
                field_name=f"exports.{export_name}",
            )
            if controls:
                raise ManifestError(
                    f"Compound subsystem {manifest.name} cannot export Control endpoints through "
                    f"exports.{export_name} ({export.contract}): {', '.join(controls)}"
                )


def _strict_contract_control_endpoints(contract: str, *, field_name: str) -> tuple[str, ...]:
    """Inspect role-policy contracts without hiding missing or malformed specs."""

    try:
        if contract.startswith("api."):
            api_ids = (contract,)
        elif contract.startswith("component."):
            component = load_component_spec(contract)
            if component.id != contract:
                raise ContractSpecError(
                    f"Component spec id {component.id!r} does not match referenced id {contract!r}"
                )
            api_ids = component.conformance_apis
        else:
            return ()

        api_contracts = []
        for api_id in api_ids:
            api_contract = load_api_contract_spec(api_id)
            if api_contract.id != api_id:
                raise ContractSpecError(
                    f"API spec id {api_contract.id!r} does not match referenced id {api_id!r}"
                )
            api_contracts.append(api_contract)
    except Exception as exc:
        raise ManifestError(
            f"Cannot inspect contract {contract!r} for {field_name}: {exc}"
        ) from exc

    return tuple(
        _unique(
            control.topic
            for api_contract in api_contracts
            for control in api_contract.controls
        )
    )


def _gateway_contract_endpoint_inventory(contract: str, *, field_name: str) -> dict[str, tuple[str, ...]]:
    """Expand a public contract into the endpoint kinds exposed to a Gateway."""

    try:
        if contract.startswith("api."):
            api_ids = (contract,)
        elif contract.startswith("component."):
            component = load_component_spec(contract)
            if component.id != contract:
                raise ContractSpecError(
                    f"Component spec id {component.id!r} does not match referenced id {contract!r}"
                )
            api_ids = component.conformance_apis
        else:
            raise ContractSpecError(f"Unknown public contract kind: {contract!r}")

        if not api_ids:
            raise ContractSpecError(f"Public contract {contract!r} does not expose any API contracts")
        api_contracts = []
        for api_id in api_ids:
            api_contract = load_api_contract_spec(api_id)
            if api_contract.id != api_id:
                raise ContractSpecError(
                    f"API spec id {api_contract.id!r} does not match referenced id {api_id!r}"
                )
            api_contracts.append(api_contract)
    except Exception as exc:
        raise ManifestError(
            f"Cannot inspect contract {contract!r} for Gateway {field_name}: {exc}"
        ) from exc

    return {
        "controls": tuple(_unique(control.topic for spec in api_contracts for control in spec.controls)),
        "events": tuple(
            _unique(event.topic for spec in api_contracts for event in spec.events)
        ),
        "procedures": tuple(
            _unique(procedure.name for spec in api_contracts for procedure in spec.procedures)
        ),
        "operations": tuple(
            _unique(operation.name for spec in api_contracts for operation in spec.operations)
        ),
    }


def _endpoint_mapping(value: Any, *, field_name: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise ManifestError(f"{field_name} must be a mapping")
    endpoints: dict[str, tuple[str, ...]] = {}
    for key, raw_items in value.items():
        endpoint_kind = str(key or "").strip()
        if not endpoint_kind:
            raise ManifestError(f"{field_name} contains an empty endpoint kind")
        if isinstance(raw_items, str):
            items = (raw_items.strip(),)
        elif isinstance(raw_items, Iterable):
            items = tuple(str(item).strip() for item in raw_items if str(item).strip())
        else:
            raise ManifestError(f"{field_name}.{endpoint_kind} must be a string or list")
        endpoints[endpoint_kind] = tuple(item for item in items if item)
    return endpoints


def _runtime_names(runtime: Any, method_name: str) -> set[str]:
    method = getattr(runtime, method_name, None)
    if method is None:
        return set()
    try:
        values = method() or []
    except Exception:
        return set()
    names: set[str] = set()
    for value in values:
        if isinstance(value, str):
            names.add(value)
        elif isinstance(value, tuple) and value:
            names.add(str(value[0]))
        elif isinstance(value, dict):
            for key in ("name", "topic"):
                item = value.get(key)
                if item:
                    names.add(str(item))
    return names


def _reject_capability_contract(contract: str, *, context: str) -> None:
    """Capability contracts were removed for 1.0; reject them instead of ignoring them."""

    if contract.startswith(_CAPABILITY_CONTRACT_PREFIX):
        raise ManifestError(_capability_contract_error(contract, context))


def _is_known_public_contract(contract: str) -> bool:
    if contract.startswith("component."):
        try:
            load_component_spec(contract)
            return True
        except ContractSpecError:
            return False
    if contract.startswith("api."):
        try:
            load_api_contract_spec(contract)
            return True
        except (ContractSpecError, Exception):
            return False
    return False


def _interface_endpoint_names(declaration: InterfaceDeclaration, endpoint_kind: str) -> tuple[str, ...]:
    explicit = declaration.endpoints.get(endpoint_kind) or ()
    if explicit:
        return explicit
    if declaration.visibility != "public":
        return ()
    return _contract_endpoint_names(declaration.contract).get(endpoint_kind, ())


def _contract_endpoint_names(contract: str) -> dict[str, tuple[str, ...]]:
    api_contracts = []
    if contract.startswith("component."):
        try:
            component = load_component_spec(contract)
            api_contracts = [load_api_contract_spec(api_id) for api_id in component.conformance_apis]
        except ContractSpecError:
            return {}
    elif contract.startswith("api."):
        try:
            api_contracts = [load_api_contract_spec(contract)]
        except ContractSpecError:
            return {}
    else:
        return {}

    endpoints = {
        "publishes": [],
        "subscribes": [],
        "controls": [],
        "procedures": [],
        "operations": [],
    }
    for api_contract in api_contracts:
        endpoints["publishes"].extend(event.topic for event in api_contract.published_events())
        endpoints["subscribes"].extend(event.topic for event in api_contract.subscribed_events())
        endpoints["controls"].extend(control.topic for control in api_contract.controls)
        endpoints["procedures"].extend(procedure.name for procedure in api_contract.procedures)
        endpoints["operations"].extend(operation.name for operation in api_contract.operations)
    return {kind: tuple(_unique(values)) for kind, values in endpoints.items()}
