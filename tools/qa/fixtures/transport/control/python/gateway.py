from __future__ import annotations

import argparse
import resource
import sys
import time
from pathlib import Path
_FIXTURE_PY = Path(__file__).resolve().parent
if str(_FIXTURE_PY) not in sys.path:
    sys.path.insert(0, str(_FIXTURE_PY))

from rt_profile import POLICIES, add_arguments, profile_from_args, write_json


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "tutorials" / "support" / "python").is_dir():
            return parent
    raise RuntimeError(f"could not resolve repository root from {__file__}")


_SUPPORT_PY = repo_root() / "tutorials" / "support" / "python"
if str(_SUPPORT_PY) not in sys.path:
    sys.path.insert(0, str(_SUPPORT_PY))

from runtime import (
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
    run_subsystem,
)
from benchmark import LatencyStats, make_payload, payload_seq, summary_dict, write_report

try:
    from r2kernel.wrapper.subsystem import Subsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class GatewaySubsystem(Subsystem):
    def __init__(
        self,
        *,
        name: str = "control_gateway",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        port: int = 0,
        expected_count: int = 0,
        warmup: int = 0,
        request_size: int = 8,
        request_sizes: list[int] | None = None,
        response_size: int = 8,
        processing_delay_us: int = 0,
        quiet: bool = False,
        rt_profile: dict | None = None,
        worker_ready_path: str | None = None,
        preparation_timeout_ms: int = 1000,
        worker_ready_timeout_ms: int = 30_000,
        measurement_seconds: float | None = None,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "gateway"),
            manifest_path=manifest_path or default_manifest_path(__file__, name),
        )
        self._port = port
        self._expected_count = max(expected_count, 0)
        self._warmup = max(warmup, 0)
        self._request_size = max(request_size, 8)
        self._request_sizes = sorted({max(int(size), 8) for size in (request_sizes or [self._request_size])})
        if not self._request_sizes:
            raise ValueError("request_sizes must contain at least one payload size")
        self._response_size = max(response_size, 8)
        self._processing_delay_us = max(processing_delay_us, 0)
        self._quiet = quiet
        self._stats = LatencyStats()
        self._handled = 0
        self._measured = 0
        self._handshakes = 0
        self._integrity_errors = 0
        self._failures = 0
        self._done = False
        self._measured_started_ns: int | None = None
        self._measured_finished_ns: int | None = None
        self._run_started_ns: int | None = None
        self._run_finished_ns: int | None = None
        self._rt_profile = rt_profile
        self._worker_ready_path = worker_ready_path
        self._worker_profiles = []
        self._worker_ready_written = False
        self._worker_ready_deadline = None
        self._worker_ready_timeout_ms = worker_ready_timeout_ms
        self._measurement_seconds = measurement_seconds
        self._setup_error = None

    def on_initialize(self) -> bool:
        for topic in ("control_cmd", "control_hello"):
            self.register_raw_control_endpoint(topic, self._handle_request)
        self.logger.info("Lifecycle", "Control gateway initialized")
        return True

    def on_start(self) -> bool:
        self._run_started_ns = time.perf_counter_ns()
        if self._rt_profile is not None:
            self.configure_control_worker(self._rt_profile["policy"], self._rt_profile["priority"],
                                          self._rt_profile["cpus"])
        if self._worker_ready_path:
            self._worker_ready_deadline = time.monotonic() + self._worker_ready_timeout_ms / 1000
        if self._port > 0:
            self.bind_control(port=self._port)
        self.start_control_server()
        self.logger.info("Lifecycle", f"Control gateway listening on port {self.get_control_port()}")
        return True

    def on_terminate(self) -> bool:
        self.stop_control_server()
        self._run_finished_ns = time.perf_counter_ns()
        self.logger.info("Lifecycle", "Control gateway terminated")
        return True

    def on_reset(self) -> bool:
        self.stop_control_server()
        self.logger.info("Lifecycle", "Control gateway reset")
        return True

    def done(self) -> bool:
        # Called by the lifecycle supervisor, never by the timed data-worker callback.
        if self._worker_ready_path and not self._worker_ready_written:
            statuses = self.control_worker_status()
            errors = [str(worker["error"]) for worker in statuses if worker.get("error")]
            if errors:
                self._setup_error = "; ".join(errors)
                self._failures += 1
                self._done = True
            elif statuses and all(worker.get("ready") for worker in statuses):
                self._worker_profiles = [{
                    **worker, "ok": True, "explicit": self._rt_profile is not None,
                    "policy_name": next((name for name, policy in POLICIES.items()
                                         if policy == worker["policy"]), str(worker["policy"])),
                    "isolation_evidence": (self._rt_profile or {}).get("isolation_evidence"),
                } for worker in statuses]
                write_json(self._worker_ready_path, {
                    "phase": "profile_applied", "provider_ready": False,
                    "ok": True, "workers": self._worker_profiles,
                    "isolation_evidence": (self._rt_profile or {}).get("isolation_evidence"),
                    "ready_monotonic_ns": time.monotonic_ns(),
                })
                self._worker_ready_written = True
            elif self._worker_ready_deadline is not None and time.monotonic() >= self._worker_ready_deadline:
                self._setup_error = "actual Control data-worker profile readiness timed out"
                self._failures += 1
                self._done = True
        return self._done

    def report(self) -> dict:
        measured_elapsed_s = 0.0
        if self._measured_started_ns is not None and self._measured_finished_ns is not None:
            measured_elapsed_s = max(
                (self._measured_finished_ns - self._measured_started_ns) / 1_000_000_000.0,
                0.0,
            )
        process_usage = resource.getrusage(resource.RUSAGE_SELF)
        peak_rss_bytes = int(process_usage.ru_maxrss)
        if sys.platform != "darwin":
            peak_rss_bytes *= 1024
        total_elapsed_ms = 0.0
        if self._run_started_ns is not None and self._run_finished_ns is not None:
            total_elapsed_ms = (self._run_finished_ns - self._run_started_ns) / 1_000_000.0
        return {
            "kind": "control",
            "role": "gateway",
            **summary_dict(self._stats),
            "failures": self._failures,
            "dropped": 0,
            "extra": {
                "handled": self._handled,
                "measured": self._measured,
                "handshakes": self._handshakes,
                "warmup": self._warmup,
                "request_payload_size": self._request_size,
                "request_payload_sizes": self._request_sizes,
                "response_payload_size": self._response_size,
                "integrity_errors": self._integrity_errors,
                "measured_elapsed_s": measured_elapsed_s,
                "achieved_rate_hz": self._measured / measured_elapsed_s if measured_elapsed_s > 0 else 0.0,
                "total_elapsed_ms": total_elapsed_ms,
                "scheduled_requests": self._expected_count + self._warmup if self._expected_count > 0 else "unbounded",
                "latency_scope": "request_timestamp_to_gateway_arrival",
                "process_user_cpu_s": process_usage.ru_utime,
                "process_system_cpu_s": process_usage.ru_stime,
                "process_peak_rss_bytes": peak_rss_bytes,
                "processing_delay_us": self._processing_delay_us,
                "thread_profiles": self._worker_profiles,
                "setup_error": self._setup_error,
                "periodic": {
                    "scope": "synthetic_echo_provider", "language": "python",
                    "worker_ready": self._worker_ready_written, "provider_ready": False,
                    "thread_profiles": self._worker_profiles, "setup_error": self._setup_error,
                    "isolation_evidence": (self._rt_profile or {}).get("isolation_evidence"),
                } if self._measurement_seconds is not None or self._rt_profile or self._worker_ready_path else None,
            },
        }

    def _handle_request(self, req: dict) -> dict:
        recv_ts = self.now_ns()
        payload = req.get("payload") or b""
        seq = payload_seq(payload) if payload else 0
        topic = str(req.get("topic") or "")
        is_handshake = topic == "control_hello"
        if is_handshake:
            self._handshakes += 1
        else:
            self._handled += 1
        if not is_handshake and self._handled > self._warmup:
            self._measured += 1
            if self._measured_started_ns is None:
                self._measured_started_ns = time.perf_counter_ns()
            if self._measurement_seconds is None:
                self._stats.add_us((recv_ts - int(req.get("ts", recv_ts))) / 1_000.0)
            if self._expected_count > 0 and self._measured >= self._expected_count:
                self._done = True
        if not self._quiet and self._measurement_seconds is None and self._rt_profile is None:
            print(f"{self.name} topic={topic} seq={seq} latency_us={(recv_ts - int(req.get('ts', recv_ts))) / 1_000.0:.3f}")
        if self._processing_delay_us > 0:
            time.sleep(self._processing_delay_us / 1_000_000.0)
        if is_handshake:
            response_payload = payload
            status = 0
        else:
            request_valid = any(bytes(payload) == make_payload(seq, size) for size in self._request_sizes)
            if not request_valid:
                self._integrity_errors += 1
                self._failures += 1
            response_payload = make_payload(seq, self._response_size)
            status = 0 if request_valid else 1
        if not is_handshake:
            self._measured_finished_ns = time.perf_counter_ns()
        return {"ts": recv_ts, "status": status, "payload": response_payload}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Python Control gateway tutorial.")
    parser.add_argument("--name", default="control_gateway", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--port", type=int, default=0, help="Control listen port override. Default is control-assigned.")
    parser.add_argument("--expected-count", type=int, default=0, help="Measured request count before auto-exit. 0 means run until interrupted.")
    parser.add_argument("--warmup", type=int, default=0, help="Warmup request count excluded from stats.")
    parser.add_argument("--request-size", "--payload-size", dest="request_size", type=int, default=8, help="Expected raw request payload size in bytes.")
    parser.add_argument("--request-sizes", default=None, help="Explicit comma-separated list of accepted raw request sizes.")
    parser.add_argument("--response-size", type=int, default=8, help="Raw response payload size in bytes.")
    parser.add_argument("--processing-delay-us", type=int, default=0, help="Artificial per-request processing delay.")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-request output.")
    parser.add_argument("--report-format", choices=["text", "json"], default="text", help="Final report format.")
    parser.add_argument("--report-path", default=None, help="Optional output path for the final report.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    add_arguments(parser)
    parser.add_argument("--worker-ready-timeout-ms", type=int, default=30_000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    request_sizes = None
    if args.request_sizes is not None:
        try:
            request_sizes = [int(value.strip()) for value in args.request_sizes.split(",") if value.strip()]
        except ValueError as exc:
            raise SystemExit(f"Invalid --request-sizes value: {exc}") from exc
        if not request_sizes:
            raise SystemExit("--request-sizes must contain one or more sizes")
    try:
        rt_profile = profile_from_args(args)
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
    if args.worker_ready_timeout_ms <= 0:
        raise SystemExit("--worker-ready-timeout-ms must be positive")
    ensure_control_running()
    instance = GatewaySubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        port=args.port,
        expected_count=args.expected_count,
        warmup=args.warmup,
        request_size=args.request_size,
        request_sizes=request_sizes,
        response_size=args.response_size,
        processing_delay_us=args.processing_delay_us,
        quiet=args.quiet,
        rt_profile=rt_profile,
        worker_ready_path=args.worker_ready_path,
        preparation_timeout_ms=args.preparation_timeout_ms,
        measurement_seconds=args.measurement_seconds,
        worker_ready_timeout_ms=args.worker_ready_timeout_ms,
    )
    run_subsystem(instance, should_stop=instance.done)
    write_report(report_format=args.report_format, report_path=args.report_path, report=instance.report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
