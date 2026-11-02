from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "subsystems").is_dir() and (parent / "simulator").is_dir())
for path in (ROOT / "lib", ROOT / "learning" / "radar_policy"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from evaluate import eligible_seeds, run_episode  # noqa: E402
from learned_radar_policy import RadarPolicyModel, feature_vector  # noqa: E402


MODEL = ROOT / "subsystems" / "app.cleaner.learned_radar" / "radar_policy_v1.json"


class LearnedRadarPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.model = RadarPolicyModel.load(MODEL)

    def test_model_emits_finite_bounded_reference(self) -> None:
        features = feature_vector(
            x=0.18,
            y=0.50,
            heading=0.0,
            phase=math.pi / 3.0,
            detection=None,
            scan_range=0.30,
        )
        action = self.model.predict(features)
        self.assertEqual(action.shape, (2,))
        self.assertTrue(all(math.isfinite(float(value)) for value in action))
        self.assertTrue(all(-1.0 <= float(value) <= 1.0 for value in action))

    def test_unseen_out_of_range_targets_are_discovered_and_reached(self) -> None:
        for seed in eligible_seeds(300, 3):
            result = run_episode(self.model, seed=seed, max_steps=750, dt=0.12)
            self.assertTrue(result["discovered"], result)
            self.assertTrue(result["reached"], result)

    def test_evaluation_uses_body_geometry_for_a_mounted_reachable_target(self) -> None:
        # This point is straight ahead at the physical gripper, but off-axis
        # and closer when observed from the translated/rotated radar mount.
        yaw = math.radians(15.0)
        radar_x = math.cos(yaw) * 0.07 - math.sin(yaw) * 0.03
        radar_y = -math.sin(yaw) * 0.07 - math.cos(yaw) * 0.03
        arena = Mock()
        arena.scan.return_value = {"detections": [{"id": "target", "fresh": True,
                                  "distance": math.hypot(radar_x, radar_y),
                                  "bearing_deg": math.degrees(math.atan2(radar_y, radar_x))}]}
        arena.get_state.return_value = {"robot": {"x": 0.18, "y": 0.50, "heading": 0.0}}
        model = Mock()
        model.predict.return_value = [0.0, 0.0]

        with patch("evaluate.ArenaSimulator", return_value=arena):
            result = run_episode(model, seed=7, max_steps=1, dt=0.12)

        self.assertTrue(result["reached"])
        self.assertEqual(result["reach_s"], 0.0)
        features = model.predict.call_args.args[0]
        self.assertAlmostEqual(features[1], 0.15 / 0.30)
        self.assertAlmostEqual(features[2], 0.0)
        self.assertAlmostEqual(features[3], 1.0)
        arena.apply_drive.assert_not_called()

    def test_evaluation_does_not_use_a_stale_cached_target(self) -> None:
        arena = Mock()
        arena.scan.return_value = {"detections": [{"id": "stale", "fresh": False, "distance": 0.10, "bearing_deg": 0.0}]}
        arena.get_state.return_value = {"robot": {"x": 0.18, "y": 0.50, "heading": 0.0}}
        model = Mock()
        model.predict.return_value = [0.1, 0.0]

        with patch("evaluate.ArenaSimulator", return_value=arena):
            result = run_episode(model, seed=7, max_steps=1, dt=0.12)

        self.assertFalse(result["discovered"])
        self.assertFalse(result["reached"])
        self.assertEqual(model.predict.call_args.args[0][0], 0.0)

    def test_evaluation_stops_through_availability_gap_then_reacquires(self) -> None:
        yaw = math.radians(15.0)

        def sensor_target(body_distance):
            radar_x = math.cos(yaw) * (body_distance - 0.08) - math.sin(yaw) * 0.03
            radar_y = -math.sin(yaw) * (body_distance - 0.08) - math.cos(yaw) * 0.03
            return {"id": "active", "fresh": True, "distance": math.hypot(radar_x, radar_y),
                    "bearing_deg": math.degrees(math.atan2(radar_y, radar_x))}

        arena = Mock()
        arena.scan.side_effect = [
            {"detections": [sensor_target(0.25)]},
            {"detections": [{"id": "active", "fresh": False, "distance": float("nan"), "bearing_deg": float("nan")}]},
            {"detections": [sensor_target(0.15)]},
        ]
        arena.get_state.return_value = {"robot": {"x": 0.18, "y": 0.50, "heading": 0.0}}
        model = Mock()
        model.predict.return_value = [0.1, 0.0]

        with patch("evaluate.ArenaSimulator", return_value=arena):
            result = run_episode(model, seed=7, max_steps=3, dt=0.12)

        self.assertTrue(result["reached"])
        self.assertEqual(result["reach_s"], 0.24)
        self.assertEqual(result["waiting_sample_steps"], 1)
        self.assertEqual(model.predict.call_count, 2)
        self.assertEqual(arena.apply_drive.call_count, 2)
        self.assertEqual(arena.apply_drive.call_args.args[0]["vx"], 0.0)
        self.assertEqual(arena.apply_drive.call_args.args[0]["wz"], 0.0)

    def test_evaluation_resumes_model_exploration_after_tracker_retirement(self) -> None:
        arena = Mock()
        arena.scan.side_effect = [
            {"detections": [{"id": "active", "fresh": True, "distance": 0.29, "bearing_deg": 0.0}]},
            {"detections": [{"id": "active", "fresh": False}]},
            {"detections": []},
        ]
        arena.get_state.return_value = {"robot": {"x": 0.18, "y": 0.50, "heading": 0.0}}
        model = Mock()
        model.predict.return_value = [0.1, 0.0]

        with patch("evaluate.ArenaSimulator", return_value=arena):
            result = run_episode(model, seed=7, max_steps=3, dt=0.12)

        self.assertFalse(result["reached"])
        self.assertEqual(result["waiting_sample_steps"], 1)
        self.assertEqual(model.predict.call_count, 2)
        self.assertEqual(model.predict.call_args.args[0][0], 0.0)
        self.assertEqual(arena.apply_drive.call_args.args[0]["vx"], 0.1)


if __name__ == "__main__":
    unittest.main()
