from __future__ import annotations

import argparse
import sys
from pathlib import Path


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "tutorials" / "support" / "python").is_dir():
            return parent
    raise RuntimeError(f"could not resolve repository root from {__file__}")


_SUPPORT_PY = repo_root() / "tutorials" / "support" / "python"
if str(_SUPPORT_PY) not in sys.path:
    sys.path.insert(0, str(_SUPPORT_PY))

from runtime import default_manifest_path, default_user_data_path, ensure_control_running, format_python_import_error

try:
    from r2kernel.datatype.common import DeviceState
    from r2kernel.wrapper.subsystem import Subsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class InvalidDigitalIoServerSubsystem(Subsystem):
    def __init__(
        self,
        *,
        name: str = "interface_invalid_server",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "invalid_server"),
            manifest_path=manifest_path or default_manifest_path(__file__, name),
        )

    def _get_digital_input(self, params: dict | None) -> dict:
        index = int((params or {}).get("value", 0))
        return {"value": bool(index % 2)}

    def on_initialize(self) -> bool:
        self.register_procedure(
            "get_digital_input",
            self._get_digital_input,
            request_type="msg.core.common.primitive.v1/UInt32Value",
            response_type="msg.core.common.primitive.v1/BoolValue",
        )
        self.logger.info("Lifecycle", "registered intentionally incomplete digital IO gateway surface")
        return True

    def on_start(self) -> bool:
        self.start_procedure_server()
        return True

    def on_terminate(self) -> bool:
        self.stop_procedure_server()
        return True

    def on_reset(self) -> bool:
        self.stop_procedure_server()
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an intentionally invalid digital IO gateway implementation.")
    parser.add_argument("--name", default="interface_invalid_server", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    args = parser.parse_args()

    ensure_control_running()
    instance = InvalidDigitalIoServerSubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
    )
    instance.initialize()
    if instance.state == DeviceState.ERROR:
        print(f"{instance.name} contract validation failed as expected")
        return 1
    print(f"{instance.name} unexpectedly passed validation")
    instance.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
