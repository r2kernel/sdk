from __future__ import annotations

import argparse
import json
import os
import socket
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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

from runtime import (  # noqa: E402
    default_manifest_path,
    default_user_data_path,
    ensure_control_running,
    format_python_import_error,
    run_subsystem,
    wait_for_control_ready,
)

try:
    from r2kernel.wrapper.subsystem import AppSubsystem as _SubsystemImportCheck  # noqa: F401
except ModuleNotFoundError as exc:
    raise SystemExit(format_python_import_error(exc.name))

from app_base import CleanerApplicationBase  # noqa: E402
from standard_types import APP_STATUS, EMPTY  # noqa: E402

try:
    from r2kernel.core import ResourceBrokerClient  # noqa: E402
except ModuleNotFoundError:
    ResourceBrokerClient = None  # type: ignore[assignment]


class _BrokeredFrontendServer(socketserver.ThreadingMixIn, socketserver.BaseServer):
    daemon_threads = True

    def __init__(self, server_socket: socket.socket, handler: type[BaseHTTPRequestHandler]) -> None:
        self.socket = server_socket
        self.server_address = server_socket.getsockname()
        super().__init__(self.server_address, handler)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def fileno(self) -> int:
        return self.socket.fileno()

    def get_request(self) -> tuple[socket.socket, Any]:
        return self.socket.accept()

    def server_close(self) -> None:
        self.socket.close()

    def close_request(self, request: socket.socket) -> None:
        request.close()


def _frontend_handler(app: "TeleopCleanerApp") -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "R2KernelCleanerTeleop/1.0"

        def log_message(self, _format: str, *args: Any) -> None:
            return

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("content-length") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("request body must be a JSON object")
            return value

        def do_GET(self) -> None:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/health":
                self._send_json(200, {"ok": True, "kind": "teleop.frontend_api"})
                return
            if path == "/status":
                self._send_json(200, {"ok": True, "status": app._status_payload(reason="frontend.status")})
                return
            self._send_json(404, {"ok": False, "error": f"unknown endpoint: {path}"})

        def do_POST(self) -> None:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path != "/command":
                self._send_json(404, {"ok": False, "error": f"unknown endpoint: {path}"})
                return
            try:
                params = self._read_json()
                result = app._command(params)
                self._send_json(200, {"ok": True, "result": result})
            except ValueError as exc:
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
            except Exception as exc:
                app.logger.error("Frontend", f"teleop frontend command failed: {exc}")
                self._send_json(
                    500,
                    {
                        "ok": False,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )

    return Handler


class TeleopCleanerApp(CleanerApplicationBase):
    def __init__(
        self,
        *,
        name: str = "app.cleaner.teleop",
        log_level: str = "INFO",
        user_data_path: str | None = None,
        manifest_path: str | None = None,
        deployment_manifest_path: str | None = None,
    ) -> None:
        super().__init__(
            name=name,
            log_level=log_level,
            user_data_path=user_data_path or default_user_data_path(__file__, "teleop"),
            manifest_path=manifest_path or default_manifest_path(__file__, "teleop_app"),
            deployment_manifest_path=deployment_manifest_path or str((_ROOT / "deployments" / "teleop.deployment.yaml").resolve()),
            mode="teleop",
        )
        self._frontend_server: _BrokeredFrontendServer | None = None
        self._frontend_thread: threading.Thread | None = None

    def on_initialize(self) -> bool:
        if not super().on_initialize():
            return False
        self.register_procedure("start", self._start, request_type=EMPTY, response_type=APP_STATUS)
        self.register_procedure("stop", self._stop, request_type=EMPTY, response_type=APP_STATUS)
        self.logger.info("Lifecycle", "teleop cleaner app initialized")
        return True

    def on_start(self) -> bool:
        if not super().on_start():
            return False
        self._start_frontend_api()
        return True

    def on_resume(self) -> bool:
        if not super().on_resume():
            return False
        self._start_frontend_api()
        return True

    def on_pause(self) -> bool:
        self._stop_frontend_api()
        return super().on_pause()

    def on_stop(self) -> bool:
        self._stop_frontend_api()
        return super().on_stop()

    def on_reset(self) -> bool:
        self._stop_frontend_api()
        return super().on_reset()

    def on_terminate(self) -> bool:
        self._stop_frontend_api()
        return super().on_terminate()

    def _start_frontend_api(self) -> None:
        if self._frontend_server is not None:
            return
        if not os.getenv("R2KERNEL_RESOURCE_LEASE_ID"):
            return
        if ResourceBrokerClient is None:
            self.logger.warning("Frontend", "r2kernel.core.ResourceBrokerClient is unavailable; app frontend API disabled")
            return
        try:
            brokered = ResourceBrokerClient().open_frontend_tcp_listener(kind="api")
            server = _BrokeredFrontendServer(brokered.socket, _frontend_handler(self))
            thread = threading.Thread(target=server.serve_forever, name=f"{self.name}-frontend-api", daemon=True)
            thread.start()
            self._frontend_server = server
            self._frontend_thread = thread
            self.logger.info("Frontend", f"teleop app frontend API listening on {server.server_address}")
        except Exception as exc:
            self.logger.error("Frontend", f"failed to start teleop app frontend API: {exc}")

    def _stop_frontend_api(self) -> None:
        server = self._frontend_server
        thread = self._frontend_thread
        self._frontend_server = None
        self._frontend_thread = None
        if server is None:
            return
        try:
            server.shutdown()
        finally:
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def _start(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        status = self._set_status(action="teleop.start", active=True)
        self._publish_status(reason="teleop.start")
        return status

    def _stop(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        result = self._stop_drive()
        status = self._set_status(action="teleop.stop", result={"result": result}, active=False)
        self._publish_status(reason="teleop.stop")
        return status

    def _command(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = params or {}
        if "dt" in params:
            raise ValueError(
                "The 'dt' field is no longer supported. Remove it; command timing is managed by the Component."
            )
        action = str(params.get("action") or "stop").strip().lower()
        if action not in {"drive", "velocity", "v"} and not any(key in params for key in ("vx", "vy", "wz")):
            self._set_status(action=f"teleop.{action}.start", result={"action": action, "state": "running"}, active=True)
            self._publish_status(reason=f"teleop.{action}.start")
        if action in {"drive", "velocity", "v"} or any(key in params for key in ("vx", "vy", "wz")):
            result = self._drive(
                vx=float(params.get("vx") or 0.0),
                vy=float(params.get("vy") or 0.0),
                wz=float(params.get("wz") or 0.0),
            )
        elif action in {"forward", "up", "w"}:
            result = self._drive(vx=1.0, vy=0.0, wz=0.0)
        elif action in {"backward", "down", "s"}:
            result = self._drive(vx=-1.0, vy=0.0, wz=0.0)
        elif action in {"left", "a"}:
            result = self._drive(vx=0.0, vy=-1.0, wz=0.0)
        elif action in {"right", "d"}:
            result = self._drive(vx=0.0, vy=1.0, wz=0.0)
        elif action in {"rotate_left", "turn_left", "q"}:
            result = self._drive(vx=0.0, vy=0.0, wz=-1.0)
        elif action in {"rotate_right", "turn_right", "e"}:
            result = self._drive(vx=0.0, vy=0.0, wz=1.0)
        elif action == "open":
            try:
                result = self._open_gripper()
            except Exception as exc:
                result = {"ok": False, "action": "open", "error": str(exc), "error_type": type(exc).__name__}
        elif action == "close":
            try:
                result = self._close_gripper()
            except Exception as exc:
                result = {"ok": False, "action": "close", "error": str(exc), "error_type": type(exc).__name__}
        elif action == "extend":
            try:
                result = self._extend_arm()
            except Exception as exc:
                result = {"ok": False, "action": "extend", "error": str(exc), "error_type": type(exc).__name__}
        elif action in {"shrink", "retract"}:
            action = "shrink"
            try:
                result = self._shrink_arm()
            except Exception as exc:
                result = {"ok": False, "action": "shrink", "error": str(exc), "error_type": type(exc).__name__}
        elif action == "stow":
            try:
                result = self._stow_arm()
            except Exception as exc:
                result = {"ok": False, "action": "stow", "error": str(exc), "error_type": type(exc).__name__}
        elif action in {"collect", "space", "grab"}:
            result = {
                "ok": False,
                "action": "collect",
                "error": "teleop collect macro is disabled; use h extend, j shrink, k open, l close, and space burn",
                "steps": [{"name": "manual", "state": "blocked", "detail": "use h/j/k/l/space"}],
            }
        elif action in {"burn", "incinerate"}:
            try:
                result = self._burn()
            except Exception as exc:
                result = {"ok": False, "action": "burn", "error": str(exc), "error_type": type(exc).__name__}
        else:
            action = "stop"
            result = self._stop_drive()
        active = False if action not in {"drive", "velocity", "v"} else None
        status = self._set_status(action=f"teleop.{action}", result={"result": result}, active=active)
        self._publish_status(reason=f"teleop.{action}")
        return {"action": action, "result": result, "status": status}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the robot cleaner teleoperation app.")
    parser.add_argument("--name", default="app.cleaner.teleop", help="Subsystem name.")
    parser.add_argument("--log-level", default="INFO", help="Logger level.")
    parser.add_argument("--manifest", default=None, help="Path to this subsystem manifest.")
    parser.add_argument("--deployment-manifest", default=None, help="Path to the deployment binding manifest.")
    parser.add_argument("--user-data-path", default=None, help="Path to userdata directory.")
    args = parser.parse_args()

    ensure_control_running()
    wait_for_control_ready()
    instance = TeleopCleanerApp(
        name=args.name,
        log_level=args.log_level,
        user_data_path=args.user_data_path,
        manifest_path=args.manifest,
        deployment_manifest_path=args.deployment_manifest,
    )
    run_subsystem(instance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
