from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock


TUTORIALS = Path(__file__).resolve().parents[1]
for _relative in ("subsystems/component.perception", "lib"):
    _path = str(TUTORIALS / _relative)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from perception_controller import PerceptionControllerSubsystem, RadarObservationTracker  # noqa: E402


def _cycle(sequence: int, generation: int = 77) -> dict[str, int | str]:
    return {
        "generation": generation,
        "sequence": sequence,
        "intent": "sample",
        "state_revision": 0,
    }


def _radar_return(
    *,
    range_m: float = 0.25,
    azimuth_rad: float = 0.2,
    observed_at_sync_ns: int = 1_000_000_000,
    radial_velocity_mps: float = 0.0,
    signal_strength: float = 0.8,
) -> dict[str, float | int]:
    return {
        "range_m": range_m,
        "azimuth_rad": azimuth_rad,
        "radial_velocity_mps": radial_velocity_mps,
        "signal_strength": signal_strength,
        "observed_at_sync_ns": observed_at_sync_ns,
    }


def _sector(
    sample_id: int,
    *,
    revolution_id: int = 1,
    azimuth_start_rad: float = 0.0,
    azimuth_span_rad: float = 1.0,
    timestamp_ns: int = 1_000_000_000,
    returns: list[dict[str, float | int]] | None = None,
    revolution_complete: bool = False,
    session_id: str = "session-a",
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "sample_id": sample_id,
        "revolution_id": revolution_id,
        "azimuth_start_rad": azimuth_start_rad,
        "azimuth_span_rad": azimuth_span_rad,
        "scan_started_at_sync_ns": timestamp_ns - 1,
        "scan_ended_at_sync_ns": timestamp_ns,
        "frame_id": "cleaner_radar",
        "revolution_complete": revolution_complete,
        "returns": returns if returns is not None else [_radar_return(observed_at_sync_ns=timestamp_ns)],
    }


def _response(
    sequence: int,
    *,
    status: str = "sample_available",
    session_id: str = "session-a",
    sector: dict[str, object] | None = None,
    detail: str | None = None,
) -> dict[str, object]:
    response: dict[str, object] = {
        "cycle": _cycle(sequence),
        "session_id": session_id,
        "status": status,
    }
    if sector is not None:
        response["sector"] = sector
    if detail is not None:
        response["detail"] = detail
    return response


class RadarObservationTrackerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = RadarObservationTracker(track_id_prefix="test")

    def _apply(self, sequence: int, response: dict[str, object], now_ns: int) -> None:
        self.tracker.apply_response(response, expected_cycle=_cycle(sequence), now_ns=now_ns)

    def test_track_id_survives_a_full_three_second_radar_revolution(self) -> None:
        first_time = 1_000_000_000
        self._apply(
            1,
            _response(1, sector=_sector(1, timestamp_ns=first_time)),
            first_time,
        )
        first_id = self.tracker.snapshot(
            now_ns=first_time, max_range_m=1.0, fov_deg=360.0, mode="all"
        )["detections"][0]["id"]

        next_revolution_time = first_time + 3_000_000_000
        self._apply(
            2,
            _response(
                2,
                sector=_sector(
                    2,
                    revolution_id=2,
                    timestamp_ns=next_revolution_time,
                ),
            ),
            next_revolution_time,
        )
        detections = self.tracker.snapshot(
            now_ns=next_revolution_time, max_range_m=1.0, fov_deg=360.0, mode="all"
        )["detections"]

        self.assertEqual(detections[0]["id"], first_id)
        self.assertEqual(detections[0]["track_age_ms"], 3000.0)

    def test_session_change_resets_cursor_tracks_and_coverage(self) -> None:
        self._apply(1, _response(1, sector=_sector(1)), 1_000_000_000)
        old_id = self.tracker.snapshot(
            now_ns=1_000_000_000, max_range_m=1.0, fov_deg=360.0, mode="all"
        )["detections"][0]["id"]
        new_sector = _sector(1, timestamp_ns=2_000_000_000, session_id="session-b")
        self._apply(2, _response(2, session_id="session-b", sector=new_sector), 2_000_000_000)

        result = self.tracker.snapshot(
            now_ns=2_000_000_000, max_range_m=1.0, fov_deg=360.0, mode="all"
        )
        self.assertEqual(result["session_id"], "session-b")
        self.assertEqual(result["continuity"], "session_reset")
        self.assertNotEqual(result["detections"][0]["id"], old_id)
        self.assertEqual([item["sample_id"] for item in result["coverage"]["sectors"]], [1])

    def test_sample_gap_resets_tracks_and_reports_partial_coverage(self) -> None:
        self._apply(1, _response(1, sector=_sector(1)), 1_000_000_000)
        self._apply(
            2,
            _response(2, sector=_sector(3, timestamp_ns=1_100_000_000)),
            1_100_000_000,
        )

        result = self.tracker.snapshot(
            now_ns=1_100_000_000, max_range_m=1.0, fov_deg=360.0, mode="all"
        )
        self.assertEqual(result["continuity"], "sample_gap")
        self.assertEqual([item["sample_id"] for item in result["coverage"]["sectors"]], [3])
        self.assertFalse(result["coverage_complete"])
        self.assertLess(result["coverage"]["coverage_ratio"], 1.0)

    def test_resync_requires_a_new_cursor_and_clears_tracks(self) -> None:
        self._apply(1, _response(1, sector=_sector(1)), 1_000_000_000)
        self._apply(
            2,
            _response(2, status="resync_required", session_id="session-b"),
            1_100_000_000,
        )

        result = self.tracker.snapshot(
            now_ns=1_100_000_000, max_range_m=1.0, fov_deg=360.0, mode="all"
        )
        self.assertEqual(result["status"], "resync_required")
        self.assertEqual(result["session_id"], "session-b")
        self.assertIsNone(result["sample_id"])
        self.assertEqual(result["detections"], [])
        self.assertEqual(result["coverage"]["sectors"], [])

    def test_device_error_clears_targets_so_apps_cannot_use_old_coordinates(self) -> None:
        self._apply(1, _response(1, sector=_sector(1)), 1_000_000_000)
        self._apply(
            2,
            _response(2, status="device_error", detail="radar communication fault"),
            1_100_000_000,
        )

        result = self.tracker.snapshot(
            now_ns=1_100_000_000, max_range_m=1.0, fov_deg=360.0, mode="all"
        )
        self.assertEqual(result["status"], "device_error")
        self.assertEqual(result["detail"], "radar communication fault")
        self.assertEqual(result["detections"], [])
        self.assertEqual(result["coverage"]["sectors"], [])

    def test_no_new_sample_preserves_fresh_track_without_gateway_query(self) -> None:
        sample_time = 1_000_000_000
        self._apply(1, _response(1, sector=_sector(1, timestamp_ns=sample_time)), sample_time)
        self._apply(2, _response(2, status="no_new_sample"), sample_time + 100_000_000)

        result = self.tracker.snapshot(
            now_ns=sample_time + 100_000_000,
            max_range_m=1.0,
            fov_deg=360.0,
            mode="all",
        )
        self.assertEqual(result["source_status"], "no_new_sample")
        self.assertEqual(result["status"], "fresh")
        self.assertTrue(result["detections"][0]["fresh"])

    def test_observations_become_stale_then_expire_at_finite_ttl(self) -> None:
        sample_time = 1_000_000_000
        self._apply(1, _response(1, sector=_sector(1, timestamp_ns=sample_time)), sample_time)

        stale = self.tracker.snapshot(
            now_ns=sample_time + 300_000_000,
            max_range_m=1.0,
            fov_deg=360.0,
            mode="all",
        )
        self.assertEqual(stale["status"], "stale")
        self.assertTrue(stale["detections"][0]["stale"])
        self.assertEqual(stale["detections"][0]["observation_age_ms"], 300.0)

        expired = self.tracker.snapshot(
            now_ns=sample_time + 5_100_000_000,
            max_range_m=1.0,
            fov_deg=360.0,
            mode="all",
        )
        self.assertEqual(expired["status"], "no_data")
        self.assertEqual(expired["detections"], [])

    def test_app_queries_keep_expected_geometry_and_generated_ids(self) -> None:
        timestamp_ns = 1_000_000_000
        sector = _sector(
            1,
            timestamp_ns=timestamp_ns,
            azimuth_span_rad=1.0,
            returns=[
                _radar_return(range_m=0.25, azimuth_rad=0.1, observed_at_sync_ns=timestamp_ns),
                _radar_return(range_m=0.12, azimuth_rad=0.8, observed_at_sync_ns=timestamp_ns),
            ],
        )
        self._apply(1, _response(1, sector=sector), timestamp_ns)

        full_scan = self.tracker.snapshot(
            now_ns=timestamp_ns, max_range_m=1.0, fov_deg=360.0, mode="scan"
        )
        front_scan = self.tracker.snapshot(
            now_ns=timestamp_ns, max_range_m=1.0, fov_deg=30.0, mode="front"
        )
        nearest = self.tracker.snapshot(
            now_ns=timestamp_ns, max_range_m=1.0, fov_deg=360.0, mode="nearest"
        )

        self.assertEqual(len(full_scan["detections"]), 2)
        self.assertEqual(len(front_scan["detections"]), 1)
        self.assertEqual(len(nearest["detections"]), 1)
        self.assertEqual(nearest["detections"][0]["distance"], 0.12)
        detection = full_scan["detections"][0]
        for field in ("id", "relative_x", "relative_y", "distance", "bearing_deg", "fresh", "stale"):
            self.assertIn(field, detection)
        self.assertTrue(detection["id"].startswith("radar-test-"))
        self.assertNotIn("truth_id", detection)
        self.assertNotIn("world_id", detection)
        self.assertEqual(full_scan["source_frame_id"], "cleaner_radar")
        self.assertNotIn("state", full_scan)

    def test_source_precision_at_sector_boundary_is_accepted(self) -> None:
        timestamp_ns = 1_000_000_000
        boundary_sector = _sector(
            1,
            azimuth_start_rad=0.1,
            azimuth_span_rad=0.2,
            timestamp_ns=timestamp_ns,
            returns=[
                _radar_return(
                    azimuth_rad=0.3000015,
                    observed_at_sync_ns=timestamp_ns,
                )
            ],
        )
        self._apply(1, _response(1, sector=boundary_sector), timestamp_ns)
        self.assertEqual(len(self.tracker.snapshot(
            now_ns=timestamp_ns, max_range_m=1.0, fov_deg=360.0, mode="all"
        )["detections"]), 1)

    def test_full_coverage_requires_complete_sector_union_and_source_flag(self) -> None:
        first_time = 1_000_000_000
        self._apply(
            1,
            _response(
                1,
                sector=_sector(
                    1,
                    azimuth_start_rad=0.0,
                    azimuth_span_rad=3.141592653589793,
                    timestamp_ns=first_time,
                ),
            ),
            first_time,
        )
        self._apply(
            2,
            _response(
                2,
                sector=_sector(
                    2,
                    azimuth_start_rad=3.141592653589793,
                    azimuth_span_rad=3.141592653589793,
                    timestamp_ns=first_time + 100_000_000,
                    returns=[
                        _radar_return(
                            azimuth_rad=3.3415926535897933,
                            observed_at_sync_ns=first_time + 100_000_000,
                        )
                    ],
                    revolution_complete=True,
                ),
            ),
            first_time + 100_000_000,
        )

        result = self.tracker.snapshot(
            now_ns=first_time + 100_000_000,
            max_range_m=1.0,
            fov_deg=360.0,
            mode="all",
        )
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["coverage"]["coverage_ratio"], 1.0)

    def test_nonpositive_range_and_out_of_range_signal_strength_are_rejected(self) -> None:
        timestamp_ns = 1_000_000_000
        for radar_return in (
            _radar_return(range_m=0.0, observed_at_sync_ns=timestamp_ns),
            _radar_return(signal_strength=-0.1, observed_at_sync_ns=timestamp_ns),
            _radar_return(signal_strength=1.1, observed_at_sync_ns=timestamp_ns),
        ):
            with self.subTest(radar_return=radar_return):
                tracker = RadarObservationTracker(track_id_prefix="invalid")
                with self.assertRaises(RuntimeError):
                    tracker.apply_response(
                        _response(1, sector=_sector(1, timestamp_ns=timestamp_ns, returns=[radar_return])),
                        expected_cycle=_cycle(1),
                        now_ns=timestamp_ns,
                    )

    def test_normalized_signal_strength_endpoints_are_accepted(self) -> None:
        timestamp_ns = 1_000_000_000
        for signal_strength in (0.0, 1.0):
            with self.subTest(signal_strength=signal_strength):
                tracker = RadarObservationTracker(track_id_prefix="boundary")
                tracker.apply_response(
                    _response(
                        1,
                        sector=_sector(
                            1,
                            timestamp_ns=timestamp_ns,
                            returns=[_radar_return(signal_strength=signal_strength, observed_at_sync_ns=timestamp_ns)],
                        ),
                    ),
                    expected_cycle=_cycle(1),
                    now_ns=timestamp_ns,
                )
                detections = tracker.snapshot(
                    now_ns=timestamp_ns, max_range_m=1.0, fov_deg=360.0, mode="all"
                )["detections"]
                self.assertEqual(detections[0]["signal_strength"], signal_strength)


class PerceptionPollerTest(unittest.TestCase):
    def test_background_poll_runs_without_an_app_query_and_query_stays_local(self) -> None:
        component = PerceptionControllerSubsystem.__new__(PerceptionControllerSubsystem)
        component._name = "component.perception"
        component._binding = "radar_source"
        component._poll_period_ms = 100.0
        component._poll_deadline_ms = 250.0
        component._generation = 0
        component._sequence = 0
        component._poll_cycle_lock = threading.Lock()
        component._state_lock = threading.RLock()
        component._poller_stop = threading.Event()
        component._poller_thread = None
        component._runtime_binding_ready = False
        component._tracker = RadarObservationTracker(track_id_prefix="poller")
        component.now_ns = Mock(return_value=1_000_000_000)
        component._logger = Mock()
        component.resolve_runtime_binding = Mock(return_value=object())
        first_poll = threading.Event()

        def radar_cycle(**kwargs):
            request = kwargs["value"]
            cycle = request["cycle"]
            first_poll.set()
            return {
                "ok": True,
                "result": {
                    "cycle": dict(cycle),
                    "session_id": "session-poller",
                    "status": "no_new_sample",
                },
            }

        component.transact_bound_control = Mock(side_effect=radar_cycle)
        component._start_radar_poller()
        self.assertTrue(first_poll.wait(timeout=1.0))
        component._stop_radar_poller()
        call_count_after_stop = component.transact_bound_control.call_count

        result = component._query_local(mode="all", max_range_m=1.0, fov_deg=360.0)

        self.assertEqual(result["source_status"], "no_new_sample")
        self.assertEqual(component.transact_bound_control.call_count, call_count_after_stop)

    def test_initial_binding_pending_does_not_transact_or_consume_sequence(self) -> None:
        component = self._new_poller()
        component.resolve_runtime_binding = Mock(side_effect=[None, object()])
        component.transact_bound_control = Mock(
            return_value={
                "ok": True,
                "result": {
                    "cycle": _cycle(1, generation=77),
                    "session_id": "session-poller",
                    "status": "no_new_sample",
                },
            }
        )

        component._poll_once()

        component.transact_bound_control.assert_not_called()
        self.assertEqual(component._sequence, 0)
        self.assertFalse(component._runtime_binding_ready)

        component._poll_once()

        component.transact_bound_control.assert_called_once()
        self.assertEqual(component.transact_bound_control.call_args.kwargs["value"]["cycle"]["sequence"], 1)
        self.assertEqual(component._sequence, 1)
        self.assertTrue(component._runtime_binding_ready)
        self.assertEqual(component.resolve_runtime_binding.call_count, 2)

    def test_binding_readiness_error_is_logged_and_does_not_consume_sequence(self) -> None:
        component = self._new_poller()
        component.resolve_runtime_binding = Mock(side_effect=RuntimeError("binding status unavailable"))
        component.transact_bound_control = Mock()

        component._poll_once()

        component.transact_bound_control.assert_not_called()
        self.assertEqual(component._sequence, 0)
        self.assertFalse(component._runtime_binding_ready)
        component.logger.warning.assert_called_once_with(
            "Radar", "radar sample unavailable: binding status unavailable"
        )
        state = component._query_local(mode="all", max_range_m=1.0, fov_deg=360.0)
        self.assertEqual(state["status"], "device_error")

    def test_starting_poller_resets_readiness_without_caching_a_target(self) -> None:
        component = self._new_poller()
        component._runtime_binding_ready = True
        thread = Mock()
        with unittest.mock.patch("perception_controller.threading.Thread", return_value=thread):
            component._start_radar_poller()

        self.assertFalse(component._runtime_binding_ready)
        self.assertIs(component._poller_thread, thread)
        self.assertEqual(component._sequence, 0)

    def test_stop_during_initial_binding_query_prevents_first_control(self) -> None:
        component = self._new_poller()
        resolve_started = threading.Event()
        release_resolve = threading.Event()

        def resolve_after_release(_requirement: str) -> object:
            resolve_started.set()
            if not release_resolve.wait(timeout=1.0):
                raise RuntimeError("test binding resolver did not release")
            return object()

        component.resolve_runtime_binding = Mock(side_effect=resolve_after_release)
        component.transact_bound_control = Mock()
        poll_thread = threading.Thread(target=component._poll_once)
        component._poller_thread = poll_thread
        poll_thread.start()

        self.assertTrue(resolve_started.wait(timeout=1.0))
        stop_thread = threading.Thread(target=component._stop_radar_poller)
        stop_thread.start()
        self.assertTrue(component._poller_stop.wait(timeout=1.0))
        release_resolve.set()
        poll_thread.join(timeout=1.0)
        stop_thread.join(timeout=1.0)

        self.assertFalse(poll_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertIsNone(component._poller_thread)
        component.transact_bound_control.assert_not_called()
        self.assertEqual(component._sequence, 0)

    def _new_poller(self) -> PerceptionControllerSubsystem:
        component = PerceptionControllerSubsystem.__new__(PerceptionControllerSubsystem)
        component._name = "component.perception"
        component._binding = "radar_source"
        component._poll_period_ms = 100.0
        component._poll_deadline_ms = 250.0
        component._generation = 77
        component._sequence = 0
        component._poll_cycle_lock = threading.Lock()
        component._state_lock = threading.RLock()
        component._poller_stop = threading.Event()
        component._poller_thread = None
        component._runtime_binding_ready = False
        component._tracker = RadarObservationTracker(track_id_prefix="readiness")
        component.now_ns = Mock(return_value=1_000_000_000)
        component._logger = Mock()
        return component


if __name__ == "__main__":
    unittest.main()
