from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .loader import load_interface_specs
from .model import InterfaceSpec


_COMMON_CONTROL_FIELDS = {
    "version",
    "request_effect",
    "response_scope",
    "period_policy",
    "execution_policy",
    "refresh_mode",
    "ownership_policy",
    "ordering",
    "repeat_policy",
    "failure",
    "limits",
}
_COMMON_CONTROL_LIMIT_FIELDS = {"source", "required_fields", "provider_bounds"}
_COMMON_CONTROL_REQUIRED_LIMITS = {
    "max_update_gap_ns",
    "max_command_age_ns",
    "max_request_bytes",
    "max_response_bytes",
}
_COMMON_CONTROL_ENUMS = {
    "response_scope": {"endpoint_effect_completed", "sample_observed"},
    "period_policy": {"binding_required"},
    "execution_policy": {"finite_expiry_commit_check"},
    "refresh_mode": {"command_refresh", "maintain_state", "sample_exchange"},
    "ownership_policy": {"explicit_binding_generation"},
    "ordering": {"strictly_increasing_cycle"},
    "repeat_policy": {"no_automatic_replay"},
    "failure": {"report_domain_status"},
}


@dataclass(frozen=True)
class ValidationIssue:
    level: str
    message: str
    code: str
    interface_id: str


@dataclass(frozen=True)
class InterfaceValidationReport:
    specs: tuple[InterfaceSpec, ...]
    issues: tuple[ValidationIssue, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not any(issue.level == "error" for issue in self.issues)

    def format_errors(self) -> str:
        return "; ".join(issue.message for issue in self.issues if issue.level == "error")


def validate_interface_specs(
    specs: tuple[InterfaceSpec, ...] | list[InterfaceSpec],
    runtime: Any,
) -> InterfaceValidationReport:
    if not specs:
        return InterfaceValidationReport(specs=(), issues=())

    specs = tuple(specs)
    issues: list[ValidationIssue] = []

    publish_info = {topic: dtype for topic, dtype in runtime.get_publish_info()}
    subscribe_info = {topic: dtype for topic, dtype in runtime.get_subscribe_info()}
    control_topics = set(runtime.get_control_info())
    procedure_names = set(runtime.get_procedure_info())
    operation_names = set(runtime.get_operation_info())
    control_contracts = dict(runtime.get_control_contract_info())
    procedure_contracts = dict(runtime.get_procedure_contract_info())
    operation_contracts = dict(runtime.get_operation_contract_info())

    for spec in specs:
        issues.extend(validate_interface_spec_definition(spec))
        for event in spec.published_events():
            actual = publish_info.get(event.topic)
            if actual is None:
                issues.append(_error(spec.id, "missing_event_publish", f"missing required published event '{event.topic}'"))
            elif not _matches_type(spec, actual, event.type):
                issues.append(_error(spec.id, "event_publish_type_mismatch", f"event '{event.topic}' must use type '{event.type}', found '{actual}'"))

        for event in spec.subscribed_events():
            actual = subscribe_info.get(event.topic)
            if actual is None:
                issues.append(_error(spec.id, "missing_event_subscribe", f"missing required subscribed event '{event.topic}'"))
            elif not _matches_type(spec, actual, event.type):
                issues.append(_error(spec.id, "event_subscribe_type_mismatch", f"subscription '{event.topic}' must use type '{event.type}', found '{actual}'"))

        for endpoint in spec.controls:
            if endpoint.topic in procedure_names:
                issues.append(
                    _error(
                        spec.id,
                        "control_procedure_name_collision",
                        f"control endpoint '{endpoint.topic}' must not also be registered as a procedure",
                    )
                )
            if endpoint.topic not in control_topics:
                issues.append(_error(spec.id, "missing_control_endpoint", f"missing required control endpoint '{endpoint.topic}'"))
                continue
            contract = control_contracts.get(endpoint.topic)
            if not contract:
                issues.append(_error(spec.id, "missing_control_contract", f"control endpoint '{endpoint.topic}' is registered but request/response type metadata is missing"))
                continue
            request_type = contract.get("request_type")
            response_type = contract.get("response_type")
            actual_timing_support = str(contract.get("timing_support") or "none")
            if not _matches_type(spec, request_type, endpoint.request):
                issues.append(_error(spec.id, "control_request_type_mismatch", f"control endpoint '{endpoint.topic}' request type must be '{endpoint.request}', found '{request_type}'"))
            if not _matches_type(spec, response_type, endpoint.response):
                issues.append(_error(spec.id, "control_response_type_mismatch", f"control endpoint '{endpoint.topic}' response type must be '{endpoint.response}', found '{response_type}'"))
            if actual_timing_support != endpoint.timing_support:
                issues.append(
                    _error(
                        spec.id,
                        "control_timing_support_mismatch",
                        f"control endpoint '{endpoint.topic}' timing_support must be '{endpoint.timing_support}', found '{actual_timing_support}'",
                    )
                )

        for procedure in spec.procedures:
            if procedure.name in control_topics:
                issues.append(
                    _error(
                        spec.id,
                        "procedure_control_name_collision",
                        f"procedure '{procedure.name}' must not also be registered as a control endpoint",
                    )
                )
            if procedure.name not in procedure_names:
                issues.append(_error(spec.id, "missing_procedure", f"missing required procedure '{procedure.name}'"))
                continue
            contract = procedure_contracts.get(procedure.name)
            if not contract:
                issues.append(_error(spec.id, "missing_procedure_contract", f"procedure '{procedure.name}' is registered but request/response type metadata is missing"))
                continue
            request_type = contract.get("request_type")
            response_type = contract.get("response_type")
            actual_timing_support = str(contract.get("timing_support") or "none")
            if not _matches_type(spec, request_type, procedure.request):
                issues.append(_error(spec.id, "procedure_request_type_mismatch", f"procedure '{procedure.name}' request type must be '{procedure.request}', found '{request_type}'"))
            if not _matches_type(spec, response_type, procedure.response):
                issues.append(_error(spec.id, "procedure_response_type_mismatch", f"procedure '{procedure.name}' response type must be '{procedure.response}', found '{response_type}'"))
            if actual_timing_support != procedure.timing_support:
                issues.append(
                    _error(
                        spec.id,
                        "procedure_timing_support_mismatch",
                        f"procedure '{procedure.name}' timing_support must be '{procedure.timing_support}', found '{actual_timing_support}'",
                    )
                )

        for operation in spec.operations:
            if operation.name not in operation_names:
                issues.append(_error(spec.id, "missing_operation", f"missing required operation '{operation.name}'"))
                continue
            contract = operation_contracts.get(operation.name)
            if not contract:
                issues.append(_error(spec.id, "missing_operation_contract", f"operation '{operation.name}' is registered but request/feedback/result type metadata is missing"))
                continue
            if not _matches_type(spec, contract.get("request_type"), operation.request):
                issues.append(_error(spec.id, "operation_request_type_mismatch", f"operation '{operation.name}' request type must be '{operation.request}', found '{contract.get('request_type')}'"))
            if not _matches_type(spec, contract.get("feedback_type"), operation.feedback):
                issues.append(_error(spec.id, "operation_feedback_type_mismatch", f"operation '{operation.name}' feedback type must be '{operation.feedback}', found '{contract.get('feedback_type')}'"))
            if not _matches_type(spec, contract.get("result_type"), operation.result):
                issues.append(_error(spec.id, "operation_result_type_mismatch", f"operation '{operation.name}' result type must be '{operation.result}', found '{contract.get('result_type')}'"))
            expected_state = operation.state or "string"
            actual_state = contract.get("state_type") or "string"
            if not _matches_type(spec, actual_state, expected_state):
                issues.append(_error(spec.id, "operation_state_type_mismatch", f"operation '{operation.name}' state type must be '{expected_state}', found '{actual_state}'"))
            actual_timing_support = str(contract.get("timing_support") or "none")
            if actual_timing_support != operation.timing_support:
                issues.append(
                    _error(
                        spec.id,
                        "operation_timing_support_mismatch",
                        f"operation '{operation.name}' timing_support must be '{operation.timing_support}', found '{actual_timing_support}'",
                    )
                )

    return InterfaceValidationReport(specs=specs, issues=tuple(issues))


def validate_interface_spec_definition(spec: InterfaceSpec) -> tuple[ValidationIssue, ...]:
    if spec.kind != "api_contract" or not spec.id.startswith("api."):
        return ()

    issues: list[ValidationIssue] = []
    required_sections = {
        "frames": spec.frames,
        "timing": spec.timing,
        "state_model": spec.state_model,
        "failure_semantics": spec.failure_semantics,
        "authority": spec.authority,
        "limits": spec.limits,
        "conformance": spec.conformance,
    }
    for section, value in required_sections.items():
        if not value:
            issues.append(
                _error(spec.id, f"missing_{section}", f"public API '{spec.id}' must declare {section}")
            )
    if spec.messages:
        issues.append(
            _error(
                spec.id,
                "inline_public_messages",
                f"public API '{spec.id}' must reference msg.* payloads instead of inline messages",
            )
        )
    for event in spec.events:
        if not event.semantics:
            issues.append(
                _error(
                    spec.id,
                    "missing_event_semantics",
                    f"event '{event.topic}' must declare observation and delivery semantics",
                )
            )
    for control in spec.controls:
        if control.timing_support != "required":
            issues.append(
                _error(
                    spec.id,
                    "control_timing_required",
                    f"control '{control.topic}' must require the runtime timing envelope",
                )
            )
        if not control.semantics:
            issues.append(
                _error(
                    spec.id,
                    "missing_control_semantics",
                    f"control '{control.topic}' must declare validity and scheduling semantics",
                )
            )
        issues.extend(_validate_common_control_metadata(spec.id, control.topic, control.semantics))
    for procedure in spec.procedures:
        if not procedure.semantics:
            issues.append(
                _error(
                    spec.id,
                    "missing_procedure_semantics",
                    f"procedure '{procedure.name}' must declare idempotency and side-effect semantics",
                )
            )
    for operation in spec.operations:
        if not operation.semantics:
            issues.append(
                _error(
                    spec.id,
                    "missing_operation_semantics",
                    f"operation '{operation.name}' must declare progress, cancellation, and terminal-state semantics",
                )
            )
    return tuple(issues)


def _validate_common_control_metadata(
    interface_id: str,
    endpoint_name: str,
    semantics: Any,
) -> list[ValidationIssue]:
    """Validate only explicitly declared common_control v1 metadata.

    Endpoints without this declaration remain valid API contracts. The
    validator does not label those endpoints as conformant to the new profile.
    """
    if not isinstance(semantics, dict) or "common_control" not in semantics:
        return []

    common = semantics["common_control"]
    issues: list[ValidationIssue] = []

    def report(code: str, message: str) -> None:
        issues.append(_error(interface_id, code, f"control '{endpoint_name}' {message}"))

    if not isinstance(common, dict):
        report("invalid_common_control", "common_control must be a mapping")
        return issues

    missing = _COMMON_CONTROL_FIELDS - set(common)
    unknown = set(common) - _COMMON_CONTROL_FIELDS
    if missing:
        report("incomplete_common_control", f"common_control is missing {', '.join(sorted(missing))}")
    if unknown:
        report("unknown_common_control_fields", f"common_control has unknown fields {', '.join(sorted(map(str, unknown)))}")

    version = common.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        report("unsupported_common_control_version", "common_control.version must be the integer 1")

    request_effect = common.get("request_effect")
    if not isinstance(request_effect, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", request_effect):
        report("invalid_common_control_request_effect", "request_effect must be a stable snake_case identifier")

    for field_name, allowed_values in _COMMON_CONTROL_ENUMS.items():
        value = common.get(field_name)
        if not isinstance(value, str) or value not in allowed_values:
            rendered = "|".join(sorted(allowed_values))
            report("unsupported_common_control_policy", f"{field_name} must be one of {rendered}")

    if (
        common.get("response_scope") == "sample_observed"
        and common.get("refresh_mode") != "sample_exchange"
    ):
        report("incompatible_common_control_policy", "sample_observed response_scope requires sample_exchange refresh_mode")

    limits = common.get("limits")
    if not isinstance(limits, dict):
        report("invalid_common_control_limits", "limits must be a mapping")
        return issues
    limit_missing = _COMMON_CONTROL_LIMIT_FIELDS - set(limits)
    limit_unknown = set(limits) - _COMMON_CONTROL_LIMIT_FIELDS
    if limit_missing:
        report("incomplete_common_control_limits", f"limits is missing {', '.join(sorted(limit_missing))}")
    if limit_unknown:
        report("unknown_common_control_limit_fields", f"limits has unknown fields {', '.join(sorted(map(str, limit_unknown)))}")
    if limits.get("source") != "binding":
        report("invalid_common_control_limit_source", "limits.source must be binding")
    if limits.get("provider_bounds") != "constrain_selected_values":
        report("invalid_common_control_provider_bounds", "limits.provider_bounds must be constrain_selected_values")

    required_fields = limits.get("required_fields")
    if not isinstance(required_fields, list) or any(not isinstance(field, str) for field in required_fields):
        report("invalid_common_control_required_limits", "limits.required_fields must be a list of field names")
    else:
        fields = set(required_fields)
        allowed_fields = _COMMON_CONTROL_REQUIRED_LIMITS | {"max_pending_units"}
        if len(fields) != len(required_fields):
            report("duplicate_common_control_required_limit", "limits.required_fields must not contain duplicates")
        missing_required = _COMMON_CONTROL_REQUIRED_LIMITS - fields
        unknown_required = fields - allowed_fields
        if missing_required:
            report("incomplete_common_control_required_limits", f"limits.required_fields is missing {', '.join(sorted(missing_required))}")
        if unknown_required:
            report("unknown_common_control_required_limits", f"limits.required_fields has unknown entries {', '.join(sorted(unknown_required))}")

    return issues


def validate_declared_interfaces(owner: Any, runtime: Any) -> InterfaceValidationReport:
    declared = tuple(getattr(owner, "implements", ()) or ())
    if not declared:
        return InterfaceValidationReport(specs=(), issues=())

    specs = tuple(load_interface_specs(declared))
    return validate_interface_specs(specs, runtime)


def _error(interface_id: str, code: str, message: str) -> ValidationIssue:
    return ValidationIssue(level="error", code=code, message=message, interface_id=interface_id)


def _matches_type(spec: InterfaceSpec, actual: Any, expected: str) -> bool:
    if actual is None:
        return False
    actual_ref = str(actual)
    expected_ref = str(expected)
    return actual_ref in spec.accepted_type_refs(expected_ref)
