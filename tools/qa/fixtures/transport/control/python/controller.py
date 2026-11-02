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

from rt_profile import PeriodicWindow, add_arguments, apply_profile, profile_from_args, write_json


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "tutorials" / "support" / "python").is_dir():
            return parent
    raise RuntimeError(f"could not resolve repository root from {__file__}")


_SUPPORT_PY = repo_root() / "tutorials" / "support" / "python"
if str(_SUPPORT_PY) not in sys.path:
    sys.path.insert(0, str(_SUPPORT_PY))

from runtime import (
    default_deployment_manifest_path,
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
    run_subsystem,
)
from benchmark import LatencyStats, make_payload, summary_dict, topic_name, write_report

try:
    from r2kernel.wrapper.subsystem import Subsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class ControllerSubsystem(Subsystem):
    def __init__(
        self,
        *,
        name: str = "control_controller",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        deployment_manifest_path: str | None = None,
        binding: str = "gateway",
        target: str | None = None,
        topic_count: int = 1,
        topic_prefix: str = "control_cmd",
        payload_size: int = 8,
        request_sizes: list[int] | None = None,
        request_size_block_length: int = 1,
        response_size: int = 8,
        period_us: int = 1_000_000,
        count: int = 0,
        warmup: int = 0,
        timeout_ms: int = 1000,
        route_ready_timeout_s: float = 30.0,
        deadline_ms: float | None = None,
        priority: int | None = None,
        request_mode: str = "round-robin",
        start_gate_path: str | None = None,
        ready_path: str | None = None,
        gate_timeout_s: float = 60.0,
        size_block_progress_path: str | None = None,
        size_block_ack_timeout_s: float = 30.0,
        quiet: bool = False,
        rt_profile: dict | None = None,
        prepare_local: bool = False,
        preparation_timeout_ms: int = 1000,
        measurement_seconds: float | None = None,
        samples_path: str | None = None,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "controller"),
            manifest_path=manifest_path or default_manifest_path(__file__, name),
            deployment_manifest_path=deployment_manifest_path or default_deployment_manifest_path(__file__),
        )
        self._target = target or self.binding_target(binding)
        self._topic_count = max(topic_count, 1)
        self._topic_prefix = topic_prefix
        self._payload_size = max(payload_size, 8)
        self._request_sizes = [max(int(size), 8) for size in (request_sizes or [self._payload_size])]
        self._request_size_block_length = max(int(request_size_block_length), 1)
        self._response_size = max(response_size, 8)
        self._period_us = max(period_us, 0)
        self._count = max(count, 0)
        self._warmup = max(warmup, 0)
        self._timeout_ms = max(timeout_ms, 1)
        self._route_ready_timeout_s = max(route_ready_timeout_s, 0.0)
        self._deadline_ms = float(deadline_ms if deadline_ms is not None else max(self._timeout_ms, int(self._period_us / 1000) * 2 or 1000))
        self._priority = priority
        self._request_mode = request_mode
        self._start_gate_path = Path(start_gate_path) if start_gate_path else None
        self._ready_path = Path(ready_path) if ready_path else None
        self._gate_timeout_s = max(float(gate_timeout_s), 0.0)
        self._size_block_progress_path = Path(size_block_progress_path) if size_block_progress_path else None
        self._size_block_ack_timeout_s = max(float(size_block_ack_timeout_s), 0.0)
        self._quiet = quiet
        self._rtt_measurement_scope = "runtime_rtt_ns_from_transact_control"
        self._topics = [topic_name(self._topic_prefix, self._topic_count, i) for i in range(self._topic_count)]
        self._stop = threading.Event()
        self._done = threading.Event()
        self._worker: threading.Thread | None = None
        self._stats = LatencyStats()
        self._caller_stats = LatencyStats()
        self._attempts = 0
        self._success = 0
        self._failures = 0
        self._integrity_errors = 0
        self._deadline_violations = 0
        self._period_violations = 0
        self._seq = 0
        self._measured_started_ns: int | None = None
        self._measured_finished_ns: int | None = None
        self._measured_cpu_started_ns: int | None = None
        self._measured_cpu_finished_ns: int | None = None
        self._late_cycles = 0
        self._run_started_ns: int | None = None
        self._run_finished_ns: int | None = None
        self._last_error: str | None = None
        self._rt_profile = rt_profile
        self._prepare_local = prepare_local
        self._preparation_timeout_ms = preparation_timeout_ms
        self._measurement_seconds = measurement_seconds
        self._samples_path = samples_path
        self._thread_profile = None
        self._periodic = None
        self._preparation = {"ok": None, "duration_ns": 0,
                             "request_capacity": max(self._request_sizes), "response_capacity": self._response_size}
        self._first_warmup_ns = None
        self._common_start_ns = None
        if measurement_seconds is not None:
            if not 1 <= measurement_seconds <= 30 or self._period_us <= 0 or self._deadline_ms <= 0:
                raise ValueError("periodic measurement requires 1..30 seconds and positive period/deadline")
            if request_mode != "round-robin" or size_block_progress_path:
                raise ValueError("periodic measurement requires round-robin without disk stage acknowledgements")
            PeriodicWindow(0, int(measurement_seconds * 1e9), self._period_us * 1000, int(self._deadline_ms * 1e6))

    def on_initialize(self) -> bool:
        self.logger.info("Lifecycle", "Control controller initialized")
        return True

    def on_start(self) -> bool:
        route_deadline = time.monotonic() + self._route_ready_timeout_s
        route = None
        while time.monotonic() <= route_deadline:
            route = self.resolve_control_target(self._target)
            if route and route.get("host") and (
                int(route.get("control_port") or 0) > 0
                or (route.get("route_kind") == "local_control" and int(route.get("control_port") or 0) == 0)
            ):
                break
            time.sleep(0.2)
        host = str((route or {}).get("host") or "")
        port = int((route or {}).get("control_port") or 0)
        local_only = port == 0 and (route or {}).get("route_kind") == "local_control"
        if not host or port < 0 or (port == 0 and not local_only):
            self.logger.error(
                "Lifecycle",
                f"Control target {self._target} did not publish a Control route "
                f"within {self._route_ready_timeout_s:.1f}s",
            )
            return False
        self.connect_control(self._target, host, port)
        if not self._prepare_local and not self.handshake_control(self._target, timeout_ms=self._timeout_ms):
            self.logger.error("Lifecycle", f"Control handshake failed target={self._target} host={host} port={port}")
            return False
        self._stop.clear()
        self._done.clear()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()
        return True

    def on_terminate(self) -> bool:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=1.0)
            self._worker = None
        if self._run_started_ns is not None and self._run_finished_ns is None:
            self._run_finished_ns = time.perf_counter_ns()
        self.logger.info("Lifecycle", "Control controller terminated")
        return True

    def on_reset(self) -> bool:
        self._stop.set()
        return True

    def done(self) -> bool:
        return self._done.is_set()

    def report(self) -> dict:
        measured_elapsed_s = 0.0
        if self._measured_started_ns is not None and self._measured_finished_ns is not None:
            measured_elapsed_s = max(
                (self._measured_finished_ns - self._measured_started_ns) / 1_000_000_000.0,
                0.0,
            )
        measured_attempts = max(self._attempts - self._warmup, 0)
        measured_successes = self._stats.samples
        process_usage = resource.getrusage(resource.RUSAGE_SELF)
        peak_rss_bytes = int(process_usage.ru_maxrss)
        if sys.platform != "darwin":
            peak_rss_bytes *= 1024
        total_elapsed_ms = 0.0
        if self._run_started_ns is not None and self._run_finished_ns is not None:
            total_elapsed_ms = (self._run_finished_ns - self._run_started_ns) / 1_000_000.0
        return {
            "kind": "control",
            "role": "controller",
            "latency_scope": self._rtt_measurement_scope,
            **summary_dict(self._stats),
            "caller_wall_clock": summary_dict(self._caller_stats),
            "failures": self._failures,
            "dropped": 0,
            "extra": {
                "attempts": self._attempts,
                "success": self._success,
                "topic_count": self._topic_count,
                "payload_size": self._payload_size,
                "request_payload_size": self._payload_size if len(set(self._request_sizes)) == 1 else None,
                "request_payload_sizes": sorted(set(self._request_sizes)),
                "request_size_sequence": list(self._request_sizes),
                "request_size_block_length": self._request_size_block_length,
                "response_payload_size": self._response_size,
                "period_us": self._period_us,
                "timeout_ms": self._timeout_ms,
                "route_ready_timeout_s": self._route_ready_timeout_s,
                "total_elapsed_ms": total_elapsed_ms,
                "deadline_ms": self._deadline_ms,
                "priority": self._priority if self._priority is not None else "auto",
                "request_mode": self._request_mode,
                "warmup": self._warmup,
                "scheduled_requests": self._count + self._warmup if self._count > 0 else "unbounded",
                "integrity_errors": self._integrity_errors,
                "deadline_violations": self._deadline_violations,
                "period_violations": self._period_violations,
                "late_cycles": self._late_cycles,
                "last_error": self._last_error,
                "measured_elapsed_s": measured_elapsed_s,
                "achieved_rate_hz": measured_attempts / measured_elapsed_s if measured_elapsed_s > 0 else 0.0,
                "successful_rate_hz": measured_successes / measured_elapsed_s if measured_elapsed_s > 0 else 0.0,
                "rtt_measurement_scope": self._rtt_measurement_scope,
                "process_user_cpu_s": process_usage.ru_utime,
                "process_system_cpu_s": process_usage.ru_stime,
                "measured_process_cpu_s": (
                    (self._measured_cpu_finished_ns - self._measured_cpu_started_ns) / 1e9
                    if self._measured_cpu_started_ns is not None and self._measured_cpu_finished_ns is not None
                    else None
                ),
                "process_peak_rss_bytes": peak_rss_bytes,
                "measured_start_monotonic_ns": self._measured_started_ns,
                "measured_end_monotonic_ns": self._measured_finished_ns,
                "periodic": self._periodic,
                "thread_profile": self._thread_profile,
                "preparation": self._preparation,
                "first_warmup_ns": self._first_warmup_ns,
            },
        }

    def _loop(self) -> None:
        try:
            self._thread_profile = apply_profile(self._rt_profile)
            if self._prepare_local:
                started = time.monotonic_ns()
                try:
                    for topic in self._topics:
                        remaining_ms = self._preparation_timeout_ms - (time.monotonic_ns() - started) / 1e6
                        if remaining_ms < 1 or not self.prepare_control(
                            self._target, topic, max(self._request_sizes), self._response_size,
                            int(remaining_ms), False,
                        ):
                            self._preparation["ok"] = False
                            raise RuntimeError("bounded provider-ready Control preparation failed")
                    self._preparation["ok"] = True
                finally:
                    self._preparation["duration_ns"] = time.monotonic_ns() - started
            if self._measurement_seconds is not None:
                self._periodic_loop()
            else:
                self._fixed_loop()
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._failures += 1
            if self._measurement_seconds is not None:
                self._periodic = {"scope": "scheduled_tick_to_owned_response_and_validated_release",
                                  "language": "python", "setup_error": self._last_error,
                                  "application": "synthetic_echo", "period_ns": self._period_us * 1000,
                                  "completion_deadline_ns": int(self._deadline_ms * 1e6),
                                  "start_monotonic_ns": None, "end_monotonic_ns": None,
                                  "scheduled_slots": 0, "attempted": 0, "successes": 0, "failures": 0,
                                  "skipped_slots": 0, "completion_deadline_violations": 0,
                                  "caller_deadline_violations": 0, "completed_after_window": 0,
                                  "achieved_rate_hz": 0.0, "first_warmup_ns": self._first_warmup_ns,
                                  "functional_passed": False, "performance_passed": False,
                                  "preparation": self._preparation, "thread_profile": self._thread_profile,
                                  "samples": []}
        finally:
            self._run_finished_ns = time.monotonic_ns()
            self._done.set()
            self._stop.set()
            if self._samples_path and self._periodic is not None:
                write_json(self._samples_path, self._periodic)

    def _periodic_attempt(self, sequence: int, slot: int, scheduled_ns: int, measured: bool) -> dict:
        started = time.monotonic_ns()
        payload = make_payload(sequence, self._request_sizes[0] if not measured else
                               self._request_sizes[(slot // self._request_size_block_length) % len(self._request_sizes)])
        topic = self._topics[slot % len(self._topics)]
        period_ms = self._period_us / 1000.0
        last_tick_ns = self.now_ns()
        sdk_started = time.monotonic_ns()
        try:
            result = self.transact_control(
                target=self._target, topic=topic, type="bytes", value=payload,
                period_ms=period_ms, deadline_ms=self._deadline_ms,
                last_tick_ns=last_tick_ns, priority=self._priority)
        except Exception as exc:
            sdk_returned = time.monotonic_ns()
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        else:
            sdk_returned = time.monotonic_ns()
        response = result.get("response") or {}
        received = response.get("payload")
        integrity = (isinstance(received, (bytes, bytearray)) and
                     received == make_payload(sequence, self._response_size) and
                     int(response.get("status", 0) or 0) == 0)
        returned = time.monotonic_ns()
        error = str(result.get("error") or ("" if integrity else "response integrity/status mismatch"))
        sample = {"sequence": sequence, "slot": slot, "scheduled_ns": scheduled_ns,
                  "started_ns": started, "returned_ns": returned, "ok": bool(result.get("ok")),
                  "sdk_started_ns": sdk_started, "sdk_returned_ns": sdk_returned,
                  "integrity_ok": integrity, "caller_wall_ns": sdk_returned - sdk_started,
                  "task_wall_ns": returned - started,
                  "sdk_rtt_ns": result.get("rtt_ns"), "error": error}
        self._attempts += 1
        if sample["ok"] and integrity:
            self._success += 1
            if measured and sample["sdk_rtt_ns"] is not None:
                self._stats.add_us(float(sample["sdk_rtt_ns"]) / 1000)
        elif measured:
            self._failures += 1
            self._last_error = error
            self._integrity_errors += int(bool(response) and not integrity)
        if measured:
            self._caller_stats.add_us(sample["caller_wall_ns"] / 1000)
        return sample

    def _periodic_loop(self) -> None:
        self._run_started_ns = time.monotonic_ns()
        for index in range(self._warmup):
            if self._stop.is_set():
                raise RuntimeError("stopped during warmup")
            sample = self._periodic_attempt(index, index, time.monotonic_ns(), False)
            if index == 0:
                self._first_warmup_ns = sample["task_wall_ns"]
            if not sample["ok"] or not sample["integrity_ok"]:
                raise RuntimeError(f"warmup failed: {sample['error']}")
        self._mark_ready_and_wait_for_release()
        if self._stop.is_set():
            raise RuntimeError(self._last_error or "stopped before measurement")
        start = self._common_start_ns if self._common_start_ns is not None else time.monotonic_ns()
        window = PeriodicWindow(start, int(self._measurement_seconds * 1e9),
                                self._period_us * 1000, int(self._deadline_ms * 1e6))
        self._measured_started_ns = start
        self._measured_cpu_started_ns = time.process_time_ns()
        while not self._stop.is_set():
            due = window.due(time.monotonic_ns())
            if due is None:
                self._stop.wait(max(window.end - time.monotonic_ns(), 0) / 1e9)
                break
            slot, scheduled = due
            delay = scheduled - time.monotonic_ns()
            if delay > 0:
                self._stop.wait(delay / 1e9)
                continue
            sample = self._periodic_attempt(self._warmup + window.attempted, slot, scheduled, True)
            window.record(sample)
        self._measured_finished_ns = time.monotonic_ns()
        self._measured_cpu_finished_ns = time.process_time_ns()
        self._periodic = {**window.report(), "preparation": self._preparation,
                          "thread_profile": self._thread_profile, "first_warmup_ns": self._first_warmup_ns}
        if self._stop.is_set():
            self._periodic["functional_passed"] = False
            self._periodic["performance_passed"] = False
            self._periodic["setup_error"] = "measurement interrupted"

    def _fixed_loop(self) -> None:
        self._run_started_ns = time.perf_counter_ns()
        total_target = self._warmup + self._count if self._count > 0 else 0
        period_s = self._period_us / 1_000_000.0
        next_tick = time.perf_counter()
        start_gate_released = False
        while not self._stop.is_set():
            if not start_gate_released and self._attempts >= self._warmup:
                self._mark_ready_and_wait_for_release()
                start_gate_released = True
                next_tick = self._common_start_ns / 1e9 if self._common_start_ns is not None else time.perf_counter()
                if self._stop.is_set():
                    return
            if total_target > 0 and self._attempts >= total_target:
                self._measured_cpu_finished_ns = time.process_time_ns()
                self._run_finished_ns = time.perf_counter_ns()
                self._done.set()
                self._stop.set()
                return

            now = time.perf_counter()
            sleep_for = next_tick - now
            if sleep_for > 0:
                self._stop.wait(sleep_for)
                continue

            if self._request_mode == "fanout":
                topics = self._topics
            else:
                topics = [self._topics[self._attempts % len(self._topics)]]

            for topic in topics:
                if total_target > 0 and self._attempts >= total_target:
                    break
                self._seq += 1
                self._attempts += 1
                measured = self._attempts > self._warmup
                if measured and self._measured_cpu_started_ns is None:
                    self._measured_cpu_started_ns = time.process_time_ns()
                attempt_started_ns = time.perf_counter_ns()
                if measured and self._measured_started_ns is None:
                    self._measured_started_ns = attempt_started_ns
                payload_size = self._payload_size
                if self._request_sizes:
                    attempt_index = self._attempts - 1
                    measured_attempt_index = max(attempt_index - self._warmup, 0)
                    size_stage = measured_attempt_index // self._request_size_block_length
                    size_index = size_stage % len(self._request_sizes)
                    payload_size = self._request_sizes[size_index]
                    if attempt_index < self._warmup:
                        payload_size = self._request_sizes[0]
                payload = make_payload(self._seq, payload_size)
                try:
                    result = self.transact_control(
                        target=self._target,
                        topic=topic,
                        type="bytes",
                        value=payload,
                        period_ms=self._period_us / 1000.0,
                        deadline_ms=self._deadline_ms,
                        last_tick_ns=self.now_ns(),
                        priority=self._priority,
                    )
                except Exception as exc:
                    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    self._last_error = result["error"]
                if result.get("rtt_measurement_scope"):
                    self._rtt_measurement_scope = result["rtt_measurement_scope"]
                response = result.get("response") or {}
                response_payload = response.get("payload")
                expected_response = make_payload(self._seq, self._response_size)
                response_status = int(response.get("status", 0) or 0)
                payload_integrity_ok = (
                    isinstance(response_payload, (bytes, bytearray))
                    and bytes(response_payload) == expected_response
                )
                integrity_ok = payload_integrity_ok and response_status == 0
                request_elapsed_ns = time.perf_counter_ns() - attempt_started_ns
                if measured:
                    self._measured_finished_ns = time.perf_counter_ns()
                    self._caller_stats.add_us(request_elapsed_ns / 1_000.0)
                    if request_elapsed_ns > self._deadline_ms * 1_000_000:
                        self._deadline_violations += 1
                    if self._period_us > 0 and request_elapsed_ns > self._period_us * 1_000:
                        self._period_violations += 1
                if result.get("ok") and integrity_ok:
                    self._success += 1
                    if measured:
                        self._stats.add_us(float(result.get("rtt_ns", 0)) / 1_000.0)
                    if not self._quiet:
                        print(f"{self.name} topic={topic} seq={self._seq} rtt_ms={result.get('rtt_ms', 0.0):.3f}")
                else:
                    if measured:
                        self._failures += 1
                    if response and not payload_integrity_ok:
                        self._integrity_errors += 1
                        self._last_error = (
                            f"response integrity mismatch: expected {self._response_size} bytes, "
                            f"received {len(response_payload) if isinstance(response_payload, (bytes, bytearray)) else 'no byte payload'}"
                        )
                    elif not result.get("ok"):
                        self._last_error = str(result.get("error") or "Control transaction returned ok=false")
                    elif response_status != 0:
                        self._last_error = f"Control response status={response_status}"
                    if not self._quiet:
                        detail = f"error={result.get('error')}"
                        if result.get("ok") and not integrity_ok:
                            detail += " integrity_error=true"
                        if "status" in result:
                            detail += f" status={result.get('status')}"
                        print(f"{self.name} topic={topic} seq={self._seq} failed {detail}")

                if self._request_sizes and self._size_block_progress_path and measured:
                    measured_attempt_index = self._attempts - self._warmup - 1
                    if measured_attempt_index % self._request_size_block_length == 0:
                        size_stage = measured_attempt_index // self._request_size_block_length
                        size_index = size_stage % len(self._request_sizes)
                        progress = {
                            "block_index": size_index,
                            "cycle_index": size_stage // len(self._request_sizes),
                            "request_size": self._request_sizes[size_index],
                            "attempts_completed": self._attempts,
                            "monotonic_ns": time.perf_counter_ns(),
                        }
                        stage_path = self._size_block_progress_path.with_name(
                            self._size_block_progress_path.name + f".stage-{size_stage}.json"
                        )
                        self._write_json_file(stage_path, progress)
                        if not self._wait_for_size_stage_ack(stage_path):
                            return

            if period_s > 0:
                next_tick += period_s
                finished_at = time.perf_counter()
                if finished_at > next_tick:
                    missed_cycles = int((finished_at - next_tick) / period_s) + 1
                    self._late_cycles += missed_cycles
                    next_tick += missed_cycles * period_s

    def _mark_ready_and_wait_for_release(self) -> None:
        if self._start_gate_path is None and self._ready_path is None:
            return
        if self._start_gate_path is None or self._ready_path is None:
            raise ValueError("start-gate-path and ready-path must be configured together")
        self._write_json_file(self._ready_path, {
            "attempts": self._attempts,
            "ready_monotonic_ns": time.perf_counter_ns(),
            "preparation": self._preparation,
            "thread_profile": self._thread_profile,
            "first_warmup_ns": self._first_warmup_ns,
        })
        deadline = time.monotonic() + self._gate_timeout_s
        while not self._stop.is_set() and not self._start_gate_path.exists():
            if time.monotonic() >= deadline:
                self._last_error = "matrix start gate timed out"
                self._failures += 1
                self._run_finished_ns = time.perf_counter_ns()
                self._done.set()
                self._stop.set()
                return
            self._stop.wait(0.005)

        if not self._stop.is_set():
            text = self._start_gate_path.read_text(encoding="utf-8").strip()
            gate = json.loads(text) if text else {}
            common_start = gate.get("common_start_monotonic_ns")
            if common_start is not None:
                if not isinstance(common_start, int) or isinstance(common_start, bool) or common_start <= 0:
                    raise ValueError("common_start_monotonic_ns must be a positive integer")
                self._common_start_ns = common_start

    def _wait_for_size_stage_ack(self, stage_path: Path) -> bool:
        deadline = time.monotonic() + self._size_block_ack_timeout_s
        acknowledgement = Path(f"{stage_path}.ack")
        while not self._stop.is_set() and not acknowledgement.exists():
            if time.monotonic() >= deadline:
                self._last_error = f"request-size stage acknowledgement timed out: {stage_path.name}"
                self._failures += 1
                self._run_finished_ns = time.perf_counter_ns()
                self._done.set()
                self._stop.set()
                return False
            self._stop.wait(0.005)
        return not self._stop.is_set()

    @staticmethod
    def _write_json_file(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(path.name + ".tmp")
        temporary_path.write_text(json.dumps(value), encoding="utf-8")
        temporary_path.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Python Control controller tutorial.")
    parser.add_argument("--name", default="control_controller", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--binding", default="gateway", help="Required interface binding name.")
    parser.add_argument("--target", default=None, help="Direct Control target subsystem name; bypasses local deployment binding resolution.")
    parser.add_argument("--topic-count", type=int, default=1, help="Number of request topics.")
    parser.add_argument("--topic-prefix", default="control_cmd", help="Control endpoint prefix.")
    parser.add_argument("--payload-size", "--request-size", dest="payload_size", type=int, default=8, help="Raw request payload size in bytes.")
    parser.add_argument("--request-sizes", default=None, help="Ordered raw request-size blocks; each value repeats for request-size-block-length attempts.")
    parser.add_argument("--request-size-block-length", type=int, default=1, help="Consecutive attempts per --request-sizes entry.")
    parser.add_argument("--response-size", type=int, default=8, help="Expected raw response payload size in bytes.")
    parser.add_argument("--period-ms", type=int, default=None, help="Request period in milliseconds.")
    parser.add_argument("--period-us", type=int, default=None, help="Request period in microseconds.")
    parser.add_argument("--count", type=int, default=0, help="Measured request count. 0 means run forever.")
    parser.add_argument("--warmup", type=int, default=0, help="Warmup request count excluded from stats.")
    parser.add_argument("--timeout-ms", type=int, default=1000, help="Handshake/receive timeout.")
    parser.add_argument("--route-ready-timeout-s", type=float, default=30.0, help="Bounded wait for target route registration.")
    parser.add_argument("--deadline-ms", type=float, default=None, help="Control deadline passed to the request.")
    parser.add_argument("--priority", type=int, default=None, help="Optional Control priority (0-255).")
    parser.add_argument(
        "--request-mode",
        choices=["round-robin", "fanout"],
        default="round-robin",
        help="How topics are used each period.",
    )
    parser.add_argument("--start-gate-path", default=None, help="Wait for this file after warmup before measured traffic begins.")
    parser.add_argument("--ready-path", default=None, help="Write the route/handshake/warmup-ready marker here.")
    parser.add_argument("--gate-timeout-s", type=float, default=60.0)
    parser.add_argument("--size-block-progress-path", default=None, help="Write request-size block transitions here.")
    parser.add_argument("--size-block-ack-timeout-s", type=float, default=30.0)
    parser.add_argument("--quiet", action="store_true", help="Suppress per-request output.")
    parser.add_argument("--report-format", choices=["text", "json"], default="text", help="Final report format.")
    parser.add_argument("--report-path", default=None, help="Optional output path for the final report.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--deployment-manifest", default=None, help="Path to the deployment binding manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    add_arguments(parser)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    request_sizes = None
    if args.request_sizes is not None:
        try:
            request_sizes = [int(value.strip()) for value in args.request_sizes.split(",") if value.strip()]
        except ValueError as exc:
            raise SystemExit(f"Invalid --request-sizes value: {exc}") from exc
        if not request_sizes or any(size < 8 for size in request_sizes):
            raise SystemExit("--request-sizes must contain one or more sizes of at least 8 bytes")
    if args.request_size_block_length <= 0 or args.gate_timeout_s <= 0 or args.size_block_ack_timeout_s <= 0:
        raise SystemExit("request-size-block-length, gate-timeout-s and size-block-ack-timeout-s must be positive")
    if bool(args.start_gate_path) != bool(args.ready_path):
        raise SystemExit("--start-gate-path and --ready-path must be provided together")
    try:
        rt_profile = profile_from_args(args)
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
    ensure_control_running()
    period_us = args.period_us if args.period_us is not None else (args.period_ms * 1000 if args.period_ms is not None else 1_000_000)
    instance = ControllerSubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        deployment_manifest_path=args.deployment_manifest,
        binding=args.binding,
        target=args.target,
        topic_count=args.topic_count,
        topic_prefix=args.topic_prefix,
        payload_size=args.payload_size,
        request_sizes=request_sizes,
        request_size_block_length=args.request_size_block_length,
        response_size=args.response_size,
        period_us=period_us,
        count=args.count,
        warmup=args.warmup,
        timeout_ms=args.timeout_ms,
        route_ready_timeout_s=args.route_ready_timeout_s,
        deadline_ms=args.deadline_ms,
        priority=args.priority,
        request_mode=args.request_mode,
        start_gate_path=args.start_gate_path,
        ready_path=args.ready_path,
        gate_timeout_s=args.gate_timeout_s,
        size_block_progress_path=args.size_block_progress_path,
        size_block_ack_timeout_s=args.size_block_ack_timeout_s,
        quiet=args.quiet,
        rt_profile=rt_profile,
        prepare_local=args.prepare_local,
        preparation_timeout_ms=args.preparation_timeout_ms,
        measurement_seconds=args.measurement_seconds,
        samples_path=args.samples_path,
    )
    run_subsystem(instance, should_stop=instance.done)
    report = instance.report()
    write_report(report_format=args.report_format, report_path=args.report_path, report=report)
    if args.measurement_seconds is not None:
        periodic = report.get("extra", {}).get("periodic", {})
        if not periodic.get("functional_passed"):
            return 1
        return 0 if periodic.get("performance_passed") else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
