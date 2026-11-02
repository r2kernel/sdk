"""NativeControlClient raw send/recv/transact message path."""

from __future__ import annotations

import math
import socket
import time
import threading
from typing import Any

from ._control_shm import ControlShmProtocolError


class _MessagingMixin:
    """Raw send/recv/transact message path of NativeControlClient."""

    def _pending_target_state(self) -> tuple[threading.Lock, set[str]]:
        pending_guard = getattr(self, "_pending_target_guard", None)
        if pending_guard is None:
            pending_guard = threading.Lock()
            self._pending_target_guard = pending_guard
        reservations = getattr(self, "_pending_target_reservations", None)
        if reservations is None:
            reservations = set()
            self._pending_target_reservations = reservations
        return pending_guard, reservations

    def _send_raw(
        self,
        *,
        target: str,
        topic: str,
        payload: bytes,
        timestamp: int,
        period_ms: float,
        deadline_ms: float,
        priority: int | None = None,
        cost_context: dict[str, Any] | None = None,
    ) -> bool:
        if not self._managed_control_ready():
            return False
        if not self._ready.get(target, False):
            return False
        pending_guard, reservations = self._pending_target_state()
        stored_response = False
        with pending_guard:
            if target in reservations or target in self._pending_responses:
                raise ControlShmProtocolError(
                    f"a local Control v5 response for target {target!r} is still pending"
                )
            reservations.add(target)
        try:
            response = self._managed_transact_request(
                target=target,
                topic=topic,
                payload=payload,
                timestamp=timestamp,
                period_ms=period_ms,
                deadline_ms=deadline_ms,
                priority=priority,
            )
            with pending_guard:
                if response is None:
                    self._ready[target] = False
                    return False
                self._pending_responses[target] = response
                if cost_context is not None:
                    self._pending_cost_context[target] = dict(cost_context)
                self._ready[target] = True
                stored_response = True
            return True
        finally:
            if not stored_response:
                with pending_guard:
                    reservations.discard(target)

    def send(
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
        try:
            encoder = self._get_encoder(type)
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Typed Control requires r2kernel.datatype in the r2kernel-client package") from exc
        cost_enabled = self._control_cost_metrics.enabled
        encode_started = time.perf_counter_ns() if cost_enabled else 0
        payload = encoder(value) if encoder else value
        encode_ns = time.perf_counter_ns() - encode_started if cost_enabled else None
        sent = self._send_raw(
            target=target,
            topic=topic,
            payload=payload,
            timestamp=timestamp,
            period_ms=period_ms,
            deadline_ms=deadline_ms,
            priority=priority,
            cost_context=(
                {"topic": topic, "request_timestamp_ns": int(timestamp)}
                if cost_enabled
                else None
            ),
        )
        if cost_enabled:
            self._control_cost_metrics.record({
                "phase": "python_codec_encode",
                "target": target,
                "topic": topic,
                "request_timestamp_ns": int(timestamp),
                "payload_bytes": len(payload),
                "python_encode_ns": encode_ns,
                "outcome": "ok" if sent else "error",
            })
        return sent

    def _recv_raw(self, target: str, timeout_ms: int = 0) -> dict | None:
        _ = timeout_ms
        return self._take_pending_response(target)[0]

    def _take_pending_response(self, target: str) -> tuple[dict | None, dict[str, Any]]:
        pending_guard, reservations = self._pending_target_state()
        with pending_guard:
            response = self._pending_responses.pop(target, None)
            if response is None:
                return None, {}
            cost_context = self._pending_cost_context.pop(target, {})
            reservations.discard(target)
            return response, cost_context

    def recv(self, target: str, type: str, timeout_ms: int = 0) -> dict | None:
        _ = timeout_ms
        resp, cost_context = self._take_pending_response(target)
        if not resp:
            return None
        payload = resp.get("payload")
        if payload:
            try:
                decoder = self._get_decoder(type)
                decode_started = time.perf_counter_ns() if self._control_cost_metrics.enabled else 0
                resp["payload"] = decoder(payload) if decoder else payload
                if self._control_cost_metrics.enabled:
                    self._control_cost_metrics.record({
                        "phase": "python_codec_decode",
                        "target": target,
                        "topic": cost_context.get("topic"),
                        "request_timestamp_ns": cost_context.get("request_timestamp_ns"),
                        "payload_bytes": len(payload),
                        "python_decode_ns": time.perf_counter_ns() - decode_started,
                        "outcome": "ok",
                    })
            except Exception:
                resp["payload"] = None
                if self._control_cost_metrics.enabled:
                    self._control_cost_metrics.record({
                        "phase": "python_codec_decode",
                        "target": target,
                        "topic": cost_context.get("topic"),
                        "request_timestamp_ns": cost_context.get("request_timestamp_ns"),
                        "payload_bytes": len(payload),
                        "outcome": "error",
                    })
        return resp

    def _transact_raw(
        self,
        *,
        target: str,
        topic: str,
        payload: bytes,
        period_ms: float,
        deadline_ms: float,
        last_tick_ns: int | None = None,
        priority: int | None = None,
        cost_context: dict[str, Any] | None = None,
        result_ready: bool = False,
        _deadline: float | None = None,
    ) -> dict:
        if _deadline is None:
            if not math.isfinite(deadline_ms) or deadline_ms <= 0:
                raise ValueError("Control exchange deadline must be finite and positive")
            _deadline = time.monotonic() + deadline_ms / 1000.0
        # last_tick_ns is supplied in the R2Kernel synchronized clock domain
        # (Subsystem.now_ns()), so overrun checks must use that same domain.
        now_ns = self._now_ns()
        overrun = False
        if last_tick_ns is not None:
            period_ns = int(max(period_ms, 1.0) * 1_000_000)
            if now_ns - last_tick_ns > period_ns:
                overrun = True
        send_ts = self._now_ns()
        if cost_context is not None:
            cost_context["request_timestamp_ns"] = int(send_ts)
        resp = self._managed_transact_request(
            target=target,
            topic=topic,
            payload=payload,
            timestamp=send_ts,
            period_ms=period_ms,
            deadline_ms=deadline_ms,
            priority=priority,
            _deadline=_deadline,
            **({"result_ready": True} if result_ready else {}),
        )
        if not resp:
            self._ready[target] = False
            send_error = getattr(self, "_last_transport_error", None) or "send_failed"
            return {"ok": False, "overrun": overrun, "timeout": True, "error": send_error}
        managed_rtt_ns = resp.get("rtt_ns")
        managed_rtt_scope = resp.get("rtt_measurement_scope")
        status = int(resp.get("status") or 0)
        if status != 0:
            result = {
                "ok": False,
                "overrun": overrun,
                "timeout": False,
                "error": "control_channel_status",
                "status": status,
                "response": resp,
            }
            if isinstance(managed_rtt_ns, int) and managed_rtt_ns >= 0:
                result["rtt_ns"] = managed_rtt_ns
                result["rtt_ms"] = managed_rtt_ns / 1_000_000.0
                result["rtt_measurement_scope"] = managed_rtt_scope
            return result

        recv_ts = self._now_ns()
        rtt_ns = (
            int(managed_rtt_ns)
            if isinstance(managed_rtt_ns, int) and managed_rtt_ns >= 0
            else recv_ts - send_ts
        )
        if not self._ready.get(target, False):
            self._ready[target] = True
        result = {
            "ok": True,
            "overrun": overrun,
            "timeout": False,
            "rtt_ns": rtt_ns,
            "rtt_ms": rtt_ns / 1_000_000.0,
            "response": resp,
        }
        if isinstance(managed_rtt_ns, int) and managed_rtt_ns >= 0:
            result["rtt_measurement_scope"] = managed_rtt_scope
        return result

    def transact(
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
        result_ready: bool = False,
        _deadline: float | None = None,
    ) -> dict:
        """Send one typed Control exchange.

        ``last_tick_ns`` must be a timestamp from the R2Kernel synchronized
        clock, such as ``Subsystem.now_ns()``. It is compared with this
        client's synchronized clock when determining whether the period was
        exceeded.
        """
        if _deadline is None:
            if not math.isfinite(deadline_ms) or deadline_ms <= 0:
                raise ValueError("Control exchange deadline must be finite and positive")
            _deadline = time.monotonic() + deadline_ms / 1000.0
        try:
            encoder = self._get_encoder(type)
            decoder = self._get_decoder(type)
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Typed Control requires r2kernel.datatype in the r2kernel-client package") from exc
        cost_enabled = self._control_cost_metrics.enabled
        codec_metrics: dict[str, int] | None = {} if cost_enabled else None
        encode_started = time.perf_counter_ns() if cost_enabled else 0
        payload = encoder(value) if encoder else value
        request_payload_bytes = len(payload)
        if cost_enabled:
            codec_metrics["python_encode_ns"] = time.perf_counter_ns() - encode_started
        cost_context: dict[str, Any] | None = {"topic": topic} if cost_enabled else None
        result = self._transact_raw(
            target=target,
            topic=topic,
            payload=payload,
            period_ms=period_ms,
            deadline_ms=deadline_ms,
            last_tick_ns=last_tick_ns,
            priority=priority,
            cost_context=cost_context,
            **({"_deadline": _deadline} if _deadline is not None else {}),
            **({"result_ready": True} if result_ready else {}),
        )
        if cost_enabled:
            self._pending_cost_context.pop(target, None)
        decode_ns = None
        if not result.get("ok"):
            if cost_enabled:
                remote_status_error = result.get("error") == "control_channel_status"
                self._control_cost_metrics.record({
                    "phase": "python_codec_transact",
                    "target": target,
                    "topic": topic,
                    "request_timestamp_ns": cost_context.get("request_timestamp_ns") if cost_context else None,
                    "payload_bytes": request_payload_bytes,
                    "response_status": result.get("status") if remote_status_error else None,
                    "outcome": "remote_status_error" if remote_status_error else "transport_error",
                    **(codec_metrics or {}),
                })
            return result
        resp = result.get("response") or {}
        payload = resp.get("payload")
        response_payload_bytes = len(payload) if payload else 0
        codec_outcome = "ok"
        if payload:
            decode_started = time.perf_counter_ns() if cost_enabled else 0
            try:
                resp["payload"] = decoder(payload) if decoder else payload
                if cost_enabled:
                    decode_ns = time.perf_counter_ns() - decode_started
            except Exception:
                resp["payload"] = None
                codec_outcome = "decode_error"
                if cost_enabled:
                    decode_ns = time.perf_counter_ns() - decode_started
        if cost_enabled:
            self._control_cost_metrics.record({
                "phase": "python_codec_transact",
                "target": target,
                "topic": topic,
                "request_timestamp_ns": cost_context.get("request_timestamp_ns") if cost_context else None,
                "payload_bytes": request_payload_bytes,
                "response_bytes": response_payload_bytes,
                "python_decode_ns": decode_ns,
                "outcome": codec_outcome,
                **(codec_metrics or {}),
            })
        result["response"] = resp
        if _deadline is not None and time.monotonic() >= _deadline:
            raise socket.timeout("Control caller deadline exceeded after response decoding")
        return result
