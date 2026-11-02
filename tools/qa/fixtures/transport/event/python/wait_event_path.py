from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any

def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "tutorials" / "support" / "python").is_dir():
            return parent
    raise RuntimeError(f"could not resolve repository root from {__file__}")


_SUPPORT_PY = repo_root() / "tutorials" / "support" / "python"
if str(_SUPPORT_PY) not in sys.path:
    sys.path.insert(0, str(_SUPPORT_PY))

from benchmark import topic_name
from control_status import credential_headers as _credential_headers
from control_status import fetch_json as _status_json
from control_status import topic_set as _topic_set
from control_status import validate_status_url as _validate_status_url
from start_gate import StartGate


def _runtime(status: dict[str, Any]) -> dict[str, Any]:
    stamped_stream_runtime = status.get("stamped_stream_runtime_status") or {}
    runtime = stamped_stream_runtime.get("runtime") or {}
    return runtime if isinstance(runtime, dict) else {}


def _data_plane_report(status: dict[str, Any]) -> dict[str, Any]:
    applied = _runtime(status).get("applied") or {}
    if not isinstance(applied, dict):
        return {}
    data_plane = applied.get("data_plane") or {}
    return data_plane if isinstance(data_plane, dict) else {}


def _transport_peer_check(label: str, status: dict[str, Any], peer_status: dict[str, Any]) -> dict[str, Any]:
    peer_runtime = peer_status.get("stamped_stream_runtime_status")
    peer_id = peer_runtime.get("local_host") if isinstance(peer_runtime, dict) else None
    if not isinstance(peer_id, str) or not peer_id:
        peer_id = None
    applied = _runtime(status).get("applied")
    if not isinstance(applied, dict):
        applied = {}
    check: dict[str, Any] = {"label": label, "peer_id": peer_id}
    # These are native runtime observations, not the desired connections plan.
    for field in ("active_peers", "connected", "subscriptions_synced"):
        peers = applied.get(field)
        check[field] = peer_id is not None and isinstance(peers, list) and peer_id in peers
    # connected is a per-apply transition, not established-peer membership.
    # subscriptions_synced lists peers that acknowledged this host's latest
    # subscription request on the current native session.
    check["ready"] = all(check[field] for field in ("active_peers", "subscriptions_synced"))
    check["reason"] = "" if check["ready"] else _transport_not_ready_reason(check, applied)
    return check


def _peer_listed(applied: dict[str, Any], field: str, peer_id: str | None) -> bool:
    peers = applied.get(field)
    return peer_id is not None and isinstance(peers, list) and peer_id in peers


def _subscription_ack_pending(applied: dict[str, Any], peer_id: str | None) -> dict[str, Any] | None:
    entries = applied.get("subscription_ack_pending")
    if peer_id is None or not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("peer_id") == peer_id:
            return entry
    return None


def _transport_not_ready_reason(check: dict[str, Any], applied: dict[str, Any]) -> str:
    """Why the native peer is not ready, from the runtime.applied diagnostics (most specific first)."""
    peer_id = check["peer_id"]
    if peer_id is None:
        return "peer status does not report its local_host"
    if _peer_listed(applied, "security_mode_incompatible", peer_id):
        return "security_mode_incompatible: the peer's Event transport security mode cannot interoperate with this host"
    pending = _subscription_ack_pending(applied, peer_id)
    if pending is not None:
        check["subscription_ack_pending"] = {key: pending.get(key) for key in ("generation", "pending_ms")}
        return (f"subscription_ack_pending: the peer has not acknowledged subscription generation "
                f"{pending.get('generation')} (pending {pending.get('pending_ms')} ms)")
    if not check["active_peers"]:
        return "peer is not an active native session"
    return "peer has not acknowledged this host's subscriptions"


def _local_subscription_topics(status: dict[str, Any]) -> set[str]:
    # Topic-typed field only: applied.subscriptions_synced holds peer ids and is
    # read by _transport_peer_check.
    return _topic_set(_runtime(status).get("local_subscription_topics"))


def _remote_subscription_topics(status: dict[str, Any]) -> set[str]:
    topics = _topic_set(_runtime(status).get("remote_subscribe_topics"))
    federation = status.get("federation_status") or {}
    if isinstance(federation, dict):
        for peer in federation.values():
            if isinstance(peer, dict):
                topics.update(_topic_set(peer.get("control_subscribing")))
    return topics


def _forward_ready_topics(status: dict[str, Any]) -> set[str]:
    return _topic_set(_runtime(status).get("forward_ready_topics"))


def _inbound_ready_topics(status: dict[str, Any]) -> set[str]:
    return _topic_set(_runtime(status).get("inbound_ready_topics"))


def _inbound_topology_topics(status: dict[str, Any]) -> set[str]:
    return _topic_set(_runtime(status).get("inbound_topics"))


def _publisher_topics(status: dict[str, Any], publisher_name: str | None) -> set[str]:
    if not publisher_name:
        return set()
    subsystem_status = status.get("subsystem_status") or status.get("subsystems") or {}
    publisher = subsystem_status.get(publisher_name) if isinstance(subsystem_status, dict) else None
    if not isinstance(publisher, dict):
        return set()
    return _topic_set(publisher.get("publishing") or [])


def _subscriber_topics(status: dict[str, Any], subscriber_name: str | None) -> set[str]:
    if not subscriber_name:
        return set()
    subsystem_status = status.get("subsystem_status") or status.get("subsystems") or {}
    subscriber = subsystem_status.get(subscriber_name) if isinstance(subsystem_status, dict) else None
    if not isinstance(subscriber, dict):
        return set()
    return _topic_set(subscriber.get("subscribing") or [])


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _time_sync_check(label: str, status: dict[str, Any], max_offset_ns: int | None) -> dict[str, Any]:
    time_sync = status.get("time_sync_status")
    if not isinstance(time_sync, dict):
        return {
            "label": label,
            "ready": False,
            "reason": "missing time_sync_status",
        }

    role = str(time_sync.get("role") or "")
    offset_ns = _int_or_none(time_sync.get("offset_ns"))
    clock_status = time_sync.get("clock_status") if isinstance(time_sync.get("clock_status"), dict) else {}
    clock_status_present = bool(clock_status)
    clock_source = str(clock_status.get("source") or time_sync.get("source") or "")
    clock_threshold_ns = _int_or_none(clock_status.get("threshold_ns"))
    if clock_threshold_ns is not None and clock_threshold_ns < 0:
        clock_threshold_ns = None
    threshold_ns = max_offset_ns if max_offset_ns is not None else clock_threshold_ns
    if threshold_ns is None:
        threshold_ns = _int_or_none(time_sync.get("max_offset_ns"))
    logical_clock_source = clock_source in {"r2kernel_logical", "r2kernel_logical_hw"}
    threshold_kind = str(
        clock_status.get("threshold_kind") or ("uncertainty_ns" if logical_clock_source else "offset_ns")
    )
    uncertainty_ns = _int_or_none(clock_status.get("uncertainty_ns"))
    domain_compatible = clock_status.get("domain_compatible")
    domain_compatible_bool = domain_compatible if isinstance(domain_compatible, bool) else None
    enabled = bool(time_sync.get("enabled"))
    synced = bool(time_sync.get("synced"))
    ready = enabled and synced
    reason = ""

    if not enabled:
        reason = "time_sync disabled"
    elif not synced:
        reason = "time_sync not synced"
    elif logical_clock_source:
        stable = bool(clock_status.get("stable"))
        accepted_for_one_way = bool(clock_status.get("accepted_for_one_way"))
        ready = stable
        if not stable:
            reason = "logical clock not stable"
        elif threshold_ns is not None:
            if uncertainty_ns is None:
                ready = False
                reason = "missing logical clock uncertainty_ns"
            elif uncertainty_ns > threshold_ns:
                ready = False
                reason = f"uncertainty_ns {uncertainty_ns} exceeds threshold {threshold_ns}"
        if ready and not accepted_for_one_way:
            ready = False
            reason = str(clock_status.get("acceptance_reason") or "logical clock not accepted for one-way latency")
    elif role != "leader" and threshold_ns is not None:
        if offset_ns is None:
            ready = False
            reason = "missing follower offset_ns"
        elif abs(offset_ns) > threshold_ns:
            ready = False
            reason = f"offset_ns {offset_ns} exceeds threshold {threshold_ns}"
    if ready and domain_compatible_bool is False:
        ready = False
        reason = str(clock_status.get("domain_reason") or "clock domain incompatible")
    if (
        ready
        and clock_status_present
        and clock_source in {"hardware_ptp", "software_ptp"}
        and not bool(clock_status.get("accepted_for_one_way"))
    ):
        ready = False
        reason = str(clock_status.get("acceptance_reason") or "PTP clock not accepted for one-way latency")

    result = {
        "label": label,
        "ready": ready,
        "reason": reason,
        "role": role,
        "synced": synced,
        "offset_ns": offset_ns,
        "uncertainty_ns": uncertainty_ns,
        "threshold_ns": threshold_ns,
        "threshold_kind": threshold_kind,
        "max_offset_ns": threshold_ns,
        "clock_source": clock_source,
        "clock_status": clock_status,
        "domain_compatible": domain_compatible_bool,
        "domain_reason": clock_status.get("domain_reason"),
        "upstream_source": clock_status.get("upstream_source"),
        "upstream_mode": clock_status.get("upstream_mode"),
        "upstream_accepted_for_one_way": clock_status.get("upstream_accepted_for_one_way"),
        "ptp4l_running": bool(time_sync.get("ptp4l_running")),
        "sync_system_clock": bool(time_sync.get("sync_system_clock")),
    }
    if logical_clock_source:
        result["max_uncertainty_ns"] = threshold_ns
    return result


def _topics_from_args(args: argparse.Namespace) -> list[str]:
    topics = list(args.topic or [])
    if topics:
        return topics
    return [
        topic_name(args.topic_prefix, args.topic_count, index)
        for index in range(max(args.topic_count, 1))
    ]


def _ready_snapshot(args: argparse.Namespace, topics: set[str]) -> dict[str, Any]:
    if args.scope == "cross-host" and not args.receiver_status_url:
        raise ValueError("cross-host scope requires --receiver-status-url")
    sender_status = _status_json(args.sender_status_url, getattr(args, "sender_headers", None))
    receiver_status = _status_json(args.receiver_status_url, getattr(args, "receiver_headers", None)) if args.receiver_status_url else None
    scope = args.scope
    if scope == "auto":
        scope = "cross-host" if receiver_status is not None else "local"

    transport_required = scope == "cross-host"
    transport_checks = []
    if transport_required:
        transport_checks = [
            _transport_peer_check("sender", sender_status, receiver_status),
            _transport_peer_check("receiver", receiver_status, sender_status),
        ]
    transport_ready = all(check["ready"] for check in transport_checks)

    sender_topology_topics = _remote_subscription_topics(sender_status) if scope == "cross-host" else set()
    sender_forward_ready_topics = _forward_ready_topics(sender_status)
    sender_data_plane = _data_plane_report(sender_status)
    sender_data_plane_ready = True
    sender_uses_topology_fallback = False
    if scope == "cross-host":
        if not args.allow_topology_fallback:
            sender_topics = sender_forward_ready_topics
            if sender_data_plane:
                sender_data_plane_ready = (_int_or_none(sender_data_plane.get("local_event_subscriptions")) or 0) > 0
        else:
            sender_topics = sender_topology_topics
            sender_uses_topology_fallback = True
    else:
        sender_topics = _local_subscription_topics(sender_status)
    sender_ready = topics.issubset(sender_topics) and sender_data_plane_ready
    if transport_required:
        sender_ready = sender_ready and transport_checks[0]["ready"] and not sender_uses_topology_fallback
    publisher_topics = _publisher_topics(sender_status, args.publisher_name)
    publisher_observed = bool(publisher_topics)
    publisher_ready = topics.issubset(publisher_topics) if args.start_gate_file else (
        topics.issubset(publisher_topics) if args.publisher_name and publisher_observed else True
    )

    receiver_ready = True
    receiver_topics: set[str] = set()
    receiver_topology_topics: set[str] = set()
    receiver_inbound_ready_topics: set[str] = set()
    receiver_data_plane: dict[str, Any] = {}
    receiver_data_plane_ready = True
    receiver_uses_topology_fallback = False
    subscriber_ready = True
    subscriber_topics: set[str] = set()
    subscriber_observed = False
    if receiver_status is not None:
        if scope == "cross-host":
            receiver_topology_topics = _inbound_topology_topics(receiver_status)
            receiver_inbound_ready_topics = _inbound_ready_topics(receiver_status)
            receiver_data_plane = _data_plane_report(receiver_status)
            if not args.allow_topology_fallback:
                receiver_topics = receiver_inbound_ready_topics
                if receiver_data_plane:
                    receiver_data_plane_ready = (_int_or_none(receiver_data_plane.get("inbound_sink_count")) or 0) > 0
            else:
                receiver_topics = receiver_topology_topics
                receiver_uses_topology_fallback = True
        else:
            receiver_topics = _local_subscription_topics(receiver_status)
        receiver_ready = topics.issubset(receiver_topics) and receiver_data_plane_ready
        if transport_required:
            receiver_ready = receiver_ready and transport_checks[1]["ready"] and not receiver_uses_topology_fallback
        if args.subscriber_name:
            subscriber_topics = _subscriber_topics(receiver_status, args.subscriber_name)
            subscriber_observed = bool(subscriber_topics)
            if subscriber_observed:
                subscriber_ready = topics.issubset(subscriber_topics)

    data_plane_ready = sender_ready and publisher_ready and receiver_ready and subscriber_ready
    time_sync_checks: list[dict[str, Any]] = []
    time_sync_required = bool(args.require_time_sync)
    time_sync_ready = False
    time_sync_status = "not_checked"
    time_sync_reason = "time sync was not required for this readiness-only gate"
    if args.require_time_sync:
        time_sync_checks.append(_time_sync_check("sender", sender_status, args.time_sync_max_offset_ns))
        if receiver_status is not None:
            time_sync_checks.append(_time_sync_check("receiver", receiver_status, args.time_sync_max_offset_ns))
        time_sync_ready = all(check.get("ready") for check in time_sync_checks)
        time_sync_status = "accepted" if time_sync_ready else "not_accepted"
        if time_sync_ready:
            time_sync_reason = "all required time-sync checks accepted"
        else:
            failing = [str(check.get("reason") or check.get("label") or "not accepted") for check in time_sync_checks if not check.get("ready")]
            time_sync_reason = "; ".join(failing) if failing else "time-sync acceptance failed"
    ready = data_plane_ready and (time_sync_ready if time_sync_required else True)
    topology_fallback_used = sender_uses_topology_fallback or receiver_uses_topology_fallback
    latency_evidence_accepted = data_plane_ready and time_sync_required and time_sync_ready and not topology_fallback_used
    if latency_evidence_accepted:
        latency_evidence_reason = "event path and required time-sync checks accepted"
    elif topology_fallback_used:
        latency_evidence_reason = "topology fallback is diagnostic smoke only, never accepted one-way latency evidence"
    elif not data_plane_ready:
        latency_evidence_reason = "event path is not ready"
    elif not time_sync_required:
        latency_evidence_reason = "time sync was not required; one-way latency is diagnostic only"
    else:
        latency_evidence_reason = time_sync_reason

    return {
        "scope": scope,
        "topics": sorted(topics),
        "sender_ready": sender_ready,
        "publisher_ready": publisher_ready,
        "receiver_ready": receiver_ready,
        "subscriber_ready": subscriber_ready,
        "data_plane_ready": data_plane_ready,
        "transport_required": transport_required,
        "transport_ready": transport_ready,
        "transport_checks": transport_checks,
        "time_sync_required": time_sync_required,
        "time_sync_ready": time_sync_ready,
        "time_sync_status": time_sync_status,
        "time_sync_reason": time_sync_reason,
        "time_sync_checks": time_sync_checks,
        "latency_evidence_accepted": latency_evidence_accepted,
        "latency_evidence_reason": latency_evidence_reason,
        "ready": ready,
        "sender_topics": sorted(sender_topics),
        "sender_topology_topics": sorted(sender_topology_topics),
        "sender_forward_ready_topics": sorted(sender_forward_ready_topics),
        "sender_data_plane_ready": sender_data_plane_ready,
        "sender_data_plane": sender_data_plane,
        "sender_topology_fallback_used": sender_uses_topology_fallback,
        "publisher_topics": sorted(publisher_topics),
        "publisher_status_observed": publisher_observed,
        "receiver_topics": sorted(receiver_topics),
        "receiver_topology_topics": sorted(receiver_topology_topics),
        "receiver_inbound_ready_topics": sorted(receiver_inbound_ready_topics),
        "receiver_data_plane_ready": receiver_data_plane_ready,
        "receiver_data_plane": receiver_data_plane,
        "receiver_topology_fallback_used": receiver_uses_topology_fallback,
        "subscriber_topics": sorted(subscriber_topics),
        "subscriber_status_observed": subscriber_observed,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wait until an Event subscription path is visible, then optionally open a publisher start gate."
    )
    parser.add_argument(
        "--sender-status-url",
        default="http://127.0.0.1:8001/api/v1/control/status",
        help="Publisher-side control status URL.",
    )
    parser.add_argument("--receiver-status-url", help="Subscriber-side control status URL for cross-host checks.")
    for side in ("sender", "receiver"):
        parser.add_argument(f"--{side}-api-key-file", help=f"Owner-only file containing the {side} API key; never put secrets in URLs.")
        parser.add_argument(
            f"--{side}-auth-header",
            choices=["Authorization", "X-R2Kernel-API-Key"],
            default="Authorization",
            help="Header used with this side's API key file.",
        )
    parser.add_argument("--publisher-name", help="Required current-run publisher registration with --start-gate-file; optional observation otherwise.")
    parser.add_argument("--subscriber-name", help="Optional subscriber subsystem name to confirm on the receiver.")
    parser.add_argument("--topic", action="append", help="Topic to wait for. Can be passed multiple times.")
    parser.add_argument("--topic-prefix", default="x", help="Topic prefix used when --topic is omitted.")
    parser.add_argument("--topic-count", type=int, default=1, help="Topic count used when --topic is omitted.")
    parser.add_argument("--scope", choices=["auto", "local", "cross-host"], default="auto")
    parser.add_argument("--timeout-s", type=float, default=30.0)
    parser.add_argument("--interval-ms", type=int, default=100)
    parser.add_argument("--stable-polls", type=int, default=1, help="Consecutive ready snapshots required before opening the gate.")
    parser.add_argument(
        "--allow-topology-fallback",
        action="store_true",
        help=(
            "Report topology-only diagnostic topics; never approve a cross-host gate or one-way latency evidence. "
            "Cross-host readiness requires native applied peers and subscription synchronization on both hosts "
            "in addition to forward_ready_topics and inbound_ready_topics."
        ),
    )
    parser.add_argument(
        "--require-time-sync",
        action="store_true",
        help=(
            "Require time-sync acceptance before opening the gate. Without this flag, "
            "the helper performs data-plane readiness only and marks one-way latency evidence as diagnostic."
        ),
    )
    parser.add_argument(
        "--time-sync-max-offset-ns",
        type=int,
        default=None,
        help=(
            "Optional explicit threshold for --require-time-sync. "
            "PTP modes compare follower offset_ns; r2kernel_logical compares uncertainty_ns."
        ),
    )
    parser.add_argument(
        "--time-sync-max-uncertainty-ns",
        type=int,
        default=None,
        help="Alias for --time-sync-max-offset-ns when using r2kernel_logical mode.",
    )
    parser.add_argument("--start-gate-file", help="Absent gate in a fresh private 0700 run directory; create exclusively as 0600 after readiness.")
    parser.add_argument("--start-gate-session", help="Unique attempt identity shared with the publisher; required with --start-gate-file.")
    parser.add_argument("--report-format", choices=["text", "json"], default="text")
    parser.add_argument(
        "--report-path",
        help="Write the final JSON readiness report to this path regardless of stdout format.",
    )
    args = parser.parse_args()
    if args.scope == "cross-host" and not args.receiver_status_url:
        parser.error("--scope cross-host requires --receiver-status-url")
    if bool(args.start_gate_file) != bool(args.start_gate_session):
        parser.error("--start-gate-file and --start-gate-session are required together")
    if args.start_gate_file and not args.publisher_name:
        parser.error("--start-gate-file requires --publisher-name")
    if args.start_gate_file and args.report_path:
        gate_path = Path(args.start_gate_file)
        if Path(args.report_path).resolve() in {
            gate_path.resolve(), gate_path.with_name(gate_path.name + ".ack").resolve()
        }:
            parser.error("--report-path must not refer to the start gate or its acknowledgement")
    try:
        for side in ("sender", "receiver"):
            url = getattr(args, f"{side}_status_url")
            if url:
                _validate_status_url(url)
            setattr(args, f"{side}_headers", _credential_headers(
                getattr(args, f"{side}_api_key_file"), getattr(args, f"{side}_auth_header")
            ))
    except ValueError as exc:
        parser.error(str(exc))
    if args.time_sync_max_uncertainty_ns is not None:
        args.time_sync_max_offset_ns = args.time_sync_max_uncertainty_ns
    return args


def _write_report_file(path: str | None, report: dict[str, Any]) -> None:
    if not path:
        return
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def _print_text_report(report: dict[str, Any], *, topics: set[str], timeout_s: float, success: bool) -> None:
    topic_text = ", ".join(sorted(topics))
    if success:
        print(f"event path ready: {topic_text}")
    else:
        print(f"event path did not become ready within {timeout_s:.1f}s: {topic_text}", file=sys.stderr)
    print(
        "time sync: "
        f"{report.get('time_sync_status', 'unknown')} "
        f"(required={str(bool(report.get('time_sync_required'))).lower()}, "
        f"ready={str(bool(report.get('time_sync_ready'))).lower()})"
    )
    print(
        "one-way latency evidence: "
        f"{'accepted' if report.get('latency_evidence_accepted') else 'diagnostic-only'} "
        f"({report.get('latency_evidence_reason') or 'no reason reported'})"
    )
    if not success:
        for check in report.get("transport_checks") or []:
            if not check.get("ready"):
                print(f"transport {check.get('label')}: not ready toward {check.get('peer_id')}: {check.get('reason')}")


def main() -> int:
    args = parse_args()
    topics = set(_topics_from_args(args))
    deadline = time.monotonic() + max(args.timeout_s, 0.1)
    interval_s = max(args.interval_ms, 10) / 1000.0
    stable_required = max(args.stable_polls, 1)
    stable_count = 0
    last: dict[str, Any] | None = None
    try:
        start_gate = StartGate(args.start_gate_file) if args.start_gate_file else None
    except (OSError, ValueError) as exc:
        report = {"ready": False, "latency_evidence_accepted": False, "error": str(exc), "topics": sorted(topics), "exit_code": 1}
        _write_report_file(args.report_path, report)
        if args.report_format == "json":
            print(json.dumps(report, indent=2))
        else:
            print(f"start gate refused: {exc}", file=sys.stderr)
        return 1

    while time.monotonic() < deadline:
        acknowledged = False
        if start_gate is not None:
            try:
                acknowledged = start_gate.acknowledged(args.start_gate_session, args.publisher_name)
            except (OSError, ValueError) as exc:
                last = {"ready": False, "latency_evidence_accepted": False,
                        "error": f"start gate refused: {exc}", "topics": sorted(topics)}
                break
        try:
            last = _ready_snapshot(args, topics)
            if start_gate is not None:
                last["publisher_startup_acknowledged"] = acknowledged
                last["start_gate_session"] = args.start_gate_session
                if not acknowledged:
                    last.update(ready=False, latency_evidence_accepted=False,
                                latency_evidence_reason="current-run publisher has not acknowledged startup")
        except urllib.error.HTTPError as exc:
            last = {"ready": False, "error": f"status endpoint returned HTTP {exc.code}", "topics": sorted(topics)}
        except (urllib.error.URLError, TimeoutError, ValueError, UnicodeError, OSError):
            last = {"ready": False, "error": "status endpoint unavailable or returned invalid JSON", "topics": sorted(topics)}
        if last.get("ready"):
            stable_count += 1
        else:
            stable_count = 0
        last["stable_polls"] = {"current": stable_count, "required": stable_required}
        if stable_count >= stable_required:
            if start_gate is not None:
                try:
                    start_gate.open(args.start_gate_session, args.publisher_name)
                except (OSError, ValueError) as exc:
                    last.update(ready=False, latency_evidence_accepted=False,
                                latency_evidence_reason="start gate could not be opened safely",
                                error=f"start gate refused: {exc}")
                    break
                last["start_gate_file"] = str(start_gate.path)
            last["exit_code"] = 0
            _write_report_file(args.report_path, last)
            if args.report_format == "json":
                print(json.dumps(last, indent=2))
            else:
                _print_text_report(last, topics=topics, timeout_s=args.timeout_s, success=True)
            return 0
        time.sleep(interval_s)

    report = last or {"ready": False, "topics": sorted(topics)}
    report["exit_code"] = 1
    _write_report_file(args.report_path, report)
    if args.report_format == "json":
        print(json.dumps(report, indent=2))
    else:
        _print_text_report(report, topics=topics, timeout_s=args.timeout_s, success=False)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
