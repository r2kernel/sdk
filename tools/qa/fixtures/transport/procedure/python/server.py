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
    from r2kernel.wrapper.subsystem import Subsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class ProcedureServerSubsystem(Subsystem):
    def __init__(
        self,
        *,
        name: str = "procedure_server",
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
        self._count = 0

    def _increment(self, params: dict | None) -> dict:
        arg = int((params or {}).get("arg", 1))
        self._count += arg
        self.logger.info("Procedure", f"increment arg={arg} count={self._count}")
        return {"count": self._count}

    def on_initialize(self) -> bool:
        # "bytes" carries the JSON object payload ({"arg": n} -> {"count": n}) as is.
        self.register_procedure("increment", self._increment, request_type="bytes", response_type="bytes")
        self.logger.info("Lifecycle", "procedure server initialized")
        return True

    def on_start(self) -> bool:
        self.start_procedure_server()
        self.logger.info("Lifecycle", f"procedure server ready procedures={self.get_procedure_info()}")
        return True

    def on_terminate(self) -> bool:
        self.stop_procedure_server()
        self.logger.info("Lifecycle", "procedure server terminated")
        return True

    def on_reset(self) -> bool:
        self.stop_procedure_server()
        self.logger.info("Lifecycle", "procedure server reset")
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Python procedure server tutorial.")
    parser.add_argument("--name", default="procedure_server", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    args = parser.parse_args()

    ensure_control_running()
    instance = ProcedureServerSubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
    )
    run_subsystem(instance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
