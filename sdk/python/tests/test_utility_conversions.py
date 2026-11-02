from __future__ import annotations

import math
import sys
import unittest
from unittest import mock

import numpy as np

from r2kernel.datatype.coordinate import Orientation, Position, Pose
from r2kernel.datatype.manipulator import JointValues, Payload, TaskWayPoint, TCP, ToolSettings
from r2kernel.datatype.physics import Inertia
from r2kernel.utility.common import blend_pose, offset_pose, to_meter_radian_kg, to_millimeter_degree_g
from r2kernel.utility.rotation_transform import (
    convert_list_to_homogeneous,
    rotation_to_rpy,
    rotation_to_zyz,
    rpy_to_rotation,
    zyz_to_rotation,
)


class UnitConversionTests(unittest.TestCase):
    def assertVectorClose(self, actual, expected):
        np.testing.assert_allclose(list(actual), list(expected), rtol=1e-6, atol=1e-6)

    def test_revolute_prismatic_round_trip_preserves_custom_joint_names(self):
        source = JointValues([180, 250, -90], names=("shoulder", "slide", "elbow"))
        converted = to_meter_radian_kg(source, "RPR")
        self.assertVectorClose(converted, [math.pi, 0.25, -math.pi / 2])
        self.assertAlmostEqual(converted.slide, 0.25)
        restored = to_millimeter_degree_g(converted, ["R", "P", "R"])
        self.assertVectorClose(restored, source)
        self.assertVectorClose(source, [180, 250, -90])
        converted.slide = 2
        self.assertEqual(source.slide, 250)

    def test_default_seven_joint_vector_round_trip(self):
        source = JointValues([180, 1000, 90, 500, -45, -200, 0])
        converted = to_meter_radian_kg(source, "RPRPRPR")
        self.assertVectorClose(converted, [math.pi, 1, math.pi / 2, 0.5, -math.pi / 4, -0.2, 0])
        self.assertVectorClose(to_millimeter_degree_g(converted, "RPRPRPR"), source)

    def test_missing_mismatched_and_unknown_joint_types_fail_in_both_directions(self):
        source = JointValues([1, 2], names=("a", "b"))
        for convert in (to_meter_radian_kg, to_millimeter_degree_g):
            for kinds in (None, [], "R", "RRR", "RX", ["R", "prismatic"]):
                with self.subTest(convert=convert.__name__, kinds=kinds):
                    with self.assertRaises(ValueError):
                        convert(source, kinds)
            with self.assertRaises(ValueError):
                convert(Position(), "R")

    def test_coordinate_round_trips_and_pose_helpers_do_not_change_source(self):
        position = Position(1000, -500, 25)
        orientation = Orientation(180, -90, 45)
        self.assertVectorClose(to_meter_radian_kg(position), [1, -0.5, 0.025])
        self.assertVectorClose(to_meter_radian_kg(orientation), [math.pi, -math.pi / 2, math.pi / 4])
        for source in (position, orientation, Pose(position, orientation)):
            with self.subTest(datatype=type(source).__name__):
                converted = to_meter_radian_kg(source)
                self.assertIsNot(converted, source)
                self.assertVectorClose(to_millimeter_degree_g(converted), source)
        source = Pose(position, orientation)
        self.assertVectorClose(offset_pose(source, x=10, rz=5), [1010, -500, 25, 180, -90, 50])
        self.assertVectorClose(blend_pose(source, y=10, rx=20), [1000, 10, 25, 20, -90, 45])
        self.assertVectorClose(source, [1000, -500, 25, 180, -90, 45])

    def test_current_task_waypoint_fields_survive_round_trip(self):
        source = TaskWayPoint(Pose(Position(1000, 20, -30), Orientation(90, 0, -180)),
                              radius=50, vel=12, acc=34, cornering_vel=56)
        converted = to_meter_radian_kg(source)
        self.assertIsInstance(converted, TaskWayPoint)
        self.assertVectorClose(converted.pose, [1, 0.02, -0.03, math.pi / 2, 0, -math.pi])
        self.assertAlmostEqual(converted.radius, 0.05)
        self.assertEqual((converted.vel, converted.acc, converted.cornering_vel), (12, 34, 56))
        restored = to_millimeter_degree_g(converted)
        self.assertVectorClose(restored.pose, source.pose)
        self.assertAlmostEqual(restored.radius, source.radius)

    def test_tool_settings_payload_and_tcp_round_trip_with_physical_units(self):
        source = ToolSettings(
            payload=Payload(2500, Position(100, 200, -300), Inertia([1e9, 2e9, 3e9, 4e8, -5e8, 6e8])),
            tcp=TCP(Position(1000, -2000, 500), Orientation(180, 90, -45)),
        )
        converted = to_meter_radian_kg(source)
        self.assertIsInstance(converted, ToolSettings)
        self.assertEqual(converted.payload.mass, 2.5)
        self.assertVectorClose(converted.payload.center_of_mass, [0.1, 0.2, -0.3])
        self.assertVectorClose(converted.payload.inertia, [1, 2, 3, 0.4, -0.5, 0.6])
        self.assertVectorClose(converted.tcp.position, [1, -2, 0.5])
        self.assertVectorClose(converted.tcp.orientation, [math.pi, math.pi / 2, -math.pi / 4])
        for value in (source, source.payload, source.tcp):
            with self.subTest(datatype=type(value).__name__):
                restored = to_millimeter_degree_g(to_meter_radian_kg(value))
                payload = restored.payload if isinstance(restored, ToolSettings) else restored
                if isinstance(payload, Payload):
                    self.assertAlmostEqual(payload.mass, source.payload.mass)
                    self.assertVectorClose(payload.center_of_mass, source.payload.center_of_mass)
                    self.assertVectorClose(payload.inertia, source.payload.inertia)
                tcp = restored.tcp if isinstance(restored, ToolSettings) else restored
                if isinstance(tcp, TCP):
                    self.assertVectorClose(tcp.position, source.tcp.position)
                    self.assertVectorClose(tcp.orientation, source.tcp.orientation)
        converted.payload.center_of_mass.x = 9
        self.assertEqual(source.payload.center_of_mass.x, 100)

    def test_documented_rotation_transforms_use_radians(self):
        angles = (0.2, -0.3, 0.4)
        self.assertVectorClose(rotation_to_rpy(rpy_to_rotation(*angles)), angles)
        zyz_angles = (0.2, 0.3, -0.4)
        self.assertVectorClose(rotation_to_zyz(zyz_to_rotation(*zyz_angles)), zyz_angles)
        for convention in ("rotvec", "rpy", "zyz"):
            transform = convert_list_to_homogeneous([1, 2, 3, *zyz_angles], convention)
            self.assertVectorClose(transform[:3, 3], [1, 2, 3])
            np.testing.assert_allclose(transform[:3, :3].T @ transform[:3, :3], np.eye(3), atol=1e-6)
            self.assertVectorClose(transform[3], [0, 0, 0, 1])

    def test_rotation_helpers_explain_the_missing_optional_scipy_extra(self):
        with mock.patch.dict(sys.modules, {"scipy": None, "scipy.spatial": None, "scipy.spatial.transform": None}):
            with self.assertRaisesRegex(ImportError, r"r2kernel-client\[rotation\]"):
                rpy_to_rotation(0.0, 0.0, 0.0)


if __name__ == "__main__":
    unittest.main()
