from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class Summary:
    samples: int
    min_us: float
    avg_us: float
    p50_us: float
    p95_us: float
    p99_us: float
    max_us: float


class LatencyStats:
    def __init__(self) -> None:
        self._samples: list[float] = []

    def add_us(self, value_us: float) -> None:
        self._samples.append(float(value_us))

    @property
    def samples(self) -> int:
        return len(self._samples)

    def summary(self) -> Summary:
        if not self._samples:
            return Summary(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        ordered = sorted(self._samples)
        total = float(sum(ordered))
        return Summary(
            samples=len(ordered),
            min_us=ordered[0],
            avg_us=total / len(ordered),
            p50_us=_percentile(ordered, 50.0),
            p95_us=_percentile(ordered, 95.0),
            p99_us=_percentile(ordered, 99.0),
            max_us=ordered[-1],
        )


def topic_name(prefix: str, topic_count: int, index: int) -> str:
    if topic_count <= 1:
        return prefix
    return f"{prefix}{index}"


def make_payload(seq: int, payload_size: int) -> bytes:
    size = max(payload_size, 8)
    payload = bytearray(size)
    payload[:8] = int(seq).to_bytes(8, byteorder="big", signed=False)
    if size > 8:
        fill = seq & 0xFF
        payload[8:] = bytes([fill]) * (size - 8)
    return bytes(payload)


def payload_seq(payload: bytes | bytearray | memoryview) -> int:
    view = memoryview(payload)
    if len(view) < 8:
        return 0
    return int.from_bytes(view[:8], byteorder="big", signed=False)


def effective_payload_size(payload_size: int) -> int:
    return max(int(payload_size), 8)


def write_report(*, report_format: str, report_path: str | None, report: dict[str, Any]) -> None:
    report = _with_clock_snapshot(report)
    text = render_report(report_format=report_format, report=report)
    print(text)
    if report_path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if report_format == "json":
            path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            path.write_text(text + "\n", encoding="utf-8")


def render_report(*, report_format: str, report: dict[str, Any]) -> str:
    if report_format == "json":
        return json.dumps(report, indent=2, sort_keys=True)
    lines = [
        f"report.kind={report.get('kind')}",
        f"report.role={report.get('role')}",
        f"report.samples={report.get('samples', 0)}",
    ]
    if "failures" in report:
        lines.append(f"report.failures={report.get('failures', 0)}")
    if "dropped" in report:
        lines.append(f"report.dropped={report.get('dropped', 0)}")
    lines.extend(
        [
            f"report.min_us={_fmt(report.get('min_us'))}",
            f"report.avg_us={_fmt(report.get('avg_us'))}",
            f"report.p50_us={_fmt(report.get('p50_us'))}",
            f"report.p95_us={_fmt(report.get('p95_us'))}",
            f"report.p99_us={_fmt(report.get('p99_us'))}",
            f"report.max_us={_fmt(report.get('max_us'))}",
        ]
    )
    extra = report.get("extra") or {}
    for key in sorted(extra):
        lines.append(f"report.extra.{key}={extra[key]}")
    clock = report.get("clock") or {}
    if isinstance(clock, dict):
        for key in sorted(clock):
            lines.append(f"report.clock.{key}={clock[key]}")
    return "\n".join(lines)


def summary_dict(stats: LatencyStats) -> dict[str, Any]:
    return asdict(stats.summary())


def _percentile(ordered: list[float], pct: float) -> float:
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _fmt(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.3f}"
    return str(value)


def _with_clock_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    if "clock" in report:
        return report
    enriched = dict(report)
    try:
        from r2kernel.core.clock import clock_snapshot

        enriched["clock"] = clock_snapshot().to_dict()
    except Exception as exc:
        enriched["clock"] = {
            "source": "unknown",
            "stable": False,
            "accepted_for_one_way": False,
            "error": str(exc),
        }
    return enriched
