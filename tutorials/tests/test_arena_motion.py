from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "subsystems").is_dir() and (parent / "simulator").is_dir())
for path in (ROOT / "lib", ROOT / "simulator"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from arena import ArenaSimulator  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now_ns = 1_000_000_000

    def __call__(self) -> int:
        return self.now_ns

    def advance(self, seconds: float) -> None:
        self.now_ns += int(seconds * 1_000_000_000)


class ArenaMotionTest(unittest.TestCase):
    def test_far_target_scenario_starts_outside_radar_range(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)
        state = arena.reset({"scenario": "far_target"})

        robot = state["robot"]
        target = state["trash"][0]
        self.assertGreater(
            ((target["x"] - robot["x"]) ** 2 + (target["y"] - robot["y"]) ** 2) ** 0.5,
            0.30,
        )
        scan = arena.scan({"mode": "radar", "max_range": 0.30, "fov_deg": 360.0})
        self.assertEqual(scan["detections"], [])

    def test_immediate_stop_clears_current_velocity(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)

        arena.apply_drive({"vx": 1.0, "vy": 0.0, "wz": 0.0, "dt": 0.5})
        clock.advance(0.20)
        moving = arena.get_state()["robot"]
        self.assertGreater(moving["vx"], 0.0)
        self.assertGreater(moving["x"], 0.18)

        stopped = arena.apply_drive({"vx": 0.0, "vy": 0.0, "wz": 0.0, "dt": 0.02, "immediate_stop": True})["robot"]
        self.assertEqual(stopped["target_vx"], 0.0)
        self.assertEqual(stopped["target_vy"], 0.0)
        self.assertEqual(stopped["target_wz"], 0.0)
        self.assertEqual(stopped["vx"], 0.0)
        self.assertEqual(stopped["vy"], 0.0)
        self.assertEqual(stopped["wz"], 0.0)

        stopped_x = stopped["x"]
        clock.advance(0.20)
        later = arena.get_state()["robot"]
        self.assertEqual(later["vx"], 0.0)
        self.assertAlmostEqual(later["x"], stopped_x, places=9)

    def test_radar_detects_only_inside_the_rotating_beam(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)
        arena.reset({"seed": 7, "count": 0})
        sensor_x, sensor_y, sensor_heading = arena._radar_pose_locked()
        arena.spawn_trash(
            {
                "x": sensor_x + 0.22 * math.cos(sensor_heading),
                "y": sensor_y + 0.22 * math.sin(sensor_heading),
            }
        )

        facing_target = arena.scan({"mode": "radar", "max_range": 0.30, "fov_deg": 30.0})
        self.assertEqual(len(facing_target["detections"]), 1)
        self.assertAlmostEqual(facing_target["state"]["scan"]["bearing_center_deg"], 0.0)

        clock.advance(1.0)
        facing_away = arena.scan({"mode": "radar", "max_range": 0.30, "fov_deg": 30.0})
        self.assertEqual(facing_away["state"]["scan"]["beam_detections"], [])
        self.assertEqual(len(facing_away["detections"]), 1)
        self.assertFalse(facing_away["detections"][0]["fresh"])
        self.assertAlmostEqual(facing_away["state"]["scan"]["bearing_center_deg"], 120.0)

        clock.advance(3.0)
        expired = arena.scan({"mode": "radar", "max_range": 0.30, "fov_deg": 30.0})
        self.assertEqual(expired["detections"], [])

    def test_radar_returns_all_tracked_contacts_in_acquisition_order(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)
        arena.reset({"seed": 7, "count": 0})
        sensor_x, sensor_y, sensor_heading = arena._radar_pose_locked()
        for distance in (0.18, 0.24):
            arena.spawn_trash(
                {
                    "x": sensor_x + distance * math.cos(sensor_heading),
                    "y": sensor_y + distance * math.sin(sensor_heading),
                }
            )

        result = arena.scan({"mode": "radar", "max_range": 0.30, "fov_deg": 30.0})

        self.assertEqual([item["id"] for item in result["detections"]], ["trash-1", "trash-2"])
        self.assertNotIn("x", result["detections"][0])
        self.assertNotIn("y", result["detections"][0])
        self.assertIn("relative_x", result["detections"][0])
        self.assertIn("relative_y", result["detections"][0])

    def test_internal_scan_does_not_replace_visible_radar_state(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)
        arena.reset({"seed": 7, "count": 0})
        arena.spawn_trash({"x": 0.36, "y": 0.50})
        radar = arena.scan({"mode": "radar", "max_range": 0.30, "fov_deg": 30.0})

        internal = arena.scan({"mode": "nearest", "max_range": 0.70, "fov_deg": 360.0, "visualize": False})

        self.assertEqual(radar["state"]["scan"]["mode"], "radar")
        self.assertEqual(internal["state"]["scan"]["mode"], "radar")
        self.assertEqual(internal["state"]["scan"]["max_range"], 0.30)

    def test_world_tick_keeps_radar_rotating_without_app_scan_calls(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)
        arena.reset({"seed": 7, "count": 0})
        arena.scan({"mode": "radar", "max_range": 0.30, "fov_deg": 30.0})

        clock.advance(0.50)
        during_blocking_action = arena.tick()

        self.assertAlmostEqual(during_blocking_action["scan"]["bearing_center_deg"], 60.0)
        self.assertEqual(during_blocking_action["scan"]["sweep_deg_per_s"], 120.0)
        self.assertEqual(during_blocking_action["scan"]["sampled_at_ns"], clock.now_ns)


if __name__ == "__main__":
    unittest.main()
