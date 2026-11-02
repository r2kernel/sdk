from __future__ import annotations

import json
import math
import sys
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import yaml


TUTORIALS = Path(__file__).resolve().parents[1]
ROOT = TUTORIALS.parent
for _relative in (
    "simulator",
    "lib",
    "subsystems/component.localizer",
    "subsystems/gateway.device_gateway",
):
    _path = str(TUTORIALS / _relative)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from arena_server import ArenaServer  # noqa: E402
from device_gateway import (  # noqa: E402
    GNSS_CYCLE_REQUEST,
    GNSS_CYCLE_RESPONSE,
    GNSS_PROVIDER_MAX_RESPONSE_BYTES,
    DeviceGatewaySubsystem,
)
from gnss_cycle import GnssSampleTracker  # noqa: E402
from gnss_fixture import load_gnss_fixture  # noqa: E402
from gnss_math import (  # noqa: E402
    antenna_enu_to_body_pose,
    body_pose_covariance6,
    geodetic_to_local_enu,
    local_enu_to_geodetic,
)
from gnss_mount import GnssMount, load_gnss_mount  # noqa: E402
from gnss_provider import GnssResponseNormalizer  # noqa: E402
from gnss_source import GnssReceiverSimulator  # noqa: E402
from localizer import (  # noqa: E402
    GNSS_CYCLE_REQUEST as LOCALIZER_GNSS_CYCLE_REQUEST,
    GNSS_CYCLE_RESPONSE as LOCALIZER_GNSS_CYCLE_RESPONSE,
    LocalizerSubsystem,
)


def cycle(sequence: int = 1) -> dict[str, int | str]:
    return {
        "generation": 10,
        "sequence": sequence,
        "intent": "sample",
        "state_revision": 0,
    }


def gnss_fix(
    *,
    session_id: str = "receiver-a",
    sample_id: int = 1,
    mode: str = "fix_3d",
    observation_time_status: str = "known_sync_time",
    observed_at_sync_ns: int = 100,
) -> dict[str, object]:
    fix: dict[str, object] = {
        "session_id": session_id,
        "sample_id": sample_id,
        "frame_id": "gnss_antenna",
        "fix_mode": mode,
        "correction_status": "unknown" if mode in {"unknown", "no_fix"} else "autonomous",
        "observation_time_status": observation_time_status,
    }
    if observation_time_status == "known_sync_time":
        fix["observed_at_sync_ns"] = observed_at_sync_ns
    if mode in {"fix_2d", "fix_3d"}:
        fix.update(latitude_deg=37.422, longitude_deg=-122.084)
    if mode == "fix_3d":
        fix.update(
            altitude_m=12.7,
            altitude_reference="wgs84_ellipsoid",
            position_covariance_m2=[0.64, 0.0, 0.08, 0.0, 0.81, -0.03, 0.08, -0.03, 2.56],
        )
    return fix


def tracker_response(
    *,
    sequence: int,
    session_id: str = "receiver-a",
    status: str = "sample_available",
    fix: dict[str, object] | None = None,
) -> dict[str, object]:
    response: dict[str, object] = {
        "cycle": cycle(sequence),
        "session_id": session_id,
        "status": status,
    }
    if fix is not None:
        response["fix"] = fix
    if status in {"resync_required", "device_error"}:
        response["detail"] = "simulated receiver status"
    return response


def source_sample(*, sample_id: int = 1, acquired_at_clock_ns: int = 1_015) -> dict[str, object]:
    return {
        "session_id": "receiver-a",
        "sample_id": sample_id,
        "frame_id": "gnss_antenna",
        "fix_mode": "fix_3d",
        "correction_status": "autonomous",
        "latitude_deg": 37.422,
        "longitude_deg": -122.084,
        "altitude_m": 12.7,
        "altitude_reference": "wgs84_ellipsoid",
        "position_covariance_m2": [0.64, 0.0, 0.0, 0.0, 0.64, 0.0, 0.0, 0.0, 2.56],
        "acquired_at_clock_ns": acquired_at_clock_ns,
        "acquisition_clock_id": "host_system",
    }


def localizer_odometry(*, x: float = 1.0, y: float = 2.0, z: float = 0.0, yaw: float = math.pi / 2) -> dict[str, object]:
    return {
        "pose": {
            "position": {"x": x, "y": y, "z": z},
            "orientation": {
                "x": 0.0,
                "y": 0.0,
                "z": math.sin(yaw / 2),
                "w": math.cos(yaw / 2),
            },
        }
    }


class GnssMathAndMountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = load_gnss_fixture(TUTORIALS / "config" / "gnss_fixture.yaml")
        cls.mount = load_gnss_mount(
            TUTORIALS / "subsystems" / "compound.cleaner" / "subsystem.yaml",
            TUTORIALS / "subsystems" / "component.localizer" / "subsystem.yaml",
        )

    def test_wgs84_origin_maps_to_zero_and_enu_round_trips(self) -> None:
        origin = self.fixture.geographic_origin
        self.assertEqual(
            geodetic_to_local_enu(
                origin.latitude_deg, origin.longitude_deg, origin.altitude_m, origin
            ),
            (0.0, 0.0, 0.0),
        )
        geodetic = local_enu_to_geodetic(12.5, -7.25, 1.2, origin)
        recovered = geodetic_to_local_enu(*geodetic, origin)
        for actual, expected in zip(recovered, (12.5, -7.25, 1.2)):
            self.assertAlmostEqual(actual, expected, places=5)

    def test_body_pose_uses_mount_offset_and_odometry_yaw_not_receiver_heading(self) -> None:
        body_pose = antenna_enu_to_body_pose(
            (10.0, 20.0, 0.25),
            math.pi / 2,
            self.mount,
            map_from_odometry_yaw_rad=0.1,
            planar_body_height_m=0.0,
        )
        expected_offset_x = math.cos(math.pi / 2 + 0.1) * 0.12 - math.sin(math.pi / 2 + 0.1) * -0.04
        expected_offset_y = math.sin(math.pi / 2 + 0.1) * 0.12 + math.cos(math.pi / 2 + 0.1) * -0.04
        self.assertAlmostEqual(body_pose[0], 10.0 - expected_offset_x, places=12)
        self.assertAlmostEqual(body_pose[1], 20.0 - expected_offset_y, places=12)
        self.assertAlmostEqual(body_pose[2], 0.0, places=12)
        self.assertAlmostEqual(body_pose[3], math.pi / 2 + 0.1, places=12)

    def test_pose_covariance_preserves_measured_vertical_and_cross_terms(self) -> None:
        covariance_enu = [0.64, 0.02, 0.08, 0.02, 0.81, -0.03, 0.08, -0.03, 2.56]
        covariance = body_pose_covariance6(
            antenna_covariance_enu_m2=covariance_enu,
            odometry_yaw_rad=0.0,
            map_from_odometry_yaw_rad=0.0,
            mount=self.mount,
            heading_variance_rad2=0.0012,
            roll_pitch_variance_rad2=0.04,
        )["values"]
        self.assertAlmostEqual(covariance[14], 2.56 + 0.04 * (0.04**2 + 0.12**2), places=12)
        self.assertAlmostEqual(covariance[2], 0.08 - 0.0012, places=12)
        self.assertAlmostEqual(covariance[12], covariance[2], places=12)
        self.assertAlmostEqual(covariance[8], -0.03 + 0.0004, places=12)
        self.assertAlmostEqual(covariance[13], covariance[8], places=12)
        self.assertAlmostEqual(covariance[5], -0.04 * 0.0012, places=15)
        self.assertAlmostEqual(covariance[30], covariance[5], places=15)
        self.assertAlmostEqual(covariance[11], -0.12 * 0.0012, places=15)
        self.assertAlmostEqual(covariance[31], covariance[11], places=15)

    def test_zero_vertical_variance_is_preserved_and_planar_height_prior_is_explicit(self) -> None:
        zero_covariance = [0.0] * 9
        zero_mount = GnssMount("gnss_antenna", 0.0, 0.0, 0.0)
        measured_zero = body_pose_covariance6(
            antenna_covariance_enu_m2=zero_covariance,
            odometry_yaw_rad=0.2,
            map_from_odometry_yaw_rad=0.0,
            mount=zero_mount,
            heading_variance_rad2=0.0,
            roll_pitch_variance_rad2=0.04,
        )["values"]
        self.assertEqual(measured_zero[14], 0.0)

        planar_prior = body_pose_covariance6(
            antenna_covariance_enu_m2=zero_covariance,
            odometry_yaw_rad=0.2,
            map_from_odometry_yaw_rad=0.0,
            mount=zero_mount,
            heading_variance_rad2=0.0,
            roll_pitch_variance_rad2=0.04,
            planar_vertical_variance_m2=4.0,
        )["values"]
        self.assertEqual(planar_prior[14], 4.0)
        self.assertEqual((planar_prior[2], planar_prior[8], planar_prior[12], planar_prior[13]), (0, 0, 0, 0))


class GnssSampleTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = GnssSampleTracker(expected_frame_id="gnss_antenna")
        self.validity = {
            "maximum_measurement_age_ns": 100,
            "future_timestamp_tolerance_ns": 10,
        }

    def apply(self, response: dict[str, object], *, now_ns: int) -> str:
        sequence = response["cycle"]["sequence"]  # type: ignore[index]
        return self.tracker.apply_response(
            response,
            expected_cycle=cycle(int(sequence)),
            now_sync_ns=now_ns,
            **self.validity,
        )

    def test_no_fix_is_distinct_and_clears_previous_position_measurement(self) -> None:
        self.assertEqual(
            self.apply(tracker_response(sequence=1, fix=gnss_fix()), now_ns=100),
            "localized",
        )
        no_fix = gnss_fix(sample_id=2, mode="no_fix", observation_time_status="unknown")
        self.assertEqual(self.apply(tracker_response(sequence=2, fix=no_fix), now_ns=110), "no_fix")
        self.assertIsNone(self.tracker.measurement)
        self.assertEqual(self.tracker.request_cursor(), {"expected_session_id": "receiver-a", "after_sample_id": 2})

    def test_fix_2d_is_recognized_but_not_fused_without_required_3d_covariance(self) -> None:
        fix = gnss_fix(sample_id=1, mode="fix_2d")
        self.assertEqual(self.apply(tracker_response(sequence=1, fix=fix), now_ns=100), "uncertainty_unavailable")
        self.assertIsNone(self.tracker.measurement)

    def test_fix_3d_sample_and_skipped_ids_advance_latest_only_cursor(self) -> None:
        self.assertEqual(self.apply(tracker_response(sequence=1, fix=gnss_fix()), now_ns=100), "localized")
        self.assertEqual(self.tracker.measurement.sample_id, 1)  # type: ignore[union-attr]
        skipped = gnss_fix(sample_id=4, observed_at_sync_ns=150)
        self.assertEqual(self.apply(tracker_response(sequence=2, fix=skipped), now_ns=150), "localized")
        self.assertEqual(self.tracker.request_cursor(), {"expected_session_id": "receiver-a", "after_sample_id": 4})
        self.assertEqual(
            self.apply(tracker_response(sequence=3, status="no_new_sample"), now_ns=160),
            "localized",
        )
        self.assertEqual(self.tracker.measurement.observed_at_sync_ns, 150)  # type: ignore[union-attr]

    def test_unknown_observation_time_is_not_assigned_receive_time(self) -> None:
        fix = gnss_fix(observation_time_status="unknown")
        self.assertNotIn("observed_at_sync_ns", fix)
        self.assertEqual(self.apply(tracker_response(sequence=1, fix=fix), now_ns=100), "unknown_measurement_time")
        self.assertIsNone(self.tracker.measurement)

    def test_session_resync_is_separate_from_no_new_sample(self) -> None:
        self.apply(tracker_response(sequence=1, fix=gnss_fix()), now_ns=100)
        resync = tracker_response(sequence=2, session_id="receiver-b", status="resync_required")
        self.assertEqual(self.apply(resync, now_ns=101), "resync_required")
        self.assertIsNone(self.tracker.measurement)
        self.assertEqual(self.tracker.request_cursor(), {"expected_session_id": "receiver-b", "after_sample_id": 0})
        self.assertEqual(
            self.apply(tracker_response(sequence=3, session_id="receiver-b", status="no_new_sample"), now_ns=102),
            "acquiring",
        )

    def test_sample_timestamp_zero_is_valid_and_expiry_runs_before_invalid_response(self) -> None:
        zero_time = gnss_fix(observed_at_sync_ns=0)
        self.assertEqual(self.apply(tracker_response(sequence=1, fix=zero_time), now_ns=0), "localized")
        with self.assertRaises(ValueError):
            self.apply({"cycle": cycle(2)}, now_ns=101)
        self.assertIsNone(self.tracker.measurement)
        self.assertEqual(self.tracker.status, "stale_measurement")

    def test_retained_measurement_expires_on_stale_or_future_clock_step(self) -> None:
        self.apply(tracker_response(sequence=1, fix=gnss_fix(observed_at_sync_ns=100)), now_ns=100)
        self.assertEqual(self.tracker.refresh_usability(now_sync_ns=201, **self.validity), "stale_measurement")
        self.assertIsNone(self.tracker.measurement)

        self.tracker.reset()
        self.apply(tracker_response(sequence=1, fix=gnss_fix(observed_at_sync_ns=100)), now_ns=100)
        self.assertEqual(self.tracker.refresh_usability(now_sync_ns=0, **self.validity), "future_measurement_time")
        self.assertIsNone(self.tracker.measurement)

    def test_device_error_is_not_no_new_sample_and_does_not_refresh_measurement_time(self) -> None:
        self.apply(tracker_response(sequence=1, fix=gnss_fix()), now_ns=100)
        error = tracker_response(sequence=2, status="device_error")
        self.assertEqual(self.apply(error, now_ns=101), "device_error")
        self.assertEqual(self.tracker.measurement.observed_at_sync_ns, 100)  # type: ignore[union-attr]
        self.assertEqual(self.tracker.refresh_usability(now_sync_ns=201, **self.validity), "stale_measurement")


class GnssSimulatorAndNormalizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = load_gnss_fixture(TUTORIALS / "config" / "gnss_fixture.yaml")

    def test_simulator_samples_are_finite_rate_latest_only_and_immutable(self) -> None:
        receiver = GnssReceiverSimulator()
        receiver.reset(now_ns=100)
        self.assertFalse(receiver.capture(body_x_m=0.0, body_y_m=0.0, body_yaw_rad=0.0, now_ns=100 + receiver.sample_period_ns - 1))
        self.assertTrue(receiver.capture(body_x_m=0.0, body_y_m=0.0, body_yaw_rad=0.0, now_ns=100 + receiver.sample_period_ns))
        first = receiver.read_cycle({"cycle": cycle(), "after_sample_id": 0}, now_ns=200_000_100)
        self.assertEqual(first["status"], "sample_available")
        self.assertEqual(first["sample"]["frame_id"], "gnss_antenna")  # type: ignore[index]
        self.assertEqual(first["sample"]["fix_mode"], "fix_3d")  # type: ignore[index]
        antenna_enu = geodetic_to_local_enu(
            first["sample"]["latitude_deg"],  # type: ignore[index]
            first["sample"]["longitude_deg"],  # type: ignore[index]
            first["sample"]["altitude_m"],  # type: ignore[index]
            self.fixture.geographic_origin,
        )
        self.assertAlmostEqual(antenna_enu[0], 0.12, places=5)
        self.assertAlmostEqual(antenna_enu[1], -0.04, places=5)
        self.assertAlmostEqual(antenna_enu[2], 0.25, places=5)
        first["sample"]["position_covariance_m2"][0] = 999.0  # type: ignore[index]
        repeated = receiver.read_cycle(
            {"cycle": cycle(2), "after_sample_id": 1, "expected_session_id": first["session_id"]},
            now_ns=200_000_101,
        )
        self.assertEqual(repeated["status"], "no_new_sample")
        self.assertTrue(receiver.capture(body_x_m=0.2, body_y_m=0.0, body_yaw_rad=0.4, now_ns=100 + 2 * receiver.sample_period_ns))
        latest = receiver.read_cycle(
            {"cycle": cycle(3), "after_sample_id": 1, "expected_session_id": first["session_id"]},
            now_ns=400_000_100,
        )
        self.assertEqual(latest["sample"]["sample_id"], 2)  # type: ignore[index]
        self.assertEqual(latest["sample"]["position_covariance_m2"][0], 0.64)  # type: ignore[index]
        changed_session = receiver.read_cycle(
            {"cycle": cycle(4), "after_sample_id": 2, "expected_session_id": "old-session"},
            now_ns=400_000_100,
        )
        self.assertEqual(changed_session["status"], "resync_required")
        self.assertNotIn("sample", changed_session)

    def test_unknown_clock_mapping_preserves_unknown_time_and_deep_cache_immutability(self) -> None:
        unknown_fixture = replace(
            self.fixture,
            clock_status="unknown",
            source_clock=None,
            target_clock=None,
            clock_offset_ns=None,
            clock_alignment_tolerance_ns=None,
        )
        normalizer = GnssResponseNormalizer(unknown_fixture)
        sample = source_sample()
        response = {
            "cycle": cycle(),
            "session_id": "receiver-a",
            "status": "sample_available",
            "simulator_now_ns": 1_015,
            "simulator_clock_id": "host_system",
            "sample": sample,
        }
        canonical = normalizer.normalize_response(
            response,
            expected_cycle=_control_cycle_object(cycle()),
            expected_session_id=None,
            before_sync_ns=1_000,
            after_sync_ns=1_020,
        )
        self.assertEqual(canonical["fix"]["observation_time_status"], "unknown")  # type: ignore[index]
        self.assertNotIn("observed_at_sync_ns", canonical["fix"])  # type: ignore[operator]
        canonical["fix"]["position_covariance_m2"][0] = 777.0  # type: ignore[index]
        repeated = normalizer.normalize_response(
            {**response, "cycle": cycle(2)},
            expected_cycle=_control_cycle_object(cycle(2)),
            expected_session_id=None,
            before_sync_ns=9_000,
            after_sync_ns=9_020,
        )
        self.assertEqual(repeated["fix"]["position_covariance_m2"][0], 0.64)  # type: ignore[index]
        changed = {**sample, "latitude_deg": 37.5}
        with self.assertRaisesRegex(RuntimeError, "immutable GNSS sample"):
            normalizer.normalize_response(
                {**response, "cycle": cycle(3), "sample": changed},
                expected_cycle=_control_cycle_object(cycle(3)),
                expected_session_id=None,
                before_sync_ns=9_000,
                after_sync_ns=9_020,
            )

    def test_known_clock_sample_mapping_is_frozen_across_a_later_alignment_failure(self) -> None:
        normalizer = GnssResponseNormalizer(self.fixture)
        sample = source_sample()
        first = {
            "cycle": cycle(),
            "session_id": "receiver-a",
            "status": "sample_available",
            "simulator_now_ns": 1_015,
            "simulator_clock_id": "host_system",
            "sample": sample,
        }
        accepted = normalizer.normalize_response(
            first,
            expected_cycle=_control_cycle_object(cycle()),
            expected_session_id=None,
            before_sync_ns=1_000,
            after_sync_ns=1_020,
        )
        repeated = normalizer.normalize_response(
            {**first, "cycle": cycle(2)},
            expected_cycle=_control_cycle_object(cycle(2)),
            expected_session_id=None,
            before_sync_ns=9_000,
            after_sync_ns=9_020,
        )
        self.assertEqual(accepted["fix"]["observed_at_sync_ns"], 1_015)  # type: ignore[index]
        self.assertEqual(repeated["fix"]["observed_at_sync_ns"], 1_015)  # type: ignore[index]

    def test_changed_session_resync_is_not_rewritten_by_bad_clock_alignment(self) -> None:
        normalizer = GnssResponseNormalizer(self.fixture)
        response = {
            "cycle": cycle(),
            "session_id": "receiver-new",
            "status": "resync_required",
            "detail": "session changed",
            "simulator_now_ns": 0,
            "simulator_clock_id": "wrong_clock",
        }
        canonical = normalizer.normalize_response(
            response,
            expected_cycle=_control_cycle_object(cycle()),
            expected_session_id="receiver-old",
            before_sync_ns=1_000,
            after_sync_ns=1_020,
        )
        self.assertEqual(canonical["status"], "resync_required")
        self.assertNotIn("fix", canonical)


class GnssAdapterIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = load_gnss_fixture(TUTORIALS / "config" / "gnss_fixture.yaml")
        cls.mount = load_gnss_mount(
            TUTORIALS / "subsystems" / "compound.cleaner" / "subsystem.yaml",
            TUTORIALS / "subsystems" / "component.localizer" / "subsystem.yaml",
        )

    def _gateway(self, response: dict[str, object], *, times: list[int] | None = None) -> DeviceGatewaySubsystem:
        gateway = DeviceGatewaySubsystem.__new__(DeviceGatewaySubsystem)
        gateway._gnss_fixture = self.fixture
        gateway._gnss_response_normalizer = GnssResponseNormalizer(self.fixture)
        gateway._gnss_cycle_lock = threading.Lock()
        gateway._gnss_control_owner = None
        gateway._gnss_control_generation = None
        gateway._gnss_control_sequence = 0
        gateway._simulator = Mock()
        gateway._simulator.call.return_value = response
        gateway._gnss_message_codec = None
        gateway._encode_gnss_message = Mock(side_effect=lambda _kind, payload: json.dumps(payload).encode("utf-8"))
        gateway.bound_control_owner = Mock(return_value="component.localizer")
        gateway.bound_control_authority = Mock(side_effect=lambda interface: {"source": gateway.bound_control_owner(interface),
            "source_reference": "component.localizer.gnss_source", "peer": None, "source_session": "localizer-session",
            "target_session": "gateway-session", "interface": interface, "binding_revision": "binding-a",
            "source_registration": "source-registration", "target_registration": "target-registration"})
        gateway.now_ns = Mock(side_effect=times or [1_000, 1_010, 1_020, 1_030])
        return gateway

    def test_gateway_typed_endpoint_maps_acquisition_timestamp_and_checks_owner(self) -> None:
        sample = source_sample(acquired_at_clock_ns=1_015)
        response = {
            "cycle": cycle(),
            "session_id": "receiver-a",
            "status": "sample_available",
            "simulator_now_ns": 1_015,
            "simulator_clock_id": "host_system",
            "sample": sample,
        }
        gateway = self._gateway(response)
        context = SimpleNamespace(
            source_subsystem="component.localizer",
            timed=True,
            generated_at_sync_ns=900,
            valid_until_sync_ns=2_000,
            execute_at_sync_ns=None,
        )
        result = gateway._gnss_cycle({"cycle": cycle()}, context)
        self.assertEqual(result["status"], "sample_available")
        self.assertEqual(result["fix"]["observed_at_sync_ns"], 1_015)  # type: ignore[index]
        self.assertEqual(result["fix"]["observation_time_status"], "known_sync_time")  # type: ignore[index]
        gateway._simulator.call.assert_called_once_with("gnss_cycle", {"cycle": cycle(), "after_sample_id": 0})
        self.assertEqual(gateway._gnss_control_sequence, 1)

    def test_gateway_preserves_resync_status_on_oversized_resync_detail(self) -> None:
        response = {
            "cycle": cycle(),
            "session_id": "receiver-new",
            "status": "resync_required",
            "detail": "new receiver session",
            "simulator_now_ns": 0,
            "simulator_clock_id": "wrong_clock",
        }
        gateway = self._gateway(response)
        encoded_response_calls = 0

        def encode(message_type: str, payload: dict[str, object]) -> bytes:
            nonlocal encoded_response_calls
            if message_type == GNSS_CYCLE_RESPONSE:
                encoded_response_calls += 1
                if "detail" in payload:
                    return b"x" * (GNSS_PROVIDER_MAX_RESPONSE_BYTES + 1)
            return json.dumps(payload).encode("utf-8")

        gateway._encode_gnss_message.side_effect = encode
        context = SimpleNamespace(
            source_subsystem="component.localizer",
            timed=True,
            generated_at_sync_ns=900,
            valid_until_sync_ns=2_000,
            execute_at_sync_ns=None,
        )
        result = gateway._gnss_cycle(
            {"cycle": cycle(), "expected_session_id": "receiver-old", "after_sample_id": 1},
            context,
        )
        self.assertEqual(result["status"], "resync_required")
        self.assertNotIn("detail", result)
        self.assertEqual(encoded_response_calls, 2)

    def test_gateway_unknown_clock_mapping_delivers_no_fabricated_timestamp(self) -> None:
        unknown_fixture = replace(
            self.fixture,
            clock_status="unknown",
            source_clock=None,
            target_clock=None,
            clock_offset_ns=None,
            clock_alignment_tolerance_ns=None,
        )
        response = {
            "cycle": cycle(),
            "session_id": "receiver-a",
            "status": "sample_available",
            "simulator_now_ns": 1_015,
            "simulator_clock_id": "host_system",
            "sample": source_sample(),
        }
        gateway = self._gateway(response)
        gateway._gnss_fixture = unknown_fixture
        gateway._gnss_response_normalizer = GnssResponseNormalizer(unknown_fixture)
        context = SimpleNamespace(
            source_subsystem="component.localizer",
            timed=True,
            generated_at_sync_ns=900,
            valid_until_sync_ns=2_000,
            execute_at_sync_ns=None,
        )
        result = gateway._gnss_cycle({"cycle": cycle()}, context)
        self.assertEqual(result["status"], "sample_available")
        self.assertEqual(result["fix"]["observation_time_status"], "unknown")  # type: ignore[index]
        self.assertNotIn("observed_at_sync_ns", result["fix"])  # type: ignore[operator]

    def test_gateway_rejects_wrong_owner_and_expiry_before_commit(self) -> None:
        response = {
            "cycle": cycle(),
            "session_id": "receiver-a",
            "status": "sample_available",
            "simulator_now_ns": 1_015,
            "simulator_clock_id": "host_system",
            "sample": source_sample(),
        }
        context = SimpleNamespace(
            source_subsystem="component.localizer",
            timed=True,
            generated_at_sync_ns=900,
            valid_until_sync_ns=2_000,
            execute_at_sync_ns=None,
        )
        wrong_owner = self._gateway(response)
        wrong_owner.bound_control_owner.return_value = "another.component"
        with self.assertRaisesRegex(ValueError, "bound to another.component"):
            wrong_owner._gnss_cycle({"cycle": cycle()}, context)
        wrong_owner._simulator.call.assert_not_called()

        expired = self._gateway(response, times=[1_000, 1_010, 1_020, 2_000])
        with self.assertRaisesRegex(ValueError, "expired before the sample response commit"):
            expired._gnss_cycle({"cycle": cycle()}, context)
        self.assertIsNone(expired._gnss_control_owner)
        self.assertEqual(expired._gnss_control_sequence, 0)

    def test_gateway_rejects_expired_request_before_contacting_simulator(self) -> None:
        gateway = self._gateway(
            {
                "cycle": cycle(),
                "session_id": "receiver-a",
                "status": "no_new_sample",
                "simulator_now_ns": 1_000,
                "simulator_clock_id": "host_system",
            },
            times=[2_000],
        )
        context = SimpleNamespace(
            source_subsystem="component.localizer",
            timed=True,
            generated_at_sync_ns=900,
            valid_until_sync_ns=2_000,
            execute_at_sync_ns=None,
        )
        with self.assertRaisesRegex(ValueError, "GNSS Control request is expired"):
            gateway._gnss_cycle({"cycle": cycle()}, context)
        gateway._simulator.call.assert_not_called()

    def test_localizer_publishes_body_fix_once_per_sample_and_odometry_yaw(self) -> None:
        tracker = GnssSampleTracker(expected_frame_id=self.mount.frame_id)
        tracker.apply_response(
            tracker_response(sequence=1, fix=gnss_fix()),
            expected_cycle=cycle(),
            now_sync_ns=100,
            maximum_measurement_age_ns=self.fixture.maximum_measurement_age_ns,
            future_timestamp_tolerance_ns=self.fixture.future_timestamp_tolerance_ns,
        )
        component = LocalizerSubsystem.__new__(LocalizerSubsystem)
        component._fixture = self.fixture
        component._mount = self.mount
        component._lock = threading.RLock()
        component._tracker = tracker
        component._odometry = localizer_odometry()
        component._position_fix = None
        component._position_covariance = None
        component._map_odom = None
        component._map_odom_height_m = None
        component._aligned_sample_identity = None
        component.publish_event = Mock()
        component.set_frame_transform = Mock()
        component.now_ns = Mock(return_value=110)
        component._logger = Mock()

        component._align_latest_measurement_locked()
        first_map_odom = component._map_odom
        component._align_latest_measurement_locked()
        self.assertEqual(component._map_odom, first_map_odom)
        position_fix = component._position_fix
        self.assertEqual(position_fix["frame_id"], "map")  # type: ignore[index]
        self.assertEqual(position_fix["child_frame_id"], "cleaner_base")  # type: ignore[index]
        fix_yaw = 2.0 * math.atan2(
            position_fix["pose"]["orientation"]["z"],  # type: ignore[index]
            position_fix["pose"]["orientation"]["w"],  # type: ignore[index]
        )
        self.assertAlmostEqual(fix_yaw, math.pi / 2, places=12)
        self.assertNotIn("truth_heading", position_fix)  # type: ignore[operator]

        component._publish_estimate_locked(120)
        component._odometry = localizer_odometry(x=2.0, y=2.5, z=0.1, yaw=math.pi / 2 + 0.2)
        component._publish_estimate_locked(130)
        localizer_manifest = yaml.safe_load(
            (
                TUTORIALS
                / "subsystems"
                / "component.localizer"
                / "subsystem.yaml"
            ).read_text(encoding="utf-8")
        )
        localizer_ports = localizer_manifest["spatial"]["ports"]
        mobile_manifest = yaml.safe_load(
            (
                TUTORIALS
                / "subsystems"
                / "component.mobile"
                / "subsystem.yaml"
            ).read_text(encoding="utf-8")
        )
        deployment_manifest = yaml.safe_load(
            (
                TUTORIALS
                / "deployments"
                / "stages"
                / "robot.deployment.yaml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(localizer_ports["reference"]["source"], {"requirement": "odometry", "port": "reference"})
        self.assertNotIn("odom", localizer_ports)
        self.assertEqual(
            deployment_manifest["bindings"]["component.localizer.odometry"]["to"],
            "component.mobile.mobile_base",
        )
        self.assertEqual(mobile_manifest["spatial"]["ports"]["reference"]["frame"], "odom")
        frame_transforms = [call.kwargs for call in component.set_frame_transform.call_args_list]
        self.assertEqual(len(frame_transforms), 2)
        for transform in frame_transforms:
            self.assertEqual(transform["parent_frame"], "map")
            self.assertEqual(transform["child_frame"], "reference")
            self.assertIn(transform["parent_frame"], localizer_ports)
            self.assertIn(transform["child_frame"], localizer_ports)
        estimates = [
            call.kwargs["value"]
            for call in component.publish_event.call_args_list
            if call.kwargs.get("topic") == "pose_estimate"
        ]
        self.assertEqual(len(estimates), 2)
        last_yaw = 2.0 * math.atan2(
            estimates[-1]["pose"]["orientation"]["z"], estimates[-1]["pose"]["orientation"]["w"]
        )
        self.assertAlmostEqual(last_yaw, math.pi / 2 + 0.2, places=12)
        expected_height = position_fix["pose"]["position"]["z"] + 0.1  # type: ignore[index]
        self.assertAlmostEqual(estimates[-1]["pose"]["position"]["z"], expected_height, places=12)
        self.assertEqual(component._map_odom, first_map_odom)

    def test_localizer_poller_requests_typed_latest_sample_exchange(self) -> None:
        component = LocalizerSubsystem.__new__(LocalizerSubsystem)
        component._fixture = self.fixture
        component._mount = self.mount
        component._lock = threading.RLock()
        component._poll_cycle_lock = threading.Lock()
        component._tracker = GnssSampleTracker(expected_frame_id=self.mount.frame_id)
        component._odometry = None
        component._position_fix = None
        component._position_covariance = None
        component._map_odom = None
        component._map_odom_height_m = None
        component._aligned_sample_identity = None
        component._poller_stop = threading.Event()
        component._runtime_binding_ready = True
        component._generation = 10
        component._sequence = 0
        component.now_ns = Mock(side_effect=[1_000, 1_020])
        component.transact_bound_control = Mock(
            return_value={
                "ok": True,
                "result": tracker_response(sequence=1, fix=gnss_fix(observed_at_sync_ns=1_015)),
            }
        )
        component.publish_event = Mock()
        component.set_frame_transform = Mock()
        component._logger = Mock()

        component._poll_once()

        component.transact_bound_control.assert_called_once_with(
            binding="gnss_source",
            topic="gnss_cycle",
            type="bytes",
            value={"cycle": cycle(), "after_sample_id": 0},
            period_ms=200.0,
            deadline_ms=1000.0,
            request_type=LOCALIZER_GNSS_CYCLE_REQUEST,
            response_type=LOCALIZER_GNSS_CYCLE_RESPONSE,
            timing={"generated_at_sync_ns": 1_000, "valid_for_ns": 1_000_000_000, "late_policy": "reject"},
        )
        self.assertEqual(component._tracker.status, "localized")

    def test_localizer_poller_keeps_live_thread_handle_after_join_timeout(self) -> None:
        component = LocalizerSubsystem.__new__(LocalizerSubsystem)
        component._poller_stop = threading.Event()
        running_poller = Mock()
        running_poller.is_alive.return_value = True
        component._poller_thread = running_poller

        with self.assertRaisesRegex(RuntimeError, "did not stop"):
            component._stop_gnss_poller()
        self.assertIs(component._poller_thread, running_poller)
        with self.assertRaisesRegex(RuntimeError, "previous GNSS poller"):
            component._start_gnss_poller()
        self.assertIs(component._poller_thread, running_poller)


def _control_cycle_object(value: dict[str, int | str]) -> Any:
    from r2kernel.core.common_control import ControlCycle

    return ControlCycle.from_mapping(value)


class ArenaServerCleanupTests(unittest.TestCase):
    def test_both_capture_threads_are_stopped_even_when_gnss_join_fails(self) -> None:
        server = ArenaServer.__new__(ArenaServer)
        server._stop_gnss_capture = Mock(side_effect=RuntimeError("GNSS thread remains alive"))
        server._stop_radar_capture = Mock()
        with self.assertRaisesRegex(RuntimeError, "GNSS thread remains alive"):
            server._stop_capture_threads()
        server._stop_gnss_capture.assert_called_once()
        server._stop_radar_capture.assert_called_once()

    def test_server_socket_closes_even_when_capture_cleanup_fails(self) -> None:
        server = ArenaServer.__new__(ArenaServer)
        server._stop_gnss_capture = Mock(side_effect=RuntimeError("GNSS thread remains alive"))
        server._stop_radar_capture = Mock()
        with patch("http.server.ThreadingHTTPServer.server_close") as base_close:
            with self.assertRaisesRegex(RuntimeError, "GNSS thread remains alive"):
                server.server_close()
        base_close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
