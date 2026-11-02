from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "subsystems").is_dir())
RADAR_DIR = ROOT / "subsystems" / "app.cleaner.radar"
if str(RADAR_DIR) not in sys.path:
    sys.path.insert(0, str(RADAR_DIR))

from radar_app import RadarCleanerApp, RadarTargetQueue  # noqa: E402
from navigation_math import body_pose_in_radar, resolve_target_pose  # noqa: E402


def radar_mount_transform() -> dict[str, object]:
    half_yaw = math.radians(7.5)
    return {
        "res": "ok",
        "translation": [0.08, 0.03, 0.0],
        "rotation_xyzw": [0.0, 0.0, math.sin(half_yaw), math.cos(half_yaw)],
    }


class RadarTargetQueueTest(unittest.TestCase):
    def test_new_contacts_do_not_replace_active_target(self) -> None:
        queue = RadarTargetQueue()
        first = {"id": "trash-1", "distance": 0.25}
        second = {"id": "trash-2", "distance": 0.10}

        target, lost = queue.select([first])
        self.assertEqual(target["id"], "trash-1")
        self.assertIsNone(lost)

        target, lost = queue.select([second, first])
        self.assertEqual(target["id"], "trash-1")
        self.assertIsNone(lost)
        self.assertEqual(queue.pending_count, 1)

        queue.complete_active()
        target, lost = queue.select([second])
        self.assertEqual(target["id"], "trash-2")
        self.assertIsNone(lost)

    def test_lost_contacts_are_not_followed_from_stale_coordinates(self) -> None:
        queue = RadarTargetQueue()
        queue.select([{"id": "trash-1", "distance": 0.25}])

        target, lost = queue.select([])

        self.assertIsNone(target)
        self.assertEqual(lost, "trash-1")
        self.assertEqual(queue.pending_count, 0)


class RadarNavigationTest(unittest.TestCase):
    def test_collect_extension_uses_body_distance_not_sensor_range(self) -> None:
        app = RadarCleanerApp.__new__(RadarCleanerApp)
        app._targets = RadarTargetQueue()
        app._set_status = Mock()
        app._publish_status = Mock()
        app._open_gripper = Mock(return_value={"opened": True})
        app._extend_arm = Mock(return_value={"reached": True})
        app._close_gripper = Mock(return_value={"closed": True})
        app._shrink_arm = Mock(return_value={"retracted": True})
        app._burn = Mock(return_value={"fired": True})

        with patch("radar_app.time.sleep"):
            app._collect_target({"id": "mounted", "distance": 0.076}, body_distance=0.15)

        app._extend_arm.assert_called_once()
        self.assertAlmostEqual(app._extend_arm.call_args.args[0], (0.15 - 0.08) / 0.18)
        self.assertEqual(app._set_status.call_args.kwargs["action"], "radar.collect_done")

    def test_mounted_radar_target_is_collected_using_body_gripper_geometry(self) -> None:
        app = RadarCleanerApp.__new__(RadarCleanerApp)
        app._scan_range = 0.30
        app._collect_distance = 0.18
        app._collect_bearing_deg = 5.0
        app._minimum_collect_distance = 0.07
        app._targets = RadarTargetQueue()
        transform = radar_mount_transform()
        radar_x, radar_y, _ = body_pose_in_radar(0.15, 0.0, 0.0, transform)
        app._scan = Mock(return_value={
            "source_frame_id": "cleaner_radar",
            "detections": [{
                "id": "trash-mounted",
                "distance": math.hypot(radar_x, radar_y),
                "bearing_deg": math.degrees(math.atan2(radar_y, radar_x)),
                "fresh": True,
            }],
        })
        app.lookup_frame_transform = Mock(return_value=transform)
        app._collect_target = Mock()
        app._navigate_to_pose = Mock()

        app._loop_once()

        app.lookup_frame_transform.assert_called_once_with("cleaner_base", "cleaner_radar")
        app._collect_target.assert_called_once()
        self.assertAlmostEqual(app._collect_target.call_args.kwargs["body_distance"], 0.15)
        app._navigate_to_pose.assert_not_called()

    def test_mounted_radar_goal_round_trips_through_navigation_spatial_lookup(self) -> None:
        app = RadarCleanerApp.__new__(RadarCleanerApp)
        app._scan_range = 0.30
        app._collect_distance = 0.18
        app._collect_bearing_deg = 5.0
        app._minimum_collect_distance = 0.07
        app._stop_loop = Mock()
        app._stop_loop.is_set.return_value = False
        app._targets = RadarTargetQueue()
        transform = radar_mount_transform()
        radar_x, radar_y, _ = body_pose_in_radar(0.28, 0.0, 0.0, transform)
        app._scan = Mock(return_value={
            "source_frame_id": "cleaner_radar",
            "detections": [{
                "id": "trash-mounted-far",
                "distance": math.hypot(radar_x, radar_y),
                "bearing_deg": math.degrees(math.atan2(radar_y, radar_x)),
                "fresh": True,
            }],
        })
        app.lookup_frame_transform = Mock(return_value=transform)
        app._set_status = Mock()
        app._publish_status = Mock()
        app._navigate_to_pose = Mock(return_value={"completed": True, "state": "succeeded"})

        app._loop_once()

        app._navigate_to_pose.assert_called_once()
        call = app._navigate_to_pose.call_args.kwargs
        self.assertEqual(call["frame_id"], "cleaner_radar")
        body_goal = resolve_target_pose(
            frame_id=call["frame_id"],
            target_pose=call["target_pose"],
            current_map_pose=(0.0, 0.0, 0.0),
            body_radar_transform=transform,
        )
        self.assertAlmostEqual(body_goal[0], 0.28 - 0.18 * 0.82, places=9)
        self.assertAlmostEqual(body_goal[1], 0.0, places=9)
        self.assertAlmostEqual(body_goal[2], 0.0, places=9)

    def test_far_detection_is_delegated_to_navigation_component(self) -> None:
        app = RadarCleanerApp.__new__(RadarCleanerApp)
        app._scan_range = 0.30
        app._collect_distance = 0.18
        app._collect_bearing_deg = 5.0
        app._minimum_collect_distance = 0.07
        app._stop_loop = Mock()
        app._stop_loop.is_set.return_value = False
        app._targets = RadarTargetQueue()
        app._scan = Mock(
            return_value={
                "source_frame_id": "cleaner_base",
                "detections": [
                    {
                        "id": "trash-1",
                        "distance": 0.28,
                        "bearing_deg": 20.0,
                        "relative_x": 0.2631,
                        "relative_y": 0.0958,
                        "fresh": True,
                    }
                ],
            }
        )
        app._set_status = Mock()
        app._publish_status = Mock()
        app._navigate_to_pose = Mock(return_value={"completed": True, "state": "succeeded"})
        app._collect_target = Mock()

        app._loop_once()

        app._navigate_to_pose.assert_called_once()
        call = app._navigate_to_pose.call_args.kwargs
        self.assertEqual(call["frame_id"], "cleaner_base")
        self.assertGreater(call["target_pose"]["position"]["x"], 0.0)
        app._collect_target.assert_not_called()
        self.assertEqual(app._set_status.call_args.kwargs["action"], "radar.navigation_done")

    def test_too_close_detection_repositions_before_collection(self) -> None:
        app = RadarCleanerApp.__new__(RadarCleanerApp)
        app._scan_range = 0.30
        app._collect_distance = 0.18
        app._collect_bearing_deg = 5.0
        app._minimum_collect_distance = 0.07
        app._stop_loop = Mock()
        app._stop_loop.is_set.return_value = False
        app._targets = RadarTargetQueue()
        app._scan = Mock(
            return_value={
                "source_frame_id": "cleaner_base",
                "detections": [
                    {
                        "id": "trash-too-close",
                        "distance": 0.02,
                        "bearing_deg": 0.0,
                        "relative_x": 0.02,
                        "relative_y": 0.0,
                        "fresh": True,
                    }
                ],
            }
        )
        app._set_status = Mock()
        app._publish_status = Mock()
        app._navigate_to_pose = Mock(return_value={"completed": True, "state": "succeeded"})
        app._collect_target = Mock()

        app._loop_once()

        app._navigate_to_pose.assert_called_once()
        goal = app._navigate_to_pose.call_args.kwargs["target_pose"]
        self.assertLess(goal["position"]["x"], 0.0)
        app._collect_target.assert_not_called()

    def test_stale_radar_detection_is_not_used_for_navigation(self) -> None:
        app = RadarCleanerApp.__new__(RadarCleanerApp)
        app._scan_range = 0.30
        app._stop_loop = Mock()
        app._targets = RadarTargetQueue()
        app._scan = Mock(
            return_value={
                "source_frame_id": "cleaner_radar",
                "detections": [
                    {"id": "stale-track", "distance": 0.20, "bearing_deg": 0.0, "fresh": False}
                ],
            }
        )
        app._set_status = Mock()
        app._publish_progress = Mock()
        app._navigate_to_pose = Mock()
        app._collect_target = Mock()

        app._loop_once()

        app._navigate_to_pose.assert_not_called()
        app._collect_target.assert_not_called()
        self.assertEqual(app._set_status.call_args.kwargs["action"], "radar.no_target")


if __name__ == "__main__":
    unittest.main()
