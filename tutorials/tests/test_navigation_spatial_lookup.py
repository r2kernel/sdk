from __future__ import annotations

import importlib.util
import math
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]


def _load_navigation_subsystem():
    module_name = "_tutorial_navigation_spatial_lookup_subject"
    module_names = (
        "r2kernel",
        "r2kernel.wrapper",
        "r2kernel.wrapper.subsystem",
        "common",
        "localization_math",
        "navigation_math",
        "public_sdk",
        "runtime",
        "standard_types",
        module_name,
    )
    missing = object()
    previous_modules = {name: sys.modules.get(name, missing) for name in module_names}
    previous_path = sys.path[:]

    fake_r2kernel = types.ModuleType("r2kernel")
    fake_r2kernel.__path__ = []
    fake_wrapper = types.ModuleType("r2kernel.wrapper")
    fake_wrapper.__path__ = []
    fake_subsystem = types.ModuleType("r2kernel.wrapper.subsystem")
    fake_subsystem.ComponentSubsystem = type("ComponentSubsystem", (), {})
    sys.modules.update(
        {
            "r2kernel": fake_r2kernel,
            "r2kernel.wrapper": fake_wrapper,
            "r2kernel.wrapper.subsystem": fake_subsystem,
        }
    )

    try:
        spec = importlib.util.spec_from_file_location(
            module_name,
            ROOT / "subsystems" / "component.navigation" / "navigation.py",
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load navigation subsystem module")
        navigation_module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = navigation_module
        spec.loader.exec_module(navigation_module)
        return navigation_module.NavigationSubsystem
    finally:
        sys.path[:] = previous_path
        for name, previous in previous_modules.items():
            if previous is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


class NavigationSpatialLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.navigation_subsystem_type = _load_navigation_subsystem()

    def setUp(self) -> None:
        self.navigation = object.__new__(self.navigation_subsystem_type)
        self.navigation._fresh_pose = Mock(return_value=(2.0, 3.0, math.pi / 2.0))
        self.navigation._run_goal = Mock(return_value={"completed": True})

    def test_radar_goal_looks_up_mount_and_uses_current_localized_pose(self) -> None:
        mount_yaw = math.radians(15.0)
        self.navigation.lookup_frame_transform = Mock(
            return_value={
                "res": "ok",
                "source_frame": "component.mobile/cleaner_base",
                "target_frame": "component.perception/cleaner_radar",
                "translation": [0.08, 0.03, 0.0],
                "rotation_xyzw": [
                    0.0,
                    0.0,
                    math.sin(mount_yaw / 2.0),
                    math.cos(mount_yaw / 2.0),
                ],
            }
        )

        self.navigation._navigate_to_pose(
            {
                "frame_id": "cleaner_radar",
                "target_pose": {
                    "position": {"x": 1.0, "y": 0.25, "z": 0.0},
                    "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                },
            }
        )

        self.navigation.lookup_frame_transform.assert_called_once_with(
            "cleaner_base", "cleaner_radar"
        )
        expected_body_x = 0.08 + math.cos(mount_yaw) - math.sin(mount_yaw) * 0.25
        expected_body_y = 0.03 + math.sin(mount_yaw) + math.cos(mount_yaw) * 0.25
        expected_map_x = 2.0 - expected_body_y
        expected_map_y = 3.0 + expected_body_x
        target = self.navigation._run_goal.call_args.args[0]
        self.assertAlmostEqual(target[0], expected_map_x, places=9)
        self.assertAlmostEqual(target[1], expected_map_y, places=9)
        self.assertAlmostEqual(target[2], math.pi / 2.0 + mount_yaw, places=9)

    def test_radar_goal_propagates_lookup_failure_without_starting_motion(self) -> None:
        self.navigation.lookup_frame_transform = Mock(
            return_value={
                "res": "error",
                "error": "no current transform path from cleaner_base to cleaner_radar",
            }
        )

        with self.assertRaisesRegex(RuntimeError, "no current transform path"):
            self.navigation._navigate_to_pose(
                {
                    "frame_id": "cleaner_radar",
                    "target_pose": {
                        "position": {"x": 1.0, "y": 0.25, "z": 0.0},
                        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                    },
                }
            )

        self.navigation.lookup_frame_transform.assert_called_once_with(
            "cleaner_base", "cleaner_radar"
        )
        self.navigation._run_goal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
