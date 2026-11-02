from __future__ import annotations

import argparse
import sys
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
from benchmark import LatencyStats, effective_payload_size, payload_seq, summary_dict, topic_name, write_report

try:
    from r2kernel.wrapper.subsystem import Subsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class SubscriberSubsystem(Subsystem):
    def __init__(
        self,
        *,
        name: str = "subscriber",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        topic_count: int = 1,
        topic_prefix: str = "x",
        payload_size: int = 8,
        expected_count: int = 0,
        warmup: int = 0,
        quiet: bool = False,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "subscriber"),
            manifest_path=manifest_path or default_manifest_path(__file__, name),
        )
        self._topic_count = max(topic_count, 1)
        self._topic_prefix = topic_prefix
        self._requested_payload_size = max(payload_size, 0)
        self._payload_size = effective_payload_size(payload_size)
        self._expected_count = max(expected_count, 0)
        self._warmup = max(warmup, 0)
        self._quiet = quiet
        self._topics = [topic_name(self._topic_prefix, self._topic_count, i) for i in range(self._topic_count)]
        self._stats = LatencyStats()
        self._received_total = 0
        self._measured = 0
        self._dropped = 0
        self._prefix_missing = 0
        self._gap_missing = 0
        self._last_seq: dict[str, int] = {}
        self._first_seq: dict[str, int] = {}
        self._per_topic: dict[str, int] = {topic: 0 for topic in self._topics}
        self._done = False

    def on_initialize(self) -> bool:
        for topic in self._topics:
            self.register_event_subscribe(
                topic=topic,
                type="bytes",
                size=self._payload_size,
                # Keyword-only: the SDK passes timing metadata to a third positional parameter.
                callback=lambda payload, timestamp, *, topic=topic: self.on_event(topic, payload, timestamp),
            )
        self.logger.info("Lifecycle", "Subscriber initialized")
        return True

    def on_start(self) -> bool:
        return True

    def on_terminate(self) -> bool:
        self.logger.info("Lifecycle", "Subscriber terminated")
        return True

    def on_reset(self) -> bool:
        self.logger.info("Lifecycle", "Subscriber reset")
        return True

    def done(self) -> bool:
        return self._done

    def report(self) -> dict:
        expected_total = self._warmup + self._expected_count if self._expected_count > 0 else 0
        observed_total = self._received_total + self._dropped
        tail_missing = max(0, expected_total - observed_total) if expected_total > 0 else 0
        report = {
            "kind": "event",
            "role": "subscriber",
            **summary_dict(self._stats),
            "dropped": self._dropped,
            "extra": {
                "topic_count": self._topic_count,
                "payload_size_requested": self._requested_payload_size,
                "payload_size_effective": self._payload_size,
                "expected_count": self._expected_count,
                "expected_total": expected_total,
                "received_total": self._received_total,
                "observed_total": observed_total,
                "measured": self._measured,
                "warmup": self._warmup,
                "prefix_missing": self._prefix_missing,
                "gap_missing": self._gap_missing,
                "tail_missing": tail_missing,
                "per_topic": self._per_topic,
                "per_topic_first_seq": self._first_seq,
                "per_topic_last_seq": self._last_seq,
            },
        }
        return report

    def on_event(self, topic: str, payload: bytes, timestamp: int) -> None:
        if self._done:
            return
        self._received_total += 1
        self._per_topic[topic] = self._per_topic.get(topic, 0) + 1
        seq = payload_seq(payload)
        last_seq = self._last_seq.get(topic)
        if last_seq is None:
            self._first_seq[topic] = seq
            if seq > 1:
                missing = seq - 1
                self._prefix_missing += missing
                self._dropped += missing
        elif seq > last_seq + 1:
            missing = seq - last_seq - 1
            self._gap_missing += missing
            self._dropped += missing
        self._last_seq[topic] = seq
        observed_total = self._received_total + self._dropped

        if self._received_total <= self._warmup:
            if self._expected_count > 0 and observed_total >= self._warmup + self._expected_count:
                self._done = True
            return

        latency_us = (self.now_ns() - timestamp) / 1_000.0
        self._stats.add_us(latency_us)
        self._measured += 1
        if not self._quiet:
            print(f"{self.name} topic={topic} seq={seq} latency_us={latency_us:.3f}")
        if self._expected_count > 0 and observed_total >= self._warmup + self._expected_count:
            self._done = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Python event subscriber tutorial.")
    parser.add_argument("--name", default="subscriber", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--topic-count", type=int, default=1, help="Number of topics to subscribe.")
    parser.add_argument("--topic-prefix", default="x", help="Topic prefix.")
    parser.add_argument("--payload-size", type=int, default=8, help="Expected payload size in bytes.")
    parser.add_argument("--expected-count", type=int, default=0, help="Measured message count before auto-exit. 0 means run until interrupted.")
    parser.add_argument("--warmup", type=int, default=0, help="Number of initial received messages to exclude from stats.")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-message output.")
    parser.add_argument("--report-format", choices=["text", "json"], default="text", help="Final report format.")
    parser.add_argument("--report-path", default=None, help="Optional output path for the final report.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_control_running()
    instance = SubscriberSubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        topic_count=args.topic_count,
        topic_prefix=args.topic_prefix,
        payload_size=args.payload_size,
        expected_count=args.expected_count,
        warmup=args.warmup,
        quiet=args.quiet,
    )
    run_subsystem(instance, should_stop=instance.done)
    write_report(report_format=args.report_format, report_path=args.report_path, report=instance.report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
