from __future__ import annotations

import math
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock


TUTORIALS = Path(__file__).resolve().parents[1]
ROOT = TUTORIALS.parent
LEARNED_APP = TUTORIALS / "subsystems" / "app.cleaner.learned_radar"
for path in (ROOT / "sdk" / "python", TUTORIALS / "lib", LEARNED_APP):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from learned_radar_app import LearnedRadarCleanerApp, _RunCancelled  # noqa: E402


def make_app() -> LearnedRadarCleanerApp:
    app = LearnedRadarCleanerApp.__new__(LearnedRadarCleanerApp)
    app._name = "app.cleaner.learned_radar"
    app._run_transition_lock = threading.RLock()
    app._run_state_lock = threading.RLock()
    app._command_lock = threading.RLock()
    app._run_generation = 0
    app._stop_loop = threading.Event()
    app._worker = None
    app._loop_period = 0.005
    app._scan_range = 0.30
    app._collect_distance = 0.18
    app._collect_bearing_deg = 7.0
    app._search_period = 32.0
    app._search_started = time.monotonic()
    app._active_target_id = None
    app._progress_tick = 0
    app._model_sha256 = "test-model"
    app._model = Mock()
    app._model.predict.return_value = [0.1, 0.0]
    app._fresh_pose = Mock(return_value=(0.18, 0.50, 0.0))
    app._set_status = Mock(side_effect=lambda **kwargs: kwargs)
    app._status_payload = Mock(side_effect=lambda **kwargs: kwargs)
    app._publish_status = Mock(return_value={})
    app._record_loop_error = Mock()
    app._safe_stop_drive = Mock()
    app._stop_drive = Mock(return_value={"accepted": True})
    return app


class LearnedRadarLifecycleTest(unittest.TestCase):
    def test_stale_detection_cannot_be_selected_for_collection(self) -> None:
        app = make_app()

        self.assertIsNone(
            app._select_detection(
                [{"id": "stale-target", "distance": 0.12, "bearing_deg": 0.0, "fresh": False}]
            )
        )
        self.assertIsNone(app._active_target_id)

    def test_delayed_scan_cannot_drive_after_stop_and_worker_is_retained(self) -> None:
        app = make_app()
        entered = threading.Event()
        release = threading.Event()
        drive_calls: list[str] = []

        def blocked_scan(**_kwargs):
            entered.set()
            self.assertTrue(release.wait(5.0))
            return {"detections": []}

        app._scan = blocked_scan
        app._drive = lambda **_kwargs: drive_calls.append("drive") or {"accepted": True}

        app._start({})
        worker = app._worker
        self.assertIsNotNone(worker)
        self.assertTrue(entered.wait(1.0))

        app._stop({})

        self.assertTrue(worker.is_alive())
        self.assertIs(app._worker, worker)
        app._start({})
        self.assertIs(app._worker, worker)
        app._status_payload.assert_called_with(reason="learned_radar.already_running")

        release.set()
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(app._worker)
        self.assertEqual(drive_calls, [])

        app._scan = Mock(return_value={"detections": []})
        app._start({})
        restarted_worker = app._worker
        self.assertIsNotNone(restarted_worker)
        self.assertIsNot(restarted_worker, worker)
        app._stop({})
        restarted_worker.join(1.0)
        self.assertFalse(restarted_worker.is_alive())

    def test_stop_serializes_with_every_collection_actuation(self) -> None:
        stages = ("open", "extend", "close", "shrink", "burn")
        for blocked_stage in stages:
            with self.subTest(stage=blocked_stage):
                app = make_app()
                stop_event = threading.Event()
                app._stop_loop = stop_event
                app._run_generation = 1
                app._scan = Mock(
                    return_value={
                        "source_frame_id": "cleaner_base",
                        "detections": [
                            {"id": "target", "distance": 0.12, "bearing_deg": 0.0, "fresh": True}
                        ]
                    }
                )
                entered = threading.Event()
                release = threading.Event()
                actions: list[str] = []

                def action(name: str, result: dict[str, bool]):
                    def run(*_args, **_kwargs):
                        actions.append(name)
                        if name == blocked_stage:
                            entered.set()
                            self.assertTrue(release.wait(5.0))
                        return result

                    return run

                app._open_gripper = action("open", {"opened": True})
                app._extend_arm = action("extend", {"completed": True})
                app._close_gripper = action("close", {"closed": True})
                app._shrink_arm = action("shrink", {"completed": True})
                app._burn = action("burn", {"fired": True})
                app._stop_drive = Mock(
                    side_effect=lambda: actions.append("drive_stop") or {"accepted": True}
                )
                app._safe_stop_drive = Mock(
                    side_effect=lambda: actions.append("safe_stop")
                )

                def run_once() -> None:
                    try:
                        app._loop_once(1, stop_event)
                    except _RunCancelled:
                        pass

                worker = threading.Thread(target=run_once)
                app._worker = worker
                worker.start()
                self.assertTrue(entered.wait(1.0), blocked_stage)

                stop_returned = threading.Event()

                def stop() -> None:
                    app._stop({})
                    actions.append("stop_returned")
                    stop_returned.set()

                stopper = threading.Thread(target=stop)
                stopper.start()
                time.sleep(0.02)
                self.assertFalse(stop_returned.is_set(), blocked_stage)

                release.set()
                stopper.join(2.0)
                worker.join(1.0)
                self.assertTrue(stop_returned.is_set(), blocked_stage)
                self.assertFalse(worker.is_alive(), blocked_stage)
                self.assertIsNone(app._worker)

                returned_index = actions.index("stop_returned")
                self.assertEqual(actions[returned_index + 1 :], [], actions)
                blocked_index = actions.index(blocked_stage)
                for later_stage in stages[stages.index(blocked_stage) + 1 :]:
                    self.assertNotIn(later_stage, actions[blocked_index + 1 :], actions)


class LearnedRadarObservationTest(unittest.TestCase):
    @staticmethod
    def mount_transform():
        yaw = math.radians(15.0)
        return {"res": "ok", "translation": [0.08, 0.03, 0.0],
                "rotation_xyzw": [0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0)]}

    @staticmethod
    def sensor_detection(body_x, body_y):
        # Independently invert the declared mount for a known physical body point.
        yaw = math.radians(15.0)
        radar_x = math.cos(yaw) * (body_x - 0.08) + math.sin(yaw) * (body_y - 0.03)
        radar_y = -math.sin(yaw) * (body_x - 0.08) + math.cos(yaw) * (body_y - 0.03)
        return {"id": "mounted-target", "distance": math.hypot(radar_x, radar_y),
                "bearing_deg": math.degrees(math.atan2(radar_y, radar_x)),
                "fresh": True, "observed_at_sync_ns": 99, "sample_id": 4}

    def prepare_loop(self, detection, *, frame="cleaner_radar"):
        app = make_app()
        app._run_generation = 1
        app._scan = Mock(return_value={"source_frame_id": frame, "detections": [detection]})
        app.lookup_frame_transform = Mock(return_value=self.mount_transform())
        app._drive = Mock(return_value={"accepted": True})
        return app

    def test_mounted_observation_reaches_model_in_body_coordinates(self) -> None:
        raw = self.sensor_detection(0.28, 0.06)
        app = self.prepare_loop(raw)

        app._loop_once(1, app._stop_loop)

        app.lookup_frame_transform.assert_called_once_with("cleaner_base", "cleaner_radar")
        features = app._model.predict.call_args.args[0]
        self.assertAlmostEqual(features[1], math.hypot(0.28, 0.06) / 0.30)
        self.assertAlmostEqual(features[2], 0.06 / math.hypot(0.28, 0.06))
        self.assertAlmostEqual(features[3], 0.28 / math.hypot(0.28, 0.06))
        observation = app._set_status.call_args.kwargs["result"]["observation"]
        self.assertEqual(observation["frame_id"], "cleaner_base")
        self.assertAlmostEqual(observation["distance"], math.hypot(0.28, 0.06))
        self.assertAlmostEqual(observation["bearing_deg"], math.degrees(math.atan2(0.06, 0.28)))
        self.assertEqual(raw, self.sensor_detection(0.28, 0.06))

    def test_mounted_reachable_target_uses_body_collection_reach(self) -> None:
        raw = self.sensor_detection(0.15, 0.0)
        self.assertGreater(abs(raw["bearing_deg"]), 7.0)
        app = self.prepare_loop(raw)
        app._wait_for_run = Mock()
        app._open_gripper = Mock(return_value={"opened": True})
        app._extend_arm = Mock(return_value={"completed": True})
        app._close_gripper = Mock(return_value={"closed": True})
        app._shrink_arm = Mock(return_value={"completed": True})
        app._burn = Mock(return_value={"fired": True})

        app._loop_once(1, app._stop_loop)

        app._model.predict.assert_not_called()
        self.assertAlmostEqual(app._extend_arm.call_args.args[0], (0.15 - 0.08) / 0.18)
        app._burn.assert_called_once_with()
        self.assertEqual(app._set_status.call_args.kwargs["action"], "learned_radar.collect_done")
        target = app._set_status.call_args_list[0].kwargs["result"]["target"]
        self.assertEqual((target["fresh"], target["observed_at_sync_ns"], target["sample_id"]), (True, 99, 4))

    def test_stale_detection_does_not_lookup_or_collect(self) -> None:
        app = self.prepare_loop({**self.sensor_detection(0.15, 0.0), "fresh": False})
        app._collect_target = Mock()

        app._loop_once(1, app._stop_loop)

        app.lookup_frame_transform.assert_not_called()
        app._collect_target.assert_not_called()
        self.assertEqual(app._model.predict.call_args.args[0][0], 0.0)

    def test_retained_target_waits_without_stale_inference_then_reacquires(self) -> None:
        raw = self.sensor_detection(0.28, 0.06)
        app = self.prepare_loop(raw)
        app._collect_target = Mock()
        app._loop_once(1, app._stop_loop)
        app._model.predict.reset_mock()
        app._drive.reset_mock()
        app.lookup_frame_transform.reset_mock()
        stale = {"id": raw["id"], "fresh": False, "distance": float("nan"), "bearing_deg": float("nan")}
        app._scan.return_value = {"source_frame_id": "cleaner_radar", "detections": [stale]}

        app._loop_once(1, app._stop_loop)

        app._stop_drive.assert_called_once_with()
        app._model.predict.assert_not_called()
        app._drive.assert_not_called()
        app.lookup_frame_transform.assert_not_called()
        app._collect_target.assert_not_called()
        self.assertEqual(app._active_target_id, raw["id"])
        waiting = app._set_status.call_args.kwargs
        self.assertEqual(waiting["action"], "learned_radar.waiting_sample")
        self.assertIsNone(waiting["result"]["proposed"])
        self.assertIsNone(waiting["result"]["observation"]["distance"])

        app._scan.return_value = {"source_frame_id": "cleaner_radar", "detections": [raw]}
        app._loop_once(1, app._stop_loop)
        app._model.predict.assert_called_once()
        app._drive.assert_called_once()
        self.assertEqual(app._set_status.call_args.kwargs["action"], "learned_radar.approach")

    def test_retired_target_resumes_exploration(self) -> None:
        app = self.prepare_loop(self.sensor_detection(0.28, 0.06))
        app._loop_once(1, app._stop_loop)
        app._model.predict.reset_mock()
        app._scan.return_value = {"source_frame_id": "cleaner_radar", "detections": []}

        app._loop_once(1, app._stop_loop)

        self.assertIsNone(app._active_target_id)
        self.assertEqual(app._model.predict.call_args.args[0][0], 0.0)
        self.assertEqual(app._set_status.call_args.kwargs["action"], "learned_radar.explore")

    def test_retained_active_target_has_priority_over_new_fresh_target(self) -> None:
        app = make_app()
        app._active_target_id = "active"
        alternative = {"id": "alternative", "fresh": True, "distance": 0.10, "bearing_deg": 0.0}

        self.assertIsNone(app._select_detection([{"id": "active", "fresh": False}, alternative]))
        self.assertEqual(app._active_target_id, "active")
        self.assertEqual(app._select_detection([alternative]), alternative)
        self.assertEqual(app._active_target_id, "alternative")

    def test_canceled_run_cannot_issue_waiting_stop(self) -> None:
        app = make_app()
        app._run_generation = 1
        app._active_target_id = "active"
        app._stop_loop.set()
        with self.assertRaises(_RunCancelled):
            app._wait_for_target_sample(1, app._stop_loop)
        app._stop_drive.assert_not_called()
        app._set_status.assert_not_called()

    def test_failed_transform_cannot_drive_or_collect(self) -> None:
        app = self.prepare_loop(self.sensor_detection(0.15, 0.0))
        app.lookup_frame_transform.return_value = {"res": "error", "error": "no current transform path"}
        app._collect_target = Mock()

        with self.assertRaisesRegex(RuntimeError, "no current transform path"):
            app._loop_once(1, app._stop_loop)

        app._model.predict.assert_not_called()
        app._drive.assert_not_called()
        app._collect_target.assert_not_called()

    def test_unknown_frame_cannot_drive(self) -> None:
        app = self.prepare_loop(self.sensor_detection(0.28, 0.06), frame="unknown_sensor")
        with self.assertRaisesRegex(ValueError, "unsupported radar observation frame"):
            app._loop_once(1, app._stop_loop)
        app._drive.assert_not_called()


if __name__ == "__main__":
    unittest.main()
