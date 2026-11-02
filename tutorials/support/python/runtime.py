from __future__ import annotations

import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from public_sdk import ensure_repo_python_sdk, python_tutorial_environment_message

if TYPE_CHECKING:
    from r2kernel.wrapper.subsystem import Subsystem


ensure_repo_python_sdk()


def format_python_import_error(exc_name: str | None) -> str:
    return python_tutorial_environment_message(exc_name)


def default_user_data_path(tutorial_file: str, name: str) -> str:
    runtime_dir = os.getenv("R2KERNEL_PACKAGE_RUNTIME_DIR") or os.getenv("R2KERNEL_PACKAGE_INSTANCE_DIR")
    if runtime_dir:
        return str((Path(runtime_dir).expanduser() / "userdata" / name).resolve())
    return str((Path(tutorial_file).resolve().parent / "userdata" / name).resolve())


def default_manifest_path(tutorial_file: str, name: str) -> str:
    directory = Path(tutorial_file).resolve().parent
    default_path = directory / "subsystem.yaml"
    if default_path.exists():
        return str(default_path.resolve())
    return str((directory / f"{name}.subsystem.yaml").resolve())


def default_deployment_manifest_path(tutorial_file: str) -> str:
    tutorial_dir = Path(tutorial_file).resolve().parent
    for candidate in (tutorial_dir / "r2kernel.deployment.yaml", tutorial_dir.parent / "r2kernel.deployment.yaml"):
        if candidate.exists():
            return str(candidate.resolve())
    return str((tutorial_dir.parent / "r2kernel.deployment.yaml").resolve())


def resolve_control_port(default: int = 40000) -> int:
    env_port = os.getenv("R2KERNEL_CONTROL_UDP_PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError as exc:
            raise SystemExit(f"Invalid control UDP port in environment: {env_port}") from exc
    return int(default)


def resolve_control_api_port(default: int = 8001) -> int:
    env_port = os.getenv("R2KERNEL_CONTROL_API_PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError as exc:
            raise SystemExit(f"Invalid control API port in environment: {env_port}") from exc
    return int(default)


def is_managed_package_launch() -> bool:
    return bool(
        os.getenv("R2KERNEL_PACKAGE_RUNTIME_DIR")
        or os.getenv("R2KERNEL_PACKAGE_INSTANCE_DIR")
    )


def ensure_control_running(port: int | None = None) -> None:
    if is_managed_package_launch():
        return
    if os.getenv("R2KERNEL_CONTROL_SOCKET"):
        control_socket = os.getenv("R2KERNEL_CONTROL_SOCKET")
        if control_socket and Path(control_socket).exists():
            return
    port = resolve_control_port(40000 if port is None else port)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return
    finally:
        probe.close()
    prefix = Path(os.getenv("R2KERNEL_PREFIX", "/opt/r2kernel")).expanduser()
    command = f". {prefix}/env.sh && r2kernel control --config {prefix}/config.yaml"
    raise SystemExit(
        f"No control backend detected on port {port}; start it first and retry.\n"
        f"Recommended installed-runtime command:\n  {command}"
    )


def wait_for_control_ready(timeout_s: float = 10.0) -> None:
    if is_managed_package_launch():
        return
    if os.getenv("R2KERNEL_CONTROL_SOCKET"):
        control_socket = os.getenv("R2KERNEL_CONTROL_SOCKET")
        if control_socket and Path(control_socket).exists():
            return
    deadline = time.monotonic() + max(timeout_s, 0.5)
    api_port = resolve_control_api_port()
    url = f"http://127.0.0.1:{api_port}/api/v1/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return
        except urllib.error.URLError:
            pass
        time.sleep(0.2)
    raise SystemExit(f"Control API did not become ready at {url} within {timeout_s:.1f}s.")


def run_subsystem(
    instance: "Subsystem",
    should_stop: Callable[[], bool] | None = None,
    *,
    check_state: bool = True,
    poll_s: float = 0.1,
) -> None:
    instance.initialize()
    if check_state and str(getattr(instance, "state", "")).lower().endswith("error"):
        try:
            instance.terminate()
        finally:
            raise SystemExit(f"{getattr(instance, 'name', 'subsystem')} initialize failed")
    instance.start()
    if check_state and str(getattr(instance, "state", "")).lower().endswith("error"):
        try:
            instance.terminate()
        finally:
            raise SystemExit(f"{getattr(instance, 'name', 'subsystem')} start failed")
    try:
        while True:
            if should_stop and should_stop():
                break
            time.sleep(poll_s)
    except KeyboardInterrupt:
        pass
    finally:
        instance.terminate()
