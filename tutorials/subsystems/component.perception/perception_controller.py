from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "subsystems").is_dir() and (parent / "simulator").is_dir())
_SUPPORT_PY = _ROOT / "support" / "python"
_LIB = _ROOT / "lib"
for _path in (_SUPPORT_PY, _LIB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from common import encode_json_text, extract_payload, response_ok  # noqa: E402
from runtime import (  # noqa: E402
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
    run_subsystem,
    wait_for_control_ready,
)
from standard_types import JSON_TEXT  # noqa: E402

try:
    from r2kernel.wrapper.subsystem import ComponentSubsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


RADAR_CYCLE_REQUEST = "msg.sensor.radar.v1/RadarCycleRequest"
RADAR_CYCLE_RESPONSE = "msg.sensor.radar.v1/RadarCycleResponse"
RADAR_CYCLE_TOPIC = "radar_cycle"
RADAR_FRAME_ID = "cleaner_radar"
TWO_PI = math.tau
MAX_INT64 = (1 << 63) - 1
SECTOR_ANGLE_TOLERANCE_RAD = 2.0e-6


@dataclass(slots=True)
class RadarTrack:
    track_id: str
    range_m: float
    azimuth_rad: float
    radial_velocity_mps: float
    signal_strength: float
    observed_at_sync_ns: int
    created_at_sync_ns: int
    sample_id: int
    revolution_id: int


@dataclass(slots=True)
class SectorCoverage:
    sample_id: int
    revolution_id: int
    azimuth_start_rad: float
    azimuth_span_rad: float
    scan_started_at_sync_ns: int
    scan_ended_at_sync_ns: int
    revolution_complete: bool


class RadarObservationTracker:
    """Bounded local tracks and angular coverage derived only from radar samples."""

    def __init__(
        self,
        *,
        freshness_ns: int = 250_000_000,
        track_ttl_ns: int = 5_000_000_000,
        max_tracks: int = 256,
        max_returns_per_sector: int = 1024,
        max_coverage_sectors: int = 128,
        association_distance_m: float = 0.12,
        track_id_prefix: str | None = None,
    ) -> None:
        if freshness_ns <= 0 or track_ttl_ns <= freshness_ns:
            raise ValueError("radar freshness and track TTL must be positive, with TTL greater than freshness")
        if max_tracks <= 0 or max_returns_per_sector <= 0 or max_coverage_sectors <= 0:
            raise ValueError("radar tracking bounds must be positive")
        if association_distance_m <= 0.0:
            raise ValueError("radar association distance must be positive")
        self.freshness_ns = int(freshness_ns)
        self.track_ttl_ns = int(track_ttl_ns)
        self.max_tracks = int(max_tracks)
        self.max_returns_per_sector = int(max_returns_per_sector)
        self.max_coverage_sectors = int(max_coverage_sectors)
        self.association_distance_m = float(association_distance_m)
        self._track_id_prefix = track_id_prefix or uuid.uuid4().hex[:8]
        self._next_track_number = 1
        self.session_id: str | None = None
        self.last_sample_id: int | None = None
        self.source_status = "no_sample"
        self.detail: str | None = None
        self.continuity = "continuous"
        self._tracks: dict[str, RadarTrack] = {}
        self._coverage: deque[SectorCoverage] = deque(maxlen=self.max_coverage_sectors)
        self._latest_sampled_at_sync_ns: int | None = None

    def reset(self, *, session_id: str | None, continuity: str) -> None:
        self.session_id = session_id
        self.last_sample_id = None
        self._tracks.clear()
        self._coverage.clear()
        self._latest_sampled_at_sync_ns = None
        self.continuity = continuity
        self.source_status = "resync_required"
        self.detail = None

    def mark_device_error(self, detail: str) -> None:
        self._tracks.clear()
        self._coverage.clear()
        self._latest_sampled_at_sync_ns = None
        self.source_status = "device_error"
        self.detail = str(detail)

    def apply_response(self, response: dict[str, Any], *, expected_cycle: dict[str, Any], now_ns: int) -> None:
        if not isinstance(response, dict):
            raise RuntimeError("radar source response must be an object")
        self._validate_cycle_echo(response.get("cycle"), expected_cycle)
        session_id = response.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("radar source response is missing a valid session_id")

        session_changed = self.session_id is not None and session_id != self.session_id
        if self.session_id is None:
            self.session_id = session_id
        elif session_changed:
            self.reset(session_id=session_id, continuity="session_reset")

        status = response.get("status")
        if status not in {"sample_available", "no_new_sample", "resync_required", "device_error"}:
            raise RuntimeError(f"radar source returned unsupported status {status!r}")
        self.detail = response.get("detail") if isinstance(response.get("detail"), str) else None

        if status == "resync_required":
            if response.get("sector") is not None:
                raise RuntimeError("resync_required radar response must not include a sector")
            self.reset(session_id=session_id, continuity="resync_required")
            self.source_status = "resync_required"
            self.detail = response.get("detail") if isinstance(response.get("detail"), str) else None
            return
        if status == "device_error":
            if response.get("sector") is not None:
                raise RuntimeError("device_error radar response must not include a sector")
            self.mark_device_error(self.detail or "radar source reported device_error")
            return
        if status == "no_new_sample":
            if response.get("sector") is not None:
                raise RuntimeError("no_new_sample radar response must not include a sector")
            self.source_status = "no_new_sample"
            self._prune(now_ns)
            return

        sector = self._validate_sector(response.get("sector"), session_id=session_id)
        if sector["scan_ended_at_sync_ns"] > now_ns:
            raise RuntimeError("radar sector acquisition time is in the future")
        sample_id = sector["sample_id"]
        gap_detected = self.last_sample_id is not None and sample_id != self.last_sample_id + 1
        if self.last_sample_id is not None and sample_id <= self.last_sample_id:
            raise RuntimeError(
                f"radar sample_id did not advance: last={self.last_sample_id}, received={sample_id}"
            )
        if (
            not gap_detected
            and self._latest_sampled_at_sync_ns is not None
            and sector["scan_ended_at_sync_ns"] < self._latest_sampled_at_sync_ns
        ):
            raise RuntimeError("radar sector acquisition time moved backwards")
        if gap_detected:
            self._tracks.clear()
            self._coverage.clear()
            self._latest_sampled_at_sync_ns = None
            self.continuity = "sample_gap"

        self._prune(now_ns)
        self.last_sample_id = sample_id
        self._latest_sampled_at_sync_ns = sector["scan_ended_at_sync_ns"]
        self._coverage.append(
            SectorCoverage(
                sample_id=sample_id,
                revolution_id=sector["revolution_id"],
                azimuth_start_rad=sector["azimuth_start_rad"],
                azimuth_span_rad=sector["azimuth_span_rad"],
                scan_started_at_sync_ns=sector["scan_started_at_sync_ns"],
                scan_ended_at_sync_ns=sector["scan_ended_at_sync_ns"],
                revolution_complete=sector["revolution_complete"],
            )
        )
        self._update_tracks(sector)
        self.source_status = "sample_available"
        self.detail = response.get("detail") if isinstance(response.get("detail"), str) else None
        self._prune(now_ns)
        if self.continuity in {"sample_gap", "session_reset", "resync_required"}:
            coverage = self._coverage_snapshot(now_ns)
            if coverage["coverage_complete"]:
                self.continuity = "continuous"

    def snapshot(self, *, now_ns: int, max_range_m: float, fov_deg: float, mode: str) -> dict[str, Any]:
        self._prune(now_ns)
        if mode not in {"scan", "all", "radar", "front", "nearest"}:
            raise ValueError(f"unsupported perception scan mode: {mode}")

        detections = [self._detection_payload(track, now_ns=now_ns) for track in self._tracks.values()]
        detections = [item for item in detections if item["distance"] <= max_range_m]
        if mode == "front":
            detections = [item for item in detections if abs(float(item["bearing_deg"])) <= fov_deg / 2.0]
        detections.sort(key=lambda item: (float(item["distance"]), abs(float(item["bearing_deg"]))))
        if mode == "nearest" and detections:
            detections = detections[:1]

        coverage = self._coverage_snapshot(now_ns)
        ages = [float(item["observation_age_ms"]) for item in detections]
        if self.source_status in {"device_error", "resync_required"}:
            observation_status = self.source_status
        elif not detections:
            observation_status = "no_data"
        elif any(age <= self.freshness_ns / 1_000_000.0 for age in ages):
            observation_status = "fresh"
        else:
            observation_status = "stale"

        result: dict[str, Any] = {
            "mode": mode,
            "source_frame_id": RADAR_FRAME_ID,
            "status": observation_status,
            "source_status": self.source_status,
            "continuity": self.continuity,
            "session_id": self.session_id,
            "sample_id": self.last_sample_id,
            "sampled_at_ns": self._latest_sampled_at_sync_ns,
            "freshness_ms": self.freshness_ns / 1_000_000.0,
            "track_ttl_ms": self.track_ttl_ns / 1_000_000.0,
            "coverage": coverage,
            "coverage_complete": coverage["coverage_complete"],
            "detections": detections,
        }
        if self.detail:
            result["detail"] = self.detail
        return result

    @staticmethod
    def _validate_cycle_echo(value: Any, expected_cycle: dict[str, Any]) -> None:
        if not isinstance(value, dict):
            raise RuntimeError("radar source response is missing cycle identity")
        for key in ("generation", "sequence", "intent", "state_revision"):
            if value.get(key) != expected_cycle.get(key):
                raise RuntimeError(f"radar source response cycle {key} does not match the request")

    def _validate_sector(self, value: Any, *, session_id: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise RuntimeError("sample_available radar response is missing its sector")
        sector_session = value.get("session_id")
        if sector_session != session_id:
            raise RuntimeError("radar sector session_id does not match the response")
        if value.get("frame_id") != RADAR_FRAME_ID:
            raise RuntimeError(f"radar sector frame_id must be {RADAR_FRAME_ID!r}")

        sample_id = _positive_int(value.get("sample_id"), "sector.sample_id")
        revolution_id = _positive_int(value.get("revolution_id"), "sector.revolution_id")
        start = _finite_number(value.get("azimuth_start_rad"), "sector.azimuth_start_rad")
        span = _finite_number(value.get("azimuth_span_rad"), "sector.azimuth_span_rad")
        started = _nonnegative_int(value.get("scan_started_at_sync_ns"), "sector.scan_started_at_sync_ns")
        ended = _nonnegative_int(value.get("scan_ended_at_sync_ns"), "sector.scan_ended_at_sync_ns")
        revolution_complete = value.get("revolution_complete")
        if not 0.0 <= start < TWO_PI:
            raise RuntimeError("radar sector azimuth_start_rad must be in [0, 2pi)")
        if not 0.0 < span <= TWO_PI:
            raise RuntimeError("radar sector azimuth_span_rad must be in (0, 2pi]")
        if ended <= started:
            raise RuntimeError("radar sector scan end must be later than its start")
        if not isinstance(revolution_complete, bool):
            raise RuntimeError("radar sector revolution_complete must be a boolean")
        returns = value.get("returns")
        if not isinstance(returns, list):
            raise RuntimeError("radar sector returns must be a list")
        if len(returns) > self.max_returns_per_sector:
            raise RuntimeError(
                f"radar sector exceeds the configured limit of {self.max_returns_per_sector} returns"
            )

        validated_returns = []
        for index, radar_return in enumerate(returns):
            if not isinstance(radar_return, dict):
                raise RuntimeError(f"radar return {index} must be an object")
            range_m = _finite_number(radar_return.get("range_m"), f"returns[{index}].range_m")
            azimuth_rad = _finite_number(radar_return.get("azimuth_rad"), f"returns[{index}].azimuth_rad")
            radial_velocity_mps = _finite_number(
                radar_return.get("radial_velocity_mps"), f"returns[{index}].radial_velocity_mps"
            )
            signal_strength = _finite_number(
                radar_return.get("signal_strength"), f"returns[{index}].signal_strength"
            )
            observed_at = _nonnegative_int(
                radar_return.get("observed_at_sync_ns"), f"returns[{index}].observed_at_sync_ns"
            )
            if range_m <= 0.0:
                raise RuntimeError(f"radar return {index} range_m must be positive")
            if not 0.0 <= signal_strength <= 1.0:
                raise RuntimeError(f"radar return {index} signal_strength must be in [0, 1]")
            if not 0.0 <= azimuth_rad < TWO_PI:
                raise RuntimeError(f"radar return {index} azimuth_rad must be in [0, 2pi)")
            if not started <= observed_at <= ended:
                raise RuntimeError(f"radar return {index} acquisition time is outside its sector interval")
            offset = (azimuth_rad - start) % TWO_PI
            if offset > span + SECTOR_ANGLE_TOLERANCE_RAD:
                raise RuntimeError(f"radar return {index} azimuth is outside its sector coverage")
            validated_returns.append(
                {
                    "range_m": range_m,
                    "azimuth_rad": azimuth_rad,
                    "radial_velocity_mps": radial_velocity_mps,
                    "signal_strength": signal_strength,
                    "observed_at_sync_ns": observed_at,
                }
            )
        return {
            "sample_id": sample_id,
            "revolution_id": revolution_id,
            "azimuth_start_rad": start,
            "azimuth_span_rad": span,
            "scan_started_at_sync_ns": started,
            "scan_ended_at_sync_ns": ended,
            "revolution_complete": revolution_complete,
            "returns": validated_returns,
        }

    def _update_tracks(self, sector: dict[str, Any]) -> None:
        available_tracks = sorted(
            self._tracks.values(), key=lambda track: (track.observed_at_sync_ns, track.track_id)
        )
        claimed_track_ids: set[str] = set()
        for radar_return in sector["returns"]:
            return_x = radar_return["range_m"] * math.cos(radar_return["azimuth_rad"])
            return_y = radar_return["range_m"] * math.sin(radar_return["azimuth_rad"])
            matching_track = self._nearest_matching_track(
                available_tracks,
                radar_return,
                return_x,
                return_y,
                claimed_track_ids=claimed_track_ids,
            )
            if matching_track is None:
                track_id = self._new_track_id()
                created_at_ns = radar_return["observed_at_sync_ns"]
            else:
                track_id = matching_track.track_id
                created_at_ns = matching_track.created_at_sync_ns
                claimed_track_ids.add(track_id)
            track = RadarTrack(
                track_id=track_id,
                range_m=radar_return["range_m"],
                azimuth_rad=radar_return["azimuth_rad"],
                radial_velocity_mps=radar_return["radial_velocity_mps"],
                signal_strength=radar_return["signal_strength"],
                observed_at_sync_ns=radar_return["observed_at_sync_ns"],
                created_at_sync_ns=created_at_ns,
                sample_id=sector["sample_id"],
                revolution_id=sector["revolution_id"],
            )
            self._tracks[track_id] = track
        if len(self._tracks) > self.max_tracks:
            oldest_tracks = sorted(
                self._tracks.values(), key=lambda track: (track.observed_at_sync_ns, track.track_id)
            )
            for track in oldest_tracks[: len(self._tracks) - self.max_tracks]:
                del self._tracks[track.track_id]

    def _nearest_matching_track(
        self,
        candidates: list[RadarTrack],
        radar_return: dict[str, float | int],
        return_x: float,
        return_y: float,
        *,
        claimed_track_ids: set[str],
    ) -> RadarTrack | None:
        closest: RadarTrack | None = None
        closest_distance = self.association_distance_m
        for track in candidates:
            if track.track_id in claimed_track_ids:
                continue
            if radar_return["observed_at_sync_ns"] - track.observed_at_sync_ns > self.track_ttl_ns:
                continue
            track_x = track.range_m * math.cos(track.azimuth_rad)
            track_y = track.range_m * math.sin(track.azimuth_rad)
            distance = math.hypot(return_x - track_x, return_y - track_y)
            if distance <= closest_distance:
                closest = track
                closest_distance = distance
        return closest

    def _new_track_id(self) -> str:
        track_id = f"radar-{self._track_id_prefix}-{self._next_track_number:08d}"
        self._next_track_number += 1
        return track_id

    def _detection_payload(self, track: RadarTrack, *, now_ns: int) -> dict[str, Any]:
        observation_age_ns = max(0, now_ns - track.observed_at_sync_ns)
        track_age_ns = max(0, now_ns - track.created_at_sync_ns)
        bearing_rad = _wrap_signed_radians(track.azimuth_rad)
        return {
            "id": track.track_id,
            "relative_x": round(track.range_m * math.cos(track.azimuth_rad), 4),
            "relative_y": round(track.range_m * math.sin(track.azimuth_rad), 4),
            "distance": round(track.range_m, 4),
            "bearing_deg": round(math.degrees(bearing_rad), 2),
            "radial_velocity_mps": track.radial_velocity_mps,
            "signal_strength": track.signal_strength,
            "observed_at_sync_ns": track.observed_at_sync_ns,
            "sample_id": track.sample_id,
            "revolution_id": track.revolution_id,
            "observation_age_ms": round(observation_age_ns / 1_000_000.0, 1),
            "track_age_ms": round(track_age_ns / 1_000_000.0, 1),
            "fresh": observation_age_ns <= self.freshness_ns,
            "stale": observation_age_ns > self.freshness_ns,
        }

    def _coverage_snapshot(self, now_ns: int) -> dict[str, Any]:
        current_revolution = max((sector.revolution_id for sector in self._coverage), default=None)
        sectors = [
            sector
            for sector in self._coverage
            if sector.revolution_id == current_revolution and now_ns - sector.scan_ended_at_sync_ns <= self.track_ttl_ns
        ]
        intervals: list[tuple[float, float]] = []
        for sector in sectors:
            intervals.extend(_split_angular_interval(sector.azimuth_start_rad, sector.azimuth_span_rad))
        covered_rad = _union_interval_length(intervals)
        coverage_complete = covered_rad >= TWO_PI - 1e-6 and any(
            sector.revolution_complete for sector in sectors
        )
        return {
            "revolution_id": current_revolution,
            "covered_rad": round(covered_rad, 6),
            "coverage_ratio": round(min(1.0, covered_rad / TWO_PI), 6),
            "coverage_complete": coverage_complete,
            "sectors": [
                {
                    "sample_id": sector.sample_id,
                    "revolution_id": sector.revolution_id,
                    "azimuth_start_rad": sector.azimuth_start_rad,
                    "azimuth_span_rad": sector.azimuth_span_rad,
                    "scan_started_at_sync_ns": sector.scan_started_at_sync_ns,
                    "scan_ended_at_sync_ns": sector.scan_ended_at_sync_ns,
                    "revolution_complete": sector.revolution_complete,
                }
                for sector in sectors
            ],
        }

    def _prune(self, now_ns: int) -> None:
        expired_track_ids = [
            track_id
            for track_id, track in self._tracks.items()
            if now_ns - track.observed_at_sync_ns > self.track_ttl_ns
        ]
        for track_id in expired_track_ids:
            del self._tracks[track_id]
        retained_coverage = [
            sector
            for sector in self._coverage
            if now_ns - sector.scan_ended_at_sync_ns <= self.track_ttl_ns
        ]
        self._coverage = deque(retained_coverage, maxlen=self.max_coverage_sectors)


class PerceptionControllerSubsystem(ComponentSubsystem):
    def __init__(
        self,
        *,
        name: str = "component.perception",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        deployment_manifest_path: str | None = None,
        binding: str = "radar_source",
        poll_period_ms: float = 50.0,
        poll_deadline_ms: float = 250.0,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "perception"),
            manifest_path=manifest_path or default_manifest_path(__file__, "perception_controller"),
            deployment_manifest_path=deployment_manifest_path,
        )
        if poll_period_ms <= 0.0 or poll_deadline_ms <= 0.0:
            raise ValueError("radar poll period and deadline must be positive")
        self._binding = binding
        self._poll_period_ms = float(poll_period_ms)
        self._poll_deadline_ms = float(poll_deadline_ms)
        self._tracker = RadarObservationTracker()
        self._state_lock = threading.RLock()
        self._poll_cycle_lock = threading.Lock()
        self._poller_stop = threading.Event()
        self._poller_thread: threading.Thread | None = None
        self._runtime_binding_ready = False
        self._generation = 0
        self._sequence = 0

    def on_initialize(self) -> bool:
        self.register_procedure("scan", self._scan, request_type=JSON_TEXT, response_type=JSON_TEXT)
        self.register_procedure("front", self._front, request_type=JSON_TEXT, response_type=JSON_TEXT)
        self.register_procedure("nearest", self._nearest, request_type=JSON_TEXT, response_type=JSON_TEXT)
        self.logger.info("Lifecycle", "perception controller initialized")
        return True

    def on_start(self) -> bool:
        self.start_procedure_server()
        self._start_radar_poller()
        return True

    def on_terminate(self) -> bool:
        self._stop_radar_poller()
        self.stop_procedure_server()
        return True

    def on_reset(self) -> bool:
        self._stop_radar_poller()
        with self._state_lock:
            self._tracker.reset(session_id=None, continuity="resync_required")
        self.stop_procedure_server()
        return True

    def on_pause(self) -> bool:
        self._stop_radar_poller()
        self.stop_procedure_server()
        return True

    def on_resume(self) -> bool:
        self.start_procedure_server()
        self._start_radar_poller()
        return True

    def on_stop(self) -> bool:
        return self.on_terminate()

    def _start_radar_poller(self) -> None:
        if self._poller_thread is not None:
            if self._poller_thread.is_alive():
                if self._poller_stop.is_set():
                    raise RuntimeError("previous radar poller has not stopped")
                return
            self._poller_thread = None
        self._runtime_binding_ready = False
        self._poller_stop.clear()
        self._generation = max(1, int(self.now_ns()), self._generation + 1)
        if self._generation > MAX_INT64:
            raise RuntimeError("radar cycle generation exceeds the positive int64 range")
        self._sequence = 0
        self._poller_thread = threading.Thread(
            target=self._radar_poll_loop,
            name=f"{self.name}-radar-source-adapter",
            daemon=True,
        )
        self._poller_thread.start()

    def _stop_radar_poller(self) -> None:
        self._poller_stop.set()
        if self._poller_thread is not None:
            join_timeout_s = max(1.0, self._poll_deadline_ms / 1000.0 + 0.5)
            self._poller_thread.join(timeout=join_timeout_s)
            if self._poller_thread.is_alive():
                raise RuntimeError("radar poller did not stop before lifecycle deadline")
        self._poller_thread = None

    def _radar_poll_loop(self) -> None:
        while not self._poller_stop.is_set():
            self._poll_once()
            self._poller_stop.wait(self._poll_period_ms / 1000.0)

    def _poll_once(self) -> None:
        with self._poll_cycle_lock:
            try:
                if not self._runtime_binding_ready:
                    if self.resolve_runtime_binding(self._binding) is None:
                        return
                    self._runtime_binding_ready = True
                if self._poller_stop.is_set():
                    return

                self._sequence += 1
                if self._sequence > MAX_INT64:
                    with self._state_lock:
                        self._tracker.mark_device_error("radar cycle sequence exceeded the positive int64 range")
                    return
                expected_cycle = {
                    "generation": self._generation,
                    "sequence": self._sequence,
                    "intent": "sample",
                    "state_revision": 0,
                }
                request: dict[str, Any] = {"cycle": expected_cycle, "after_sample_id": 0}
                with self._state_lock:
                    if self._tracker.last_sample_id is not None:
                        request["after_sample_id"] = self._tracker.last_sample_id
                    if self._tracker.session_id is not None:
                        request["expected_session_id"] = self._tracker.session_id
                response = self.transact_bound_control(
                    binding=self._binding,
                    topic=RADAR_CYCLE_TOPIC,
                    type="bytes",
                    value=request,
                    period_ms=self._poll_period_ms,
                    deadline_ms=self._poll_deadline_ms,
                    request_type=RADAR_CYCLE_REQUEST,
                    response_type=RADAR_CYCLE_RESPONSE,
                    timing={
                        "generated_at_sync_ns": self.now_ns(),
                        "valid_for_ns": int(self._poll_deadline_ms * 1_000_000),
                        "late_policy": "reject",
                    },
                )
                if not response_ok(response):
                    message = response.get("error_message") or response.get("error_code") or "radar cycle Control failed"
                    raise RuntimeError(str(message))
                payload = extract_payload(response)
                if not isinstance(payload, dict):
                    raise RuntimeError("radar cycle Control response must be an object")
                with self._state_lock:
                    self._tracker.apply_response(payload, expected_cycle=expected_cycle, now_ns=self.now_ns())
            except Exception as exc:
                with self._state_lock:
                    self._tracker.mark_device_error(str(exc))
                self.logger.warning("Radar", f"radar sample unavailable: {exc}")

    def _scan(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params, wrapped = _decode_query_params(params)
        mode = _query_mode(params.get("mode", "nearest"))
        max_range_m = _bounded_query_number(params, "max_range", 0.45, 0.05, 2.0)
        fov_deg = _bounded_query_number(params, "fov_deg", 360.0, 1.0, 360.0)
        result = self._query_local(mode=mode, max_range_m=max_range_m, fov_deg=fov_deg)
        return encode_json_text(result) if wrapped else result

    def _front(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params, wrapped = _decode_query_params(params)
        max_range_m = _bounded_query_number(params, "max_range", 0.28, 0.05, 2.0)
        fov_deg = _bounded_query_number(params, "fov_deg", 65.0, 1.0, 360.0)
        result = self._query_local(mode="front", max_range_m=max_range_m, fov_deg=fov_deg)
        return encode_json_text(result) if wrapped else result

    def _nearest(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params, wrapped = _decode_query_params(params)
        max_range_m = _bounded_query_number(params, "max_range", 0.70, 0.05, 2.0)
        result = self._query_local(mode="nearest", max_range_m=max_range_m, fov_deg=360.0)
        return encode_json_text(result) if wrapped else result

    def _query_local(self, *, mode: str, max_range_m: float, fov_deg: float) -> dict[str, Any]:
        with self._state_lock:
            return self._tracker.snapshot(
                now_ns=self.now_ns(),
                max_range_m=max_range_m,
                fov_deg=fov_deg,
                mode=mode,
            )


def _decode_query_params(params: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    if params is None:
        return {}, False
    if not isinstance(params, dict):
        raise ValueError("perception query must be an object")
    if "value" in params and set(params) <= {"value"}:
        raw = params.get("value")
        if not isinstance(raw, str):
            raise ValueError("perception query JSON wrapper must contain a string value")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("perception query contains invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("perception query JSON must decode to an object")
        return decoded, True
    return dict(params), False


def _query_mode(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("perception query mode must be a string")
    normalized = value.strip().lower()
    if normalized not in {"scan", "all", "radar", "front", "nearest"}:
        raise ValueError(f"unsupported perception scan mode: {normalized}")
    return normalized


def _bounded_query_number(
    params: dict[str, Any], key: str, default: float, lower: float, upper: float
) -> float:
    if key not in params:
        return default
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"perception query {key} must be a finite number")
    return max(lower, min(float(value), upper))


def _positive_int(value: Any, field_name: str) -> int:
    parsed = _nonnegative_int(value, field_name)
    if parsed <= 0 or parsed > MAX_INT64:
        raise RuntimeError(f"{field_name} must be a positive int64")
    return parsed


def _nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_INT64:
        raise RuntimeError(f"{field_name} must be a nonnegative int64")
    return value


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise RuntimeError(f"{field_name} must be a finite number")
    return float(value)


def _wrap_signed_radians(value: float) -> float:
    wrapped = (value + math.pi) % TWO_PI - math.pi
    return math.pi if wrapped == -math.pi and value > 0.0 else wrapped


def _split_angular_interval(start: float, span: float) -> list[tuple[float, float]]:
    end = start + span
    if end <= TWO_PI:
        return [(start, end)]
    return [(start, TWO_PI), (0.0, end - TWO_PI)]


def _union_interval_length(intervals: list[tuple[float, float]]) -> float:
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    total = 0.0
    start, end = ordered[0]
    for next_start, next_end in ordered[1:]:
        if next_start <= end:
            end = max(end, next_end)
            continue
        total += end - start
        start, end = next_start, next_end
    return min(TWO_PI, total + end - start)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the robot cleaner perception controller.")
    parser.add_argument("--name", default="component.perception", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--binding", default="radar_source", help="Required radar source binding name.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--deployment-manifest", default=None, help="Path to the deployment binding manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    args = parser.parse_args()

    ensure_control_running()
    wait_for_control_ready()
    instance = PerceptionControllerSubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        deployment_manifest_path=args.deployment_manifest,
        binding=args.binding,
    )
    run_subsystem(instance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
