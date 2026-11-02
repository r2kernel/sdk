"""Bounded, opt-in JSONL samples for Control-managed exchange stages."""

from __future__ import annotations

import atexit
from collections import deque
import json
import os
import sys
import threading
from typing import Any

_ENABLED_ENV = "R2KERNEL_CONTROL_COST_METRICS"
_CAPACITY_ENV = "R2KERNEL_CONTROL_COST_METRICS_MAX_SAMPLES"
_DEFAULT_CAPACITY = 256
_MAX_CAPACITY = 4096


class ControlCostMetrics:
    """Keep a bounded recent sample ring and emit it to stderr when stopped."""

    def __init__(self, *, enabled: bool | None = None, capacity: int | None = None):
        if enabled is None:
            enabled_value = os.getenv(_ENABLED_ENV, "0").strip().lower()
            if enabled_value in {"1", "true", "yes", "on"}:
                enabled = True
            elif enabled_value in {"", "0", "false", "no", "off"}:
                enabled = False
            else:
                raise ValueError(f"invalid {_ENABLED_ENV} value {enabled_value!r}; use 1 or 0")
        if capacity is None:
            raw_capacity = os.getenv(_CAPACITY_ENV) if enabled else None
            capacity = _DEFAULT_CAPACITY if raw_capacity is None else int(raw_capacity)
        if not 1 <= capacity <= _MAX_CAPACITY:
            raise ValueError(f"{_CAPACITY_ENV} must be between 1 and {_MAX_CAPACITY}")
        self.enabled = enabled
        self.capacity = capacity
        self._samples: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._total_samples = 0
        self._flushed = False
        self._lock = threading.Lock()
        if self.enabled:
            atexit.register(self.flush)

    def record(self, sample: dict[str, Any]) -> None:
        if not self.enabled:
            return
        bounded_sample = dict(sample)
        for label_key in ("target", "topic"):
            label = bounded_sample.get(label_key)
            if isinstance(label, str):
                bounded_sample[label_key] = label[:128]
        with self._lock:
            self._total_samples += 1
            self._samples.append(bounded_sample)

    def flush(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._flushed:
                return
            self._flushed = True
            samples = list(self._samples)
            total_samples = self._total_samples
        summary = {
            "event": "control_cost_summary",
            "runtime": "python_sdk",
            "capacity": self.capacity,
            "total_samples": total_samples,
            "retained_samples": len(samples),
            "overwritten_samples": max(0, total_samples - len(samples)),
        }
        print(json.dumps(summary, separators=(",", ":")), file=sys.stderr, flush=True)
        for sample in samples:
            output = {"event": "control_cost_sample", "runtime": "python_sdk", **sample}
            print(json.dumps(output, separators=(",", ":")), file=sys.stderr, flush=True)
