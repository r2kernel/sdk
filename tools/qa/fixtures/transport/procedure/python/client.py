from __future__ import annotations

import argparse
import sys
import threading
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
    run_subsystem,
)

try:
    from r2kernel.wrapper.subsystem import Subsystem
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))


class ProcedureClientSubsystem(Subsystem):
    def __init__(
        self,
        *,
        name: str = "procedure_client",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        deployment_manifest_path: str | None = None,
        binding: str = "procedure",
        arg: int = 1,
        period_ms: int = 1000,
        count: int = 0,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "client"),
            manifest_path=manifest_path or default_manifest_path(__file__, name),
            deployment_manifest_path=deployment_manifest_path or default_deployment_manifest_path(__file__),
        )
        self._target = self.binding_target(binding)
        self._arg = arg
        self._period_ms = period_ms
        self._count = max(count, 0)
        self._stop = threading.Event()
        self._done = threading.Event()
        self._worker: threading.Thread | None = None
        self._failed = False

    def on_initialize(self) -> bool:
        self.logger.info("Lifecycle", "procedure client initialized")
        return True

    def on_start(self) -> bool:
        self._stop.clear()
        self._done.clear()
        self._worker = threading.Thread(target=self._run_work_loop, daemon=True)
        self._worker.start()
        return True

    def on_terminate(self) -> bool:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=1.0)
            self._worker = None
        self.logger.info("Lifecycle", "procedure client terminated")
        return True

    def on_reset(self) -> bool:
        return self.on_terminate()

    def done(self) -> bool:
        return self._done.is_set()

    def failed(self) -> bool:
        return self._failed

    def _run_work_loop(self) -> None:
        # A failed request ends the run with an error instead of leaving a live
        # process whose worker thread has died.
        try:
            self._work_loop()
        except Exception as error:
            print(f"{self.name} procedure_error={type(error).__name__}: {error}", flush=True)
            self._failed = True
            self._done.set()
            self._stop.set()

    def _work_loop(self) -> None:
        period_s = self._period_ms / 1000.0
        sent = 0
        while not self._stop.wait(period_s):
            response = self.call_procedure(
                target=self._target,
                procedure="increment",
                params={"arg": self._arg},
                timeout_ms=max(self._period_ms, 1000),
            )
            print(f"{self.name} procedure_response={response}")
            sent += 1
            if self._count > 0 and sent >= self._count:
                self._done.set()
                self._stop.set()
                return


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Python procedure client tutorial.")
    parser.add_argument("--name", default="procedure_client", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--binding", default="procedure", help="Required interface binding name.")
    parser.add_argument("--arg", type=int, default=1, help="Argument for the increment procedure.")
    parser.add_argument("--period-ms", type=int, default=1000, help="Procedure request period in milliseconds.")
    parser.add_argument("--count", type=int, default=0, help="Number of requests before auto-exit. 0 means run until interrupted.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--deployment-manifest", default=None, help="Path to the deployment binding manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    args = parser.parse_args()

    ensure_control_running()
    instance = ProcedureClientSubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        deployment_manifest_path=args.deployment_manifest,
        binding=args.binding,
        arg=args.arg,
        period_ms=args.period_ms,
        count=args.count,
    )
    run_subsystem(instance, should_stop=instance.done)
    return 1 if instance.failed() else 0


if __name__ == "__main__":
    raise SystemExit(main())
