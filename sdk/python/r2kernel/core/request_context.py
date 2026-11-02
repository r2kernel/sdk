"""Request, Procedure and Operation handler contexts and the Operation record."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .subsystem_runtime import SubsystemRuntime


@dataclass
class OperationRecord:
    operation_id: str
    operation: str
    params: Any
    created_ts: int
    updated_ts: int
    state: str = "queued"
    cancel_requested: bool = False
    feedback_count: int = 0
    result: Any = None
    error_code: str | None = None
    error_message: str | None = None
    state_payload: Any = None
    request_context: "RequestContext | None" = None
    result_type: str | None = None

    def to_query_response(self) -> dict[str, Any]:
        response = {
            "operation_id": self.operation_id,
            "operation": self.operation,
            "state": self.state,
            "cancel_requested": self.cancel_requested,
            "updated_ts": self.updated_ts,
            "created_ts": self.created_ts,
            "feedback_count": self.feedback_count,
        }
        if self.result is not None:
            response["result"] = self.result
        if self.error_code is not None:
            response["error_code"] = self.error_code
        if self.error_message is not None:
            response["error_message"] = self.error_message
        if self.state_payload is not None:
            response["state_payload"] = self.state_payload
        if self.request_context is not None:
            response["timed"] = self.request_context.timed
        return response


def _current_host_authority(runtime):
    value = getattr(getattr(runtime, "_procedure_handler_context", None), "authenticated_host", None)
    return dict(value) if isinstance(value, dict) else None


@dataclass(frozen=True)
class RequestContext:
    kind: str
    name: str
    topic: str
    accepted_at_sync_ns: int
    timed: bool = False
    request_header: dict[str, Any] | None = None
    payload_type: str | None = None
    response_type: str | None = None
    timed_transport: str | None = None
    timed_request_id_u64: int | None = None
    request_id_text: str | None = None
    correlation_id_text: str | None = None
    generated_at_sync_ns: int | None = None
    sent_at_sync_ns: int | None = None
    execute_at_sync_ns: int | None = None
    valid_until_sync_ns: int | None = None
    priority_value: int | None = None
    late_policy_name: str | None = None
    max_lateness_ns_value: int | None = None
    source_subsystem_name: str | None = None
    target_subsystem_name: str | None = None
    period_ms: float | None = None
    deadline_ms: float | None = None
    runtime: "SubsystemRuntime | None" = field(default=None, repr=False, compare=False)
    source_authority: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    host_authority: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    @property
    def request_id(self) -> str | None:
        if self.request_id_text is not None:
            return str(self.request_id_text)
        if self.timed_request_id_u64 is not None and not self.request_header:
            return f"{int(self.timed_request_id_u64) & ((1 << 64) - 1):016x}"
        if not self.request_header:
            return None
        value = self.request_header.get("request_id")
        return str(value) if value is not None else None

    @property
    def correlation_id(self) -> str | None:
        if self.correlation_id_text is not None:
            return str(self.correlation_id_text)
        if not self.request_header:
            return None
        value = self.request_header.get("correlation_id")
        return str(value) if value is not None else None

    @property
    def execution(self) -> dict[str, Any] | None:
        if self.execute_at_sync_ns is not None or self.late_policy_name is not None or self.max_lateness_ns_value is not None:
            execution: dict[str, Any] = {
                "mode": "at_time" if self.execute_at_sync_ns is not None else "immediate",
            }
            if self.execute_at_sync_ns is not None:
                execution["execute_at_sync_ns"] = int(self.execute_at_sync_ns)
            if self.late_policy_name is not None:
                execution["late_policy"] = str(self.late_policy_name)
            if self.max_lateness_ns_value is not None:
                execution["max_lateness_ns"] = int(self.max_lateness_ns_value)
            return execution
        if not self.request_header:
            return None
        execution = self.request_header.get("execution")
        return execution if isinstance(execution, dict) else None

    @property
    def source_subsystem(self) -> str | None:
        if self.source_subsystem_name is not None:
            return str(self.source_subsystem_name)
        if not self.request_header:
            return None
        value = self.request_header.get("source_subsystem")
        return str(value) if value is not None else None

    @property
    def target_subsystem(self) -> str | None:
        if self.target_subsystem_name is not None:
            return str(self.target_subsystem_name)
        if not self.request_header:
            return None
        value = self.request_header.get("target_subsystem")
        return str(value) if value is not None else None

    @property
    def priority(self) -> int | None:
        if self.priority_value is not None:
            return int(self.priority_value)
        if not self.request_header:
            return None
        value = self.request_header.get("priority")
        return int(value) if value is not None else None

    @property
    def late_policy(self) -> str | None:
        if self.late_policy_name is not None:
            return str(self.late_policy_name)
        execution = self.execution
        if not execution:
            return None
        value = execution.get("late_policy")
        return str(value) if value is not None else None

    @property
    def max_lateness_ns(self) -> int | None:
        if self.max_lateness_ns_value is not None:
            return int(self.max_lateness_ns_value)
        execution = self.execution
        if not execution:
            return None
        value = execution.get("max_lateness_ns")
        return int(value) if value is not None else None

    def resolved_request_header(self) -> dict[str, Any] | None:
        if self.request_header is not None:
            return self.request_header
        header: dict[str, Any] = {}
        request_id = self.request_id
        if request_id is not None:
            header["request_id"] = request_id
        correlation_id = self.correlation_id
        if correlation_id is not None:
            header["correlation_id"] = correlation_id
        source_subsystem = self.source_subsystem
        if source_subsystem is not None:
            header["source_subsystem"] = source_subsystem
        target_subsystem = self.target_subsystem
        if target_subsystem is not None:
            header["target_subsystem"] = target_subsystem
        if self.sent_at_sync_ns is not None:
            header["sent_at_sync_ns"] = int(self.sent_at_sync_ns)
        if self.generated_at_sync_ns is not None:
            header["generated_at_sync_ns"] = int(self.generated_at_sync_ns)
        priority = self.priority
        if priority is not None:
            header["priority"] = int(priority)
        execution = self.execution
        if execution is not None:
            if self.valid_until_sync_ns is not None:
                execution = dict(execution)
                execution["valid_until_sync_ns"] = int(self.valid_until_sync_ns)
            header["execution"] = execution
        return header or None

    def build_timed_response(
        self,
        payload: dict[str, Any],
        *,
        execution_report: dict[str, Any],
        payload_type: str | None = None,
        ok: bool | None = None,
        extra: dict[str, Any] | None = None,
        status_code: int | None = None,
        status_message: str | None = None,
    ) -> dict[str, Any]:
        runtime = self.runtime
        if runtime is None:
            raise RuntimeError("request context is detached from runtime")
        return runtime._build_timed_result(
            request_context=self,
            payload=payload,
            execution_report=execution_report,
            payload_type=payload_type,
            ok=ok,
            extra=extra,
            status_code=status_code,
            status_message=status_message,
        )

    def build_timed_transport_response(
        self,
        payload: dict[str, Any],
        *,
        execution_report: dict[str, Any],
        payload_type: str | None = None,
        ok: bool | None = None,
        extra: dict[str, Any] | None = None,
        status_code: int | None = None,
        status_message: str | None = None,
        transport_status: int = 0,
        responded_at_sync_ns: int | None = None,
    ) -> dict[str, Any]:
        runtime = self.runtime
        if runtime is None:
            raise RuntimeError("request context is detached from runtime")
        return runtime._build_timed_transport_response(
            request_context=self,
            payload=payload,
            execution_report=execution_report,
            payload_type=payload_type,
            ok=ok,
            extra=extra,
            status_code=status_code,
            status_message=status_message,
            transport_status=transport_status,
            responded_at_sync_ns=responded_at_sync_ns,
        )


class ProcedureContext(RequestContext):
    pass


class ControlRequestContext(RequestContext):
    pass


class OperationContext:
    def __init__(
        self,
        runtime: "SubsystemRuntime",
        operation_id: str,
        *,
        request_context: RequestContext | None = None,
        result_type: str | None = None,
    ):
        self._runtime = runtime
        self._operation_id = operation_id
        self._request_context = request_context
        self._result_type = str(result_type).strip() if result_type is not None else None

    @property
    def operation_id(self) -> str:
        return self._operation_id

    @property
    def request_context(self) -> RequestContext | None:
        return self._request_context

    @property
    def timed(self) -> bool:
        return bool(self._request_context and self._request_context.timed)

    @property
    def request_header(self) -> dict[str, Any] | None:
        return self._request_context.resolved_request_header() if self._request_context else None

    @property
    def payload_type(self) -> str | None:
        return self._request_context.payload_type if self._request_context else None

    @property
    def accepted_at_sync_ns(self) -> int | None:
        return self._request_context.accepted_at_sync_ns if self._request_context else None

    @property
    def sent_at_sync_ns(self) -> int | None:
        return self._request_context.sent_at_sync_ns if self._request_context else None

    @property
    def execute_at_sync_ns(self) -> int | None:
        return self._request_context.execute_at_sync_ns if self._request_context else None

    @property
    def priority(self) -> int | None:
        return self._request_context.priority if self._request_context else None

    @property
    def late_policy(self) -> str | None:
        return self._request_context.late_policy if self._request_context else None

    @property
    def max_lateness_ns(self) -> int | None:
        return self._request_context.max_lateness_ns if self._request_context else None

    @property
    def result_type(self) -> str | None:
        return self._result_type

    def is_cancel_requested(self) -> bool:
        return self._runtime.is_operation_cancel_requested(self._operation_id)

    def publish_feedback(self, payload: Any) -> None:
        self._runtime.publish_operation_feedback(operation_id=self._operation_id, payload=payload)

    def publish_state(self, state: str, payload: Any = None) -> None:
        self._runtime.publish_operation_state(operation_id=self._operation_id, state=state, payload=payload)

    def publish_result(self, payload: Any) -> None:
        self._runtime.publish_operation_result(operation_id=self._operation_id, payload=payload)

    def build_timed_result(
        self,
        payload: dict[str, Any],
        *,
        execution_report: dict[str, Any],
        payload_type: str | None = None,
        ok: bool | None = None,
        extra: dict[str, Any] | None = None,
        status_code: int | None = None,
        status_message: str | None = None,
    ) -> dict[str, Any]:
        request_context = self._request_context
        if request_context is None:
            raise RuntimeError("timed Operation result requires a request context")
        resolved_payload_type = payload_type or self._result_type or request_context.payload_type
        return self._runtime._build_timed_result(
            request_context=request_context,
            payload=payload,
            execution_report=execution_report,
            payload_type=resolved_payload_type,
            ok=ok,
            extra=extra,
            status_code=status_code,
            status_message=status_message,
        )

    def succeed(self, result: Any = None) -> None:
        self._runtime.complete_operation(operation_id=self._operation_id, state="succeeded", result=result)

    def fail(
        self,
        *,
        error_code: str = "execution_failed",
        error_message: str,
        payload: Any = None,
    ) -> None:
        self._runtime.complete_operation(
            operation_id=self._operation_id,
            state="failed",
            result=payload,
            error_code=error_code,
            error_message=error_message,
        )

    def canceled(self, payload: Any = None) -> None:
        self._runtime.complete_operation(operation_id=self._operation_id, state="canceled", result=payload)

    def succeed_timed(
        self,
        payload: dict[str, Any],
        *,
        execution_report: dict[str, Any],
        payload_type: str | None = None,
        extra: dict[str, Any] | None = None,
        ok: bool | None = None,
        status_code: int | None = None,
        status_message: str | None = None,
    ) -> None:
        self.succeed(
            self.build_timed_result(
                payload,
                execution_report=execution_report,
                payload_type=payload_type,
                extra=extra,
                ok=ok,
                status_code=status_code,
                status_message=status_message,
            )
        )

    def fail_timed(
        self,
        *,
        error_message: str,
        execution_report: dict[str, Any],
        payload: dict[str, Any],
        payload_type: str | None = None,
        extra: dict[str, Any] | None = None,
        error_code: str = "execution_failed",
        ok: bool | None = None,
        status_code: int | None = None,
        status_message: str | None = None,
    ) -> None:
        self.fail(
            error_code=error_code,
            error_message=error_message,
            payload=self.build_timed_result(
                payload,
                execution_report=execution_report,
                payload_type=payload_type,
                extra=extra,
                ok=ok,
                status_code=status_code,
                status_message=status_message,
            ),
        )

    def canceled_timed(
        self,
        payload: dict[str, Any],
        *,
        execution_report: dict[str, Any],
        payload_type: str | None = None,
        extra: dict[str, Any] | None = None,
        ok: bool | None = None,
        status_code: int | None = None,
        status_message: str | None = None,
    ) -> None:
        self.canceled(
            self.build_timed_result(
                payload,
                execution_report=execution_report,
                payload_type=payload_type,
                extra=extra,
                ok=ok,
                status_code=status_code,
                status_message=status_message,
            )
        )
