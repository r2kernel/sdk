from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any

from r2kernel.interface import InterfaceSpec, ValidationIssue, validate_interface_specs

from .loader import (
    ComponentSpec,
    ContractSpecError,
    expand_declared_api_ids,
    load_component_specs,
    load_api_contract_specs,
)

R2KERNEL_CONTRACT_POLICY_ENV = "R2KERNEL_CONTRACT_POLICY"
DEFAULT_CONTRACT_POLICY = "strict"
_VALID_CONTRACT_POLICIES = {"strict", "warn", "off"}
_SCALAR_TYPE_REFS = {
    "bool",
    "bytes",
    "float32",
    "float64",
    "int32",
    "int64",
    "str",
    "string",
    "uint8",
    "uint16",
    "uint32",
}


@dataclass(frozen=True)
class ContractValidationReport:
    api_contracts: tuple[InterfaceSpec, ...]
    components: tuple[ComponentSpec, ...] = ()
    issues: tuple[ValidationIssue, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not any(issue.level == "error" for issue in self.issues)

    def format_errors(self) -> str:
        return "; ".join(issue.message for issue in self.issues if issue.level == "error")


def validate_declared_contracts(owner: Any, runtime: Any) -> ContractValidationReport:
    declared_apis = tuple(getattr(owner, "declared_apis", ()) or getattr(owner, "declares_apis", ()) or ())
    declared_components = tuple(getattr(owner, "declared_components", ()) or getattr(owner, "declares_components", ()) or ())
    for removed in ("declared_capabilities", "declares_capabilities"):
        if getattr(owner, removed, ()):
            raise ContractSpecError(
                f"{removed} is not supported; capability contracts were removed, "
                "declare an api.* or component.* contract"
            )
    if not declared_apis and not declared_components:
        return ContractValidationReport(api_contracts=(), components=(), issues=())

    components = tuple(load_component_specs(declared_components))
    expanded_api_ids = expand_declared_api_ids(
        declared_apis=declared_apis,
        declared_components=components,
    )
    api_contracts = tuple(load_api_contract_specs(expanded_api_ids))
    report = validate_interface_specs(api_contracts, runtime)
    return ContractValidationReport(
        api_contracts=api_contracts,
        components=components,
        issues=report.issues,
    )


@dataclass(frozen=True)
class RuntimeContractTypeReport:
    policy: str
    issues: tuple[ValidationIssue, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not any(issue.level == "error" for issue in self.issues)

    def format_errors(self) -> str:
        return "; ".join(issue.message for issue in self.issues if issue.level == "error")


def validate_runtime_contract_types(runtime: Any, policy: str | None = None, *, codec: Any = None) -> RuntimeContractTypeReport:
    effective_policy = _normalize_policy(policy or os.environ.get(R2KERNEL_CONTRACT_POLICY_ENV))
    if effective_policy == "off":
        return RuntimeContractTypeReport(policy=effective_policy, issues=())

    issues: list[ValidationIssue] = []
    try:
        from r2kernel.interface.codec import RustMessageCodec

        if codec is None:
            codec = RustMessageCodec()
    except Exception as exc:
        level = "error" if effective_policy == "strict" else "warning"
        issues.append(
            _issue(
                level,
                "schema_runtime_unavailable",
                f"contract type validation requires r2kernel-core schema runtime: {exc}",
            )
        )
        return RuntimeContractTypeReport(policy=effective_policy, issues=tuple(issues))

    control_contracts = dict(runtime.get_control_contract_info())
    procedure_contracts = dict(runtime.get_procedure_contract_info())
    operation_contracts = dict(runtime.get_operation_contract_info())

    control_names = set(runtime.get_control_info())
    for topic in sorted(control_names):
        contract = control_contracts.get(topic)
        if not contract:
            issues.append(_missing_contract(effective_policy, "control", topic, "request/response type metadata"))
            continue
        _ensure_type_ref(codec, effective_policy, contract.get("request_type"), f"control '{topic}' request_type", issues)
        _ensure_type_ref(codec, effective_policy, contract.get("response_type"), f"control '{topic}' response_type", issues)

    procedure_names = set(runtime.get_procedure_info())
    for name in sorted(procedure_names):
        contract = procedure_contracts.get(name)
        if not contract:
            issues.append(_missing_contract(effective_policy, "procedure", name, "request/response type metadata"))
            continue
        _ensure_type_ref(codec, effective_policy, contract.get("request_type"), f"procedure '{name}' request_type", issues)
        _ensure_type_ref(codec, effective_policy, contract.get("response_type"), f"procedure '{name}' response_type", issues)

    operation_names = set(runtime.get_operation_info())
    for name in sorted(operation_names):
        contract = operation_contracts.get(name)
        if not contract:
            issues.append(_missing_contract(effective_policy, "operation", name, "request/feedback/result/state type metadata"))
            continue
        _ensure_type_ref(codec, effective_policy, contract.get("request_type"), f"operation '{name}' request_type", issues)
        _ensure_type_ref(codec, effective_policy, contract.get("feedback_type"), f"operation '{name}' feedback_type", issues)
        _ensure_type_ref(codec, effective_policy, contract.get("result_type"), f"operation '{name}' result_type", issues)
        state_type = contract.get("state_type")
        if state_type:
            _ensure_type_ref(codec, effective_policy, state_type, f"operation '{name}' state_type", issues)

    return RuntimeContractTypeReport(policy=effective_policy, issues=tuple(issues))


def _normalize_policy(raw: str | None) -> str:
    value = str(raw or DEFAULT_CONTRACT_POLICY).strip().lower() or DEFAULT_CONTRACT_POLICY
    if value not in _VALID_CONTRACT_POLICIES:
        return "strict"
    return value


def _missing_contract(policy: str, kind: str, name: str, detail: str) -> ValidationIssue:
    return _issue(
        "error" if policy == "strict" else "warning",
        "missing_runtime_contract_metadata",
        f"{kind} '{name}' is registered without {detail}",
    )


def _ensure_type_ref(
    codec: Any,
    policy: str,
    raw_type_ref: Any,
    location: str,
    issues: list[ValidationIssue],
) -> None:
    type_ref = str(raw_type_ref or "").strip()
    if not type_ref:
        issues.append(
            _issue(
                "error" if policy == "strict" else "warning",
                "missing_type_ref",
                f"{location} is empty",
            )
        )
        return
    if _type_ref_is_known(codec, type_ref):
        return
    issues.append(
        _issue(
            "error" if policy == "strict" else "warning",
            "unknown_type_ref",
            f"{location} references unknown message type '{type_ref}'",
        )
    )


def _type_ref_is_known(codec: Any, type_ref: str) -> bool:
    normalized = type_ref.strip()
    if not normalized:
        return False
    if normalized in _SCALAR_TYPE_REFS:
        return True
    if normalized.endswith("_array"):
        return _type_ref_is_known(codec, normalized[:-6])
    if normalized.startswith("vector<") and normalized.endswith(">"):
        return _type_ref_is_known(codec, normalized[7:-1].strip())
    if normalized.startswith("array<") and normalized.endswith(">"):
        inner = normalized[6:-1]
        element, separator, _ = inner.rpartition(",")
        return bool(separator and _type_ref_is_known(codec, element.strip()))
    if "/" not in normalized:
        return False
    namespace, _, message_name = normalized.partition("/")
    if not _is_versioned_namespace(namespace) or not message_name:
        return False
    if _api_local_message_is_known(namespace, message_name):
        return True
    try:
        codec.get_message_schema(normalized)
        return True
    except Exception:
        return False


def _api_local_message_is_known(api_id: str, message_name: str) -> bool:
    if not api_id.startswith("api."):
        return False
    try:
        api_contracts = load_api_contract_specs((api_id,))
    except Exception:
        return False
    return any(message_name in api_contract.messages for api_contract in api_contracts)


def _is_versioned_namespace(namespace: str) -> bool:
    suffix = namespace.rsplit(".", 1)[-1]
    return len(suffix) > 1 and suffix[0] == "v" and suffix[1:].isdigit()


def _issue(level: str, code: str, message: str) -> ValidationIssue:
    return ValidationIssue(
        level=level,
        code=code,
        message=message,
        interface_id="runtime_contract_types",
    )
