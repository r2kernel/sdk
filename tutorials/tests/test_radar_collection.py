from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch


ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "subsystems").is_dir())
APP_DIR = ROOT / "subsystems" / "app.cleaner.radar"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from radar_app import RadarCleanerApp  # noqa: E402


def app_probe() -> RadarCleanerApp:
    probe = RadarCleanerApp.__new__(RadarCleanerApp)
    probe._stop_loop = threading.Event()
    probe._targets = Mock()
    probe._set_status = Mock()
    probe._publish_status = Mock()
    probe._safe_stop_drive = Mock()
    probe._open_gripper = Mock(return_value={"opened": True})
    probe._extend_arm = Mock(return_value={"extension": 0.5})
    probe._close_gripper = Mock(return_value={"closed": True})
    probe._shrink_arm = Mock(return_value={"extension": 0.0})
    probe._burn = Mock(return_value={"fired": True})
    return probe


class RadarCollectionTest(unittest.TestCase):
    @patch("radar_app.time.sleep", return_value=None)
    def test_app_owns_complete_collection_sequence(self, _sleep: Mock) -> None:
        probe = app_probe()
        actions: list[str] = []

        def record(name: str, result: dict[str, object]):
            actions.append(name)
            return result

        probe._open_gripper.side_effect = lambda: record("open", {"opened": True})
        probe._extend_arm.side_effect = lambda value: record("extend", {"extension": value})
        probe._close_gripper.side_effect = lambda: record("close", {"closed": True})
        probe._shrink_arm.side_effect = lambda value: record("shrink", {"extension": value})
        probe._burn.side_effect = lambda: record("burn", {"fired": True})

        probe._collect_target({"id": "trash-1", "distance": 0.17}, body_distance=0.17)

        self.assertEqual(
            actions,
            ["open", "extend", "close", "shrink", "burn"],
        )
        probe._targets.complete_active.assert_called_once_with()
        self.assertEqual(probe._set_status.call_args_list[-1].kwargs["action"], "radar.collect_done")

    @patch("radar_app.time.sleep", return_value=None)
    def test_app_faults_and_retracts_when_burn_fails(self, _sleep: Mock) -> None:
        probe = app_probe()
        probe._burn.return_value = {"fired": False}

        probe._collect_target({"id": "trash-1", "distance": 0.17}, body_distance=0.17)

        self.assertTrue(probe._stop_loop.is_set())
        self.assertEqual(probe._shrink_arm.call_args_list, [call(0.0), call(0.0)])
        probe._safe_stop_drive.assert_called_once_with()
        probe._targets.complete_active.assert_not_called()
        self.assertEqual(probe._set_status.call_args_list[-1].kwargs["action"], "radar.fault")


if __name__ == "__main__":
    unittest.main()
