"""Python drive controller: sends timed api.actuator.drive.v1 drive_command requests and reports RTT."""
from __future__ import annotations

import argparse
import json
import resource
import sys
import threading
import time
from pathlib import Path

_FIXTURE_PY = Path(__file__).resolve().parent
if str(_FIXTURE_PY) not in sys.path:
    sys.path.insert(0, str(_FIXTURE_PY))

from drive_payload import (  # noqa: E402
    CONTRACT,
    MAX_SEQUENCE,
    REQUEST_TYPE,
    RESPONSE_TYPE,
    TOPIC,
    channels_for_payload,
    make_command,
    state_matches,
)


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "tutorials" / "support" / "python").is_dir():
            return parent
    raise RuntimeError(f"could not resolve repository root from {__file__}")


_SUPPORT_PY = repo_root() / "tutorials" / "support" / "python"
if str(_SUPPORT_PY) not in sys.path:
    sys.path.insert(0, str(_SUPPORT_PY))

from runtime import (  # noqa: E402
    default_deployment_manifest_path,
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
    run_subsystem,
)
from benchmark import LatencyStats, summary_dict, write_report  # noqa: E402

try:
    from r2kernel.interface.codec import RustMessageCodec
    from r2kernel.wrapper.subsystem import Subsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class DriveControllerSubsystem(Subsystem):
    def __init__(
        self,
        *,
        name: str = "drive_controller",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        deployment_manifest_path: str | None = None,
        binding: str = "gateway",
        target: str | None = None,
        payload_size: int = 256,
        period_us: int = 1_000_000,
        count: int = 0,
        warmup: int = 0,
        timeout_ms: int = 1000,
        deadline_ms: float | None = None,
        validity_ms: float = 30_000.0,
        priority: int | None = None,
        start_gate_path: str | None = None,
        ready_path: str | None = None,
        gate_timeout_s: float = 60.0,
        quiet: bool = False,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "controller"),
            manifest_path=manifest_path or default_manifest_path(__file__, name),
            deployment_manifest_path=deployment_manifest_path or default_deployment_manifest_path(__file__),
        )
        self._target = target or self.binding_target(binding)
        self._payload_size = payload_size
        self._channels = channels_for_payload(payload_size)
        self._period_us = max(period_us, 0)
        self._count = max(count, 0)
        self._warmup = max(warmup, 0)
        self._timeout_ms = max(timeout_ms, 1)
        self._deadline_ms = float(deadline_ms if deadline_ms is not None
                                  else max(self._timeout_ms, int(self._period_us / 1000) * 2 or 1000))
        self._validity_ns = int(validity_ms * 1_000_000)
        self._validity_ms = validity_ms
        self._priority = priority
        self._start_gate_path = Path(start_gate_path) if start_gate_path else None
        self._ready_path = Path(ready_path) if ready_path else None
        self._gate_timeout_s = max(float(gate_timeout_s), 0.0)
        self._quiet = quiet
        self._stop = threading.Event()
        self._done = threading.Event()
        self._worker: threading.Thread | None = None
        self._stats = LatencyStats()
        self._caller_stats = LatencyStats()
        self._attempts = 0
        self._success = 0
        self._failures = 0
        self._warmup_failures = 0
        self._integrity_errors = 0
        self._deadline_violations = 0
        self._period_violations = 0
        self._late_cycles = 0
        self._seq = 0
        self._request_encoded_bytes: int | None = None
        self._last_error: str | None = None
        self._rtt_measurement_scope = "runtime_rtt_ns_from_transact_control"
        self._measured_started_ns: int | None = None
        self._measured_finished_ns: int | None = None
        self._measured_cpu_started_ns: int | None = None
        self._measured_cpu_finished_ns: int | None = None
        self._run_started_ns: int | None = None
        self._run_finished_ns: int | None = None

    def on_initialize(self) -> bool:
        self.logger.info("Lifecycle", f"Drive controller initialized target={self._target} channels={self._channels}")
        return True

    def on_start(self) -> bool:
        self._stop.clear()
        self._done.clear()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()
        return True

    def on_terminate(self) -> bool:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
        if self._run_started_ns is not None and self._run_finished_ns is None:
            self._run_finished_ns = time.perf_counter_ns()
        self.logger.info("Lifecycle", "Drive controller terminated")
        return True

    def on_reset(self) -> bool:
        self._stop.set()
        return True

    def done(self) -> bool:
        return self._done.is_set()

    def _loop(self) -> None:
        try:
            self._run_started_ns = time.perf_counter_ns()
            # Ready before the first request: a cross-host driver binds the requirement, then opens the gate.
            if not self._wait_for_release():
                return
            for _ in range(self._warmup):
                if self._stop.is_set():
                    return
                if not self._attempt(measured=False):
                    self._warmup_failures += 1
            self._measured_loop()
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._failures += 1
        finally:
            if self._run_finished_ns is None:
                self._run_finished_ns = time.perf_counter_ns()
            self._done.set()
            self._stop.set()

    def _measured_loop(self) -> None:
        period_s = self._period_us / 1_000_000.0
        next_tick = time.perf_counter()
        measured = 0
        while not self._stop.is_set() and (self._count == 0 or measured < self._count):
            sleep_for = next_tick - time.perf_counter()
            if sleep_for > 0:
                self._stop.wait(sleep_for)
                continue
            if self._measured_started_ns is None:
                self._measured_started_ns = time.perf_counter_ns()
                self._measured_cpu_started_ns = time.process_time_ns()
            self._attempt(measured=True)
            measured += 1
            self._measured_finished_ns = time.perf_counter_ns()
            if period_s > 0:
                next_tick += period_s
                finished_at = time.perf_counter()
                if finished_at > next_tick:
                    missed = int((finished_at - next_tick) / period_s) + 1
                    self._late_cycles += missed
                    next_tick += missed * period_s
        self._measured_cpu_finished_ns = time.process_time_ns()
        self._run_finished_ns = time.perf_counter_ns()

    def _attempt(self, *, measured: bool) -> bool:
        self._seq += 1
        if self._seq >= MAX_SEQUENCE:
            raise RuntimeError("drive sequence exhausted")
        self._attempts += 1
        seq = self._seq
        command = make_command(seq, self._channels)
        started_ns = time.perf_counter_ns()
        generated_ns = self.now_ns()
        try:
            result = self.transact_control(
                target=self._target, topic=TOPIC, type="bytes", value=command,
                period_ms=self._period_us / 1000.0, deadline_ms=self._deadline_ms,
                last_tick_ns=generated_ns, priority=self._priority,
                request_type=REQUEST_TYPE, response_type=RESPONSE_TYPE,
                timing={"generated_at_sync_ns": generated_ns, "valid_for_ns": self._validity_ns},
            )
        except Exception as exc:
            result = {"ok": False, "error_message": f"{type(exc).__name__}: {exc}"}
        elapsed_ns = time.perf_counter_ns() - started_ns
        transport = result.get("transport") or {}
        rtt_ns = transport.get("rtt_ns")
        ok = bool(result.get("ok"))
        integrity_ok = ok and state_matches(result.get("result"), seq, self._channels)
        if measured:
            self._caller_stats.add_us(elapsed_ns / 1_000.0)
            if elapsed_ns > self._deadline_ms * 1_000_000:
                self._deadline_violations += 1
            if self._period_us > 0 and elapsed_ns > self._period_us * 1_000:
                self._period_violations += 1
        if integrity_ok:
            self._success += 1
            if measured and rtt_ns is not None:
                self._stats.add_us(float(rtt_ns) / 1_000.0)
            if not self._quiet:
                print(f"{self.name} topic={TOPIC} seq={seq} rtt_us={float(rtt_ns or 0) / 1000.0:.3f}")
            return True
        if ok:
            self._integrity_errors += 1
            self._last_error = "DriveState differs from the fixture pattern"
        else:
            self._last_error = str(result.get("error_message") or result.get("error_code")
                                   or transport.get("error") or "Control transaction returned ok=false")
        if measured:
            self._failures += 1
        if not self._quiet:
            print(f"{self.name} topic={TOPIC} seq={seq} failed error={self._last_error}")
        return False

    def _wait_for_release(self) -> bool:
        if self._start_gate_path is None and self._ready_path is None:
            return True
        if self._start_gate_path is None or self._ready_path is None:
            raise ValueError("start-gate-path and ready-path must be configured together")
        self._write_json_file(self._ready_path, {"attempts": self._attempts, "ready_monotonic_ns": time.perf_counter_ns()})
        deadline = time.monotonic() + self._gate_timeout_s
        while not self._stop.is_set() and not self._start_gate_path.exists():
            if time.monotonic() >= deadline:
                self._last_error = "start gate timed out"
                self._failures += 1
                return False
            self._stop.wait(0.005)
        return not self._stop.is_set()

    @staticmethod
    def _write_json_file(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(path)

    def _request_bytes(self) -> int | None:
        try:
            return len(RustMessageCodec().encode_message(REQUEST_TYPE, make_command(1, self._channels)))
        except Exception:
            return None

    def report(self) -> dict:
        measured_elapsed_s = 0.0
        if self._measured_started_ns is not None and self._measured_finished_ns is not None:
            measured_elapsed_s = max((self._measured_finished_ns - self._measured_started_ns) / 1e9, 0.0)
        measured_attempts = max(self._attempts - self._warmup, 0)
        usage = resource.getrusage(resource.RUSAGE_SELF)
        peak_rss_bytes = int(usage.ru_maxrss) * (1 if sys.platform == "darwin" else 1024)
        total_elapsed_ms = 0.0
        if self._run_started_ns is not None and self._run_finished_ns is not None:
            total_elapsed_ms = (self._run_finished_ns - self._run_started_ns) / 1e6
        return {
            "kind": "control",
            "role": "controller",
            "latency_scope": self._rtt_measurement_scope,
            **summary_dict(self._stats),
            "caller_wall_clock": summary_dict(self._caller_stats),
            "failures": self._failures,
            "dropped": 0,
            "extra": {
                "contract": CONTRACT,
                "topic": TOPIC,
                "attempts": self._attempts,
                "success": self._success,
                "topic_count": 1,
                "payload_size": self._payload_size,
                "channels": self._channels,
                "request_encoded_bytes": self._request_bytes(),
                "period_us": self._period_us,
                "timeout_ms": self._timeout_ms,
                "deadline_ms": self._deadline_ms,
                "validity_ms": self._validity_ms,
                "priority": self._priority if self._priority is not None else "auto",
                "request_mode": "round-robin",
                "warmup": self._warmup,
                "warmup_failures": self._warmup_failures,
                "scheduled_requests": self._count + self._warmup if self._count > 0 else "unbounded",
                "integrity_errors": self._integrity_errors,
                "deadline_violations": self._deadline_violations,
                "period_violations": self._period_violations,
                "late_cycles": self._late_cycles,
                "last_error": self._last_error,
                "measured_elapsed_s": measured_elapsed_s,
                "achieved_rate_hz": measured_attempts / measured_elapsed_s if measured_elapsed_s > 0 else 0.0,
                "successful_rate_hz": self._stats.samples / measured_elapsed_s if measured_elapsed_s > 0 else 0.0,
                "rtt_measurement_scope": self._rtt_measurement_scope,
                "total_elapsed_ms": total_elapsed_ms,
                "process_user_cpu_s": usage.ru_utime,
                "process_system_cpu_s": usage.ru_stime,
                "measured_process_cpu_s": (
                    (self._measured_cpu_finished_ns - self._measured_cpu_started_ns) / 1e9
                    if self._measured_cpu_started_ns is not None and self._measured_cpu_finished_ns is not None
                    else None
                ),
                "process_peak_rss_bytes": peak_rss_bytes,
                "measured_start_monotonic_ns": self._measured_started_ns,
                "measured_end_monotonic_ns": self._measured_finished_ns,
            },
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Python api.actuator.drive.v1 controller fixture.")
    parser.add_argument("--name", default="drive_controller", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--binding", default="gateway", help="Required interface binding name.")
    parser.add_argument("--target", default=None, help="Direct Control target subsystem name; bypasses deployment binding resolution.")
    parser.add_argument("--topic-count", type=int, default=1, help="Accepted for CLI parity; the contract has one control.")
    parser.add_argument("--topic-prefix", default=TOPIC, help=f"Control endpoint; the contract declares only {TOPIC}.")
    parser.add_argument("--payload-size", "--request-size", dest="payload_size", type=int, default=256,
                        help="Approximate request data bytes; sets the drive vector length (21 bytes per channel).")
    parser.add_argument("--period-ms", type=int, default=None, help="Request period in milliseconds.")
    parser.add_argument("--period-us", type=int, default=None, help="Request period in microseconds.")
    parser.add_argument("--count", type=int, default=0, help="Measured request count. 0 means run until interrupted.")
    parser.add_argument("--warmup", type=int, default=0, help="Warmup request count excluded from stats.")
    parser.add_argument("--timeout-ms", type=int, default=1000, help="Lower bound of the default request deadline.")
    parser.add_argument("--deadline-ms", type=float, default=None, help="Control deadline passed to the request.")
    parser.add_argument("--validity-ms", type=float, default=30_000.0,
                        help="Request validity window (valid_until = generated_at + this); covers cross-host clock offset.")
    parser.add_argument("--priority", type=int, default=None, help="Optional Control priority (0-255).")
    parser.add_argument("--request-mode", choices=["round-robin"], default="round-robin", help="Accepted for CLI parity.")
    parser.add_argument("--start-gate-path", default=None, help="Wait for this file before the first request.")
    parser.add_argument("--ready-path", default=None, help="Write the ready marker here before waiting on the start gate.")
    parser.add_argument("--gate-timeout-s", type=float, default=60.0, help="Bounded wait for the start gate.")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-request output.")
    parser.add_argument("--report-format", choices=["text", "json"], default="text", help="Final report format.")
    parser.add_argument("--report-path", default=None, help="Optional output path for the final report.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--deployment-manifest", default=None, help="Path to the deployment binding manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.topic_count != 1 or args.topic_prefix != TOPIC:
        raise SystemExit(f"api.actuator.drive.v1 declares one control: use --topic-count 1 --topic-prefix {TOPIC}")
    if args.payload_size <= 0 or args.validity_ms <= 0 or args.gate_timeout_s <= 0:
        raise SystemExit("--payload-size, --validity-ms and --gate-timeout-s must be positive")
    if bool(args.start_gate_path) != bool(args.ready_path):
        raise SystemExit("--start-gate-path and --ready-path must be provided together")
    ensure_control_running()
    period_us = args.period_us if args.period_us is not None else (
        args.period_ms * 1000 if args.period_ms is not None else 1_000_000)
    instance = DriveControllerSubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        deployment_manifest_path=args.deployment_manifest,
        binding=args.binding,
        target=args.target,
        payload_size=args.payload_size,
        period_us=period_us,
        count=args.count,
        warmup=args.warmup,
        timeout_ms=args.timeout_ms,
        deadline_ms=args.deadline_ms,
        validity_ms=args.validity_ms,
        priority=args.priority,
        start_gate_path=args.start_gate_path,
        ready_path=args.ready_path,
        gate_timeout_s=args.gate_timeout_s,
        quiet=args.quiet,
    )
    run_subsystem(instance, should_stop=instance.done)
    report = instance.report()
    write_report(report_format=args.report_format, report_path=args.report_path, report=report)
    return 0 if report["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
