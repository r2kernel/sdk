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

from runtime import (
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
    run_subsystem,
)

try:
    from r2kernel.interface.bindings import bind_digital_io_interface_typed
    from r2kernel.wrapper.subsystem import Subsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class InMemoryDigitalIoDriver:
    def __init__(self, channel_count: int = 4) -> None:
        self._inputs = [(index % 2) == 1 for index in range(channel_count)]
        self._outputs = [False for _ in range(channel_count)]

    def _normalize_index(self, index: int) -> int:
        if index < 0 or index >= len(self._outputs):
            raise IndexError(f"digital channel index out of range: {index}")
        return index

    def get_digital_input(self, index: int) -> bool:
        return bool(self._inputs[self._normalize_index(index)])

    def set_digital_output(self, index: int, value: bool) -> bool:
        self._outputs[self._normalize_index(index)] = bool(value)
        return True

    def get_digital_output(self, index: int) -> bool:
        return bool(self._outputs[self._normalize_index(index)])


class TypedDigitalIoServerSubsystem(Subsystem):
    def __init__(
        self,
        *,
        name: str = "interface_digital_typed_server",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        channel_count: int = 4,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "typed_server"),
            manifest_path=manifest_path or default_manifest_path(__file__, name),
        )
        self._driver = InMemoryDigitalIoDriver(channel_count=channel_count)

    def on_initialize(self) -> bool:
        bind_digital_io_interface_typed(self, self._driver)
        self.logger.info("Lifecycle", "typed digital IO gateway contract bound")
        return True

    def on_start(self) -> bool:
        self.start_procedure_server()
        self.logger.info("Lifecycle", f"typed digital IO gateway ready services={self.get_procedure_info()}")
        return True

    def on_terminate(self) -> bool:
        self.stop_procedure_server()
        self.logger.info("Lifecycle", "typed digital IO gateway terminated")
        return True

    def on_reset(self) -> bool:
        self.stop_procedure_server()
        self.logger.info("Lifecycle", "typed digital IO gateway reset")
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the typed digital IO gateway server tutorial.")
    parser.add_argument("--name", default="interface_digital_typed_server", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--channel-count", type=int, default=4, help="Number of digital channels.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    args = parser.parse_args()

    ensure_control_running()
    instance = TypedDigitalIoServerSubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        channel_count=args.channel_count,
    )
    run_subsystem(instance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
