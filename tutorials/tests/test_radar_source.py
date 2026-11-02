from __future__ import annotations

import math
import json
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml


ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "subsystems").is_dir())
TUTORIALS = ROOT
for _relative in ("simulator", "lib", "subsystems/gateway.device_gateway"):
    _path = str(TUTORIALS / _relative)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from arena import ArenaSimulator  # noqa: E402
import device_gateway  # noqa: E402
from device_gateway import (  # noqa: E402
    RADAR_CYCLE_REQUEST,
    RADAR_CYCLE_RESPONSE,
    RADAR_CYCLE_TOPIC,
    DeviceGatewaySubsystem,
)
from radar_profile import validate_radar_cycle_response  # noqa: E402


class JsonMessageCodec:
    def encode_message(self, _message_type: str, payload: dict[str, object]) -> bytes:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


class FakeClock:
    def __init__(self) -> None:
        self.now_ns = 10_000_000_000

    def __call__(self) -> int:
        return self.now_ns

    def advance_ns(self, value: int) -> None:
        self.now_ns += value


def control_cycle(*, generation: int = 1, sequence: int = 1) -> dict[str, int | str]:
    return {
        "generation": generation,
        "sequence": sequence,
        "intent": "sample",
        "state_revision": 0,
    }


def gateway_context(
    *,
    source_subsystem: str = "component.perception",
    generated_at_sync_ns: int = 900,
    valid_until_sync_ns: int = 2_000,
) -> SimpleNamespace:
    return SimpleNamespace(
        source_subsystem=source_subsystem,
        timed=True,
        generated_at_sync_ns=generated_at_sync_ns,
        valid_until_sync_ns=valid_until_sync_ns,
        execute_at_sync_ns=None,
    )


def sector_sample(
    *,
    session_id: str = "session-a",
    sample_id: int = 1,
    returns: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "sample_id": sample_id,
        "revolution_id": 1,
        "azimuth_start_rad": 0.0,
        "azimuth_span_rad": 0.20943951,
        "scan_started_at_sync_ns": 90,
        "scan_ended_at_sync_ns": 100,
        "frame_id": "cleaner_radar",
        "revolution_complete": False,
        "returns": list(returns or []),
    }


def simulator_response(
    *,
    cycle: dict[str, int | str] | None = None,
    session_id: str = "session-a",
    status: str = "sample_available",
    sector: dict[str, object] | None = None,
    simulator_now_ns: int = 1_000,
) -> dict[str, object]:
    response: dict[str, object] = {
        "cycle": dict(cycle or control_cycle()),
        "session_id": session_id,
        "status": status,
        "simulator_now_ns": simulator_now_ns,
    }
    if sector is not None:
        response["sector"] = sector
    if status in {"resync_required", "device_error"}:
        response["detail"] = "simulator domain status"
    return response


def binding_authority(source: str, *, session: str = "session-a", revision: str = "binding-a") -> dict[str, object]:
    return {"source": source, "source_reference": f"{source}.radar_source", "peer": None,
            "source_session": session, "target_session": "gateway-session", "interface": "radar_source",
            "binding_revision": revision, "source_registration": "source-registration", "target_registration": "target-registration"}


def radar_gateway_probe(
    *,
    response: dict[str, object] | None = None,
    now_ns: int = 1_000,
) -> DeviceGatewaySubsystem:
    gateway = DeviceGatewaySubsystem.__new__(DeviceGatewaySubsystem)
    gateway._simulator = Mock()
    gateway._simulator.call.return_value = response or simulator_response(sector=sector_sample())
    gateway.now_ns = Mock(return_value=now_ns)
    gateway._radar_cycle_lock = threading.Lock()
    gateway._radar_control_owner = None
    gateway._radar_control_generation = None
    gateway._radar_control_sequence = 0
    gateway._radar_message_codec = JsonMessageCodec()
    gateway.bound_control_owner = Mock(return_value="component.perception")
    gateway.bound_control_authority = Mock(side_effect=lambda _interface: binding_authority(gateway.bound_control_owner(_interface)))
    return gateway


class RadarSourceSimulatorTest(unittest.TestCase):
    def test_radar_mount_is_loaded_from_the_compound_manifest(self) -> None:
        manifest_path = TUTORIALS / "subsystems" / "compound.cleaner" / "subsystem.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        mount = next(entry for entry in manifest["spatial"]["mounts"] if entry["name"] == "radar_on_body")
        translation = mount["transform"]["translation"]
        quaternion_x, quaternion_y, quaternion_z, quaternion_w = mount["transform"]["rotation_xyzw"]
        manifest_yaw = math.atan2(
            2.0 * (quaternion_w * quaternion_z + quaternion_x * quaternion_y),
            1.0 - 2.0 * (quaternion_y * quaternion_y + quaternion_z * quaternion_z),
        )

        arena = ArenaSimulator(now_ns=FakeClock())

        self.assertEqual(translation, [0.08, 0.03, 0.0])
        self.assertAlmostEqual(manifest_yaw, math.radians(15.0), places=12)
        self.assertAlmostEqual(arena._radar_mount.translation_x_m, translation[0], places=12)
        self.assertAlmostEqual(arena._radar_mount.translation_y_m, translation[1], places=12)
        self.assertAlmostEqual(arena._radar_mount.yaw_rad, manifest_yaw, places=12)
        inactive_scan = arena.start_snapshot()["scan"]
        self.assertEqual(inactive_scan["mode"], "none")
        self.assertAlmostEqual(inactive_scan["sensor_pose"]["x"], 0.26, places=9)
        self.assertAlmostEqual(inactive_scan["sensor_pose"]["y"], 0.53, places=9)
        self.assertAlmostEqual(inactive_scan["sensor_pose"]["heading"], manifest_yaw, places=9)

    def test_stationary_radar_returns_use_manifest_offset_and_sensor_frame(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)
        arena.reset({"count": 0})
        sensor_x, sensor_y, sensor_heading = arena._radar_pose_locked()
        target_sensor_bearing_rad = math.radians(5.0)
        target_world_bearing_rad = sensor_heading + target_sensor_bearing_rad
        target_range_m = 0.20
        arena._trash = [
            {
                "id": "private-mounted-target",
                "x": sensor_x + target_range_m * math.cos(target_world_bearing_rad),
                "y": sensor_y + target_range_m * math.sin(target_world_bearing_rad),
                "state": "loose",
            }
        ]

        clock.advance_ns(100_000_000)
        self.assertEqual(arena.capture_radar_sectors(), 1)
        sector = arena.read_radar_cycle({"cycle": control_cycle()})["sector"]
        radar_return = sector["returns"][0]
        body_center_range_m = math.hypot(
            float(arena._trash[0]["x"]) - float(arena._robot["x"]),
            float(arena._trash[0]["y"]) - float(arena._robot["y"]),
        )
        viewer = arena.scan({"mode": "radar", "max_range": 0.30, "fov_deg": 30.0})["state"]["scan"]

        self.assertAlmostEqual(radar_return["range_m"], target_range_m, places=6)
        self.assertAlmostEqual(radar_return["azimuth_rad"], target_sensor_bearing_rad, places=7)
        self.assertGreater(body_center_range_m, radar_return["range_m"] + 0.05)
        self.assertEqual(viewer["mode"], "radar")
        self.assertAlmostEqual(viewer["beam_detections"][0]["distance"], target_range_m, places=4)
        self.assertAlmostEqual(viewer["beam_detections"][0]["bearing_deg"], 5.0, places=2)
        self.assertAlmostEqual(viewer["sensor_pose"]["x"], 0.26, places=9)
        self.assertAlmostEqual(viewer["sensor_pose"]["y"], 0.53, places=9)
        self.assertAlmostEqual(viewer["sensor_pose"]["heading"], math.radians(15.0), places=9)

    def test_rotating_radar_radial_velocity_includes_mount_lever_arm(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)
        arena.reset({"count": 0})
        initial_time_ns = clock.now_ns
        arena._radar_epoch_ns = initial_time_ns - 750_000_000
        arena._drive_until_ns = initial_time_ns + 1_000_000_000
        arena._robot["wz"] = 1.0
        arena._robot["target_wz"] = 1.0

        clock.advance_ns(100_000_000)
        arena._advance_motion_locked(now_ns=clock.now_ns)
        sensor_x, sensor_y, sensor_heading = arena._radar_pose_locked()
        target_sensor_bearing_rad = math.radians(96.0)
        target_world_bearing_rad = sensor_heading + target_sensor_bearing_rad
        target_range_m = 0.20
        arena._trash = [
            {
                "id": "private-turning-target",
                "x": sensor_x + target_range_m * math.cos(target_world_bearing_rad),
                "y": sensor_y + target_range_m * math.sin(target_world_bearing_rad),
                "state": "loose",
            }
        ]
        self.assertEqual(arena.capture_radar_sectors(), 1)
        sector = arena.read_radar_cycle({"cycle": control_cycle()})["sector"]

        body_heading = float(arena._robot["heading"])
        mount_x = arena._radar_mount.translation_x_m
        mount_y = arena._radar_mount.translation_y_m
        offset_x = math.cos(body_heading) * mount_x - math.sin(body_heading) * mount_y
        offset_y = math.sin(body_heading) * mount_x + math.cos(body_heading) * mount_y
        angular_velocity_rad_per_second = float(arena._robot["wz"]) * math.radians(150.0)
        expected_radial_velocity_mps = -(
            -angular_velocity_rad_per_second * offset_y * math.cos(target_world_bearing_rad)
            + angular_velocity_rad_per_second * offset_x * math.sin(target_world_bearing_rad)
        )

        self.assertAlmostEqual(sector["azimuth_start_rad"], math.radians(90.0), places=8)
        self.assertAlmostEqual(sector["returns"][0]["azimuth_rad"], target_sensor_bearing_rad, places=7)
        self.assertAlmostEqual(sector["returns"][0]["radial_velocity_mps"], expected_radial_velocity_mps, places=6)
        self.assertGreater(abs(sector["returns"][0]["radial_velocity_mps"]), 0.15)

    def test_sector_timing_and_robot_facing_data_exclude_truth_identifiers(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)
        arena.reset({"count": 0})
        sensor_x, sensor_y, sensor_heading = arena._radar_pose_locked()
        angle_rad = sensor_heading + math.radians(9.0)
        arena._trash = [
            {
                "id": "private-trash-1",
                "x": sensor_x + 0.2 * math.cos(angle_rad),
                "y": sensor_y + 0.2 * math.sin(angle_rad),
                "state": "loose",
            }
        ]

        clock.advance_ns(100_000_000)
        self.assertEqual(arena.capture_radar_sectors(), 1)
        response = arena.read_radar_cycle({"cycle": control_cycle(), "after_sample_id": 0})

        self.assertEqual(response["status"], "sample_available")
        sector = response["sector"]
        self.assertEqual(sector["sample_id"], 1)
        self.assertEqual(sector["revolution_id"], 1)
        self.assertEqual(sector["frame_id"], "cleaner_radar")
        self.assertAlmostEqual(sector["azimuth_span_rad"], math.radians(12), places=8)
        self.assertEqual(sector["scan_ended_at_sync_ns"] - sector["scan_started_at_sync_ns"], 100_000_000)
        self.assertEqual(len(sector["returns"]), 1)
        radar_return = sector["returns"][0]
        self.assertEqual(
            set(radar_return),
            {"range_m", "azimuth_rad", "radial_velocity_mps", "signal_strength", "observed_at_sync_ns"},
        )
        self.assertGreater(radar_return["range_m"], 0.0)
        self.assertGreaterEqual(radar_return["observed_at_sync_ns"], sector["scan_started_at_sync_ns"])
        self.assertLessEqual(radar_return["observed_at_sync_ns"], sector["scan_ended_at_sync_ns"])
        self.assertNotIn("id", sector)
        self.assertNotIn("x", radar_return)
        self.assertNotIn("y", radar_return)
        viewer_scan = arena.get_state()["scan"]
        self.assertEqual(viewer_scan["mode"], "radar")
        self.assertEqual(viewer_scan["beam_detections"][0]["id"], "private-trash-1")

    def test_empty_sector_is_valid_coverage_and_updates_only_viewer_scan_state(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)
        arena.reset({"count": 0})
        clock.advance_ns(100_000_000)

        arena.capture_radar_sectors()
        response = arena.read_radar_cycle({"cycle": control_cycle()})

        self.assertEqual(response["status"], "sample_available")
        self.assertEqual(response["sector"]["returns"], [])
        self.assertEqual(arena.get_state()["scan"]["mode"], "radar")

    def test_world_update_before_capture_does_not_force_session_reset(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)
        arena.reset({"count": 0})
        session_id = arena._radar_session_id
        clock.advance_ns(100_000_000)
        arena.get_state()

        self.assertEqual(arena.capture_radar_sectors(), 1)
        response = arena.read_radar_cycle({"cycle": control_cycle()})
        self.assertEqual(response["session_id"], session_id)
        self.assertEqual(response["sector"]["scan_ended_at_sync_ns"], clock.now_ns)

    def test_wraparound_and_revolution_completion(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)
        arena.reset({"count": 0})
        sensor_x, sensor_y, sensor_heading = arena._radar_pose_locked()
        target_angle_rad = math.radians(354.0)
        target_world_bearing_rad = sensor_heading + target_angle_rad
        arena._trash = [
            {
                "id": "private-wrap-target",
                "x": sensor_x + 0.2 * math.cos(target_world_bearing_rad),
                "y": sensor_y + 0.2 * math.sin(target_world_bearing_rad),
                "state": "loose",
            }
        ]
        for _ in range(29):
            clock.advance_ns(100_000_000)
            arena.capture_radar_sectors()
        clock.advance_ns(100_000_000)
        arena.capture_radar_sectors()

        response = arena.read_radar_cycle(
            {"cycle": control_cycle(), "expected_session_id": arena._radar_session_id, "after_sample_id": 29}
        )
        sector = response["sector"]
        self.assertEqual(response["status"], "sample_available")
        self.assertGreater(sector["azimuth_start_rad"], math.radians(340))
        self.assertTrue(sector["revolution_complete"])
        self.assertEqual(sector["revolution_id"], 1)
        self.assertEqual(len(sector["returns"]), 1)
        self.assertAlmostEqual(sector["returns"][0]["azimuth_rad"], target_angle_rad, places=5)

    def test_history_eviction_requires_resync_and_bootstrap_starts_at_latest_sample(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock, radar_history_capacity=2)
        arena.reset({"count": 0})
        session_id = arena._radar_session_id
        for _ in range(4):
            clock.advance_ns(100_000_000)
            arena.capture_radar_sectors()

        resync = arena.read_radar_cycle(
            {"cycle": control_cycle(), "expected_session_id": arena._radar_session_id, "after_sample_id": 0}
        )
        next_retained = arena.read_radar_cycle(
            {"cycle": control_cycle(sequence=2), "expected_session_id": arena._radar_session_id, "after_sample_id": 2}
        )
        bootstrap = arena.read_radar_cycle({"cycle": control_cycle(sequence=2)})

        self.assertEqual(resync["status"], "resync_required")
        self.assertEqual(next_retained["status"], "sample_available")
        self.assertEqual(next_retained["sector"]["sample_id"], 3)
        self.assertEqual(bootstrap["status"], "sample_available")
        self.assertEqual(bootstrap["sector"]["sample_id"], 4)
        self.assertEqual(bootstrap["session_id"], session_id)

    def test_reset_changes_session_and_restarts_sample_sequence(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)
        arena.reset({"count": 0})
        old_session_id = arena._radar_session_id
        clock.advance_ns(100_000_000)
        arena.capture_radar_sectors()
        arena.reset({"count": 0})

        resync = arena.read_radar_cycle(
            {"cycle": control_cycle(), "expected_session_id": old_session_id, "after_sample_id": 1}
        )
        self.assertEqual(resync["status"], "resync_required")
        self.assertNotEqual(resync["session_id"], old_session_id)
        self.assertIsNone(resync.get("sector"))
        clock.advance_ns(100_000_000)
        arena.capture_radar_sectors()
        restarted = arena.read_radar_cycle({"cycle": control_cycle(sequence=2)})
        self.assertEqual(restarted["sector"]["sample_id"], 1)

    def test_acquisition_gap_resets_session_without_backfilled_samples(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)
        arena.reset({"count": 0})
        previous_session_id = arena._radar_session_id
        clock.advance_ns(100_000_000)
        self.assertEqual(arena.capture_radar_sectors(), 1)
        clock.advance_ns(600_000_000)

        self.assertEqual(arena.capture_radar_sectors(), 0)
        self.assertNotEqual(arena._radar_session_id, previous_session_id)
        self.assertEqual(len(arena._radar_sector_history), 0)
        clock.advance_ns(100_000_000)
        self.assertEqual(arena.capture_radar_sectors(), 1)
        self.assertEqual(arena._radar_sector_history[0]["sample_id"], 1)

    def test_scheduler_jitter_bridges_coverage_and_completes_revolution_in_one_session(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)
        arena.reset({"count": 0})
        session_id = arena._radar_session_id
        jitter_sector: dict[str, object] | None = None

        for sample_index in range(30):
            clock.advance_ns(100_000_000)
            if sample_index == 8:
                clock.advance_ns(250_000_000)
            self.assertEqual(arena.capture_radar_sectors(), 1)
            newest_sector = arena._radar_sector_history[-1]
            if newest_sector["scan_ended_at_sync_ns"] - newest_sector["scan_started_at_sync_ns"] > 200_000_000:
                jitter_sector = newest_sector

        self.assertEqual(arena._radar_session_id, session_id)
        self.assertEqual(arena._radar_sample_id, 30)
        self.assertIsNotNone(jitter_sector)
        self.assertAlmostEqual(
            jitter_sector["azimuth_span_rad"],
            math.radians(42.0),
            places=8,
        )
        self.assertGreaterEqual(
            sum(float(sector["azimuth_span_rad"]) for sector in arena._radar_sector_history),
            math.tau,
        )
        self.assertTrue(any(sector["revolution_complete"] for sector in arena._radar_sector_history))

    def test_malformed_radar_requests_fail_explicitly(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)
        invalid_requests = (
            None,
            {},
            {"cycle": {"generation": 1, "sequence": 1, "intent": "sample", "state_revision": 0}, "unknown": True},
            {"cycle": {"generation": 1, "sequence": 0, "intent": "sample", "state_revision": 0}},
            {"cycle": {"generation": 1, "sequence": 1, "intent": "command", "state_revision": 0}},
            {"cycle": {"generation": 1, "sequence": 1, "intent": "sample", "state_revision": 0}, "after_sample_id": True},
        )
        for request in invalid_requests:
            with self.subTest(request=request):
                with self.assertRaises(ValueError):
                    arena.read_radar_cycle(request)

    def test_session_mismatch_resync_takes_precedence_over_capture_fault(self) -> None:
        arena = ArenaSimulator(now_ns=FakeClock())
        old_session_id = "old-source-session"
        arena.record_radar_capture_error("capture clock failed")

        response = arena.read_radar_cycle(
            {
                "cycle": control_cycle(),
                "expected_session_id": old_session_id,
                "after_sample_id": 10,
            }
        )

        self.assertEqual(response["status"], "resync_required")
        self.assertNotEqual(response["session_id"], old_session_id)
        self.assertNotIn("sector", response)


class RadarSourceGatewayTest(unittest.TestCase):
    def test_registers_typed_radar_cycle_and_does_not_expose_debug_scans(self) -> None:
        gateway = DeviceGatewaySubsystem.__new__(DeviceGatewaySubsystem)
        gateway.register_control_endpoint = Mock()
        gateway._logger = Mock()

        gateway.on_initialize()

        registered_topics = [call.args[0] for call in gateway.register_control_endpoint.call_args_list]
        self.assertIn(RADAR_CYCLE_TOPIC, registered_topics)
        self.assertNotIn("scan", registered_topics)
        self.assertNotIn("front", registered_topics)
        self.assertNotIn("nearest", registered_topics)
        radar_call = next(call for call in gateway.register_control_endpoint.call_args_list if call.args[0] == RADAR_CYCLE_TOPIC)
        self.assertEqual(radar_call.kwargs["request_type"], RADAR_CYCLE_REQUEST)
        self.assertEqual(radar_call.kwargs["response_type"], RADAR_CYCLE_RESPONSE)
        self.assertEqual(radar_call.kwargs["timing_support"], "required")

    def test_preserves_sample_times_and_robot_facing_payload_shape(self) -> None:
        gateway = radar_gateway_probe(response=simulator_response(sector=sector_sample()))

        response = gateway._radar_cycle(
            {"cycle": control_cycle(), "after_sample_id": 0},
            gateway_context(),
        )

        self.assertEqual(response["status"], "sample_available")
        self.assertEqual(response["sector"]["scan_started_at_sync_ns"], 90)
        self.assertEqual(response["sector"]["scan_ended_at_sync_ns"], 100)
        self.assertEqual(response["cycle"], control_cycle())
        self.assertNotIn("simulator_now_ns", response)
        self.assertEqual(response["sector"]["returns"], [])
        gateway._simulator.call.assert_called_once_with(
            "radar_cycle",
            {"cycle": control_cycle(), "after_sample_id": 0},
        )

    def test_generated_edge_return_passes_the_public_radar_profile_validator(self) -> None:
        clock = FakeClock()
        arena = ArenaSimulator(now_ns=clock)
        arena.reset({"count": 0})
        sensor_x, sensor_y, sensor_heading = arena._radar_pose_locked()
        target_angle_rad = math.radians(12.0)
        target_world_bearing_rad = sensor_heading + target_angle_rad
        arena._trash = [
            {
                "id": "private-edge-target",
                "x": sensor_x + 0.2 * math.cos(target_world_bearing_rad),
                "y": sensor_y + 0.2 * math.sin(target_world_bearing_rad),
                "state": "loose",
            }
        ]
        clock.advance_ns(100_000_000)
        arena.capture_radar_sectors()
        request = {
            "cycle": control_cycle(),
            "expected_session_id": arena._radar_session_id,
            "after_sample_id": 0,
        }
        simulator_response_value = arena.read_radar_cycle(request)
        gateway = radar_gateway_probe(response=simulator_response_value, now_ns=clock.now_ns)

        with patch.object(
            device_gateway,
            "validate_radar_cycle_response",
            wraps=validate_radar_cycle_response,
        ) as profile_validator:
            response = gateway._radar_cycle(
                request,
                gateway_context(
                    generated_at_sync_ns=clock.now_ns - 100,
                    valid_until_sync_ns=clock.now_ns + 1_000,
                ),
            )
            profile_validator.assert_called_once()

        self.assertEqual(response["status"], "sample_available")
        self.assertEqual(len(response["sector"]["returns"]), 1)
        self.assertLessEqual(
            response["sector"]["returns"][0]["azimuth_rad"],
            response["sector"]["azimuth_span_rad"],
        )

        near_edge_response = json.loads(json.dumps(simulator_response_value))
        near_edge_response["sector"]["returns"][0]["azimuth_rad"] = (
            near_edge_response["sector"]["azimuth_span_rad"] + 0.8e-6
        )
        tolerant_gateway = radar_gateway_probe(response=near_edge_response, now_ns=clock.now_ns)
        with patch.object(
            device_gateway,
            "validate_radar_cycle_response",
            wraps=validate_radar_cycle_response,
        ) as profile_validator:
            tolerant_result = tolerant_gateway._radar_cycle(
                request,
                gateway_context(
                    generated_at_sync_ns=clock.now_ns - 100,
                    valid_until_sync_ns=clock.now_ns + 1_000,
                ),
            )
            profile_validator.assert_called_once()
        self.assertEqual(tolerant_result["status"], "sample_available")

    def test_enforces_sequence_owner_and_generation(self) -> None:
        gateway = radar_gateway_probe()
        first = {"cycle": control_cycle(generation=2, sequence=1)}
        gateway._simulator.call.return_value = simulator_response(
            cycle=control_cycle(generation=2, sequence=1),
            sector=sector_sample(),
        )
        gateway._radar_cycle(first, gateway_context())

        with self.assertRaisesRegex(ValueError, "strictly increase"):
            gateway._radar_cycle(first, gateway_context())
        with self.assertRaisesRegex(ValueError, "stale"):
            gateway._radar_cycle(
                {"cycle": control_cycle(generation=1, sequence=3)},
                gateway_context(),
            )
        with self.assertRaisesRegex(ValueError, "bound to component.perception"):
            gateway._radar_cycle(
                {"cycle": control_cycle(generation=2, sequence=2)},
                gateway_context(source_subsystem="component.other"),
            )
        self.assertEqual(gateway._simulator.call.call_count, 1)

    def test_same_source_can_advance_generation_but_an_unbound_source_cannot(self) -> None:
        gateway = radar_gateway_probe()
        gateway._radar_cycle({"cycle": control_cycle()}, gateway_context())
        gateway._simulator.call.return_value = simulator_response(
            cycle=control_cycle(generation=2, sequence=1),
            sector=sector_sample(),
        )

        gateway._radar_cycle(
            {"cycle": control_cycle(generation=2, sequence=1)},
            gateway_context(source_subsystem="component.perception"),
        )
        self.assertEqual(gateway._radar_control_owner, tuple(binding_authority("component.perception").items()))
        self.assertEqual(gateway._radar_control_generation, 2)

        with self.assertRaisesRegex(ValueError, "bound to component.perception"):
            gateway._radar_cycle(
                {"cycle": control_cycle(generation=3, sequence=1)},
                gateway_context(source_subsystem="component.replacement"),
            )
        self.assertEqual(gateway._simulator.call.call_count, 2)

    def test_first_request_cannot_claim_a_port_bound_to_another_component(self) -> None:
        gateway = radar_gateway_probe()

        with self.assertRaisesRegex(ValueError, "bound to component.perception"):
            gateway._radar_cycle(
                {"cycle": control_cycle()},
                gateway_context(source_subsystem="component.other"),
            )

        self.assertIsNone(gateway._radar_control_owner)
        gateway._simulator.call.assert_not_called()

    def test_explicit_runtime_rebinding_replaces_the_previous_owner(self) -> None:
        gateway = radar_gateway_probe()
        gateway._radar_cycle({"cycle": control_cycle()}, gateway_context())
        gateway.bound_control_owner.return_value = "component.replacement"
        gateway._simulator.call.return_value = simulator_response(
            cycle=control_cycle(generation=2, sequence=1),
            sector=sector_sample(),
        )

        gateway._radar_cycle(
            {"cycle": control_cycle(generation=2, sequence=1)},
            gateway_context(source_subsystem="component.replacement"),
        )

        self.assertEqual(gateway._radar_control_owner, tuple(binding_authority("component.replacement").items()))
        self.assertEqual(gateway._radar_control_generation, 2)
        with self.assertRaisesRegex(ValueError, "bound to component.replacement"):
            gateway._radar_cycle(
                {"cycle": control_cycle(generation=3, sequence=1)},
                gateway_context(),
            )
        self.assertEqual(gateway._simulator.call.call_count, 2)

    def test_binding_change_during_sample_rejects_response_commit(self) -> None:
        gateway = radar_gateway_probe()
        gateway.bound_control_owner.side_effect = ["component.perception", "component.replacement"]

        with self.assertRaisesRegex(ValueError, "binding changed before"):
            gateway._radar_cycle({"cycle": control_cycle()}, gateway_context())

        self.assertIsNone(gateway._radar_control_owner)

    def test_same_name_source_registration_change_during_sample_rejects_commit(self) -> None:
        gateway = radar_gateway_probe()
        first = binding_authority("component.perception", session="session-a")
        replacement = binding_authority("component.perception", session="session-b")
        gateway.bound_control_authority.side_effect = [first, replacement]
        with self.assertRaisesRegex(ValueError, "binding changed before"):
            gateway._radar_cycle({"cycle":control_cycle()},gateway_context())
        self.assertIsNone(gateway._radar_control_owner)

    def test_missing_runtime_binding_fails_before_device_access(self) -> None:
        gateway = radar_gateway_probe()
        gateway.bound_control_owner.side_effect = RuntimeError("no active radar_source binding")

        with self.assertRaisesRegex(RuntimeError, "no active radar_source binding"):
            gateway._radar_cycle({"cycle": control_cycle()}, gateway_context())

        gateway._simulator.call.assert_not_called()

    def test_expiry_is_checked_before_and_at_response_commit(self) -> None:
        gateway = radar_gateway_probe()
        with self.assertRaisesRegex(ValueError, "expired"):
            gateway._radar_cycle(
                {"cycle": control_cycle()},
                gateway_context(valid_until_sync_ns=1_000),
            )
        gateway._simulator.call.assert_not_called()

        gateway = radar_gateway_probe()
        gateway.now_ns = Mock(side_effect=[1_000, 1_000, 1_001, 1_001])
        with self.assertRaisesRegex(ValueError, "expired before"):
            gateway._radar_cycle(
                {"cycle": control_cycle()},
                gateway_context(valid_until_sync_ns=1_001),
            )
        self.assertIsNone(gateway._radar_control_owner)

    def test_clock_misalignment_is_a_domain_error_and_does_not_relabel_data(self) -> None:
        gateway = radar_gateway_probe(response=simulator_response(sector=sector_sample(), simulator_now_ns=1_000_000_000))

        response = gateway._radar_cycle({"cycle": control_cycle()}, gateway_context())

        self.assertEqual(response["status"], "device_error")
        self.assertEqual(response["session_id"], "session-a")
        self.assertNotIn("sector", response)
        self.assertIn("not aligned", response["detail"])

    def test_rejects_malformed_simulator_response_and_private_truth_fields(self) -> None:
        malformed_responses = (
            simulator_response(sector=sector_sample()) | {"cycle": control_cycle(sequence=2)},
            simulator_response(sector=sector_sample() | {"x": 0.3}),
            simulator_response(
                sector=sector_sample(returns=[{
                    "range_m": 0.2,
                    "azimuth_rad": 0.1,
                    "radial_velocity_mps": 0.0,
                    "signal_strength": 0.2,
                    "observed_at_sync_ns": 95,
                    "id": "trash-1",
                }])
            ),
            simulator_response(
                sector=sector_sample(returns=[{
                    "range_m": 0.0,
                    "azimuth_rad": 0.1,
                    "radial_velocity_mps": 0.0,
                    "signal_strength": 0.2,
                    "observed_at_sync_ns": 95,
                }])
            ),
            simulator_response(
                sector=sector_sample(returns=[{
                    "range_m": 0.2,
                    "azimuth_rad": 0.1,
                    "radial_velocity_mps": 0.0,
                    "signal_strength": 1.1,
                    "observed_at_sync_ns": 95,
                }])
            ),
            simulator_response() | {"unexpected": True},
        )
        for response in malformed_responses:
            with self.subTest(response=response):
                gateway = radar_gateway_probe(response=response)
                with self.assertRaises(RuntimeError):
                    gateway._radar_cycle({"cycle": control_cycle()}, gateway_context())
                self.assertIsNone(gateway._radar_control_owner)

    def test_provider_return_limit_returns_device_error_without_truncation(self) -> None:
        many_returns = [
            {
                "range_m": 0.2,
                "azimuth_rad": 0.1,
                "radial_velocity_mps": 0.0,
                "signal_strength": 0.5,
                "observed_at_sync_ns": 95,
            }
            for _ in range(65)
        ]
        gateway = radar_gateway_probe(
            response=simulator_response(sector=sector_sample(returns=many_returns))
        )

        response = gateway._radar_cycle({"cycle": control_cycle()}, gateway_context())

        self.assertEqual(response["status"], "device_error")
        self.assertNotIn("sector", response)
        self.assertIn("return count limit", response["detail"])

    def test_provider_request_byte_limit_rejects_oversized_request(self) -> None:
        gateway = radar_gateway_probe()
        request = {
            "cycle": control_cycle(),
            "expected_session_id": "x" * 5_000,
        }

        with self.assertRaisesRegex(ValueError, "request byte limit"):
            gateway._radar_cycle(request, gateway_context())
        gateway._simulator.call.assert_not_called()

    def test_provider_response_byte_limit_returns_explicit_device_error(self) -> None:
        gateway = radar_gateway_probe(
            response=simulator_response(
                status="device_error",
                sector=None,
                simulator_now_ns=1_000,
            ) | {"detail": "x" * 70_000}
        )

        response = gateway._radar_cycle({"cycle": control_cycle()}, gateway_context())

        self.assertEqual(response["status"], "device_error")
        self.assertEqual(response["detail"], "radar response exceeds the provider response byte limit")
        self.assertLessEqual(
            len(gateway._radar_message_codec.encode_message(RADAR_CYCLE_RESPONSE, response)),
            65_536,
        )

    def test_rejects_missing_transport_owner_and_malformed_request(self) -> None:
        gateway = radar_gateway_probe()
        with self.assertRaisesRegex(ValueError, "transport source identity"):
            gateway._radar_cycle({"cycle": control_cycle()}, gateway_context(source_subsystem=""))
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            gateway._radar_cycle({"cycle": control_cycle(), "truth_id": "trash-1"}, gateway_context())
        gateway._simulator.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
