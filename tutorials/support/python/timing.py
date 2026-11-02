from __future__ import annotations

import json
import time
from typing import Any

from public_sdk import ensure_repo_python_sdk, python_tutorial_environment_message

ensure_repo_python_sdk()

try:
    from r2kernel.core.clock import clock_now_ns
    from r2kernel.interface import RustMessageCodec
except ModuleNotFoundError as exc:
    raise SystemExit(python_tutorial_environment_message(exc.name))

GATEWAY_DISPATCH_REPORT_REF = "msg.internal.control.v1/GatewayDispatchReport"


def sync_now_ns() -> int:
    # Timed request headers are normalized against the control/runtime sync clock.
    # The benchmark model must use the same absolute clock domain, not monotonic uptime.
    return clock_now_ns()


class TimingDemoCodec(RustMessageCodec):
    pass


def _resolve_timing_fields(request_like: Any) -> tuple[int | None, int | None, str, int | None]:
    candidate = request_like
    if not isinstance(candidate, dict) and hasattr(candidate, "request_context"):
        nested = getattr(candidate, "request_context")
        if nested is not None:
            candidate = nested
    if isinstance(candidate, dict):
        execution = candidate.get("execution") if isinstance(candidate.get("execution"), dict) else {}
        return (
            int(candidate.get("sent_at_sync_ns")) if candidate.get("sent_at_sync_ns") is not None else None,
            int(execution.get("execute_at_sync_ns")) if execution.get("execute_at_sync_ns") is not None else None,
            str(execution.get("late_policy") or "execute_immediately"),
            int(execution.get("max_lateness_ns")) if execution.get("max_lateness_ns") is not None else None,
        )
    sent_at_sync_ns = getattr(candidate, "sent_at_sync_ns", None)
    execute_at_sync_ns = getattr(candidate, "execute_at_sync_ns", None)
    late_policy = getattr(candidate, "late_policy", None)
    max_lateness_ns = getattr(candidate, "max_lateness_ns", None)
    return (
        None if sent_at_sync_ns is None else int(sent_at_sync_ns),
        None if execute_at_sync_ns is None else int(execute_at_sync_ns),
        str(late_policy or "execute_immediately"),
        None if max_lateness_ns is None else int(max_lateness_ns),
    )



def sleep_until_sync_ns(target_sync_ns: int, spin_threshold_ns: int = 200_000) -> int:
    while True:
        now = sync_now_ns()
        remaining_ns = target_sync_ns - now
        if remaining_ns <= 0:
            return now
        if remaining_ns > spin_threshold_ns:
            time.sleep(max((remaining_ns - spin_threshold_ns) / 1_000_000_000.0, 0.0))
            continue



def simulate_gateway_dispatch(
    request_like: Any,
    *,
    transport_latency_ns: int = 0,
    processing_delay_ns: int = 200_000,
) -> dict[str, Any]:
    if transport_latency_ns > 0:
        time.sleep(transport_latency_ns / 1_000_000_000.0)
    received_at_sync_ns = sync_now_ns()
    _, execute_at_sync_ns, late_policy, max_lateness_ns = _resolve_timing_fields(request_like)
    scheduled_for_sync_ns = int(execute_at_sync_ns if execute_at_sync_ns is not None else received_at_sync_ns)

    if scheduled_for_sync_ns > received_at_sync_ns:
        dispatched_at_sync_ns = sleep_until_sync_ns(scheduled_for_sync_ns)
    else:
        dispatched_at_sync_ns = received_at_sync_ns
    lateness_ns = max(0, dispatched_at_sync_ns - scheduled_for_sync_ns)

    final_policy = "scheduled"
    final_status = "executed"
    completed_at_sync_ns = dispatched_at_sync_ns
    should_execute = True

    if max_lateness_ns is not None and lateness_ns > max_lateness_ns:
        if late_policy == "drop":
            final_policy = "drop"
            final_status = "dropped"
            should_execute = False
        elif late_policy == "reject":
            final_policy = "reject"
            final_status = "rejected"
            should_execute = False
        elif late_policy == "execute_immediately":
            final_policy = "execute_immediately"
            final_status = "executed_late"
        else:
            # A name outside the late policies is refused, never executed late.
            final_policy = "reject"
            final_status = "rejected"
            should_execute = False

    if should_execute and processing_delay_ns > 0:
        time.sleep(processing_delay_ns / 1_000_000_000.0)
        completed_at_sync_ns = sync_now_ns()

    return {
        "accepted_at_sync_ns": received_at_sync_ns,
        "scheduled_for_sync_ns": scheduled_for_sync_ns,
        "dispatched_at_sync_ns": dispatched_at_sync_ns,
        "completed_at_sync_ns": completed_at_sync_ns,
        "lateness_ns": lateness_ns,
        "final_policy": final_policy,
        "final_status": final_status,
    }



def encode_roundtrip(codec: RustMessageCodec, message_ref: str, payload: dict[str, Any]) -> dict[str, Any]:
    encoded = codec.encode_message(message_ref, payload)
    decoded = codec.decode_message(message_ref, encoded)
    return {
        "message_ref": message_ref,
        "schema": codec.get_message_schema(message_ref),
        "value": payload,
        "encoded_hex": encoded.hex(),
        "roundtrip": decoded,
    }



def encode_gateway_dispatch_report(
    codec: RustMessageCodec,
    request_header: dict[str, Any],
    execution_report: dict[str, Any],
    *,
    gateway_id: str,
) -> dict[str, Any]:
    payload = {
        "request_header": request_header,
        "execution_report": execution_report,
        "gateway_id": gateway_id,
    }
    return encode_roundtrip(codec, GATEWAY_DISPATCH_REPORT_REF, payload)


def dump_json(document: dict[str, Any]) -> None:
    print(json.dumps(document, indent=2, sort_keys=True))
