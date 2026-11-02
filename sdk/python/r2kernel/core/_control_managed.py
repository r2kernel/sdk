"""NativeControlClient managed Control v5 path: route checks, preparation and transactions."""

from __future__ import annotations

import math
import os
import socket
import time
from typing import Any

from ._control_shm import (
    ControlShmProtocolError,
    decode_handshake_response as decode_shm_handshake_response,
    decode_prepare_response as decode_shm_prepare_response,
    decode_transact_response as decode_shm_transact_response,
    encode_handshake_request as encode_shm_handshake_request,
    encode_prepare_request as encode_shm_prepare_request,
    encode_transact_request as encode_shm_transact_request,
)
from . import _notify_endpoints
from ._control_shm_transport import await_control_shm_peer_close
from .control_client import exchange_control_request
from .control_source_topic import encode_source_topic


def default_control_priority(topic: str, explicit_priority: int | None) -> int:
    if explicit_priority is not None:
        return max(0, min(255, int(explicit_priority)))
    return {
        "control_hello": 255,
        "operation.cancel": 192,
        "operation.start": 192,
        "operation.query": 144,
        "procedure.call": 128,
    }.get(topic, 128)


class _ManagedControlMixin:
    """Managed Control v5 client path (route checks, preparation, SHM transactions) of NativeControlClient."""

    def _route_is_valid(self, host: str, port: int) -> bool:
        if host == "local_control":
            return bool(
                port == 0
                and getattr(self, "_control_shm_negotiated", False)
                and getattr(self, "_local_control_endpoint", None)
            )
        return bool(host) and port > 0

    def _route(self, target: str) -> tuple[str, int] | None:
        route = self._routes.get(target)
        return route if route is not None and self._route_is_valid(*route) else None

    def _managed_handshake(self, target: str, timeout_ms: int) -> bool:
        """Route check only; this does not prepare a provider lane."""
        self._last_transport_error = None
        self._require_control_shm_negotiated()
        endpoint = self._local_control_endpoint
        if self._route(target) is None:
            return False
        if not endpoint:
            raise ControlShmProtocolError("local Control v5 endpoint is not configured")
        if timeout_ms <= 0:
            raise ValueError("Control handshake timeout must be positive")
        deadline = time.monotonic() + timeout_ms / 1000.0
        channel, (host, port) = self._control_shm_channel_registry.get_route(
            endpoint, target, "", lambda: self._route(target),
        )
        response = channel.exchange(
            request_builder=lambda _lease: encode_shm_handshake_request(
                target, host, port, max(1, math.ceil((deadline - time.monotonic()) * 1000)),
            ),
            response_decoder=decode_shm_handshake_response,
            timeout_s=timeout_ms / 1000.0, deadline=deadline,
        )
        self._ready[target] = bool(response["ok"])
        self._last_transport_error = None if response["ok"] else str(response.get("error") or "handshake_failed")
        return bool(response["ok"])

    def prepare_control(
        self, target: str, topic: str, request_capacity: int, response_capacity: int,
        timeout_ms: int = 1000, result_ready: bool = False, *,
        _deadline: float | None = None,
    ) -> bool:
        """Prepare a fresh topic lane without invoking the provider callback."""
        self._require_control_shm_negotiated()
        if timeout_ms <= 0:
            raise ValueError("Control preparation timeout must be positive")
        for capacity in (request_capacity, response_capacity):
            if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
                raise ValueError("Control payload capacities must be nonnegative integers")
        deadline = _deadline if _deadline is not None else time.monotonic() + timeout_ms / 1000.0
        endpoint = self._local_control_endpoint
        if not endpoint:
            raise ControlShmProtocolError("local Control v5 endpoint is not configured")
        registry = self._control_shm_channel_registry
        ticket = registry.begin_preparation(endpoint, target, topic, lambda: self._route(target))
        (host, port), channel = ticket
        try:
            response = channel.exchange(
                request_builder=lambda lease: encode_shm_prepare_request(
                    target, host, port, encode_source_topic(self._identity, topic),
                    max(1, math.ceil(channel._remaining_timeout(deadline) * 1000)),
                    request_capacity, response_capacity, lease,
                ),
                response_decoder=decode_shm_prepare_response,
                timeout_s=timeout_ms / 1000.0, deadline=deadline,
                preparation=(request_capacity, response_capacity),
                response_has_payload=lambda decoded: bool(decoded["ok"]),
                result_ready=result_ready,
            )
            if not response["ok"]:
                raise ControlShmProtocolError(str(response.get("error") or "Control preparation failed"))
        except Exception:
            registry.finish_preparation(
                endpoint, target, topic, ticket, success=False,
                route_provider=lambda: self._route(target),
                update_ready=lambda ready: self._ready.__setitem__(target, ready),
            )
            raise
        registry.finish_preparation(
            endpoint, target, topic, ticket, success=True,
            route_provider=lambda: self._route(target),
            update_ready=lambda ready: self._ready.__setitem__(target, ready),
        )
        channel.preparation_parameters = (request_capacity, response_capacity, bool(result_ready), int(timeout_ms))
        return True

    def _recover_prepared_lane(self, target: str, topic: str, channel: Any, retired: socket.socket) -> None:
        """Cold: re-prepare a prepared lane after one late or peer-closed exchange.

        The failed exchange is not replayed and its exception is still raised.
        Wait until Control has finished that transaction and closed the old
        connection, then prepare with the lane's original capacities, completion
        mode and timeout, which also bounds the whole recovery. A failure leaves
        the lane invalid until an explicit rebind/preparation, as before.
        """
        counters = self._control_shm_channel_registry.counters
        parameters = channel.preparation_parameters
        try:
            if parameters is None:
                raise ControlShmProtocolError("prepared lane has no recorded preparation contract")
            request_capacity, response_capacity, result_ready, timeout_ms = parameters
            deadline = time.monotonic() + timeout_ms / 1000.0
            try:
                await_control_shm_peer_close(retired, deadline)
            finally:
                retired.close()
            self.prepare_control(target, topic, request_capacity, response_capacity, timeout_ms,
                                 result_ready, _deadline=deadline)
            counters.increment("prepared_lane_recoveries")
        except Exception as error:
            retired.close()
            counters.increment("prepared_lane_recovery_failures")
            self._last_transport_error = (
                f"{self._last_transport_error}; prepared lane re-preparation failed: {error}"
            )

    def _managed_transact_request(
        self,
        *,
        target: str,
        topic: str,
        payload: bytes,
        timestamp: int,
        period_ms: float,
        deadline_ms: float,
        priority: int | None = None,
        result_ready: bool = False,
        _deadline: float | None = None,
    ) -> dict[str, Any] | None:
        if not math.isfinite(deadline_ms) or deadline_ms <= 0:
            raise ValueError("Control exchange deadline must be finite and positive")
        deadline = _deadline if _deadline is not None else time.monotonic() + deadline_ms / 1000.0
        self._last_transport_error = None
        route = self._route(target)
        if route is None:
            return None
        self._require_control_shm_negotiated()
        endpoint = self._local_control_endpoint
        if not endpoint:
            raise ControlShmProtocolError("local Control v5 endpoint is not configured")
        request_payload = bytes(payload)
        if len(request_payload) > 16 * 1024 * 1024:
            raise ValueError("local Control payload exceeds the 16 MiB SDK limit")
        managed_topic = encode_source_topic(self._identity, topic)
        bounded_timeout_ms = max(1, math.ceil(deadline_ms))
        channel, (host, port) = self._control_shm_channel_registry.get_route(
            endpoint, target, topic, lambda: self._route(target)
        )
        call_started = time.perf_counter_ns()
        try:
            response = channel.exchange(
                request_builder=lambda lease: encode_shm_transact_request(
                    target=target,
                    host=host,
                    port=int(port),
                    topic=managed_topic,
                    timestamp_ns=int(timestamp),
                    period_ms=float(period_ms),
                    deadline_ms=float(deadline_ms),
                    timeout_ms=bounded_timeout_ms,
                    priority=default_control_priority(topic, priority),
                    payload_lease=lease if isinstance(lease, dict) else {},
                    remaining_ns=max(1, int(channel._remaining_timeout(deadline) * 1_000_000_000)),
                ),
                response_decoder=decode_shm_transact_response,
                timeout_s=bounded_timeout_ms / 1000.0,
                request_payload=request_payload,
                request_timestamp_ns=int(timestamp),
                response_has_payload=lambda decoded: bool(decoded.get("ok")),
                response_ticket_required=True,
                result_ready=result_ready,
                deadline=deadline,
            )
        except Exception as exc:
            self._last_transport_error = str(exc) or "local_control_shm_v5_transact_error"
            if channel.is_closed and not channel.has_reclamation_error:
                self._control_shm_channel_registry.remove(endpoint, target, topic, channel)
                self._ready[target] = False
                retired = channel.take_retired_socket(exc)
                if retired is not None:
                    self._recover_prepared_lane(target, topic, channel, retired)
            raise
        if not response["ok"]:
            self._control_shm_channel_registry.remove(endpoint, target, topic, channel)
            self._ready[target] = False
            self._last_transport_error = str(response.get("error") or "local_control_shm_v5_transact_failed")
            return None
        result = {
            "ts": int(response["timestamp_ns"]),
            "status": int(response["status"]),
            "payload": bytes(response.get("payload") or b""),
            "rtt_ns": time.perf_counter_ns() - call_started,
            "rtt_measurement_scope": (
                "python_managed_control_shm_v5_response_ready_wall_ns" if result_ready
                else "python_managed_control_shm_v5_wall_ns_including_socket_io_and_mapping_copy"
            ),
        }
        if result_ready:
            result["completion"] = "response_ready"
        self._last_transport_error = None if result["status"] == 0 else "control_channel_status"
        return result

    def wait_control_reclaimed(self, target: str, topic: str, timeout_ms: int = 1000) -> None:
        if timeout_ms <= 0:
            raise ValueError("reclamation timeout must be positive")
        endpoint = self._local_control_endpoint
        channel = self._control_shm_channel_registry.find(endpoint, target, topic)
        if channel is None:
            return
        try:
            channel.wait_reclaimed(timeout_ms / 1000.0)
        except Exception as exc:
            self._last_transport_error = str(exc)
            if channel.is_closed:
                self._ready[target] = False
            raise

    def transact_result_ready(
        self, *, target: str, topic: str, type: str, value,
        period_ms: float, deadline_ms: float,
        last_tick_ns: int | None = None, priority: int | None = None,
        _deadline: float | None = None,
    ) -> dict:
        """Return an owned response; a persistent lane worker completes reclamation."""
        return self.transact(
            target=target, topic=topic, type=type, value=value,
            period_ms=period_ms, deadline_ms=deadline_ms,
            last_tick_ns=last_tick_ns, priority=priority, result_ready=True,
            _deadline=_deadline,
        )

    def _make_local_endpoint_name(self) -> str:
        identity = self._identity or "default"
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in identity)
        return f"/tmp/r2kernel-control-handler-{safe}-{os.getpid()}.sock"

    def _new_local_endpoint(self) -> None:
        self._local_endpoint = self._make_local_endpoint_name()
        self._local_endpoint_pid = os.getpid()

    def _cleanup_local_endpoint(self) -> None:
        if self._local_endpoint:
            _notify_endpoints.unregister(self._local_endpoint)
            # A forked child that stops or closes an inherited client leaves the parent's socket in place.
            if getattr(self, "_local_endpoint_pid", None) == os.getpid():
                try:
                    os.unlink(self._local_endpoint)
                except FileNotFoundError:
                    pass
            self._local_endpoint = None

    def _exchange_control_request(
        self,
        payload: dict[str, Any],
        *,
        timeout_s: float = 1.0,
        retries: int = 3,
        retry_delay_s: float = 0.05,
    ) -> dict[str, Any] | None:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return exchange_control_request(
                    payload,
                    timeout_s=timeout_s,
                    retries=1,
                    retry_delay_s=retry_delay_s,
                )
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    time.sleep(max(0.0, retry_delay_s))
        if last_exc is not None:
            raise last_exc
        return None
