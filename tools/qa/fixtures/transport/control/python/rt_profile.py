"""Cold Linux thread setup and bounded absolute-window sample accounting."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
from array import array
from pathlib import Path

POLICIES = {"other": os.SCHED_OTHER, "fifo": os.SCHED_FIFO, "rr": os.SCHED_RR}
MAX_SAMPLES = 1_000_000


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rt-policy", choices=list(POLICIES), default="other")
    parser.add_argument("--rt-priority", type=int, default=0)
    parser.add_argument("--rt-cpus", default=None)
    parser.add_argument("--rt-require-isolated", action="store_true")
    parser.add_argument("--prepare-local", action="store_true")
    parser.add_argument("--preparation-timeout-ms", type=int, default=1000)
    parser.add_argument("--measurement-seconds", type=float, default=None)
    parser.add_argument("--samples-path", default=None)
    parser.add_argument("--worker-ready-path", default=None)


def cpu_list(text: str) -> list[int]:
    values = []
    for item in text.strip().split(","):
        if not item:
            continue
        bounds = item.split("-")
        if len(bounds) == 1:
            values.append(int(item))
        elif len(bounds) == 2:
            first, last = map(int, bounds)
            if first > last:
                raise ValueError("invalid CPU range")
            values.extend(range(first, last + 1))
        else:
            raise ValueError("invalid CPU range")
    return values


def profile_from_args(args: argparse.Namespace) -> dict | None:
    explicit = any(value.split("=", 1)[0] in {
        "--rt-policy", "--rt-priority", "--rt-cpus", "--rt-require-isolated"
    } for value in sys.argv[1:])
    if args.preparation_timeout_ms <= 0:
        raise ValueError("preparation-timeout-ms must be positive")
    if args.measurement_seconds is not None and (
        not math.isfinite(args.measurement_seconds) or not 1 <= args.measurement_seconds <= 30
    ):
        raise ValueError("measurement-seconds must be finite and between 1 and 30")
    if not explicit:
        return None
    return validate_profile(args.rt_policy, args.rt_priority, args.rt_cpus, args.rt_require_isolated)


def validate_profile(policy: str, priority: int, cpus_csv: str | None, isolated: bool) -> dict:
    if policy not in POLICIES or (priority != 0 if policy == "other" else not 1 <= priority <= 99):
        raise ValueError("OTHER requires priority 0; FIFO/RR require priority 1..99")
    allowed = set(os.sched_getaffinity(0))
    if cpus_csv is None:
        cpus = sorted(allowed)
    else:
        parts = cpus_csv.split(",")
        if any(not value.strip() for value in parts):
            raise ValueError("rt-cpus must not contain empty entries")
        cpus = [int(value) for value in parts]
        if len(cpus) != len(set(cpus)) or not set(cpus) <= allowed:
            raise ValueError("rt-cpus must be unique and within the current allowed mask")
        cpus.sort()
    if isolated and cpus_csv is None:
        raise ValueError("rt-require-isolated requires explicit rt-cpus")
    evidence = None
    if isolated:
        path = Path("/sys/devices/system/cpu/isolated")
        raw = path.read_text(encoding="utf-8").strip()
        if not set(cpus) <= set(cpu_list(raw)):
            raise ValueError("selected CPUs are not in the preexisting isolated mask")
        evidence = {"path": str(path), "mask": raw}
    return {"policy": policy, "priority": priority, "cpus": cpus, "isolation_evidence": evidence}


def read_profile() -> dict:
    policy = os.sched_getscheduler(0)
    return {"tid": threading.get_native_id(), "policy": policy,
            "policy_name": next((name for name, value in POLICIES.items() if value == policy), str(policy)),
            "priority": os.sched_getparam(0).sched_priority, "cpus": sorted(os.sched_getaffinity(0))}


def apply_profile(requested: dict | None) -> dict:
    if requested is not None:
        os.sched_setaffinity(0, requested["cpus"])
        os.sched_setscheduler(0, POLICIES[requested["policy"]], os.sched_param(requested["priority"]))
    actual = read_profile()
    if requested is not None:
        if (actual["policy"] != POLICIES[requested["policy"]] or
                actual["priority"] != requested["priority"] or actual["cpus"] != requested["cpus"]):
            raise RuntimeError("thread profile readback mismatch")
        actual["isolation_evidence"] = requested["isolation_evidence"]
    actual["explicit"] = requested is not None
    return actual


def write_json(path: str | Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)

_SAMPLE_FIELDS = (
    "sequence", "slot", "scheduled_ns", "started_ns", "returned_ns", "ok",
    "integrity_ok", "sdk_started_ns", "sdk_returned_ns", "caller_wall_ns",
    "task_wall_ns", "sdk_rtt_ns",
)



class PeriodicWindow:
    def __init__(self, start_ns: int, duration_ns: int, period_ns: int, deadline_ns: int):
        if duration_ns <= 0 or period_ns <= 0 or deadline_ns <= 0:
            raise ValueError("duration, period and deadline must be positive")
        self.start = start_ns
        self.end = start_ns + duration_ns
        self.period = period_ns
        self.deadline = deadline_ns
        self.slots = (duration_ns + period_ns - 1) // period_ns
        if self.slots > MAX_SAMPLES:
            raise ValueError("measurement exceeds bounded sample capacity")
        self.next_slot = 0
        # Retained per-tick dictionaries advance Python's GC allocation count,
        # even when their atomic contents are untracked. Keep numeric storage
        # cold-allocated and materialize JSON records only after measurement.
        self._sample_values = array("q", [0]) * (self.slots * len(_SAMPLE_FIELDS))
        self._sample_errors: list[str | None] = [None] * self.slots
        self._rtt_present = bytearray(self.slots)
        self.attempted = self.skipped = self.successes = self.failures = 0
        self.completion_violations = self.caller_violations = self.after_window = 0

    def due(self, now_ns: int) -> tuple[int, int] | None:
        if now_ns >= self.end:
            self.skipped += self.slots - self.next_slot
            self.next_slot = self.slots
            return None
        current = min(max((now_ns - self.start) // self.period, 0), self.slots)
        if current > self.next_slot:
            self.skipped += current - self.next_slot
            self.next_slot = current
        if self.next_slot >= self.slots:
            return None
        return self.next_slot, self.start + self.next_slot * self.period

    def record(self, sample: dict) -> None:
        slot = sample["slot"]
        if slot != self.next_slot:
            raise ValueError("sample slot is not the next scheduled slot")
        offset = self.attempted * len(_SAMPLE_FIELDS)
        self._rtt_present[self.attempted] = sample["sdk_rtt_ns"] is not None
        for column, name in enumerate(_SAMPLE_FIELDS):
            value = sample[name]
            self._sample_values[offset + column] = 0 if value is None else value
        self._sample_errors[self.attempted] = sample["error"]
        self.attempted += 1
        self.next_slot = slot + 1
        success = bool(sample["ok"] and sample["integrity_ok"])
        self.successes += int(success)
        self.failures += int(not success)
        self.completion_violations += int(sample["returned_ns"] > sample["scheduled_ns"] + self.deadline)
        self.caller_violations += int(sample["caller_wall_ns"] > self.deadline)
        self.after_window += int(sample["returned_ns"] > self.end)
        # No overdue dispatch immediately after a late completion. Exact ticks remain eligible.
        next_future = min(self.slots, max(self.next_slot,
            (sample["returned_ns"] - self.start + self.period - 1) // self.period))
        self.skipped += next_future - self.next_slot
        self.next_slot = next_future

    def report(self) -> dict:
        functional = self.failures == 0 and self.attempted > 0
        samples = []
        for row in range(self.attempted):
            offset = row * len(_SAMPLE_FIELDS)
            sample = {name: self._sample_values[offset + column]
                      for column, name in enumerate(_SAMPLE_FIELDS)}
            sample["ok"] = bool(sample["ok"])
            sample["integrity_ok"] = bool(sample["integrity_ok"])
            if not self._rtt_present[row]:
                sample["sdk_rtt_ns"] = None
            sample["error"] = self._sample_errors[row]
            samples.append(sample)
        in_window = sum(sample["ok"] and sample["integrity_ok"] and sample["returned_ns"] <= self.end
                        for sample in samples)
        return {"scope": "scheduled_tick_to_owned_response_and_validated_release", "language": "python",
                "application": "synthetic_echo", "period_ns": self.period,
                "completion_deadline_ns": self.deadline, "start_monotonic_ns": self.start,
                "end_monotonic_ns": self.end, "scheduled_slots": self.slots,
                "attempted": self.attempted, "successes": self.successes, "failures": self.failures,
                "skipped_slots": self.skipped, "completion_deadline_violations": self.completion_violations,
                "caller_deadline_violations": self.caller_violations, "completed_after_window": self.after_window,
                "attempt_rate_hz": self.attempted * 1e9 / (self.end - self.start),
                "achieved_rate_hz": in_window * 1e9 / (self.end - self.start),
                "successful_completions_in_window": in_window,
                "functional_passed": functional,
                "performance_passed": functional and self.attempted == self.slots and not (
                    self.skipped or self.completion_violations or self.caller_violations or self.after_window),
                "samples": samples}
