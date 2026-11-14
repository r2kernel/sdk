from __future__ import annotations

import argparse
import math
import os
import sys
import threading
from pathlib import Path
from typing import Any


def _tutorial_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "subsystems").is_dir() and (parent / "simulator").is_dir():
            return parent
    candidates = [
        os.environ.get("R2KERNEL_TUTORIAL_ROOT"),
        str(Path(os.environ.get("R2KERNEL_HOME", "/opt/r2kernel")) / "tutorials"),
        "/opt/r2kernel/tutorials",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        root = Path(candidate)
        if (root / "subsystems").is_dir() and (root / "simulator").is_dir():
            return root
    raise RuntimeError(
        "could not locate R2Kernel tutorial runtime; set R2KERNEL_TUTORIAL_ROOT or install tutorials under /opt/r2kernel/tutorials"
    )


_ROOT = _tutorial_root()
_SUPPORT_PY = _ROOT / "support" / "python"
_LIB = _ROOT / "lib"
for _path in (_SUPPORT_PY, _LIB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from common import WORLD_EVENT_SIZE, extract_payload, response_ok  # noqa: E402
from gnss_cycle import GnssSampleTracker  # noqa: E402
from gnss_fixture import GnssFixtureConfiguration, load_gnss_fixture  # noqa: E402
from gnss_localization import (  # noqa: E402
    absolute_fix_to_map_odom,
    absolute_fix_to_map_odom_height,
    build_absolute_pose_fix,
    build_pose_estimate,
    covariance_values,
    validate_odometry_pose,
)
from gnss_mount import GnssMount, load_gnss_mount  # noqa: E402
from runtime import (  # noqa: E402
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
    run_subsystem,
    wait_for_control_ready,
)
from standard_types import (  # noqa: E402
    ABSOLUTE_POSE_FIX,
    LOCALIZATION_STATUS,
    ODOMETRY,
    POSE_ESTIMATE,
)

try:
    from r2kernel.wrapper.subsystem import ComponentSubsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


GNSS_CYCLE_TOPIC = "gnss_cycle"
GNSS_CYCLE_REQUEST = "msg.sensor.gnss.v1/GnssCycleRequest"
GNSS_CYCLE_RESPONSE = "msg.sensor.gnss.v1/GnssCycleResponse"
GNSS_POLL_PERIOD_MS = 200.0
GNSS_POLL_DEADLINE_MS = 1000.0
MAX_INT64 = (1 << 63) - 1


class LocalizerSubsystem(ComponentSubsystem):
    def __init__(
        self,
        *,
        name: str = "component.localizer",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        deployment_manifest_path: str | None = None,
        gnss_fixture_path: str | None = None,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "localizer"),
            manifest_path=manifest_path or default_manifest_path(__file__, "localizer"),
            deployment_manifest_path=deployment_manifest_path,
        )
        tutorials_root = _ROOT
        self._fixture: GnssFixtureConfiguration = load_gnss_fixture(
            Path(gnss_fixture_path)
            if gnss_fixture_path is not None
            else tutorials_root / "config" / "gnss_fixture.yaml"
        )
        self._mount: GnssMount = load_gnss_mount(
            tutorials_root / "subsystems" / "compound.cleaner" / "subsystem.yaml",
            tutorials_root / "subsystems" / "component.localizer" / "subsystem.yaml",
        )
        self._lock = threading.RLock()
        self._poll_cycle_lock = threading.Lock()
        self._tracker = GnssSampleTracker(expected_frame_id=self._mount.frame_id)
        self._odometry: dict[str, Any] | None = None
        self._position_fix: dict[str, Any] | None = None
        self._position_covariance: dict[str, list[float]] | None = None
        self._map_odom: tuple[float, float, float] | None = None
        self._map_odom_height_m: float | None = None
        self._aligned_sample_identity: tuple[str, int] | None = None
        self._poller_stop = threading.Event()
        self._poller_thread: threading.Thread | None = None
        self._runtime_binding_ready = False
        self._generation = 0
        self._sequence = 0

    def on_initialize(self) -> bool:
        self.register_event_publish(
            topic="position_fix",
            type=ABSOLUTE_POSE_FIX,
            size=WORLD_EVENT_SIZE,
            priority=5,
            queue_depth=4,
        )
        self.register_event_subscribe(
            topic="odometry",
            type=ODOMETRY,
            size=WORLD_EVENT_SIZE,
            callback=self._on_odometry,
        )
        self.register_event_publish(
            topic="pose_estimate",
            type=POSE_ESTIMATE,
            size=WORLD_EVENT_SIZE,
            priority=5,
            queue_depth=4,
        )
        self.register_event_publish(
            topic="localization_status",
            type=LOCALIZATION_STATUS,
            size=WORLD_EVENT_SIZE,
            priority=5,
            queue_depth=4,
        )
        return True

    def on_start(self) -> bool:
        self.start_event_transport()
        self._publish_status("acquiring", timestamp=self.now_ns())
        self._start_gnss_poller()
        return True

    def on_terminate(self) -> bool:
        self._stop_gnss_poller()
        self.stop_event_transport()
        return True

    def on_reset(self) -> bool:
        self._stop_gnss_poller()
        with self._lock:
            self._tracker.reset()
            self._odometry = None
            self._position_fix = None
            self._position_covariance = None
            self._map_odom = None
            self._map_odom_height_m = None
            self._aligned_sample_identity = None
        self.stop_event_transport()
        return True

    def on_pause(self) -> bool:
        return self.on_terminate()

    def on_resume(self) -> bool:
        return self.on_start()

    def on_stop(self) -> bool:
        return self.on_terminate()

    def _start_gnss_poller(self) -> None:
        if self._poller_thread is not None:
            if self._poller_thread.is_alive():
                if self._poller_stop.is_set():
                    raise RuntimeError("previous GNSS poller has not stopped")
                return
            self._poller_thread = None
        self._runtime_binding_ready = False
        self._poller_stop.clear()
        self._generation = max(1, int(self.now_ns()), self._generation + 1)
        if self._generation > MAX_INT64:
            raise RuntimeError("GNSS cycle generation exceeds the positive int64 range")
        self._sequence = 0
        self._poller_thread = threading.Thread(
            target=self._gnss_poll_loop,
            name=f"{self.name}-gnss-source-adapter",
            daemon=True,
        )
        self._poller_thread.start()

    def _stop_gnss_poller(self) -> None:
        self._poller_stop.set()
        if self._poller_thread is not None:
            join_timeout_s = GNSS_POLL_DEADLINE_MS / 1000.0 + 0.5
            self._poller_thread.join(timeout=join_timeout_s)
            if self._poller_thread.is_alive():
                raise RuntimeError("GNSS poller did not stop before lifecycle deadline")
        self._poller_thread = None

    def _gnss_poll_loop(self) -> None:
        while not self._poller_stop.is_set():
            self._poll_once()
            self._poller_stop.wait(GNSS_POLL_PERIOD_MS / 1000.0)

    def _poll_once(self) -> None:
        with self._poll_cycle_lock:
            now_sync_ns = self.now_ns()
            with self._lock:
                self._refresh_usability_locked(now_sync_ns)
                self._publish_estimate_locked(now_sync_ns)
            try:
                if not self._runtime_binding_ready:
                    if self.resolve_runtime_binding("gnss_source") is None:
                        return
                    self._runtime_binding_ready = True
                if self._poller_stop.is_set():
                    return

                self._sequence += 1
                if self._sequence > MAX_INT64:
                    raise RuntimeError("GNSS cycle sequence exceeded the positive int64 range")
                expected_cycle = {
                    "generation": self._generation,
                    "sequence": self._sequence,
                    "intent": "sample",
                    "state_revision": 0,
                }
                with self._lock:
                    request = {"cycle": expected_cycle, **self._tracker.request_cursor()}
                response = self.transact_bound_control(
                    binding="gnss_source",
                    topic=GNSS_CYCLE_TOPIC,
                    type="bytes",
                    value=request,
                    period_ms=GNSS_POLL_PERIOD_MS,
                    deadline_ms=GNSS_POLL_DEADLINE_MS,
                    request_type=GNSS_CYCLE_REQUEST,
                    response_type=GNSS_CYCLE_RESPONSE,
                    timing={
                        "generated_at_sync_ns": now_sync_ns,
                        "valid_for_ns": int(GNSS_POLL_DEADLINE_MS * 1_000_000),
                        "late_policy": "reject",
                    },
                )
                if not response_ok(response):
                    message = response.get("error_message") or response.get("error_code") or "GNSS cycle Control failed"
                    raise RuntimeError(str(message))
                payload = extract_payload(response)
                if not isinstance(payload, dict):
                    raise RuntimeError("GNSS cycle Control response must be an object")
                completed_at_sync_ns = self.now_ns()
                with self._lock:
                    self._tracker.apply_response(
                        payload,
                        expected_cycle=expected_cycle,
                        now_sync_ns=completed_at_sync_ns,
                        maximum_measurement_age_ns=self._fixture.maximum_measurement_age_ns,
                        future_timestamp_tolerance_ns=self._fixture.future_timestamp_tolerance_ns,
                    )
                    self._align_latest_measurement_locked()
                    self._publish_estimate_locked(completed_at_sync_ns)
            except Exception as exc:
                completed_at_sync_ns = self.now_ns()
                with self._lock:
                    self._refresh_usability_locked(completed_at_sync_ns)
                    self._tracker.mark_device_error(str(exc))
                    self._publish_estimate_locked(completed_at_sync_ns)
                self.logger.warning("GNSS", f"GNSS measurement unavailable: {exc}")

    def _on_odometry(self, value: Any, timestamp: int, _timing: Any = None) -> None:
        try:
            odometry = validate_odometry_pose(value)
        except ValueError as exc:
            self.logger.warning("GNSS", f"invalid odometry pose: {exc}")
            return
        now_sync_ns = self.now_ns()
        with self._lock:
            self._odometry = odometry
            self._refresh_usability_locked(now_sync_ns)
            self._align_latest_measurement_locked()
            self._publish_estimate_locked(timestamp if timestamp >= 0 else now_sync_ns)

    def _align_latest_measurement_locked(self) -> None:
        measurement = self._tracker.measurement
        if (
            measurement is None
            or self._tracker.status != "localized"
            or self._odometry is None
        ):
            return
        sample_identity = (measurement.session_id, measurement.sample_id)
        if sample_identity == self._aligned_sample_identity:
            return
        position_fix = build_absolute_pose_fix(
            measurement,
            self._odometry,
            fixture=self._fixture,
            mount=self._mount,
        )
        self._position_fix = position_fix
        self._position_covariance = {
            "values": covariance_values(position_fix["covariance"])
        }
        self._map_odom = absolute_fix_to_map_odom(position_fix, self._odometry)
        self._map_odom_height_m = absolute_fix_to_map_odom_height(position_fix, self._odometry)
        self._aligned_sample_identity = sample_identity
        self.publish_event(
            topic="position_fix",
            value=position_fix,
            timestamp=measurement.observed_at_sync_ns,
        )

    def _refresh_usability_locked(self, now_sync_ns: int) -> None:
        self._tracker.refresh_usability(
            now_sync_ns=now_sync_ns,
            maximum_measurement_age_ns=self._fixture.maximum_measurement_age_ns,
            future_timestamp_tolerance_ns=self._fixture.future_timestamp_tolerance_ns,
        )

    def _publish_estimate_locked(self, timestamp: int) -> None:
        if (
            self._odometry is None
            or self._map_odom is None
            or self._map_odom_height_m is None
            or self._position_covariance is None
        ):
            state = self._tracker.status
            if state == "localized":
                state = "acquiring_odometry"
            self._publish_status(state, timestamp=timestamp)
            return

        estimate = build_pose_estimate(
            self._odometry,
            self._map_odom,
            self._position_covariance,
            frame_id=self._fixture.geographic_origin.frame_id,
            map_odom_height_m=self._map_odom_height_m,
        )
        map_x, map_y, map_yaw = self._map_odom
        self.set_frame_transform(
            parent_frame="map",
            child_frame="reference",
            translation=(map_x, map_y, self._map_odom_height_m),
            rotation_xyzw=(0.0, 0.0, math.sin(map_yaw * 0.5), math.cos(map_yaw * 0.5)),
            observed_at_sync_ns=timestamp,
            stale_after_ms=self._fixture.maximum_measurement_age_ns / 1_000_000.0,
        )
        self.publish_event(topic="pose_estimate", value=estimate, timestamp=timestamp)
        state = self._tracker.status
        if state == "acquiring":
            state = "dead_reckoning"
        self._publish_status(state, timestamp=timestamp)

    def _publish_status(self, state: str, *, timestamp: int | None = None) -> None:
        status_timestamp = self.now_ns() if timestamp is None else timestamp
        with self._lock:
            self._refresh_usability_locked(status_timestamp)
            if state == "localized" and self._tracker.status != "localized":
                state = self._tracker.status
            self.publish_event(
                topic="localization_status",
                value={"state": state, "map_id": self._fixture.geographic_origin.frame_id},
                timestamp=status_timestamp,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the robot cleaner localization component.")
    parser.add_argument("--name", default="component.localizer", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--deployment-manifest", default=None, help="Path to the deployment binding manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    parser.add_argument(
        "--gnss-fixture",
        default=None,
        help="Path to the explicit GNSS geographic origin, timing, mount, and uncertainty fixture.",
    )
    args = parser.parse_args()

    ensure_control_running()
    wait_for_control_ready()
    run_subsystem(
        LocalizerSubsystem(
            name=args.name,
            log_level=args.log_level,
            user_data_path=args.user_data_path,
            manifest_path=args.manifest,
            deployment_manifest_path=args.deployment_manifest,
            gnss_fixture_path=args.gnss_fixture,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
