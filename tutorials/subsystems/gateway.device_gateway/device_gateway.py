from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _tutorial_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "subsystems").is_dir() and (parent / "simulator").is_dir():
            return parent
    candidates = [
        os.environ.get("R2KERNEL_TUTORIAL_ROOT"),
        str(Path(os.environ.get("R2KERNEL_HOME", "/opt/r2kernel")) / "tutorials"),
        "/opt/r2kernel/tutorials",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        root = Path(candidate)
        if (root / "subsystems").is_dir() and (root / "simulator").is_dir():
            return root
    raise RuntimeError(
        "could not locate R2Kernel tutorial runtime; set R2KERNEL_TUTORIAL_ROOT or install tutorials under /opt/r2kernel/tutorials"
    )


_ROOT = _tutorial_root()
_SUPPORT_PY = _ROOT / "support" / "python"
_LIB = _ROOT / "lib"
for _path in (_SUPPORT_PY, _LIB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from control_cycles import ControlCycleOrdering, control_source_identity, parse_control_cycle  # noqa: E402
from gnss_fixture import GnssFixtureConfiguration, load_gnss_fixture  # noqa: E402
from gnss_provider import GnssResponseNormalizer  # noqa: E402
from runtime import (  # noqa: E402
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
    run_subsystem,
    wait_for_control_ready,
)
from servo_simulator import (  # noqa: E402
    actuator_state_from_simulator,
    servo_control_response,
    servo_target_to_simulator_command,
)
from standard_types import (  # noqa: E402
    BOOL,
    INDEXED_BOOL,
    SERVO_CONTROL_REQUEST,
    SERVO_CONTROL_RESPONSE,
    TIMED_OUTPUT_CONTROL_REQUEST,
    TIMED_OUTPUT_CONTROL_RESPONSE,
    UINT32,
)
from timed_output import (  # noqa: E402
    TimedOutputHold,
    command_check as timed_output_command_check,
    keep_check as timed_output_keep_check,
    timed_output_response,
)
from r2kernel.core.common_control import ControlCycle, ControlContractError  # noqa: E402
from gnss_profile import validate_gnss_cycle_response  # noqa: E402
from radar_profile import validate_radar_cycle_response  # noqa: E402

try:
    from r2kernel.wrapper.subsystem import GatewaySubsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


SERVO_CONTROL_TOPIC = "servo_control"
DIGITAL_OUTPUT_CONTROL_TOPIC = "digital_output_control"
RADAR_CYCLE_TOPIC = "radar_cycle"
RADAR_CYCLE_REQUEST = "msg.sensor.radar.v1/RadarCycleRequest"
RADAR_CYCLE_RESPONSE = "msg.sensor.radar.v1/RadarCycleResponse"
GNSS_CYCLE_TOPIC = "gnss_cycle"
GNSS_CYCLE_REQUEST = "msg.sensor.gnss.v1/GnssCycleRequest"
GNSS_CYCLE_RESPONSE = "msg.sensor.gnss.v1/GnssCycleResponse"
RADAR_CLOCK_ALIGNMENT_TOLERANCE_NS = 5_000_000
GNSS_PROVIDER_MAX_REQUEST_BYTES = 4_096
GNSS_PROVIDER_MAX_RESPONSE_BYTES = 65_536
# Provider ceilings are enforced until binding-selected limits are available in
# the endpoint context; they do not imply that the binding profile was checked.
RADAR_PROVIDER_MAX_REQUEST_BYTES = 4_096
RADAR_PROVIDER_MAX_RETURN_COUNT = 64
RADAR_PROVIDER_MAX_RESPONSE_BYTES = 65_536
RADAR_RETURN_FIELDS = {
    "range_m",
    "azimuth_rad",
    "radial_velocity_mps",
    "signal_strength",
    "observed_at_sync_ns",
}
RADAR_SECTOR_FIELDS = {
    "session_id",
    "sample_id",
    "revolution_id",
    "azimuth_start_rad",
    "azimuth_span_rad",
    "scan_started_at_sync_ns",
    "scan_ended_at_sync_ns",
    "frame_id",
    "revolution_complete",
    "returns",
}


class SimulatorClient:
    def __init__(self, base_url: str, *, timeout_s: float = 1.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = max(0.1, timeout_s)

    def wait_until_ready(self, *, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                self.health()
                return
            except RuntimeError:
                time.sleep(0.1)
        raise RuntimeError(f"simulator API is not ready at {self._base_url}")

    def health(self) -> dict[str, Any]:
        return self._get("/health")

    def call(self, operation: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._post(f"/{operation}", params or {})

    def _get(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(f"{self._base_url}{path}", method="GET")
        return self._send(request)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        return self._send(request)

    def _send(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"simulator request failed: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("simulator response must be a JSON object")
        return value


class DeviceGatewaySubsystem(GatewaySubsystem):
    def __init__(
        self,
        *,
        name: str = "gateway.device_gateway",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        simulator_url: str = "http://127.0.0.1:8092",
        simulator_timeout_ms: int = 1000,
        gnss_fixture_path: str | None = None,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "device_gateway"),
            manifest_path=manifest_path or default_manifest_path(__file__, "device_gateway"),
        )
        self._simulator = SimulatorClient(simulator_url, timeout_s=simulator_timeout_ms / 1000.0)
        self._gnss_fixture: GnssFixtureConfiguration = load_gnss_fixture(
            Path(gnss_fixture_path)
            if gnss_fixture_path is not None
            else _ROOT / "config" / "gnss_fixture.yaml"
        )
        self._gnss_response_normalizer = GnssResponseNormalizer(self._gnss_fixture)
        self._digital_outputs: dict[int, bool] = {}
        self._digital_output_expiry_ns: dict[int, int] = {}
        self._digital_output_lock = threading.Lock()
        self._digital_output_ordering = ControlCycleOrdering()
        self._digital_output_hold: TimedOutputHold | None = None
        self._servo_lock = threading.Lock()
        self._servo_ordering = ControlCycleOrdering()
        self._digital_watchdog_stop = threading.Event()
        self._digital_watchdog_thread: threading.Thread | None = None
        self._radar_cycle_lock = threading.Lock()
        self._radar_control_owner: str | None = None
        self._radar_control_generation: int | None = None
        self._radar_control_sequence = 0
        self._radar_message_codec = None
        self._gnss_cycle_lock = threading.Lock()
        self._gnss_control_owner: str | None = None
        self._gnss_control_generation: int | None = None
        self._gnss_control_sequence = 0
        self._gnss_message_codec = None

    def on_initialize(self) -> bool:
        self.register_control_endpoint(
            SERVO_CONTROL_TOPIC,
            self._servo_control,
            request_type=SERVO_CONTROL_REQUEST,
            response_type=SERVO_CONTROL_RESPONSE,
            timing_support="required",
        )
        self.register_control_endpoint(
            DIGITAL_OUTPUT_CONTROL_TOPIC,
            self._digital_output_control,
            request_type=TIMED_OUTPUT_CONTROL_REQUEST,
            response_type=TIMED_OUTPUT_CONTROL_RESPONSE,
            timing_support="required",
        )
        self.register_control_endpoint(
            "digital_output_set",
            self._set_digital_output,
            request_type=INDEXED_BOOL,
            response_type=BOOL,
            timing_support="required",
        )
        self.register_control_endpoint(
            "digital_output_readback",
            self._get_digital_output,
            request_type=UINT32,
            response_type=BOOL,
            timing_support="required",
        )
        self.register_control_endpoint(
            RADAR_CYCLE_TOPIC,
            self._radar_cycle,
            request_type=RADAR_CYCLE_REQUEST,
            response_type=RADAR_CYCLE_RESPONSE,
            timing_support="required",
        )
        self.register_control_endpoint(
            GNSS_CYCLE_TOPIC,
            self._gnss_cycle,
            request_type=GNSS_CYCLE_REQUEST,
            response_type=GNSS_CYCLE_RESPONSE,
            timing_support="required",
        )
        self.logger.info("Lifecycle", "device gateway initialized with Control-only robot-facing endpoints")
        return True

    def on_start(self) -> bool:
        self._simulator.wait_until_ready()
        self.start_control_server()
        self._start_digital_output_watchdog()
        return True

    def on_terminate(self) -> bool:
        self._stop_digital_output_watchdog()
        self.stop_control_server()
        self.logger.info("Lifecycle", "device gateway terminated")
        return True

    def on_reset(self) -> bool:
        return self.on_terminate()

    def on_pause(self) -> bool:
        return self.on_terminate()

    def on_resume(self) -> bool:
        return self.on_start()

    def on_stop(self) -> bool:
        return self.on_terminate()

    def _servo_control(self, params: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
        if not isinstance(params, dict) or set(params) != {"cycle", "command"}:
            raise ValueError("servo_control request requires exactly cycle and command")
        cycle = parse_control_cycle(params.get("cycle"), label="servo")
        command = params.get("command")
        if not isinstance(command, dict):
            raise ValueError("servo_control command must be an ActuatorCommand object")
        source = control_source_identity(context, label="servo")
        emergency = command.get("emergency") is True
        if emergency:
            # A protective stop never applies the carried target.
            simulator_command: dict[str, Any] | None = {
                "axis": "base", "vx": 0.0, "vy": 0.0, "wz": 0.0, "dt": 0.02, "immediate_stop": True,
            }
        else:
            simulator_command = servo_target_to_simulator_command(
                command.get("target"),
                duration_s=self._control_validity_s(context),
                immediate_stop=False,
            )
        with self._servo_lock:
            if cycle.intent.value != "command" or cycle.state_revision <= 0:
                rejection: str | None = "servo ControlCycle requires command intent with a positive state_revision"
            else:
                rejection = self._servo_ordering.admit(source, cycle)
            if rejection is None and simulator_command is None:
                rejection = "servo target does not identify a supported simulator axis"
            if rejection is None and self._control_request_expired(context):
                rejection = "servo Control request expired before dispatch"
            if rejection is not None:
                return servo_control_response(cycle, "rejected", detail=rejection)
            assert simulator_command is not None
            try:
                result = self._dispatch_servo_command(simulator_command)
            except Exception as exc:
                return servo_control_response(cycle, "device_error", detail=f"simulator servo dispatch failed: {exc}")
            state = actuator_state_from_simulator(simulator_command, result, servo_on=not emergency)
            if not state["link_alive"]:
                return servo_control_response(cycle, "device_error", state=state, detail="simulator rejected the servo command")
            return servo_control_response(cycle, "applied", state=state)

    def _control_validity_s(self, context: Any) -> float:
        """Hold a servo command for its remaining Control validity, else one period."""
        valid_until_ns = getattr(context, "valid_until_sync_ns", None)
        if isinstance(valid_until_ns, int) and not isinstance(valid_until_ns, bool):
            return max(0.0, (valid_until_ns - self.now_ns()) / 1_000_000_000.0)
        period_ms = getattr(context, "period_ms", None)
        if isinstance(period_ms, (int, float)) and not isinstance(period_ms, bool) and period_ms > 0:
            return float(period_ms) / 1000.0
        return 0.08

    def _control_request_expired(self, context: Any) -> bool:
        valid_until_ns = getattr(context, "valid_until_sync_ns", None)
        if valid_until_ns is None:
            return False
        return int(valid_until_ns) <= self.now_ns()

    def _dispatch_servo_command(self, params: dict[str, Any]) -> dict[str, Any]:
        axis = str(params.get("axis") or params.get("joint") or "").strip()
        if axis in {"base", "mobile_base", "drive"}:
            return self._simulator.call("apply_drive", params)
        if axis in {"arm", "manipulator", "extension"}:
            return self._simulator.call("set_arm", params)
        if axis in {"gripper", "tool"}:
            return self._simulator.call("set_gripper", params)
        raise ValueError(f"unsupported servo axis: {axis or '<empty>'}")

    def _set_digital_output(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = params or {}
        index = int(params.get("index", params.get("channel", 0)) or 0)
        value = bool(params.get("value", params.get("state", False)))
        if index == 1:
            raise ValueError("digital output 1 is lease-controlled; use the digital_output_control Control")
        with self._digital_output_lock:
            self._digital_output_expiry_ns.pop(index, None)
            return self._apply_digital_output(index, value)

    def _digital_output_control(self, params: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
        if not isinstance(params, dict) or "cycle" not in params or set(params) - {"cycle", "output"}:
            raise ValueError("digital_output_control request requires cycle and an optional output only")
        cycle = parse_control_cycle(params.get("cycle"), label="digital output")
        source = control_source_identity(context, label="digital output")
        output = params.get("output")
        with self._digital_output_lock:
            hold = self._digital_output_hold
            channel = hold.channel_index if hold is not None else 0
            value = False
            if cycle.intent.value == "command":
                rejection, channel, value = timed_output_command_check(cycle, output, hold, source)
            elif cycle.intent.value == "keep":
                rejection = timed_output_keep_check(
                    cycle,
                    output,
                    hold,
                    source,
                    lease_expiry_ns=self._digital_output_expiry_ns.get(channel),
                    now_ns=self.now_ns(),
                )
            else:
                rejection = "digital output Control accepts command or keep intent only"
            if rejection is None:
                rejection = self._digital_output_ordering.admit(source, cycle)
            if rejection is None and self._control_request_expired(context):
                rejection = "digital output Control request expired before dispatch"
            if rejection is not None:
                return timed_output_response(cycle, "rejected", channel, detail=rejection)
            lease_deadline_ns = self._digital_output_lease_deadline_ns(context)
            if cycle.intent.value == "keep":
                if hold is not None and hold.value:
                    self._digital_output_expiry_ns[channel] = lease_deadline_ns
                return timed_output_response(cycle, "applied", channel)
            return self._apply_timed_output_command(cycle, source, channel, value, lease_deadline_ns)

    def _apply_timed_output_command(
        self,
        cycle: ControlCycle,
        source: str,
        channel: int,
        value: bool,
        lease_deadline_ns: int,
    ) -> dict[str, Any]:
        previous = self._digital_output_hold
        # The previous state is replaced; it can no longer be kept, even on failure.
        self._digital_output_hold = None
        try:
            if previous is not None and previous.value and previous.channel_index != channel:
                self._apply_digital_output(previous.channel_index, False)
                self._digital_output_expiry_ns.pop(previous.channel_index, None)
            if value:
                # Arm the lease before touching the driver. If the driver applies
                # the output but loses the acknowledgement, the watchdog must
                # still have enough state to force the channel low.
                self._digital_output_expiry_ns[channel] = lease_deadline_ns
            result = self._apply_digital_output(channel, value)
        except Exception as exc:
            return timed_output_response(cycle, "device_error", channel, detail=f"digital output driver failed: {exc}")
        if not value:
            self._digital_output_expiry_ns.pop(channel, None)
        if result.get("value") is not True:
            return timed_output_response(
                cycle, "device_error", channel, detail="digital output driver did not acknowledge the command"
            )
        self._digital_output_hold = TimedOutputHold(
            source=source,
            generation=cycle.generation,
            state_revision=cycle.state_revision,
            channel_index=channel,
            value=value,
        )
        return timed_output_response(cycle, "applied", channel)

    def _digital_output_lease_deadline_ns(self, context: Any) -> int:
        valid_until_ns = getattr(context, "valid_until_sync_ns", None)
        if valid_until_ns is not None:
            return int(valid_until_ns)
        period_ms = max(float(getattr(context, "period_ms", 0.0) or 0.0), 1.0)
        return self.now_ns() + int(max(period_ms * 3.0, 60.0) * 1_000_000)

    def _apply_digital_output(self, index: int, value: bool) -> dict[str, Any]:
        previous = bool(self._digital_outputs.get(index, False))
        if index == 0:
            result = self._simulator.call("set_gripper", {"open": value})
        elif index == 1 and value and not previous:
            result = self._simulator.call("incinerate", {})
        else:
            result = {"ok": True, "index": index, "value": value, "state": self._simulator.call("get_state")}
        self._digital_outputs[index] = value
        return {"value": bool(result.get("ok", True))}

    def _get_digital_output(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = params or {}
        index = int(params.get("index", params.get("channel", params.get("value", 0))) or 0)
        with self._digital_output_lock:
            return {"value": bool(self._digital_outputs.get(index, False))}

    def _start_digital_output_watchdog(self) -> None:
        if self._digital_watchdog_thread is not None and self._digital_watchdog_thread.is_alive():
            return
        self._digital_watchdog_stop.clear()
        self._digital_watchdog_thread = threading.Thread(
            target=self._digital_output_watchdog_loop,
            name=f"{self.name}-digital-output-watchdog",
            daemon=True,
        )
        self._digital_watchdog_thread.start()

    def _stop_digital_output_watchdog(self) -> None:
        self._digital_watchdog_stop.set()
        if self._digital_watchdog_thread is not None:
            self._digital_watchdog_thread.join(timeout=1.0)
        self._digital_watchdog_thread = None
        with self._digital_output_lock:
            active = [index for index, value in self._digital_outputs.items() if value and index in self._digital_output_expiry_ns]
            self._digital_output_expiry_ns.clear()
            for index in active:
                try:
                    self._apply_digital_output(index, False)
                except Exception as exc:
                    self.logger.warning("Control", f"digital output {index} safe-off failed: {exc}")

    def _digital_output_watchdog_loop(self) -> None:
        while not self._digital_watchdog_stop.wait(0.01):
            now_ns = self.now_ns()
            with self._digital_output_lock:
                expired = [
                    index
                    for index, deadline_ns in self._digital_output_expiry_ns.items()
                    if deadline_ns <= now_ns
                ]
                for index in expired:
                    if not self._digital_outputs.get(index, False):
                        self._digital_output_expiry_ns.pop(index, None)
                        continue
                    try:
                        self._apply_digital_output(index, False)
                        self._digital_output_expiry_ns.pop(index, None)
                        self.logger.warning("Control", f"digital output {index} lease expired; forced safe-off")
                    except Exception as exc:
                        self.logger.warning("Control", f"digital output {index} watchdog safe-off failed: {exc}")

    def _radar_cycle(self, params: dict[str, Any] | None, context: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValueError("radar_cycle request must be an object")
        if set(params) - {"cycle", "expected_session_id", "after_sample_id"}:
            raise ValueError("radar_cycle request contains unsupported fields")
        cycle_value = params.get("cycle")
        try:
            cycle = ControlCycle.from_mapping(cycle_value)
        except ControlContractError as exc:
            raise ValueError(f"invalid radar ControlCycle: {exc}") from exc
        if cycle.generation <= 0 or cycle.sequence <= 0:
            raise ValueError("radar ControlCycle generation and sequence must be positive")
        if cycle.intent.value != "sample" or cycle.state_revision != 0:
            raise ValueError("radar ControlCycle requires sample intent with state_revision zero")

        source_identity_value = getattr(context, "source_subsystem", None)
        if not isinstance(source_identity_value, str) or not source_identity_value.strip():
            raise ValueError("radar Control request is missing transport source identity")
        source_identity = source_identity_value.strip()
        valid_until_sync_ns = getattr(context, "valid_until_sync_ns", None)
        generated_at_sync_ns = getattr(context, "generated_at_sync_ns", None)
        if valid_until_sync_ns is None or generated_at_sync_ns is None:
            raise ValueError("radar Control request requires generated_at_sync_ns and valid_until_sync_ns")
        valid_until_sync_ns = self._require_integer(valid_until_sync_ns, "valid_until_sync_ns", minimum=1)
        generated_at_sync_ns = self._require_integer(generated_at_sync_ns, "generated_at_sync_ns", minimum=0)
        expected_session_id = params.get("expected_session_id")
        if expected_session_id is not None and (not isinstance(expected_session_id, str) or not expected_session_id):
            raise ValueError("expected_session_id must be non-empty text when provided")
        after_sample_id = params.get("after_sample_id")
        if after_sample_id is not None:
            after_sample_id = self._require_integer(after_sample_id, "after_sample_id", minimum=0)

        with self._radar_cycle_lock:
            now_sync_ns = self.now_ns()
            self._validate_radar_request_context(
                context=context,
                generated_at_sync_ns=generated_at_sync_ns,
                valid_until_sync_ns=valid_until_sync_ns,
                now_sync_ns=now_sync_ns,
            )
            bound_authority = self.validate_control_authority("radar_source", context)
            bound_owner = bound_authority["source"]
            owner_epoch = tuple(bound_authority.items())
            if source_identity != bound_owner:
                raise ValueError(
                    f"radar source is bound to {bound_owner}; received Control request from {source_identity}"
                )
            self._validate_radar_cycle_owner(owner_epoch, cycle)

            simulator_request = {"cycle": cycle.to_mapping()}
            if expected_session_id is not None:
                simulator_request["expected_session_id"] = expected_session_id
            if after_sample_id is not None:
                simulator_request["after_sample_id"] = after_sample_id
            encoded_request = self._encode_radar_message(RADAR_CYCLE_REQUEST, simulator_request)
            if len(encoded_request) > RADAR_PROVIDER_MAX_REQUEST_BYTES:
                raise ValueError("radar Control request exceeds the provider request byte limit")
            simulator_before_ns = self.now_ns()
            self._validate_radar_request_context(
                context=context,
                generated_at_sync_ns=generated_at_sync_ns,
                valid_until_sync_ns=valid_until_sync_ns,
                now_sync_ns=simulator_before_ns,
            )
            simulator_response = self._simulator.call("radar_cycle", simulator_request)
            simulator_after_ns = self.now_ns()
            validated_response = self._validate_simulator_radar_cycle_response(
                simulator_response,
                expected_cycle=cycle,
            )
            if validated_response.get("status") == "sample_available" and not self._simulator_clock_is_aligned(
                simulator_now_ns=validated_response.pop("simulator_now_ns"),
                before_sync_ns=simulator_before_ns,
                after_sync_ns=simulator_after_ns,
            ):
                validated_response = {
                    "cycle": cycle.to_mapping(),
                    "session_id": validated_response["session_id"],
                    "status": "device_error",
                    "detail": "simulator clock is not aligned with the R2Kernel synchronized clock",
                }
            elif "simulator_now_ns" in validated_response:
                validated_response.pop("simulator_now_ns")

            sector = validated_response.get("sector")
            if isinstance(sector, dict) and len(sector.get("returns") or []) > RADAR_PROVIDER_MAX_RETURN_COUNT:
                validated_response = {
                    "cycle": cycle.to_mapping(),
                    "session_id": validated_response["session_id"],
                    "status": "device_error",
                    "detail": "radar sector exceeds the provider return count limit",
                }

            encoded_response = self._encode_radar_message(RADAR_CYCLE_RESPONSE, validated_response)
            if len(encoded_response) > RADAR_PROVIDER_MAX_RESPONSE_BYTES:
                validated_response = {
                    "cycle": cycle.to_mapping(),
                    "session_id": validated_response["session_id"],
                    "status": "device_error",
                    "detail": "radar response exceeds the provider response byte limit",
                }
                encoded_response = self._encode_radar_message(RADAR_CYCLE_RESPONSE, validated_response)
                if len(encoded_response) > RADAR_PROVIDER_MAX_RESPONSE_BYTES:
                    raise RuntimeError("provider radar device_error response exceeds its response byte limit")
            validate_radar_cycle_response(
                params,
                validated_response,
                max_return_count=RADAR_PROVIDER_MAX_RETURN_COUNT,
                response_size_bytes=len(encoded_response),
                max_response_bytes=RADAR_PROVIDER_MAX_RESPONSE_BYTES,
            )
            commit_time_ns = self.now_ns()
            if commit_time_ns >= valid_until_sync_ns:
                raise ValueError("radar Control request expired before the sample response commit")
            if self.bound_control_authority("radar_source") != bound_authority:
                raise ValueError("radar source binding changed before the sample response commit")
            self._commit_radar_cycle_owner(owner_epoch, cycle)
            return validated_response

    def _gnss_cycle(self, params: dict[str, Any] | None, context: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValueError("gnss_cycle request must be an object")
        if set(params) - {"cycle", "expected_session_id", "after_sample_id"}:
            raise ValueError("gnss_cycle request contains unsupported fields")
        try:
            cycle = ControlCycle.from_mapping(params.get("cycle"))
        except ControlContractError as exc:
            raise ValueError(f"invalid GNSS ControlCycle: {exc}") from exc
        if cycle.generation <= 0 or cycle.sequence <= 0:
            raise ValueError("GNSS ControlCycle generation and sequence must be positive")
        if cycle.intent.value != "sample" or cycle.state_revision != 0:
            raise ValueError("GNSS ControlCycle requires sample intent with state_revision zero")
        expected_session_id = params.get("expected_session_id")
        if expected_session_id is not None and (
            not isinstance(expected_session_id, str) or not expected_session_id.strip()
        ):
            raise ValueError("expected_session_id must be non-empty text when provided")
        after_sample_id = params.get("after_sample_id", 0)
        after_sample_id = self._require_integer(after_sample_id, "after_sample_id", minimum=0)
        if expected_session_id is None and after_sample_id != 0:
            raise ValueError("after_sample_id must be zero when expected_session_id is omitted")

        source_identity_value = getattr(context, "source_subsystem", None)
        if not isinstance(source_identity_value, str) or not source_identity_value.strip():
            raise ValueError("GNSS Control request is missing transport source identity")
        source_identity = source_identity_value.strip()
        valid_until_sync_ns = getattr(context, "valid_until_sync_ns", None)
        generated_at_sync_ns = getattr(context, "generated_at_sync_ns", None)
        if valid_until_sync_ns is None or generated_at_sync_ns is None:
            raise ValueError("GNSS Control request requires generated_at_sync_ns and valid_until_sync_ns")
        valid_until_sync_ns = self._require_integer(
            valid_until_sync_ns, "valid_until_sync_ns", minimum=1
        )
        generated_at_sync_ns = self._require_integer(
            generated_at_sync_ns, "generated_at_sync_ns", minimum=0
        )

        with self._gnss_cycle_lock:
            now_sync_ns = self.now_ns()
            self._validate_gnss_request_context(
                context=context,
                generated_at_sync_ns=generated_at_sync_ns,
                valid_until_sync_ns=valid_until_sync_ns,
                now_sync_ns=now_sync_ns,
            )
            bound_authority = self.validate_control_authority("gnss_source", context)
            bound_owner = bound_authority["source"]
            owner_epoch = tuple(bound_authority.items())
            if source_identity != bound_owner:
                raise ValueError(
                    f"GNSS source is bound to {bound_owner}; received Control request from {source_identity}"
                )
            self._validate_gnss_cycle_owner(owner_epoch, cycle)

            simulator_request = {
                "cycle": cycle.to_mapping(),
                "after_sample_id": after_sample_id,
            }
            if expected_session_id is not None:
                simulator_request["expected_session_id"] = expected_session_id
            encoded_request = self._encode_gnss_message(GNSS_CYCLE_REQUEST, simulator_request)
            if len(encoded_request) > GNSS_PROVIDER_MAX_REQUEST_BYTES:
                raise ValueError("GNSS Control request exceeds the provider request byte limit")

            simulator_before_ns = self.now_ns()
            self._validate_gnss_request_context(
                context=context,
                generated_at_sync_ns=generated_at_sync_ns,
                valid_until_sync_ns=valid_until_sync_ns,
                now_sync_ns=simulator_before_ns,
            )
            simulator_response = self._simulator.call("gnss_cycle", simulator_request)
            simulator_after_ns = self.now_ns()
            canonical_response = self._gnss_response_normalizer.normalize_response(
                simulator_response,
                expected_cycle=cycle,
                expected_session_id=expected_session_id,
                before_sync_ns=simulator_before_ns,
                after_sync_ns=simulator_after_ns,
            )
            encoded_response = self._encode_gnss_message(GNSS_CYCLE_RESPONSE, canonical_response)
            if len(encoded_response) > GNSS_PROVIDER_MAX_RESPONSE_BYTES:
                if canonical_response["status"] == "resync_required":
                    canonical_response.pop("detail", None)
                elif canonical_response["status"] != "device_error":
                    canonical_response = {
                        "cycle": cycle.to_mapping(),
                        "session_id": canonical_response["session_id"],
                        "status": "device_error",
                        "detail": "GNSS response exceeds the provider response byte limit",
                    }
                encoded_response = self._encode_gnss_message(GNSS_CYCLE_RESPONSE, canonical_response)
                if len(encoded_response) > GNSS_PROVIDER_MAX_RESPONSE_BYTES:
                    raise RuntimeError("provider GNSS response exceeds its response byte limit")
            validate_gnss_cycle_response(
                simulator_request,
                canonical_response,
                response_size_bytes=len(encoded_response),
                max_response_bytes=GNSS_PROVIDER_MAX_RESPONSE_BYTES,
            )
            commit_time_ns = self.now_ns()
            if commit_time_ns >= valid_until_sync_ns:
                raise ValueError("GNSS Control request expired before the sample response commit")
            if self.bound_control_authority("gnss_source") != bound_authority:
                raise ValueError("GNSS source binding changed before the sample response commit")
            self._commit_gnss_cycle_owner(owner_epoch, cycle)
            return canonical_response

    def _encode_gnss_message(self, message_type: str, payload: dict[str, Any]) -> bytes:
        if self._gnss_message_codec is None:
            self._gnss_message_codec = self._runtime._get_rust_message_codec()
        return self._gnss_message_codec.encode_message(message_type, payload)

    def _validate_gnss_request_context(
        self,
        *,
        context: Any,
        generated_at_sync_ns: int,
        valid_until_sync_ns: int,
        now_sync_ns: int,
    ) -> None:
        if getattr(context, "timed", False) is not True:
            raise ValueError("GNSS Control endpoint requires a timed Control request")
        if generated_at_sync_ns > now_sync_ns:
            raise ValueError("GNSS Control generated_at_sync_ns cannot be in the future")
        if valid_until_sync_ns <= generated_at_sync_ns:
            raise ValueError("GNSS Control valid_until_sync_ns must follow generated_at_sync_ns")
        if valid_until_sync_ns <= now_sync_ns:
            raise ValueError("GNSS Control request is expired")
        scheduled_at_sync_ns = getattr(context, "execute_at_sync_ns", None)
        if scheduled_at_sync_ns is not None:
            scheduled_at_sync_ns = self._require_integer(
                scheduled_at_sync_ns, "execute_at_sync_ns", minimum=0
            )
            if scheduled_at_sync_ns > now_sync_ns:
                raise ValueError("GNSS Control request has not reached its execution time")

    def _validate_gnss_cycle_owner(self, source_identity: str, cycle: ControlCycle) -> None:
        if self._gnss_control_owner != source_identity:
            return
        if (
            self._gnss_control_generation is not None
            and cycle.generation < self._gnss_control_generation
        ):
            raise ValueError("GNSS ControlCycle generation is stale")
        if (
            self._gnss_control_generation == cycle.generation
            and cycle.sequence <= self._gnss_control_sequence
        ):
            raise ValueError("GNSS ControlCycle sequence must strictly increase within its generation")

    def _commit_gnss_cycle_owner(self, source_identity: str, cycle: ControlCycle) -> None:
        if self._gnss_control_owner != source_identity or self._gnss_control_generation != cycle.generation:
            self._gnss_control_owner = source_identity
            self._gnss_control_generation = cycle.generation
            self._gnss_control_sequence = 0
        self._gnss_control_sequence = cycle.sequence

    def _encode_radar_message(self, message_type: str, payload: dict[str, Any]) -> bytes:
        if self._radar_message_codec is None:
            self._radar_message_codec = self._runtime._get_rust_message_codec()
        return self._radar_message_codec.encode_message(message_type, payload)

    def _validate_radar_cycle_owner(self, source_identity: str, cycle: ControlCycle) -> None:
        # A new explicit runtime binding may replace the previously committed
        # source. Packet order only carries over while that source remains the
        # active bound owner; the binding itself is never selected by a packet.
        if self._radar_control_owner != source_identity:
            return
        if self._radar_control_generation is not None and cycle.generation < self._radar_control_generation:
            raise ValueError("radar ControlCycle generation is stale")
        if self._radar_control_generation == cycle.generation and cycle.sequence <= self._radar_control_sequence:
            raise ValueError("radar ControlCycle sequence must strictly increase within its generation")

    def _commit_radar_cycle_owner(self, source_identity: str, cycle: ControlCycle) -> None:
        if self._radar_control_owner != source_identity or self._radar_control_generation != cycle.generation:
            self._radar_control_owner = source_identity
            self._radar_control_generation = cycle.generation
            self._radar_control_sequence = 0
        self._radar_control_sequence = cycle.sequence

    def _validate_radar_request_context(
        self,
        *,
        context: Any,
        generated_at_sync_ns: int,
        valid_until_sync_ns: int,
        now_sync_ns: int,
    ) -> None:
        if getattr(context, "timed", False) is not True:
            raise ValueError("radar Control endpoint requires a timed Control request")
        if generated_at_sync_ns > now_sync_ns:
            raise ValueError("radar Control generated_at_sync_ns cannot be in the future")
        if valid_until_sync_ns <= generated_at_sync_ns:
            raise ValueError("radar Control valid_until_sync_ns must follow generated_at_sync_ns")
        if valid_until_sync_ns <= now_sync_ns:
            raise ValueError("radar Control request is expired")
        scheduled_at_sync_ns = getattr(context, "execute_at_sync_ns", None)
        if scheduled_at_sync_ns is not None:
            scheduled_at_sync_ns = self._require_integer(
                scheduled_at_sync_ns, "execute_at_sync_ns", minimum=0
            )
            if scheduled_at_sync_ns > now_sync_ns:
                raise ValueError("radar Control request has not reached its execution time")

    @classmethod
    def _validate_simulator_radar_cycle_response(
        cls,
        response: Any,
        *,
        expected_cycle: ControlCycle,
    ) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise RuntimeError("simulator radar_cycle response must be an object")
        allowed_fields = {"cycle", "session_id", "status", "sector", "detail", "simulator_now_ns"}
        if set(response) - allowed_fields:
            raise RuntimeError("simulator radar_cycle response contains unsupported fields")
        session_id = response.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("simulator radar_cycle response is missing session_id")
        try:
            response_cycle = ControlCycle.from_mapping(response.get("cycle"))
        except ControlContractError as exc:
            raise RuntimeError(f"simulator radar_cycle response contains an invalid cycle: {exc}") from exc
        if response_cycle != expected_cycle:
            raise RuntimeError("simulator radar_cycle response cycle does not match the request")
        status = response.get("status")
        allowed_statuses = {"sample_available", "no_new_sample", "resync_required", "device_error"}
        if status not in allowed_statuses:
            raise RuntimeError(f"simulator radar_cycle response has unsupported status {status!r}")
        detail = response.get("detail")
        if detail is not None and not isinstance(detail, str):
            raise RuntimeError("simulator radar_cycle detail must be text")
        simulator_now_ns = cls._require_integer(response.get("simulator_now_ns"), "simulator_now_ns", minimum=0)
        sector = response.get("sector")
        if status == "sample_available":
            cls._validate_radar_sector(sector, session_id=session_id)
        elif sector is not None:
            raise RuntimeError("simulator radar_cycle returned a sector without sample_available status")
        validated = {
            "cycle": expected_cycle.to_mapping(),
            "session_id": session_id,
            "status": status,
        }
        if sector is not None:
            validated["sector"] = sector
        if detail is not None:
            validated["detail"] = detail
        validated["simulator_now_ns"] = simulator_now_ns
        return validated

    @classmethod
    def _validate_radar_sector(cls, sector: Any, *, session_id: str) -> None:
        if not isinstance(sector, dict) or set(sector) != RADAR_SECTOR_FIELDS:
            raise RuntimeError("simulator radar sector has missing or unsupported fields")
        if sector.get("session_id") != session_id:
            raise RuntimeError("simulator radar sector session_id does not match its response")
        cls._require_integer(sector.get("sample_id"), "sector.sample_id", minimum=1)
        cls._require_integer(sector.get("revolution_id"), "sector.revolution_id", minimum=1)
        scan_started_at_sync_ns = cls._require_integer(
            sector.get("scan_started_at_sync_ns"), "sector.scan_started_at_sync_ns", minimum=0
        )
        scan_ended_at_sync_ns = cls._require_integer(
            sector.get("scan_ended_at_sync_ns"), "sector.scan_ended_at_sync_ns", minimum=1
        )
        if scan_ended_at_sync_ns <= scan_started_at_sync_ns:
            raise RuntimeError("simulator radar sector scan interval must be positive")
        if sector.get("frame_id") != "cleaner_radar":
            raise RuntimeError("simulator radar sector frame_id must be cleaner_radar")
        if not isinstance(sector.get("revolution_complete"), bool):
            raise RuntimeError("simulator radar sector revolution_complete must be boolean")
        azimuth_start_rad = cls._require_finite_number(sector.get("azimuth_start_rad"), "sector.azimuth_start_rad")
        azimuth_span_rad = cls._require_finite_number(sector.get("azimuth_span_rad"), "sector.azimuth_span_rad")
        if not (0.0 <= azimuth_start_rad < math.tau):
            raise RuntimeError("simulator radar sector azimuth_start_rad must be in [0, 2*pi)")
        if not (0.0 < azimuth_span_rad <= math.tau):
            raise RuntimeError("simulator radar sector azimuth_span_rad must be in (0, 2*pi]")
        returns = sector.get("returns")
        if not isinstance(returns, list):
            raise RuntimeError("simulator radar sector returns must be a list")
        for return_value in returns:
            if not isinstance(return_value, dict) or set(return_value) != RADAR_RETURN_FIELDS:
                raise RuntimeError("simulator radar return has missing or unsupported fields")
            range_m = cls._require_finite_number(return_value.get("range_m"), "return.range_m")
            azimuth_rad = cls._require_finite_number(return_value.get("azimuth_rad"), "return.azimuth_rad")
            cls._require_finite_number(return_value.get("radial_velocity_mps"), "return.radial_velocity_mps")
            signal_strength = cls._require_finite_number(return_value.get("signal_strength"), "return.signal_strength")
            observed_at_sync_ns = cls._require_integer(
                return_value.get("observed_at_sync_ns"), "return.observed_at_sync_ns", minimum=0
            )
            if range_m <= 0.0:
                raise RuntimeError("simulator radar return range_m must be positive")
            if not (0.0 <= signal_strength <= 1.0):
                raise RuntimeError("simulator radar return signal_strength must be normalized to [0, 1]")
            if not (0.0 <= azimuth_rad < math.tau):
                raise RuntimeError("simulator radar return azimuth_rad must be in [0, 2*pi)")
            if not (scan_started_at_sync_ns <= observed_at_sync_ns <= scan_ended_at_sync_ns):
                raise RuntimeError("simulator radar return observation time is outside the scan interval")
            azimuth_offset_rad = (azimuth_rad - azimuth_start_rad) % math.tau
            if azimuth_offset_rad > azimuth_span_rad + 1e-6 and azimuth_span_rad < math.tau - 1e-6:
                raise RuntimeError("simulator radar return azimuth is outside the sector interval")

    @staticmethod
    def _simulator_clock_is_aligned(
        *,
        simulator_now_ns: int,
        before_sync_ns: int,
        after_sync_ns: int,
    ) -> bool:
        tolerance_ns = RADAR_CLOCK_ALIGNMENT_TOLERANCE_NS
        return (
            before_sync_ns - tolerance_ns
            <= simulator_now_ns
            <= after_sync_ns + tolerance_ns
        )

    @staticmethod
    def _require_integer(value: Any, field_name: str, *, minimum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{field_name} must be an integer greater than or equal to {minimum}")
        return value

    @staticmethod
    def _require_finite_number(value: Any, field_name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise RuntimeError(f"{field_name} must be a finite number")
        return float(value)

def main() -> int:
    parser = argparse.ArgumentParser(description="Run the robot cleaner simulator-backed device gateway.")
    parser.add_argument("--name", default="gateway.device_gateway", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--simulator-url", default="http://127.0.0.1:8092", help="Provided simulator API URL.")
    parser.add_argument("--simulator-timeout-ms", type=int, default=1000, help="Simulator request timeout.")
    parser.add_argument(
        "--gnss-fixture",
        default=None,
        help="Path to the explicit geographic origin, clock, and uncertainty fixture.",
    )
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    args = parser.parse_args()

    ensure_control_running()
    wait_for_control_ready()
    instance = DeviceGatewaySubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        simulator_url=args.simulator_url,
        simulator_timeout_ms=args.simulator_timeout_ms,
        gnss_fixture_path=args.gnss_fixture,
    )
    run_subsystem(instance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
