from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from navigation_math import (  # noqa: E402
    body_pose_in_radar,
    navigation_command,
    radar_point_in_body,
    resolve_target_pose,
)


class NavigationMathTests(unittest.TestCase):
    def test_body_relative_goal_is_resolved_in_map_frame(self) -> None:
        target = resolve_target_pose(
            frame_id="cleaner_base",
            target_pose={
                "position": {"x": 1.0, "y": 0.25, "z": 0.0},
                "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
            },
            current_map_pose=(2.0, 3.0, math.pi / 2.0),
        )

        self.assertAlmostEqual(target[0], 1.75, places=9)
        self.assertAlmostEqual(target[1], 4.0, places=9)
        self.assertAlmostEqual(target[2], math.pi / 2.0, places=9)

    def test_radar_goal_uses_runtime_translation_and_yaw_transform(self) -> None:
        mount_yaw = math.radians(15.0)
        body_radar_transform = {
            "res": "ok",
            "source_frame": "component.mobile/cleaner_base",
            "target_frame": "component.perception/cleaner_radar",
            "translation": [0.08, 0.03, 0.0],
            "rotation_xyzw": [0.0, 0.0, math.sin(mount_yaw / 2.0), math.cos(mount_yaw / 2.0)],
        }
        body_point = radar_point_in_body(1.0, 0.25, body_radar_transform)
        target = resolve_target_pose(
            frame_id="cleaner_radar",
            target_pose={
                "position": {"x": 1.0, "y": 0.25, "z": 0.0},
                "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
            },
            current_map_pose=(2.0, 3.0, math.pi / 2.0),
            body_radar_transform=body_radar_transform,
        )

        expected_body_x = 0.08 + math.cos(mount_yaw) - math.sin(mount_yaw) * 0.25
        expected_body_y = 0.03 + math.sin(mount_yaw) + math.cos(mount_yaw) * 0.25
        expected_map_x = 2.0 - expected_body_y
        expected_map_y = 3.0 + expected_body_x
        self.assertAlmostEqual(body_point[0], expected_body_x, places=9)
        self.assertAlmostEqual(body_point[1], expected_body_y, places=9)
        self.assertAlmostEqual(target[0], expected_map_x, places=9)
        self.assertAlmostEqual(target[1], expected_map_y, places=9)
        self.assertAlmostEqual(target[2], math.pi / 2.0 + mount_yaw, places=9)

    def test_body_pose_round_trips_through_radar_frame_and_navigation(self) -> None:
        mount_yaw = math.radians(15.0)
        body_radar_transform = {
            "res": "ok",
            "translation": [0.08, 0.03, 0.0],
            "rotation_xyzw": [0.0, 0.0, math.sin(mount_yaw / 2.0), math.cos(mount_yaw / 2.0)],
        }
        body_pose = (0.43, -0.12, 0.6)
        radar_pose = body_pose_in_radar(*body_pose, body_radar_transform)

        resolved_pose = resolve_target_pose(
            frame_id="cleaner_radar",
            target_pose={
                "position": {"x": radar_pose[0], "y": radar_pose[1], "z": 0.0},
                "orientation": {
                    "x": 0.0,
                    "y": 0.0,
                    "z": math.sin(radar_pose[2] / 2.0),
                    "w": math.cos(radar_pose[2] / 2.0),
                },
            },
            current_map_pose=(0.0, 0.0, 0.0),
            body_radar_transform=body_radar_transform,
        )

        for actual, expected in zip(resolved_pose, body_pose):
            self.assertAlmostEqual(actual, expected, places=9)

    def test_radar_goal_fails_when_frame_lookup_is_unresolved(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no current transform path"):
            resolve_target_pose(
                frame_id="cleaner_radar",
                target_pose={
                    "position": {"x": 1.0, "y": 0.25, "z": 0.0},
                    "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                },
                current_map_pose=(2.0, 3.0, math.pi / 2.0),
                body_radar_transform={
                    "res": "error",
                    "error": "no current transform path from component.mobile/cleaner_base to component.perception/cleaner_radar",
                },
            )

    def test_radar_goal_fails_when_transform_is_absent(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "requires a current Spatial transform"):
            resolve_target_pose(
                frame_id="cleaner_radar",
                target_pose={
                    "position": {"x": 1.0, "y": 0.25, "z": 0.0},
                    "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                },
                current_map_pose=(2.0, 3.0, math.pi / 2.0),
            )

    def test_controller_turns_before_advancing_when_goal_is_to_the_side(self) -> None:
        command = navigation_command(
            (0.0, 0.0, 0.0),
            (0.0, 1.0, math.pi / 2.0),
            position_tolerance=0.01,
            yaw_tolerance=math.radians(2.0),
            max_linear=0.7,
            max_angular=1.0,
        )

        self.assertFalse(command["reached"])
        self.assertAlmostEqual(float(command["linear"]), 0.0, places=9)
        self.assertGreater(float(command["angular"]), 0.0)

    def test_controller_reports_reached_only_after_final_yaw_alignment(self) -> None:
        turning = navigation_command(
            (1.0, 1.0, 0.0),
            (1.0, 1.0, math.pi / 2.0),
            position_tolerance=0.01,
            yaw_tolerance=math.radians(2.0),
            max_linear=0.7,
            max_angular=1.0,
        )
        reached = navigation_command(
            (1.0, 1.0, math.pi / 2.0),
            (1.0, 1.0, math.pi / 2.0),
            position_tolerance=0.01,
            yaw_tolerance=math.radians(2.0),
            max_linear=0.7,
            max_angular=1.0,
        )

        self.assertFalse(turning["reached"])
        self.assertTrue(reached["reached"])


if __name__ == "__main__":
    unittest.main()
