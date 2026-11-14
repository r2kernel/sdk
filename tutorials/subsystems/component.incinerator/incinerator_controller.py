from __future__ import annotations

import argparse
import os
import sys
import threading
import time
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

from common import extract_payload, response_ok  # noqa: E402
from control_cycles import ControlCycleIssuer, cycle_echo_matches  # noqa: E402
from runtime import (  # noqa: E402
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
    run_subsystem,
    wait_for_control_ready,
)
from standard_types import (  # noqa: E402
    BOOL,
    EMPTY,
    TIMED_OUTPUT_CONTROL_REQUEST,
    TIMED_OUTPUT_CONTROL_RESPONSE,
)

try:
    from r2kernel.wrapper.subsystem import ComponentSubsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class IncineratorControllerSubsystem(ComponentSubsystem):
    """Implement a configured trigger actuator over a fail-safe digital output pulse.

    The pulse uses the maintain_state timed output Control: a ``command`` cycle
    sets the output, ``keep`` cycles maintain a HIGH output under the gateway
    lease, and a LOW output is always re-commanded so safe-off never depends on
    previously retained state.
    """

    def __init__(
        self,
        *,
        name: str = "component.incinerator",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        deployment_manifest_path: str | None = None,
        binding: str = "timed_digital_output",
        output_index: int = 1,
        pulse_ms: float = 120.0,
        control_period_ms: float = 20.0,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "incinerator"),
            manifest_path=manifest_path or default_manifest_path(__file__, "incinerator_controller"),
            deployment_manifest_path=deployment_manifest_path,
        )
        self._binding = binding
        self._output_index = max(0, int(output_index))
        self._control_period_ms = max(5.0, float(control_period_ms))
        self._pulse_ms = max(self._control_period_ms, float(pulse_ms))
        self._burn_lock = threading.Lock()
        self._last_control_tick_ns: int | None = None
        self._output_cycles = ControlCycleIssuer(lambda: self.now_ns())
        self._held_value: bool | None = None

    def on_initialize(self) -> bool:
        self.register_procedure("trigger", self._trigger, request_type=EMPTY, response_type=BOOL)
        self.logger.info("Lifecycle", "trigger actuator component initialized")
        return True

    def on_start(self) -> bool:
        self._output_cycles.restart()
        self._held_value = None
        self.start_procedure_server()
        return True

    def on_terminate(self) -> bool:
        self._best_effort_safe_off()
        self.stop_procedure_server()
        return True

    def on_reset(self) -> bool:
        return self.on_terminate()

    def on_pause(self) -> bool:
        return self.on_terminate()

    def on_resume(self) -> bool:
        return self.on_start()

    def on_stop(self) -> bool:
        return self.on_terminate()

    def _trigger(self, _params: dict[str, Any] | None) -> dict[str, bool]:
        if not self._burn_lock.acquire(blocking=False):
            return {"value": False}

        pulse_complete = False
        first_ack = False
        safe_off_confirmed = False
        error = ""
        safe_off_error = ""
        try:
            deadline = time.monotonic() + self._pulse_ms / 1000.0
            first = True
            while first or time.monotonic() < deadline:
                acknowledged, response = self._drive_output(True)
                if first:
                    first_ack = acknowledged
                    first = False
                if not acknowledged:
                    error = self._output_rejection_message(response, phase="HIGH")
                    break
                remaining = deadline - time.monotonic()
                if remaining > 0.0:
                    time.sleep(min(self._control_period_ms / 1000.0, remaining))
            pulse_complete = first_ack and not error and time.monotonic() >= deadline
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                safe_off_confirmed, off_response = self._drive_output(False)
                if not safe_off_confirmed:
                    safe_off_error = self._output_rejection_message(off_response, phase="safe-off")
            except Exception as exc:
                safe_off_error = f"safe-off {type(exc).__name__}: {exc}"
            self._burn_lock.release()

        fired = pulse_complete and safe_off_confirmed
        if error or safe_off_error:
            self.logger.warning("Trigger", "; ".join(message for message in (error, safe_off_error) if message))
        return {"value": fired}

    @staticmethod
    def _output_rejection_message(response: dict[str, Any], *, phase: str) -> str:
        transport = response.get("transport")
        transport_error = transport.get("error") if isinstance(transport, dict) else None
        reason = response.get("error_message") or response.get("error_code") or transport_error
        if not reason and response_ok(response):
            payload = extract_payload(response)
            status = payload.get("status") if isinstance(payload, dict) else None
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if isinstance(status, str) and status != "applied":
                reason = f"digital output {status}" + (f": {detail}" if isinstance(detail, str) and detail else "")
            else:
                reason = "digital output was not applied"
        if not reason:
            reason = "digital output was rejected"
        return f"{phase}: {reason}"

    def _drive_output(self, value: bool) -> tuple[bool, dict[str, Any]]:
        """Send one timed output cycle; return whether the gateway applied it."""
        keep = bool(value) and self._held_value is True
        cycle = self._output_cycles.keep() if keep else self._output_cycles.command()
        request: dict[str, Any] = {"cycle": cycle}
        if not keep:
            request["output"] = {"index": self._output_index, "value": bool(value)}
        period_ms = self._control_period_ms
        now_ns = self.now_ns()
        response = self.transact_bound_control(
            binding=self._binding,
            topic="digital_output_control",
            type="bytes",
            value=request,
            period_ms=period_ms,
            deadline_ms=max(period_ms * 1.5, 20.0),
            last_tick_ns=self._last_control_tick_ns,
            request_type=TIMED_OUTPUT_CONTROL_REQUEST,
            response_type=TIMED_OUTPUT_CONTROL_RESPONSE,
            timing={
                "generated_at_sync_ns": now_ns,
                "valid_for_ns": int(max(period_ms * 3.0, 60.0) * 1_000_000),
                "late_policy": "reject",
            },
        )
        self._last_control_tick_ns = now_ns
        payload = extract_payload(response) if response_ok(response) else None
        applied = (
            cycle_echo_matches(payload, cycle)
            and payload.get("status") == "applied"
            and payload.get("channel_index") == self._output_index
        )
        self._held_value = bool(value) if applied else None
        return applied, response

    def _best_effort_safe_off(self) -> None:
        try:
            self._drive_output(False)
        except Exception as exc:
            self.logger.warning("Control", f"incinerator safe-off confirmation unavailable: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the robot cleaner incinerator component.")
    parser.add_argument("--name", default="component.incinerator", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument(
        "--binding",
        default="timed_digital_output",
        help="Required timed digital output gateway binding name.",
    )
    parser.add_argument("--output-index", type=int, default=1, help="Gateway digital output index for the incinerator.")
    parser.add_argument("--pulse-ms", type=float, default=120.0, help="Required active output pulse duration.")
    parser.add_argument("--control-period-ms", type=float, default=20.0, help="Digital output control refresh period.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--deployment-manifest", default=None, help="Path to the deployment binding manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    args = parser.parse_args()

    ensure_control_running()
    wait_for_control_ready()
    instance = IncineratorControllerSubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        deployment_manifest_path=args.deployment_manifest,
        binding=args.binding,
        output_index=args.output_index,
        pulse_ms=args.pulse_ms,
        control_period_ms=args.control_period_ms,
    )
    run_subsystem(instance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
