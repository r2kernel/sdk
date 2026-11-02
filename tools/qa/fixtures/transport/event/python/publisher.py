from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path


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
from benchmark import effective_payload_size, make_payload, topic_name
from start_gate import StartGate

try:
    from r2kernel.wrapper.subsystem import Subsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class PublishSubsystem(Subsystem):
    def __init__(
        self,
        *,
        name: str = "publisher",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        topic_count: int = 1,
        topic_prefix: str = "x",
        payload_size: int = 8,
        period_us: int = 1_000_000,
        count: int = 0,
        warmup: int = 0,
        queue_depth: int | None = None,
        startup_delay_ms: int = 0,
        start_gate_file: str | None = None,
        start_gate_session: str | None = None,
        start_gate_timeout_ms: int = 30_000,
        publish_mode: str = "round-robin",
        quiet: bool = False,
    ) -> None:
        if bool(start_gate_file) != bool(start_gate_session):
            raise ValueError("--start-gate-file and --start-gate-session are required together")
        self._gate = StartGate(start_gate_file) if start_gate_file else None
        if self._gate is not None:
            self._gate.acknowledge(start_gate_session, name)
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "publisher"),
            manifest_path=manifest_path or default_manifest_path(__file__, name),
        )
        self._topic_count = max(topic_count, 1)
        self._topic_prefix = topic_prefix
        self._requested_payload_size = max(payload_size, 0)
        self._payload_size = effective_payload_size(payload_size)
        self._period_us = max(period_us, 0)
        self._count = max(count, 0)
        self._warmup = max(warmup, 0)
        self._queue_depth = queue_depth
        self._startup_delay_ms = max(startup_delay_ms, 0)
        self._start_gate_file = Path(start_gate_file) if start_gate_file else None
        self._start_gate_timeout_ms = max(start_gate_timeout_ms, 1)
        self._publish_mode = publish_mode
        self._quiet = quiet
        self._topics = [topic_name(self._topic_prefix, self._topic_count, i) for i in range(self._topic_count)]
        self._stop = threading.Event()
        self._done = threading.Event()
        self._worker: threading.Thread | None = None
        self._sent = 0
        self._topic_seq: dict[str, int] = {topic: 0 for topic in self._topics}
        self._started_ns = 0
        self._finished_ns = 0

    def on_initialize(self) -> bool:
        for topic in self._topics:
            self.register_event_publish(
                topic=topic,
                type="bytes",
                size=self._payload_size,
                priority=4,
                queue_depth=self._queue_depth,
            )
        self.logger.info("Lifecycle", "Publisher initialized")
        return True

    def on_start(self) -> bool:
        self._stop.clear()
        self._done.clear()
        self._started_ns = self.now_ns()
        self._worker = threading.Thread(target=self._publish_loop, daemon=True)
        self._worker.start()
        return True

    def on_terminate(self) -> bool:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=1.0)
            self._worker = None
        self.logger.info("Lifecycle", "Publisher terminated")
        return True

    def on_reset(self) -> bool:
        self._stop.set()
        self.logger.info("Lifecycle", "Publisher reset")
        return True

    def done(self) -> bool:
        return self._done.is_set()

    def workload_summary(self) -> dict[str, float | int | str]:
        elapsed_s = max((self._finished_ns or self.now_ns()) - self._started_ns, 0) / 1_000_000_000.0
        throughput = (self._sent / elapsed_s) if elapsed_s > 0 else 0.0
        return {
            "messages_sent": self._sent,
            "elapsed_s": elapsed_s,
            "throughput_mps": throughput,
            "topic_count": self._topic_count,
            "payload_size_requested": self._requested_payload_size,
            "payload_size_effective": self._payload_size,
            "period_us": self._period_us,
            "startup_delay_ms": self._startup_delay_ms,
            "start_gate_file": str(self._start_gate_file or ""),
            "publish_mode": self._publish_mode,
        }

    def _wait_for_start_gate(self) -> bool:
        if self._startup_delay_ms > 0:
            self._stop.wait(self._startup_delay_ms / 1000.0)
        if self._start_gate_file is None:
            return not self._stop.is_set()
        deadline = time.monotonic() + (self._start_gate_timeout_ms / 1000.0)
        while not self._stop.is_set():
            try:
                if self._gate.opened():
                    return True
            except (OSError, ValueError) as exc:
                self.logger.error("Lifecycle", f"Publisher start gate refused: {exc}")
                return False
            if time.monotonic() >= deadline:
                self.logger.error(
                    "Lifecycle",
                    f"Publisher start gate timed out path={self._start_gate_file} timeout_ms={self._start_gate_timeout_ms}",
                )
                return False
            self._stop.wait(0.01)
        return False

    def _publish_loop(self) -> None:
        if not self._wait_for_start_gate():
            self._finished_ns = self.now_ns()
            self._done.set()
            self._stop.set()
            return
        period_s = self._period_us / 1_000_000.0
        total_target = self._warmup + self._count if self._count > 0 else 0
        next_tick = time.perf_counter()
        while not self._stop.is_set():
            if total_target > 0 and self._sent >= total_target:
                self._finished_ns = self.now_ns()
                self._done.set()
                self._stop.set()
                return

            now = time.perf_counter()
            sleep_for = next_tick - now
            if sleep_for > 0:
                self._stop.wait(sleep_for)
                continue

            if self._publish_mode == "fanout":
                topics = self._topics
            else:
                topics = [self._topics[self._sent % len(self._topics)]]

            for topic in topics:
                if total_target > 0 and self._sent >= total_target:
                    break
                self._topic_seq[topic] += 1
                self._sent += 1
                timestamp = self.now_ns()
                payload = make_payload(self._topic_seq[topic], self._payload_size)
                self.publish_event(topic=topic, value=payload, timestamp=timestamp)
                if not self._quiet:
                    print(f"{self.name} topic={topic} seq={self._topic_seq[topic]} ts={timestamp}")

            if period_s > 0:
                next_tick += period_s
                if time.perf_counter() - next_tick > period_s:
                    next_tick = time.perf_counter() + period_s


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Python event publisher tutorial.")
    parser.add_argument("--name", default="publisher", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--period-ms", type=int, default=None, help="Publish period in milliseconds.")
    parser.add_argument("--period-us", type=int, default=None, help="Publish period in microseconds.")
    parser.add_argument("--topic-count", type=int, default=1, help="Number of topics to publish.")
    parser.add_argument("--topic-prefix", default="x", help="Topic prefix.")
    parser.add_argument("--payload-size", type=int, default=8, help="Payload size in bytes.")
    parser.add_argument("--count", type=int, default=0, help="Total number of messages to publish. 0 means run forever.")
    parser.add_argument("--warmup", type=int, default=0, help="Warmup message count sent before the measured count.")
    parser.add_argument("--queue-depth", type=int, default=None, help="Optional bounded event queue depth. Defaults to the SDK configuration.")
    parser.add_argument("--startup-delay-ms", type=int, default=0, help="Delay before the first publish.")
    parser.add_argument("--start-gate-file", default=None, help="Absent gate in a fresh private 0700 run directory; refuses preexisting paths.")
    parser.add_argument("--start-gate-session", help="Unique attempt identity shared with the gate helper; required with --start-gate-file.")
    parser.add_argument("--start-gate-timeout-ms", type=int, default=30_000, help="Maximum wait for --start-gate-file.")
    parser.add_argument(
        "--publish-mode",
        choices=["round-robin", "fanout"],
        default="round-robin",
        help="How topics are used each period.",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress per-message output.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_control_running()
    period_us = args.period_us if args.period_us is not None else (args.period_ms * 1000 if args.period_ms is not None else 1_000_000)

    instance = PublishSubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        topic_count=args.topic_count,
        topic_prefix=args.topic_prefix,
        payload_size=args.payload_size,
        period_us=period_us,
        count=args.count,
        warmup=args.warmup,
        queue_depth=args.queue_depth,
        startup_delay_ms=args.startup_delay_ms,
        start_gate_file=args.start_gate_file,
        start_gate_session=args.start_gate_session,
        start_gate_timeout_ms=args.start_gate_timeout_ms,
        publish_mode=args.publish_mode,
        quiet=args.quiet,
    )
    run_subsystem(instance, should_stop=instance.done)
    summary = instance.workload_summary()
    print(
        "publisher.summary "
        f"messages_sent={summary['messages_sent']} elapsed_s={summary['elapsed_s']:.3f} "
        f"throughput_mps={summary['throughput_mps']:.3f} topic_count={summary['topic_count']} "
        f"payload_size_requested={summary['payload_size_requested']} "
        f"payload_size_effective={summary['payload_size_effective']} "
        f"period_us={summary['period_us']} startup_delay_ms={summary['startup_delay_ms']} "
        f"start_gate_file={summary['start_gate_file']} publish_mode={summary['publish_mode']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
