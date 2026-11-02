"""Subsystem event, control, timing, procedure and operation facade methods."""

from __future__ import annotations

from typing import Any, Callable

from r2kernel.core._runtime_common import _is_runtime_transport_topic
from r2kernel.datatype import DTypeLiteral


class _CommunicationMixin:
    """Endpoint policy checks and the event/control/procedure/operation facade of Subsystem."""

    def _ensure_gateway_endpoint_category_allowed(self, endpoint_kind: str, endpoint: str) -> None:
        if self._subsystem_manifest.role == "gateway" and endpoint_kind != "controls":
            endpoint_name = str(endpoint or "").strip() or "<empty>"
            raise RuntimeError(
                f"Gateway subsystems may use only Control endpoints; "
                f"{self.name}.{endpoint_kind}.{endpoint_name} is not allowed"
            )

    def declared_runtime_endpoints(self, endpoint_kind: str) -> tuple[str, ...]:
        """Endpoints of ``endpoint_kind`` this subsystem's manifest and spec contracts allow."""
        return tuple(self._allowed_runtime_endpoints.get(endpoint_kind, ()))

    def _ensure_runtime_endpoint_allowed(self, endpoint_kind: str, endpoint: str) -> None:
        endpoint_name = str(endpoint or "").strip()
        if not endpoint_name:
            raise ValueError(f"{endpoint_kind} endpoint name is required")
        self._ensure_gateway_endpoint_category_allowed(endpoint_kind, endpoint_name)
        allowed = set(self._allowed_runtime_endpoints.get(endpoint_kind, ()))
        if endpoint_name in allowed:
            return
        raise RuntimeError(
            f"Runtime endpoint is not declared by subsystem manifest or R2Kernel spec contract: "
            f"{self.name}.{endpoint_kind}.{endpoint_name}. "
            "Public interfaces must use endpoints from their referenced R2Kernel spec; "
            "private endpoints must be declared with visibility: internal in subsystem.yaml. "
            "Use payload fields, not dynamic endpoint names, for request_id, operation_id, object_id, or task_id."
        )

    def register_event_publish(
        self,
        *,
        topic: str,
        type: DTypeLiteral,
        size: int | None = None,
        src_attr: str | None = None,
        priority: int = 5,
        queue_depth: int | None = None,
    ):
        self._ensure_runtime_endpoint_allowed("publishes", topic)
        return self._runtime.register_event_publish(
            topic=topic,
            type=type,
            size=size,
            src_attr=src_attr,
            priority=priority,
            queue_depth=queue_depth,
        )

    def register_event_subscribe(
        self,
        *,
        topic: str,
        type: DTypeLiteral,
        size: int | None = None,
        dest_attr: str | None = None,
        callback: Callable | None = None,
    ):
        self._ensure_runtime_endpoint_allowed("subscribes", topic)
        self._runtime.register_event_subscribe(
            topic=topic,
            type=type,
            size=size,
            dest_attr=dest_attr,
            callback=callback,
        )

    def publish_event(self, *, topic: str, value, timestamp: int | None = None) -> bool:
        self._ensure_runtime_endpoint_allowed("publishes", topic)
        return self._runtime.publish_event(topic=topic, value=value, timestamp=timestamp)

    def start_event_transport(self) -> None:
        self._runtime.start_event_transport()

    def stop_event_transport(self) -> None:
        self._runtime.stop_event_transport()

    def current_control_source_authority(self) -> dict[str, Any] | None:
        """Verified Control source authority of the request being handled on this thread.

        Returns a copy (``peer``, ``epoch``, ``source``, ``source_session``, ``source_registration``,
        ``source_reference``, ``target``, ``target_session``, ``target_registration``, ``interface``,
        ``binding_revision``, ``topic``) while a Control callback for a manager-verified request runs,
        otherwise ``None``. No SDK-declared name is authenticated.
        """
        return self._runtime.current_control_source_authority()

    def current_protocol_host_authority(self) -> dict[str, Any] | None:
        """Verified Protocol host authority (``kind``, ``peer``, ``epoch``) of the current request, or ``None``.

        Operation handlers keep the host authority captured when the Operation was accepted.
        """
        return self._runtime.current_protocol_host_authority()

    def register_control_endpoint(
        self,
        topic: str,
        handler: Callable[..., Any],
        *,
        request_type: str | None = None,
        response_type: str | None = None,
        timing_support: str = "none",
    ) -> None:
        self._ensure_control_role_allowed()
        self._ensure_runtime_endpoint_allowed("controls", topic)
        self._runtime.register_control_endpoint(
            topic=topic,
            handler=handler,
            request_type=request_type,
            response_type=response_type,
            timing_support=timing_support,
        )

    def register_raw_control_endpoint(self, topic: str, handler: Callable[..., dict]) -> None:
        """Declare a private raw callback receiving the unchanged full request."""
        self._ensure_control_role_allowed()
        self._ensure_runtime_endpoint_allowed("controls", topic)
        self._runtime.register_raw_control_endpoint(topic, handler)

    def get_control_info(self) -> list[str]:
        return self._runtime.get_control_info()

    def get_control_contract_info(self) -> list[tuple[str, dict[str, str | None]]]:
        return self._runtime.get_control_contract_info()

    def build_timing_request_header(
        self,
        *,
        target_subsystem: str,
        source_subsystem: str | None = None,
        execute_after_ns: int | None = None,
        execute_at_sync_ns: int | None = None,
        priority: int | None = 128,
        late_policy: str | None = "execute_immediately",
        max_lateness_ns: int | None = 1_000_000,
        correlation_id: str | None = None,
        request_id: str | None = None,
        generated_at_sync_ns: int | None = None,
        valid_for_ns: int | None = None,
        valid_until_sync_ns: int | None = None,
    ) -> dict[str, Any]:
        return self._runtime.build_timing_request_header(
            target_subsystem=target_subsystem,
            source_subsystem=source_subsystem,
            execute_after_ns=execute_after_ns,
            execute_at_sync_ns=execute_at_sync_ns,
            priority=priority,
            late_policy=late_policy,
            max_lateness_ns=max_lateness_ns,
            correlation_id=correlation_id,
            request_id=request_id,
            generated_at_sync_ns=generated_at_sync_ns,
            valid_for_ns=valid_for_ns,
            valid_until_sync_ns=valid_until_sync_ns,
        )

    def normalize_timing_request_header(
        self,
        header: dict[str, Any],
        *,
        accepted_at_sync_ns: int | None = None,
    ) -> tuple[dict[str, Any], int]:
        return self._runtime.normalize_timing_request_header(
            header,
            accepted_at_sync_ns=accepted_at_sync_ns,
        )

    def build_timing_response_header(
        self,
        request_header: dict[str, Any],
        execution_report: dict[str, Any],
        *,
        responded_at_sync_ns: int | None = None,
        status_code: int,
        status_message: str,
    ) -> dict[str, Any]:
        return self._runtime.build_timing_response_header(
            request_header,
            execution_report,
            responded_at_sync_ns=responded_at_sync_ns,
            status_code=status_code,
            status_message=status_message,
        )

    def pack_timing_request(
        self,
        *,
        request_header: dict[str, Any],
        payload_ref: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._runtime.pack_timing_request(
            request_header=request_header,
            payload_ref=payload_ref,
            payload=payload,
        )

    def unpack_timing_request(self, wire: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any]]:
        return self._runtime.unpack_timing_request(wire)

    def pack_timing_response(
        self,
        *,
        response_header: dict[str, Any],
        payload_ref: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._runtime.pack_timing_response(
            response_header=response_header,
            payload_ref=payload_ref,
            payload=payload,
        )

    def unpack_timing_response(self, wire: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any]]:
        return self._runtime.unpack_timing_response(wire)

    def get_control_port(self) -> int | None:
        return self._runtime.get_control_port()

    def bind_control(self, port: int | None = None, host: str = "0.0.0.0") -> int:
        self._ensure_control_role_allowed()
        return self._runtime.bind_control(port=port, host=host)

    def start_control_server(self, handler: Callable[..., dict] | None = None) -> None:
        self._ensure_control_role_allowed()
        self._runtime.start_control_server(handler=handler)

    def stop_control_server(self) -> None:
        self._runtime.stop_control_server()

    def connect_control(self, name: str, host: str, port: int) -> None:
        self._ensure_control_role_allowed()
        self._runtime.connect_control(name=name, host=host, port=port)

    def resolve_control_target(self, target: str) -> dict | None:
        return self._runtime.resolve_control_target(target)

    def handshake_control(self, target: str, timeout_ms: int = 1000) -> bool:
        return self._runtime.handshake_control(target=target, timeout_ms=timeout_ms)

    def _new_control_deadline(self, timeout_ms: float) -> float | None:
        if getattr(getattr(self._runtime, "_client", None), "_control", None) is None:
            return None
        return self._runtime._control_call_deadline(timeout_ms)

    def prepare_control(
        self, target: str, topic: str, request_capacity: int, response_capacity: int,
        timeout_ms: int = 1000, result_ready: bool = False, *,
        _deadline: float | None = None,
    ) -> bool:
        """Prepare a bounded provider-ready topic lane without callback effects."""
        if not _is_runtime_transport_topic(topic):
            self._ensure_control_role_allowed()
        return self._runtime.prepare_control(
            target, topic, request_capacity, response_capacity, timeout_ms, result_ready,
            **({"_deadline": _deadline} if _deadline is not None else {}),
        )

    def prepare_procedure(
        self, target: str, procedure: str, request_capacity: int, response_capacity: int,
        timeout_ms: int = 1000, result_ready: bool = False, *, typed: bool = True,
        _deadline: float | None = None,
    ) -> bool:
        self._ensure_gateway_endpoint_category_allowed("procedures", procedure)
        return self._runtime.prepare_procedure(
            target, procedure, request_capacity, response_capacity, timeout_ms, result_ready, typed=typed,
            **({"_deadline": _deadline} if _deadline is not None else {}),
        )

    def prepare_operation(
        self, target: str, operation: str, request_capacity: int, response_capacity: int,
        timeout_ms: int = 1000, result_ready: bool = False, *, typed: bool = True,
        _deadline: float | None = None,
    ) -> bool:
        self._ensure_gateway_endpoint_category_allowed("operations", operation)
        return self._runtime.prepare_operation(
            target, operation, request_capacity, response_capacity, timeout_ms, result_ready, typed=typed,
            **({"_deadline": _deadline} if _deadline is not None else {}),
        )

    def configure_control_worker(self, policy: str, priority: int, cpus: list[int]) -> None:
        self._runtime.configure_control_worker(policy, priority, cpus)

    def control_worker_status(self) -> list[dict]:
        return self._runtime.control_worker_status()

    def send_control(
        self,
        *,
        target: str,
        topic: str,
        type: str,
        value,
        timestamp: int,
        period_ms: float,
        deadline_ms: float,
        priority: int | None = None,
    ) -> bool:
        self._ensure_control_role_allowed()
        return self._runtime.send_control(
            target=target,
            topic=topic,
            type=type,
            value=value,
            timestamp=timestamp,
            period_ms=period_ms,
            deadline_ms=deadline_ms,
            priority=priority,
        )

    def recv_control(self, target: str, type: str, timeout_ms: int = 0) -> dict | None:
        self._ensure_control_role_allowed()
        return self._runtime.recv_control(target=target, type=type, timeout_ms=timeout_ms)

    def transact_control(
        self,
        *,
        target: str,
        topic: str,
        type: str,
        value,
        period_ms: float,
        deadline_ms: float,
        last_tick_ns: int | None = None,
        priority: int | None = None,
        request_type: str | None = None,
        response_type: str | None = None,
        timing: dict[str, Any] | None = None,
        _deadline: float | None = None,
    ) -> dict:
        deadline = _deadline if _deadline is not None else self._new_control_deadline(deadline_ms)
        self._ensure_control_role_allowed()
        return self._runtime.transact_control(
            target=target,
            topic=topic,
            type=type,
            value=value,
            period_ms=period_ms,
            deadline_ms=deadline_ms,
            last_tick_ns=last_tick_ns,
            priority=priority,
            request_type=request_type,
            response_type=response_type,
            timing=timing,
            **({"_deadline": deadline} if deadline is not None else {}),
        )

    def transact_control_result(
        self, *, target: str, topic: str, type: str, value,
        period_ms: float, deadline_ms: float,
        last_tick_ns: int | None = None, priority: int | None = None,
        request_type: str | None = None, response_type: str | None = None,
        timing: dict[str, Any] | None = None,
    ) -> dict:
        """Return copied response data before the channel's release confirmation."""
        deadline = self._new_control_deadline(deadline_ms)
        self._ensure_control_role_allowed()
        return self._runtime.transact_control_result(
            target=target, topic=topic, type=type, value=value,
            period_ms=period_ms, deadline_ms=deadline_ms,
            last_tick_ns=last_tick_ns, priority=priority,
            request_type=request_type, response_type=response_type, timing=timing,
            **({"_deadline": deadline} if deadline is not None else {}),
        )

    def wait_control_reclaimed(self, target: str, topic: str, timeout_ms: int = 1000) -> None:
        self._ensure_control_role_allowed()
        self._runtime.wait_control_reclaimed(target, topic, timeout_ms)

    def transact_bound_control(
        self,
        *,
        binding: str,
        topic: str,
        type: str,
        value,
        period_ms: float,
        deadline_ms: float,
        last_tick_ns: int | None = None,
        priority: int | None = None,
        request_type: str | None = None,
        response_type: str | None = None,
        timing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        deadline = self._new_control_deadline(deadline_ms)
        return self.transact_control(
            target=self.binding_target(binding, **({"deadline": deadline} if deadline is not None else {})),
            topic=topic,
            type=type,
            value=value,
            period_ms=period_ms,
            deadline_ms=deadline_ms,
            last_tick_ns=last_tick_ns,
            priority=priority,
            request_type=request_type,
            response_type=response_type,
            timing=timing,
            **({"_deadline": deadline} if deadline is not None else {}),
        )

    def register_typed_procedure(
        self,
        name: str,
        handler: Callable[[Any], Any],
        *,
        request_type: str,
        response_type: str,
        timing_support: str = "none",
    ) -> None:
        self._ensure_runtime_endpoint_allowed("procedures", name)
        self._runtime.register_typed_procedure(
            name=name,
            handler=handler,
            request_type=request_type,
            response_type=response_type,
            timing_support=timing_support,
        )

    def register_procedure(
        self,
        name: str,
        handler: Callable[[Any], Any],
        *,
        request_type: str | None = None,
        response_type: str | None = None,
        timing_support: str = "none",
    ) -> None:
        self._ensure_runtime_endpoint_allowed("procedures", name)
        self._runtime.register_procedure(
            name=name,
            handler=handler,
            request_type=request_type,
            response_type=response_type,
            timing_support=timing_support,
        )

    def start_procedure_server(self) -> None:
        self._runtime.start_procedure_server()

    def stop_procedure_server(self) -> None:
        self._runtime.stop_procedure_server()

    def get_procedure_info(self) -> list[str]:
        return self._runtime.get_procedure_info()

    def get_procedure_contract_info(self) -> list[tuple[str, dict[str, str | None]]]:
        return self._runtime.get_procedure_contract_info()

    def call_procedure(
        self,
        *,
        target: str,
        procedure: str,
        params: Any = None,
        timeout_ms: int = 1000,
        request_type: str | None = None,
        response_type: str | None = None,
        timing: dict[str, Any] | None = None,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        deadline = _deadline if _deadline is not None else self._new_control_deadline(timeout_ms)
        self._ensure_gateway_endpoint_category_allowed("procedures", procedure)
        return self._runtime.call_procedure(
            target=target,
            procedure=procedure,
            params=params,
            timeout_ms=timeout_ms,
            request_type=request_type,
            response_type=response_type,
            timing=timing,
            **({"_deadline": deadline} if deadline is not None else {}),
        )

    def call_bound_procedure(
        self,
        *,
        binding: str,
        procedure: str,
        params: Any = None,
        timeout_ms: int = 1000,
        request_type: str | None = None,
        response_type: str | None = None,
        timing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        deadline = self._new_control_deadline(timeout_ms)
        return self.call_procedure(
            target=self.binding_target(binding, **({"deadline": deadline} if deadline is not None else {})),
            procedure=procedure,
            params=params,
            timeout_ms=timeout_ms,
            request_type=request_type,
            response_type=response_type,
            timing=timing,
            **({"_deadline": deadline} if deadline is not None else {}),
        )

    def call_typed_procedure(
        self,
        *,
        target: str,
        procedure: str,
        request_type: str,
        response_type: str,
        params: dict[str, Any],
        timeout_ms: int = 1000,
    ) -> dict[str, Any]:
        self._ensure_gateway_endpoint_category_allowed("procedures", procedure)
        return self._runtime.call_typed_procedure(
            target=target,
            procedure=procedure,
            request_type=request_type,
            response_type=response_type,
            params=params,
            timeout_ms=timeout_ms,
        )

    def register_operation(
        self,
        name: str,
        handler: Callable[..., Any],
        *,
        request_type: str | None = None,
        feedback_type: str | None = None,
        result_type: str | None = None,
        state_type: str | None = "string",
        timing_support: str = "none",
    ) -> None:
        self._ensure_runtime_endpoint_allowed("operations", name)
        self._runtime.register_operation(
            name=name,
            handler=handler,
            request_type=request_type,
            feedback_type=feedback_type,
            result_type=result_type,
            state_type=state_type,
            timing_support=timing_support,
        )

    def start_operation_server(self) -> None:
        self._runtime.start_operation_server()

    def stop_operation_server(self) -> None:
        self._runtime.stop_operation_server()

    def get_operation_info(self) -> list[str]:
        return self._runtime.get_operation_info()

    def get_operation_contract_info(self) -> list[tuple[str, dict[str, str | None]]]:
        return self._runtime.get_operation_contract_info()

    def start_operation(
        self,
        *,
        target: str,
        operation: str,
        params: Any = None,
        timeout_ms: int | None = None,
        request_type: str | None = None,
        timing: dict[str, Any] | None = None,
        return_details: bool = False,
        _deadline: float | None = None,
    ) -> str | dict[str, Any]:
        deadline = _deadline if _deadline is not None else self._new_control_deadline(
            timeout_ms if timeout_ms is not None else 1000,
        )
        self._ensure_gateway_endpoint_category_allowed("operations", operation)
        return self._runtime.start_operation(
            target=target,
            operation=operation,
            params=params,
            timeout_ms=timeout_ms,
            request_type=request_type,
            timing=timing,
            return_details=return_details,
            **({"_deadline": deadline} if deadline is not None else {}),
        )

    def start_bound_operation(
        self,
        *,
        binding: str,
        operation: str,
        params: Any = None,
        timeout_ms: int | None = None,
        request_type: str | None = None,
        timing: dict[str, Any] | None = None,
        return_details: bool = False,
    ) -> str | dict[str, Any]:
        deadline = self._new_control_deadline(timeout_ms if timeout_ms is not None else 1000)
        return self.start_operation(
            target=self.binding_target(binding, **({"deadline": deadline} if deadline is not None else {})),
            operation=operation,
            params=params,
            timeout_ms=timeout_ms,
            request_type=request_type,
            timing=timing,
            return_details=return_details,
            **({"_deadline": deadline} if deadline is not None else {}),
        )

    def cancel_operation(
        self,
        *,
        target: str,
        operation_id: str,
        timeout_ms: int = 1000,
    ) -> dict[str, Any]:
        self._ensure_gateway_endpoint_category_allowed("operations", operation_id)
        return self._runtime.cancel_operation(
            target=target,
            operation_id=operation_id,
            timeout_ms=timeout_ms,
        )

    def query_operation(
        self,
        *,
        target: str,
        operation_id: str,
        timeout_ms: int = 1000,
    ) -> dict[str, Any]:
        self._ensure_gateway_endpoint_category_allowed("operations", operation_id)
        return self._runtime.query_operation(target=target, operation_id=operation_id, timeout_ms=timeout_ms)

    def subscribe_operation_feedback(
        self,
        *,
        target: str,
        operation: str,
        operation_id: str,
        callback: Callable[[dict[str, Any], int], None],
        size: int = 64 * 1024,
    ) -> None:
        self._ensure_gateway_endpoint_category_allowed("operations", operation)
        self._runtime.subscribe_operation_feedback(
            target=target,
            operation=operation,
            operation_id=operation_id,
            callback=callback,
            size=size,
        )

    def subscribe_operation_result(
        self,
        *,
        target: str,
        operation: str,
        operation_id: str,
        callback: Callable[[dict[str, Any], int], None],
        size: int = 64 * 1024,
    ) -> None:
        self._ensure_gateway_endpoint_category_allowed("operations", operation)
        self._runtime.subscribe_operation_result(
            target=target,
            operation=operation,
            operation_id=operation_id,
            callback=callback,
            size=size,
        )

    def subscribe_operation_state(
        self,
        *,
        target: str,
        operation: str,
        operation_id: str,
        callback: Callable[[dict[str, Any], int], None],
        size: int = 64 * 1024,
    ) -> None:
        self._ensure_gateway_endpoint_category_allowed("operations", operation)
        self._runtime.subscribe_operation_state(
            target=target,
            operation=operation,
            operation_id=operation_id,
            callback=callback,
            size=size,
        )

    def is_operation_cancel_requested(self, operation_id: str) -> bool:
        return self._runtime.is_operation_cancel_requested(operation_id)

    def publish_operation_feedback(self, *, operation_id: str, payload: Any) -> None:
        self._ensure_gateway_endpoint_category_allowed("operations", operation_id)
        self._runtime.publish_operation_feedback(operation_id=operation_id, payload=payload)

    def publish_operation_result(self, *, operation_id: str, payload: Any) -> None:
        self._ensure_gateway_endpoint_category_allowed("operations", operation_id)
        self._runtime.publish_operation_result(operation_id=operation_id, payload=payload)

    def publish_operation_state(self, *, operation_id: str, state: str, payload: Any = None) -> None:
        self._ensure_gateway_endpoint_category_allowed("operations", operation_id)
        self._runtime.publish_operation_state(operation_id=operation_id, state=state, payload=payload)

    def complete_operation(
        self,
        *,
        operation_id: str,
        state: str,
        result: Any = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self._ensure_gateway_endpoint_category_allowed("operations", operation_id)
        self._runtime.complete_operation(
            operation_id=operation_id,
            state=state,
            result=result,
            error_code=error_code,
            error_message=error_message,
        )
