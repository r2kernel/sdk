from __future__ import annotations

import argparse
import os
import sys
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

from common import extract_payload, json_payload, response_ok  # noqa: E402
from runtime import (  # noqa: E402
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
    run_subsystem,
    wait_for_control_ready,
)
from standard_types import BOOL, EMPTY, INDEXED_BOOL, UINT32  # noqa: E402

try:
    from r2kernel.wrapper.subsystem import ComponentSubsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


API_RESULT = "msg.core.common.result.v1/Result"
class GripperControllerSubsystem(ComponentSubsystem):
    def __init__(
        self,
        *,
        name: str = "component.gripper",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        deployment_manifest_path: str | None = None,
        binding: str = "digital_io",
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "gripper"),
            manifest_path=manifest_path or default_manifest_path(__file__, "gripper_controller"),
            deployment_manifest_path=deployment_manifest_path,
        )
        self._binding = binding

    def on_initialize(self) -> bool:
        self.register_procedure("open", self._open, request_type=EMPTY, response_type=API_RESULT)
        self.register_procedure("close", self._close, request_type=EMPTY, response_type=API_RESULT)
        self.register_procedure("get_gripper_state", self._get_gripper_state, request_type=EMPTY, response_type=BOOL)
        self.logger.info("Lifecycle", "gripper controller initialized")
        return True

    def on_start(self) -> bool:
        self.start_event_transport()
        self.start_procedure_server()
        self.set_frame_transform(
            parent_frame="gripper_mount",
            child_frame="cleaner_grasp",
            translation=(0.025, 0.0, 0.0),
            rotation_xyzw=(0.0, 0.0, 0.0, 1.0),
            static=True,
        )
        return True

    def on_terminate(self) -> bool:
        self.stop_procedure_server()
        self.stop_event_transport()
        return True

    def on_reset(self) -> bool:
        self.stop_procedure_server()
        self.stop_event_transport()
        return True

    def on_pause(self) -> bool:
        self.stop_procedure_server()
        self.stop_event_transport()
        return True

    def on_resume(self) -> bool:
        self.start_event_transport()
        self.start_procedure_server()
        return True

    def on_stop(self) -> bool:
        self.stop_procedure_server()
        self.stop_event_transport()
        return True

    def _open(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        response = self.transact_bound_control(
            binding=self._binding,
            topic="digital_output_set",
            type="bytes",
            value={"index": 0, "value": True},
            period_ms=20.0,
            deadline_ms=1000.0,
            request_type=INDEXED_BOOL,
            response_type=BOOL,
            timing={
                "generated_at_sync_ns": self.now_ns(),
                "valid_for_ns": 1_000_000_000,
                "late_policy": "reject",
            },
        )
        opened = self._control_result_bool(response, "digital output set")
        return self._api_result({"opened": opened}, success=opened, status="open" if opened else "failed")

    def _close(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        response = self.transact_bound_control(
            binding=self._binding,
            topic="digital_output_set",
            type="bytes",
            value={"index": 0, "value": False},
            period_ms=20.0,
            deadline_ms=1000.0,
            request_type=INDEXED_BOOL,
            response_type=BOOL,
            timing={
                "generated_at_sync_ns": self.now_ns(),
                "valid_for_ns": 1_000_000_000,
                "late_policy": "reject",
            },
        )
        closed = self._control_result_bool(response, "digital output set")
        return self._api_result({"closed": closed}, success=closed, status="closed" if closed else "failed")

    def _get_gripper_state(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        response = self.transact_bound_control(
            binding=self._binding,
            topic="digital_output_readback",
            type="bytes",
            value={"value": 0},
            period_ms=20.0,
            deadline_ms=1000.0,
            request_type=UINT32,
            response_type=BOOL,
            timing={
                "generated_at_sync_ns": self.now_ns(),
                "valid_for_ns": 1_000_000_000,
                "late_policy": "reject",
            },
        )
        return {"value": self._control_result_bool(response, "digital output readback")}

    @staticmethod
    def _control_result_bool(response: dict[str, Any], action: str) -> bool:
        if not response_ok(response):
            message = (
                response.get("error_message") or response.get("error_code")
                if isinstance(response, dict)
                else None
            )
            raise RuntimeError(str(message or f"{action} Control failed"))
        payload = extract_payload(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("value"), bool):
            raise RuntimeError(f"{action} Control returned no boolean value")
        return payload["value"]

    @staticmethod
    def _api_result(detail: Any, *, success: bool, status: str) -> dict[str, Any]:
        return {"success": bool(success), "status": status, "detail": json_payload(detail)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the robot cleaner gripper controller.")
    parser.add_argument("--name", default="component.gripper", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--binding", default="digital_io", help="Required digital I/O binding name.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--deployment-manifest", default=None, help="Path to the deployment binding manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    args = parser.parse_args()

    ensure_control_running()
    wait_for_control_ready()
    instance = GripperControllerSubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        deployment_manifest_path=args.deployment_manifest,
        binding=args.binding,
    )
    run_subsystem(instance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
