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
    default_deployment_manifest_path,
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
)

try:
    from r2kernel.datatype.common import DeviceState
    from r2kernel.wrapper.subsystem import Subsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class TypedDigitalIoClientSubsystem(Subsystem):
    def __init__(
        self,
        *,
        name: str = "interface_digital_typed_client",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        deployment_manifest_path: str | None = None,
        binding: str = "digital_io",
        index: int = 0,
        count: int = 5,
        period_ms: int = 500,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "typed_client"),
            manifest_path=manifest_path or default_manifest_path(__file__, name),
            deployment_manifest_path=deployment_manifest_path or default_deployment_manifest_path(__file__),
        )
        self._target = self.binding_target(binding)
        self._index = index
        self._count = count
        self._period_ms = period_ms

    def on_initialize(self) -> bool:
        self.logger.info("Lifecycle", "typed digital IO client initialized")
        return True

    def on_start(self) -> bool:
        request_ref = "msg.core.common.primitive.v1/UInt32Value"
        value_ref = "msg.core.common.primitive.v1/BoolValue"
        set_request_ref = "msg.core.common.primitive.v1/IndexedBoolValue"
        set_response_ref = "msg.core.common.primitive.v1/BoolValue"

        for step in range(self._count):
            desired = (step % 2) == 0
            set_response = self.call_typed_procedure(
                target=self._target,
                procedure="set_digital_output",
                request_type=set_request_ref,
                response_type=set_response_ref,
                params={"index": self._index, "value": desired},
                timeout_ms=max(self._period_ms, 1000),
            )
            output_response = self.call_typed_procedure(
                target=self._target,
                procedure="get_digital_output",
                request_type=request_ref,
                response_type=value_ref,
                params={"value": self._index},
                timeout_ms=max(self._period_ms, 1000),
            )
            print(f"{self.name} step={step} set={set_response} output={output_response}")
            if step + 1 < self._count:
                time.sleep(self._period_ms / 1000.0)
        return True

    def on_terminate(self) -> bool:
        self.logger.info("Lifecycle", "typed digital IO client terminated")
        return True

    def on_reset(self) -> bool:
        return self.on_terminate()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the typed digital IO interface client tutorial.")
    parser.add_argument("--name", default="interface_digital_typed_client", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--binding", default="digital_io", help="Required interface binding name.")
    parser.add_argument("--index", type=int, default=0, help="Digital channel index.")
    parser.add_argument("--count", type=int, default=5, help="Number of request cycles to run.")
    parser.add_argument("--period-ms", type=int, default=500, help="Delay between request cycles.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--deployment-manifest", default=None, help="Path to the deployment binding manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    args = parser.parse_args()

    ensure_control_running()
    instance = TypedDigitalIoClientSubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        deployment_manifest_path=args.deployment_manifest,
        binding=args.binding,
        index=args.index,
        count=args.count,
        period_ms=args.period_ms,
    )
    instance.initialize()
    if instance.state == DeviceState.ERROR:
        return 1
    instance.start()
    if instance.state == DeviceState.ERROR:
        instance.terminate()
        return 1
    instance.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
