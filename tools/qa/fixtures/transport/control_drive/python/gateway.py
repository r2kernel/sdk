"""Python drive gateway: provides the public api.actuator.drive.v1 Control and reports arrival latency."""
from __future__ import annotations

import argparse
import resource
import sys
import time
from pathlib import Path

_FIXTURE_PY = Path(__file__).resolve().parent
if str(_FIXTURE_PY) not in sys.path:
    sys.path.insert(0, str(_FIXTURE_PY))

from drive_payload import (  # noqa: E402
    CONTRACT,
    REQUEST_TYPE,
    RESPONSE_TYPE,
    TOPIC,
    channels_for_payload,
    command_sequence,
    make_state,
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
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
    run_subsystem,
)
from benchmark import LatencyStats, summary_dict, write_report  # noqa: E402

try:
    from r2kernel.wrapper.subsystem import Subsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class DriveGatewaySubsystem(Subsystem):
    def __init__(
        self,
        *,
        name: str = "drive_gateway",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        port: int = 0,
        expected_count: int = 0,
        warmup: int = 0,
        channels: int | None = None,
        quiet: bool = False,
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
        self._channels = channels
        self._quiet = quiet
        self._stats = LatencyStats()
        self._handled = 0
        self._measured = 0
        self._integrity_errors = 0
        self._failures = 0
        self._last_error: str | None = None
        self._done = False
        self._measured_started_ns: int | None = None
        self._measured_finished_ns: int | None = None
        self._run_started_ns: int | None = None
        self._run_finished_ns: int | None = None

    def on_initialize(self) -> bool:
        self.register_control_endpoint(TOPIC, self._handle_command, request_type=REQUEST_TYPE,
                                       response_type=RESPONSE_TYPE, timing_support="required")
        self.logger.info("Lifecycle", f"Drive gateway initialized contract={CONTRACT}")
        return True

    def on_start(self) -> bool:
        self._run_started_ns = time.perf_counter_ns()
        if self._port > 0:
            self.bind_control(port=self._port)
        self.start_control_server()
        self.logger.info("Lifecycle", f"Drive gateway listening on port {self.get_control_port()}")
        return True

    def on_terminate(self) -> bool:
        self.stop_control_server()
        self._run_finished_ns = time.perf_counter_ns()
        self.logger.info("Lifecycle", "Drive gateway terminated")
        return True

    def on_reset(self) -> bool:
        self.stop_control_server()
        return True

    def done(self) -> bool:
        return self._done

    def _handle_command(self, command: dict, context=None) -> dict:
        accepted_ns = int(getattr(context, "accepted_at_sync_ns", 0) or self.now_ns())
        sent_ns = getattr(context, "sent_at_sync_ns", None)
        self._handled += 1
        decoded = command_sequence(command)
        if decoded is None or (self._channels is not None and decoded[1] != self._channels):
            self._integrity_errors += 1
            self._failures += 1
            self._last_error = "DriveCommand differs from the fixture pattern"
            raise ValueError(self._last_error)
        seq, channels = decoded
        if self._handled > self._warmup:
            self._measured += 1
            if self._measured_started_ns is None:
                self._measured_started_ns = time.perf_counter_ns()
            if sent_ns is not None:
                self._stats.add_us((accepted_ns - int(sent_ns)) / 1_000.0)
            if self._expected_count > 0 and self._measured >= self._expected_count:
                self._done = True
            self._measured_finished_ns = time.perf_counter_ns()
        if not self._quiet:
            print(f"{self.name} topic={TOPIC} seq={seq} channels={channels}")
        return make_state(seq, channels)

    def report(self) -> dict:
        measured_elapsed_s = 0.0
        if self._measured_started_ns is not None and self._measured_finished_ns is not None:
            measured_elapsed_s = max((self._measured_finished_ns - self._measured_started_ns) / 1e9, 0.0)
        usage = resource.getrusage(resource.RUSAGE_SELF)
        peak_rss_bytes = int(usage.ru_maxrss) * (1 if sys.platform == "darwin" else 1024)
        total_elapsed_ms = 0.0
        if self._run_started_ns is not None and self._run_finished_ns is not None:
            total_elapsed_ms = (self._run_finished_ns - self._run_started_ns) / 1e6
        return {
            "kind": "control",
            "role": "gateway",
            **summary_dict(self._stats),
            "failures": self._failures,
            "dropped": 0,
            "extra": {
                "contract": CONTRACT,
                "topic": TOPIC,
                "handled": self._handled,
                "measured": self._measured,
                "warmup": self._warmup,
                "expected_channels": self._channels,
                "integrity_errors": self._integrity_errors,
                "last_error": self._last_error,
                "measured_elapsed_s": measured_elapsed_s,
                "achieved_rate_hz": self._measured / measured_elapsed_s if measured_elapsed_s > 0 else 0.0,
                "total_elapsed_ms": total_elapsed_ms,
                "scheduled_requests": self._expected_count + self._warmup if self._expected_count > 0 else "unbounded",
                "latency_scope": "request_sent_at_sync_to_gateway_acceptance",
                "process_user_cpu_s": usage.ru_utime,
                "process_system_cpu_s": usage.ru_stime,
                "process_peak_rss_bytes": peak_rss_bytes,
            },
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Python api.actuator.drive.v1 gateway fixture.")
    parser.add_argument("--name", default="drive_gateway", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--port", type=int, default=0, help="Control listen port override. Default is control-assigned.")
    parser.add_argument("--expected-count", type=int, default=0, help="Measured request count before auto-exit. 0 means run until interrupted.")
    parser.add_argument("--warmup", type=int, default=0, help="Warmup request count excluded from stats.")
    parser.add_argument("--payload-size", "--request-size", dest="payload_size", type=int, default=None,
                        help="Expected request payload size; derives the accepted vector length. Default accepts any length.")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-request output.")
    parser.add_argument("--report-format", choices=["text", "json"], default="text", help="Final report format.")
    parser.add_argument("--report-path", default=None, help="Optional output path for the final report.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_control_running()
    instance = DriveGatewaySubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        port=args.port,
        expected_count=args.expected_count,
        warmup=args.warmup,
        channels=channels_for_payload(args.payload_size) if args.payload_size is not None else None,
        quiet=args.quiet,
    )
    run_subsystem(instance, should_stop=instance.done)
    write_report(report_format=args.report_format, report_path=args.report_path, report=instance.report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
