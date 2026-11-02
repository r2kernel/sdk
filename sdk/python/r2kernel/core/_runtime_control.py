"""SubsystemRuntime control-plane transport methods (endpoint registration, prepare, send/recv/transact)."""

from __future__ import annotations

import math
import time
from typing import Any, Callable

from .codec_lifecycle import codec_activity
from ._runtime_common import _is_runtime_transport_topic


class _ControlTransportMixin:
    """Control endpoint, preparation and send/recv/transact methods of SubsystemRuntime."""

    def _get_control_port(self) -> int | None:
        return self._client.get_control_port()

    def get_control_port(self) -> int | None:
        return self._get_control_port()

    def _bind_control(self, port: int | None = None, host: str = "0.0.0.0") -> int:
        return self._client.bind_control(port=port, host=host)

    def bind_control(self, port: int | None = None, host: str = "0.0.0.0") -> int:
        return self._bind_control(port=port, host=host)

    def _register_control_endpoint(
        self,
        topic: str,
        handler: Callable[..., Any],
        *,
        request_type: str | None = None,
        response_type: str | None = None,
        timing_support: str = "none",
    ) -> None:
        topic_name = str(topic).strip()
        if not topic_name:
            raise ValueError("control topic is required")
        timing_support = self._normalize_timing_support(timing_support)
        self._ensure_endpoint_plane_available(topic_name, plane="control")
        if topic_name in self._raw_control_endpoint_handlers:
            raise ValueError(f"Control endpoint {topic_name!r} is already registered as raw")
        self._control_endpoint_handlers[topic_name] = handler
        if request_type is not None or response_type is not None or timing_support != "none":
            self._control_contracts[topic_name] = {
                "request_type": request_type,
                "response_type": response_type,
                "timing_support": timing_support,
            }
        else:
            self._control_contracts.pop(topic_name, None)

    def register_control_endpoint(
        self,
        topic: str,
        handler: Callable[..., Any],
        *,
        request_type: str | None = None,
        response_type: str | None = None,
        timing_support: str = "none",
    ) -> None:
        self._register_control_endpoint(
            topic,
            handler,
            request_type=request_type,
            response_type=response_type,
            timing_support=timing_support,
        )

    def register_raw_control_endpoint(self, topic: str, handler: Callable[..., dict]) -> None:
        """Register one declared private raw endpoint, preserving its full request."""
        topic_name = str(topic).strip()
        if not topic_name or not callable(handler):
            raise ValueError("raw Control topic and callable handler are required")
        if topic_name.startswith(("procedure.", "operation.")):
            raise ValueError("Raw Control endpoint conflicts with an SDK Protocol topic")
        self._codec_lifecycle.validate_raw_control_declaration(topic_name)
        self._ensure_endpoint_plane_available(topic_name, plane="control")
        if topic_name in self._control_endpoint_handlers:
            raise ValueError(f"Control endpoint {topic_name!r} is already registered as typed")
        self._raw_control_endpoint_handlers[topic_name] = handler

    def _get_control_info(self) -> list[str]:
        return sorted(self._control_endpoint_handlers.keys() | self._raw_control_endpoint_handlers.keys())

    def get_control_info(self) -> list[str]:
        return self._get_control_info()

    def _get_control_contract_info(self) -> list[tuple[str, dict[str, str | None]]]:
        return sorted([*self._control_contracts.items(),
                       *((name, {"request_type": "bytes", "response_type": "bytes", "timing_support": "none"})
                         for name in self._raw_control_endpoint_handlers)])

    def get_control_contract_info(self) -> list[tuple[str, dict[str, str | None]]]:
        return self._get_control_contract_info()

    def _start_control_server(self, handler: Callable[..., dict] | None = None) -> None:
        self._control_server_enabled = True
        if handler is not None:
            self._control_handler = handler
        if self._get_control_port() is None:
            self._bind_control(port=self._allocate_control_port(), host="0.0.0.0")
        self._ensure_control_dispatch_server()

    def start_control_server(self, handler: Callable[..., dict] | None = None) -> None:
        self._start_control_server(handler=handler)

    def _stop_control_server(self) -> None:
        self._control_handler = None
        self._control_server_enabled = False
        if not self._procedure_server_enabled and not self._operation_server_enabled:
            self._stop_control_dispatch_server()

    def stop_control_server(self) -> None:
        self._stop_control_server()

    def _connect_control(self, name: str, host: str, port: int) -> None:
        transport = getattr(self._client, "_control", None)
        if transport is not None and transport._route(name) != (str(host), int(port)):
            self._invalidate_control_target(name)
        self._client.connect_control(name=name, host=host, port=port)

    def connect_control(self, name: str, host: str, port: int) -> None:
        self._connect_control(name=name, host=host, port=port)

    def _resolve_control_target(self, target: str) -> dict[str, Any] | None:
        return self._resolve_control_target_cached(target, refresh=True)

    def resolve_control_target(self, target: str) -> dict[str, Any] | None:
        return self._resolve_control_target(target)

    def _handshake_control(self, target: str, timeout_ms: int = 1000) -> bool:
        return self._client.handshake_control(target=target, timeout_ms=timeout_ms)

    def handshake_control(self, target: str, timeout_ms: int = 1000) -> bool:
        return self._handshake_control(target=target, timeout_ms=timeout_ms)

    def _control_call_deadline(self, deadline_ms: float) -> float | None:
        if getattr(self._client, "_control", None) is None:
            return None
        if not math.isfinite(deadline_ms) or deadline_ms <= 0:
            raise ValueError("Control exchange deadline must be finite and positive")
        return time.monotonic() + deadline_ms / 1000.0

    @staticmethod
    def _check_control_deadline(deadline: float | None) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Control caller deadline exceeded")

    @staticmethod
    def _control_preparation_deadline(timeout_ms: float, deadline: float | None = None) -> float:
        if not math.isfinite(timeout_ms) or timeout_ms <= 0:
            raise ValueError("Control preparation timeout must be finite and positive")
        return deadline if deadline is not None else time.monotonic() + timeout_ms / 1000.0

    def prepare_control(
        self, target: str, topic: str, request_capacity: int, response_capacity: int,
        timeout_ms: int = 1000, result_ready: bool = False, *,
        _deadline: float | None = None,
    ) -> bool:
        """Provider-ready cold preparation within one absolute timeout budget."""
        deadline = self._control_preparation_deadline(timeout_ms, _deadline)
        route = self._prepare_control_target(target, control=topic, deadline=deadline)
        self._check_control_deadline(deadline)
        ready = self._client.prepare_control(
            target, topic, request_capacity, response_capacity, timeout_ms, result_ready,
            _deadline=deadline,
        )
        self._check_control_deadline(deadline)
        prepared = getattr(self, "_prepared_control_routes", None)
        if prepared is None:
            prepared = self._prepared_control_routes = {}
        prepared[(target, topic)] = dict(route)
        return ready

    def prepare_procedure(
        self, target: str, procedure: str, request_capacity: int, response_capacity: int,
        timeout_ms: int = 1000, result_ready: bool = False, *, typed: bool = True,
        _deadline: float | None = None,
    ) -> bool:
        deadline = self._control_preparation_deadline(timeout_ms, _deadline)
        self._prepare_control_target(target, procedure=procedure, deadline=deadline)
        return self.prepare_control(
            target, self._typed_procedure_topic(procedure) if typed else "procedure.call",
            request_capacity, response_capacity, timeout_ms, result_ready, _deadline=deadline,
        )

    def prepare_operation(
        self, target: str, operation: str, request_capacity: int, response_capacity: int,
        timeout_ms: int = 1000, result_ready: bool = False, *, typed: bool = True,
        _deadline: float | None = None,
    ) -> bool:
        deadline = self._control_preparation_deadline(timeout_ms, _deadline)
        self._prepare_control_target(target, operation=operation, deadline=deadline)
        return self.prepare_control(
            target, self._typed_operation_start_topic(operation) if typed else "operation.start",
            request_capacity, response_capacity, timeout_ms, result_ready, _deadline=deadline,
        )

    def configure_control_worker(self, policy: str, priority: int, cpus: list[int]) -> None:
        self._client.configure_control_worker(policy, priority, cpus)

    def control_worker_status(self) -> list[dict[str, Any]]:
        return self._client.control_worker_status()

    def _invalidate_control_target(self, target: str) -> None:
        self._control_target_cache.pop(target, None)
        getattr(self, "_control_target_cache_checked_at", {}).pop(target, None)
        prepared = getattr(self, "_prepared_control_routes", {})
        for key in [key for key in prepared if key[0] == target]:
            prepared.pop(key)

    def _resolve_control_target_cached(
        self, target: str, *, refresh: bool = False, deadline: float | None = None,
    ) -> dict[str, Any] | None:
        # This cache holds preparation metadata, never dispatch authority. The
        # manager checks its live registration/session/binding snapshot for every
        # packet; any failed exchange invalidates this metadata without replay.
        checked_at = getattr(self, "_control_target_cache_checked_at", None)
        if checked_at is None:
            checked_at = self._control_target_cache_checked_at = {}
        now = time.monotonic()
        self._check_control_deadline(deadline)
        cached = self._control_target_cache.get(target)
        if not refresh and cached is not None and now - checked_at.get(target, float("-inf")) < 1.0:
            return dict(cached)
        resolved = self._client.resolve_control_target(
            target, **({"deadline": deadline} if deadline is not None else {}),
        )
        self._check_control_deadline(deadline)
        if resolved is None:
            self._invalidate_control_target(target)
            return None
        snapshot = dict(resolved)
        if (snapshot.get("route_kind") == "local_control" and snapshot.get("host")
                and int(snapshot.get("control_port") or 0) == 0):
            snapshot["host"] = "local_control"
        self._control_target_cache[target] = snapshot
        checked_at[target] = now
        return dict(snapshot)

    def _control_route_is_valid(self, host: str, port: int) -> bool:
        transport = getattr(self._client, "_control", None)
        if transport is not None:
            return transport._route_is_valid(host, port)
        # Clients without a negotiated production transport cannot use the
        # relay-OFF sentinel; retain ordinary endpoint validation.
        return bool(host) and host != "local_control" and port > 0

    def _prepare_control_target(
        self,
        target: str,
        *,
        control: str | None = None,
        procedure: str | None = None,
        operation: str | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        self._check_control_deadline(deadline)
        prepared = getattr(self, "_prepared_control_routes", {})
        route = next((snapshot for key, snapshot in prepared.items() if key[0] == target), None)
        if route is None:
            route = self._resolve_control_target_cached(
                target, **({"deadline": deadline} if deadline is not None else {}),
            )
        if not route:
            raise RuntimeError(f"failed to resolve target subsystem: {target}")
        host = str(route.get("host") or "")
        port = int(route.get("control_port") or 0)
        if not self._control_route_is_valid(host, port):
            raise RuntimeError(f"target subsystem has no Control endpoint: {target}")
        if control is not None:
            controls = route.get("controls")
            if (
                isinstance(controls, list)
                and controls
                and control not in controls
                and not _is_runtime_transport_topic(control)
            ):
                raise RuntimeError(f"Control endpoint is not advertised by target: {control}")
        if procedure is not None:
            controls = route.get("controls")
            if isinstance(controls, list) and procedure in controls:
                raise RuntimeError(
                    f"'{procedure}' is advertised as a Control endpoint by target '{target}'; use the Control plane"
                )
            procedures = route.get("procedures")
            if isinstance(procedures, list) and procedures and procedure not in procedures:
                raise RuntimeError(f"procedure is not advertised by target: {procedure}")
        if operation is not None:
            operations = route.get("operations")
            if isinstance(operations, list) and operations and operation not in operations:
                raise RuntimeError(f"operation is not advertised by target: {operation}")
        self._connect_control(name=target, host=host, port=port)
        self._check_control_deadline(deadline)
        return route

    @codec_activity
    def _send_control_packet(
        self,
        *,
        target: str,
        topic: str,
        type: str,
        value: Any,
        timestamp: int,
        period_ms: float,
        deadline_ms: float,
        priority: int | None = None,
    ) -> bool:
        self._prepare_control_target(target, control=topic)
        return self._client.send_control(
            target=target,
            topic=topic,
            type=type,
            value=value,
            timestamp=timestamp,
            period_ms=period_ms,
            deadline_ms=deadline_ms,
            priority=priority,
        )

    def _recv_control_packet(self, target: str, type: str, timeout_ms: int = 0) -> dict | None:
        return self._client.recv_control(target=target, type=type, timeout_ms=timeout_ms)

    def send_control(
        self,
        *,
        target: str,
        topic: str,
        type: str,
        value: Any,
        timestamp: int,
        period_ms: float,
        deadline_ms: float,
        priority: int | None = None,
    ) -> bool:
        return self._send_control_packet(
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
        return self._recv_control_packet(target=target, type=type, timeout_ms=timeout_ms)

    @codec_activity
    def _transact_control_packet(
        self,
        *,
        target: str,
        topic: str,
        type: str,
        value: Any,
        period_ms: float,
        deadline_ms: float,
        last_tick_ns: int | None = None,
        priority: int | None = None,
        request_type: str | None = None,
        response_type: str | None = None,
        timing: dict[str, Any] | None = None,
        result_ready: bool = False,
        _deadline: float | None = None,
    ) -> dict:
        deadline = _deadline if _deadline is not None else self._control_call_deadline(deadline_ms)
        route = getattr(self, "_prepared_control_routes", {}).get((target, topic))
        if route is None:
            route = self._prepare_control_target(
                target, control=topic, **({"deadline": deadline} if deadline is not None else {}),
            )
        resolved_request_type = str(request_type or "").strip()
        resolved_response_type = str(response_type or "").strip()
        contract = {}
        control_contracts = route.get("control_contracts")
        if isinstance(control_contracts, dict):
            raw_contract = control_contracts.get(topic)
            if isinstance(raw_contract, dict):
                contract = raw_contract
        self._codec_lifecycle.validate_route(route, (resolved_request_type, resolved_response_type))
        timing_support = self._normalize_timing_support(contract.get("timing_support"))
        if timing_support == "required" and (period_ms <= 0.0 or deadline_ms <= 0.0):
            raise ValueError(
                f"Control endpoint '{topic}' on target '{target}' requires positive period_ms and deadline_ms"
            )
        if timing is not None:
            timing_options = dict(timing)
            if not resolved_request_type:
                raise ValueError("timed Control requires request_type")
            if not isinstance(value, dict):
                raise ValueError("timed Control requires a dict payload")
            return self._transact_control(
                target=target,
                topic=topic,
                request_type=resolved_request_type,
                response_type=resolved_response_type or None,
                payload=dict(value),
                execute_after_ns=timing_options.get("execute_after_ns"),
                execute_at_sync_ns=timing_options.get("execute_at_sync_ns"),
                priority=priority if priority is not None else timing_options.get("priority", 128),
                late_policy=timing_options.get("late_policy", "execute_immediately"),
                max_lateness_ns=timing_options.get("max_lateness_ns", 1_000_000),
                period_ms=period_ms,
                deadline_ms=deadline_ms,
                last_tick_ns=last_tick_ns,
                correlation_id=timing_options.get("correlation_id"),
                request_id=timing_options.get("request_id"),
                generated_at_sync_ns=timing_options.get("generated_at_sync_ns"),
                valid_for_ns=timing_options.get("valid_for_ns"),
                valid_until_sync_ns=timing_options.get("valid_until_sync_ns"),
                details=bool(timing_options.get("details", False)),
                decode_response=bool(timing_options.get("decode_response", True)),
                result_ready=result_ready,
                **({"_deadline": deadline} if deadline is not None else {}),
            )
        if resolved_request_type or resolved_response_type:
            if not resolved_request_type or not resolved_response_type:
                raise ValueError("typed Control requires both request_type and response_type")
            if not isinstance(value, dict):
                raise ValueError("typed Control requires a dict payload")
            return self._transact_typed_control(
                target=target,
                topic=topic,
                request_type=resolved_request_type,
                response_type=resolved_response_type,
                payload=dict(value),
                period_ms=period_ms,
                deadline_ms=deadline_ms,
                last_tick_ns=last_tick_ns,
                priority=priority,
                result_ready=result_ready,
                **({"_deadline": deadline} if deadline is not None else {}),
            )
        return (self._client.transact_control_result if result_ready
         else self._client.transact_control)(
            target=target,
            topic=topic,
            type=type,
            value=value,
            period_ms=period_ms,
            deadline_ms=deadline_ms,
            last_tick_ns=last_tick_ns,
            priority=priority,
            **({"_deadline": deadline} if deadline is not None else {}),
        )

    def transact_control(
        self,
        *,
        target: str,
        topic: str,
        type: str,
        value: Any,
        period_ms: float,
        deadline_ms: float,
        last_tick_ns: int | None = None,
        priority: int | None = None,
        request_type: str | None = None,
        response_type: str | None = None,
        timing: dict[str, Any] | None = None,
        _deadline: float | None = None,
    ) -> dict:
        deadline = _deadline if _deadline is not None else self._control_call_deadline(deadline_ms)
        return self._transact_control_packet(
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
        self, *, target: str, topic: str, type: str, value: Any,
        period_ms: float, deadline_ms: float,
        last_tick_ns: int | None = None, priority: int | None = None,
        request_type: str | None = None, response_type: str | None = None,
        timing: dict[str, Any] | None = None,
        _deadline: float | None = None,
    ) -> dict:
        deadline = _deadline if _deadline is not None else self._control_call_deadline(deadline_ms)
        return self._transact_control_packet(
            target=target, topic=topic, type=type, value=value,
            period_ms=period_ms, deadline_ms=deadline_ms,
            last_tick_ns=last_tick_ns, priority=priority,
            request_type=request_type, response_type=response_type,
            timing=timing, result_ready=True,
            **({"_deadline": deadline} if deadline is not None else {}),
        )

    def wait_control_reclaimed(self, target: str, topic: str, timeout_ms: int = 1000) -> None:
        self._client.wait_control_reclaimed(target, topic, timeout_ms)
