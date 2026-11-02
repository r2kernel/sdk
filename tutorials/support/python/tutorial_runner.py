from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def repo_root(from_file: str) -> Path:
    return Path(from_file).resolve().parents[3]


def python_tutorial_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    wrapper_python = str(root / "sdk" / "python")
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = wrapper_python if not current else wrapper_python + os.pathsep + current
    return env


def control_env(root: Path) -> dict[str, str]:
    return python_tutorial_env(root)


def installed_prefix() -> Path:
    return Path(os.getenv("R2KERNEL_PREFIX", "/opt/r2kernel")).expanduser()


def installed_control_binary() -> Path:
    return installed_prefix() / "runtime" / ("bin/r2kernel-control.exe" if os.name == "nt" else "bin/r2kernel-control")


def installed_control_config() -> Path:
    return installed_prefix() / "runtime" / "config.yaml"


def ensure_control_binary() -> tuple[Path, Path]:
    binary = installed_control_binary()
    config = installed_control_config()
    if not binary.exists() or not config.exists():
        raise SystemExit(
            f"Installed R2Kernel control backend was not found (checked {binary} and {config}). "
            "Install R2Kernel or set R2KERNEL_PREFIX to the installed runtime prefix."
        )
    return binary, config


def start_local_control(root: Path) -> subprocess.Popen:
    binary, config = ensure_control_binary()
    return start_process(
        "control",
        [str(binary), "--config", str(config)],
        root,
        env=control_env(root),
    )


def udp_port_in_use(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def wait_for_udp_port(port: int, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if udp_port_in_use(port):
            return True
        time.sleep(0.1)
    return False


def start_process(name: str, cmd: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> subprocess.Popen:
    print(f"[{name}] cwd={cwd}")
    print(f"[{name}] $ {' '.join(cmd)}")
    return subprocess.Popen(cmd, cwd=str(cwd), env=env)


def terminate_process(name: str, proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    print(f"[{name}] terminating...")
    proc.terminate()
    try:
        proc.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        print(f"[{name}] killing...")
        proc.kill()
        proc.wait(timeout=3.0)


def run_checked(cmd: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(cwd), env=env, check=True)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def control_status_url() -> str:
    return os.getenv("R2KERNEL_CONTROL_STATUS_URL", "http://127.0.0.1:8001/api/v1/control/status")


def collect_clock_observability(*, status_url: str | None = None, timeout_s: float = 1.0) -> dict[str, Any]:
    url = status_url or control_status_url()
    snapshot: dict[str, Any] = {
        "source": "control_status",
        "status_url": url,
        "available": False,
    }
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            status = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        snapshot["error"] = str(exc)
        return snapshot

    time_sync_status = status.get("time_sync_status") if isinstance(status.get("time_sync_status"), dict) else {}
    clock_status = status.get("clock_status") if isinstance(status.get("clock_status"), dict) else None
    if clock_status is None and isinstance(time_sync_status, dict):
        clock_status = time_sync_status.get("clock_status") if isinstance(time_sync_status.get("clock_status"), dict) else None
    snapshot["available"] = True
    snapshot["clock_status"] = clock_status or {}
    snapshot["clock_status_fields"] = sorted((clock_status or {}).keys())
    snapshot["time_sync_status"] = time_sync_status
    return snapshot


def attach_clock_observability(report: dict[str, Any], clock_observability: dict[str, Any] | None) -> dict[str, Any]:
    if not clock_observability:
        return report
    report["clock_observability"] = clock_observability
    clock_status = clock_observability.get("clock_status")
    if isinstance(clock_status, dict) and clock_status:
        report["clock_status"] = clock_status
    return report


def write_report(report: dict[str, Any], *, report_format: str, report_path: str | None) -> None:
    text = json.dumps(report, indent=2, sort_keys=True) if report_format == "json" else render_text_report(report)
    print(text)
    if report_path:
        out = Path(report_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")


def render_text_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in sorted(report):
        value = report[key]
        if isinstance(value, dict):
            for subkey in sorted(value):
                lines.append(f"{key}.{subkey}={value[subkey]}")
        else:
            lines.append(f"{key}={value}")
    return "\n".join(lines)
