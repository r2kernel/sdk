from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from localization_math import compose_planar, map_to_odom  # noqa: E402


class LocalizationMathTests(unittest.TestCase):
    def test_map_to_odom_composes_back_to_absolute_pose(self) -> None:
        absolute = (0.42, 0.67, math.radians(35.0))
        odometry = (0.18, -0.04, math.radians(12.0))
        correction = map_to_odom(absolute, odometry)
        estimated = compose_planar(correction, odometry)
        for actual, expected in zip(estimated, absolute):
            self.assertAlmostEqual(actual, expected, places=9)

    def test_map_to_odom_handles_rotated_odometry_frame(self) -> None:
        correction = map_to_odom((0.2, 0.5, math.pi / 2), (0.1, 0.0, 0.0))
        self.assertAlmostEqual(correction[0], 0.2, places=9)
        self.assertAlmostEqual(correction[1], 0.4, places=9)
        self.assertAlmostEqual(correction[2], math.pi / 2, places=9)


if __name__ == "__main__":
    unittest.main()
