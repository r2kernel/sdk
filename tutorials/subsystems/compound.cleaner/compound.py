from __future__ import annotations

import argparse
import sys
from pathlib import Path


_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "subsystems").is_dir() and (parent / "simulator").is_dir()
)
_SUPPORT_PY = _ROOT / "support" / "python"
if str(_SUPPORT_PY) not in sys.path:
    sys.path.insert(0, str(_SUPPORT_PY))

from runtime import (  # noqa: E402
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
    run_subsystem,
    wait_for_control_ready,
)

try:
    from r2kernel.wrapper.subsystem import CompoundSubsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class CleanerCompoundSubsystem(CompoundSubsystem):
    """Definition-only cleaner composition; API exports are runtime-resolved aliases."""

    def __init__(
        self,
        *,
        name: str = "compound.cleaner",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        deployment_manifest_path: str | None = None,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "compound.cleaner"),
            manifest_path=manifest_path or default_manifest_path(__file__, "compound"),
            deployment_manifest_path=deployment_manifest_path,
        )

    def on_initialize(self) -> bool:
        self.logger.info("Lifecycle", "cleaner spatial composition and declarative API exports loaded")
        return True

    def on_start(self) -> bool:
        return True

    def on_terminate(self) -> bool:
        return True

    def on_reset(self) -> bool:
        return True

    def on_pause(self) -> bool:
        return True

    def on_resume(self) -> bool:
        return True

    def on_stop(self) -> bool:
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the robot cleaner declarative compound composition.")
    parser.add_argument("--name", default="compound.cleaner", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--deployment-manifest", default=None, help="Path to the deployment binding manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    args = parser.parse_args()

    ensure_control_running()
    wait_for_control_ready()
    run_subsystem(
        CleanerCompoundSubsystem(
            name=args.name,
            log_level=args.log_level,
            user_data_path=args.user_data_path,
            manifest_path=args.manifest,
            deployment_manifest_path=args.deployment_manifest,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
