from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "tutorials" / "support" / "python").is_dir():
            return parent
    raise RuntimeError(f"could not resolve repository root from {__file__}")


_SUPPORT_PY = repo_root() / "tutorials" / "support" / "python"
if str(_SUPPORT_PY) not in sys.path:
    sys.path.insert(0, str(_SUPPORT_PY))

from runtime import (
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
    run_subsystem,
)

try:
    from r2kernel.wrapper.subsystem import Subsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class OperationServerSubsystem(Subsystem):
    def __init__(
        self,
        *,
        name: str = "operation_server",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "server"),
            manifest_path=manifest_path or default_manifest_path(__file__, name),
        )

    def _count_to(self, params: dict | None, context) -> dict | None:
        params = params or {}
        goal = max(0, int(params.get("goal", 5)))
        delay_ms = max(1, int(params.get("delay_ms", 200)))
        self.logger.info("Operation", f"count_to start operation_id={context.operation_id} goal={goal} delay_ms={delay_ms}")
        for current in range(1, goal + 1):
            if context.is_cancel_requested():
                payload = {"current": current - 1, "goal": goal, "canceled": True}
                self.logger.info("Operation", f"count_to cancel operation_id={context.operation_id} current={current - 1}")
                context.canceled(payload)
                return None
            time.sleep(delay_ms / 1000.0)
            context.publish_feedback({"current": current, "goal": goal})
        result = {"current": goal, "goal": goal}
        self.logger.info("Operation", f"count_to done operation_id={context.operation_id} goal={goal}")
        return result

    def on_initialize(self) -> bool:
        # "bytes" carries the JSON object goal, feedback and result payloads as is.
        self.register_operation(
            "count_to", self._count_to, request_type="bytes", feedback_type="bytes", result_type="bytes"
        )
        self.logger.info("Lifecycle", "operation server initialized")
        return True

    def on_start(self) -> bool:
        self.start_operation_server()
        self.logger.info("Lifecycle", f"operation server ready operations={self.get_operation_info()}")
        return True

    def on_terminate(self) -> bool:
        self.stop_operation_server()
        self.logger.info("Lifecycle", "operation server terminated")
        return True

    def on_reset(self) -> bool:
        self.stop_operation_server()
        self.logger.info("Lifecycle", "operation server reset")
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Python operation server tutorial.")
    parser.add_argument("--name", default="operation_server", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    args = parser.parse_args()

    ensure_control_running()
    instance = OperationServerSubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
    )
    run_subsystem(instance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
