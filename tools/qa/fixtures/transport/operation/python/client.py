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


class OperationClientSubsystem(Subsystem):
    def __init__(
        self,
        *,
        name: str = "operation_client",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        deployment_manifest_path: str | None = None,
        binding: str = "operation",
        operation_name: str = "count_to",
        goal: int = 10,
        delay_ms: int = 100,
        query_period_ms: int = 500,
        cancel_after_ms: int = 0,
        exit_on_complete: bool = False,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "client"),
            manifest_path=manifest_path or default_manifest_path(__file__, name),
            deployment_manifest_path=deployment_manifest_path or default_deployment_manifest_path(__file__),
        )
        self._target = self.binding_target(binding)
        self._operation_name = operation_name
        self._goal = goal
        self._delay_ms = delay_ms
        self._query_period_ms = query_period_ms
        self._cancel_after_ms = cancel_after_ms
        self._exit_on_complete = exit_on_complete
        self._stop = threading.Event()
        self._done = threading.Event()
        self._worker: threading.Thread | None = None
        self._failed = False
        self._operation_id: str | None = None

    def on_initialize(self) -> bool:
        self.logger.info("Lifecycle", "operation client initialized")
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
        self.logger.info("Lifecycle", "operation client terminated")
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
            print(f"{self.name} operation_error={type(error).__name__}: {error}", flush=True)
            self._failed = True
            self._done.set()
            self._stop.set()

    def _work_loop(self) -> None:
        operation_id = self.start_operation(
            target=self._target,
            operation=self._operation_name,
            params={"goal": self._goal, "delay_ms": self._delay_ms},
            timeout_ms=1000,
        )
        self._operation_id = operation_id
        print(f"{self.name} operation_started operation_id={operation_id}", flush=True)

        self.subscribe_operation_state(
            target=self._target,
            operation=self._operation_name,
            operation_id=operation_id,
            callback=lambda message, ts: print(f"{self.name} state ts={ts} message={message}", flush=True),
        )
        self.subscribe_operation_feedback(
            target=self._target,
            operation=self._operation_name,
            operation_id=operation_id,
            callback=lambda message, ts: print(f"{self.name} feedback ts={ts} message={message}", flush=True),
        )
        self.subscribe_operation_result(
            target=self._target,
            operation=self._operation_name,
            operation_id=operation_id,
            callback=lambda message, ts: self._handle_result(message, ts),
        )

        if self._cancel_after_ms > 0:
            threading.Thread(target=self._cancel_later, daemon=True).start()

        period_s = max(self._query_period_ms, 1) / 1000.0
        while not self._stop.wait(period_s):
            response = self.query_operation(
                target=self._target,
                operation_id=operation_id,
                timeout_ms=max(self._query_period_ms, 1000),
            )
            print(f"{self.name} query={response}", flush=True)
            if response.get("ok") and str(response.get("state") or "") in {"succeeded", "failed", "canceled"}:
                if self._exit_on_complete:
                    self._done.set()
                    self._stop.set()
                break

    def _cancel_later(self) -> None:
        if self._cancel_after_ms <= 0:
            return
        if self._stop.wait(self._cancel_after_ms / 1000.0):
            return
        operation_id = self._operation_id
        if not operation_id:
            return
        response = self.cancel_operation(target=self._target, operation_id=operation_id, timeout_ms=1000)
        print(f"{self.name} cancel={response}", flush=True)

    def _handle_result(self, message: dict, ts: int) -> None:
        print(f"{self.name} result ts={ts} message={message}", flush=True)
        if self._exit_on_complete:
            self._done.set()
            self._stop.set()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Python operation client tutorial.")
    parser.add_argument("--name", default="operation_client", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--binding", default="operation", help="Required interface binding name.")
    parser.add_argument("--operation-name", default="count_to", help="Target operation name.")
    parser.add_argument("--goal", type=int, default=10, help="Target count.")
    parser.add_argument("--delay-ms", type=int, default=100, help="Delay per step in milliseconds.")
    parser.add_argument("--query-period-ms", type=int, default=500, help="Query period in milliseconds.")
    parser.add_argument("--cancel-after-ms", type=int, default=0, help="Cancel after this delay in milliseconds. 0 disables cancel.")
    parser.add_argument("--exit-on-complete", action="store_true", help="Exit after a terminal result/state is observed.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--deployment-manifest", default=None, help="Path to the deployment binding manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    args = parser.parse_args()

    ensure_control_running()
    instance = OperationClientSubsystem(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        deployment_manifest_path=args.deployment_manifest,
        binding=args.binding,
        operation_name=args.operation_name,
        goal=args.goal,
        delay_ms=args.delay_ms,
        query_period_ms=args.query_period_ms,
        cancel_after_ms=args.cancel_after_ms,
        exit_on_complete=args.exit_on_complete,
    )
    run_subsystem(instance, should_stop=instance.done)
    return 1 if instance.failed() else 0


if __name__ == "__main__":
    raise SystemExit(main())
