from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def launch(label: str, command: list[str], env: dict[str, str], *, cwd: Path | None = None) -> subprocess.Popen:
    print(f"[robot-cleaner] start {label}: {' '.join(command)}", flush=True)
    proc = subprocess.Popen(command, cwd=cwd or ROOT.parents[1], env=env)
    time.sleep(0.35)
    if proc.poll() is not None:
        raise RuntimeError(f"{label} exited early with code {proc.returncode}")
    return proc


def terminate_all(processes: list[tuple[str, subprocess.Popen]]) -> None:
    for label, proc in reversed(processes):
        if proc.poll() is not None:
            continue
        print(f"[robot-cleaner] stop {label}", flush=True)
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3.0
    for _label, proc in processes:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the provided robot cleaner simulation world.")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter used for tutorial subsystems.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for both the frontend and simulator.")
    parser.add_argument("--frontend-port", type=int, default=8091, help="Local frontend HTTP port.")
    parser.add_argument("--simulator-port", type=int, default=8092, help="Provided simulator API port.")
    parser.add_argument(
        "--gnss-fixture",
        default=None,
        help="Path to the explicit GNSS geographic origin, clock, and uncertainty fixture.",
    )
    parser.add_argument("--no-frontend", action="store_true", help="Do not start the static frontend server.")
    args = parser.parse_args()

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    processes: list[tuple[str, subprocess.Popen]] = []

    try:
        simulator_command = [
            args.python,
            str(ROOT / "simulator" / "arena_server.py"),
            "--host",
            args.host,
            "--port",
            str(args.simulator_port),
            "--quiet",
        ]
        if args.gnss_fixture is not None:
            simulator_command.extend(("--gnss-fixture", args.gnss_fixture))
        processes.append(
            (
                "simulator",
                launch(
                    "simulator",
                    simulator_command,
                    env,
                    cwd=ROOT,
                ),
            )
        )

        if not args.no_frontend:
            frontend = ROOT / "frontend"
            processes.append(
                (
                    "frontend",
                    launch(
                        "frontend",
                        [
                            args.python,
                            "-m",
                            "http.server",
                            str(args.frontend_port),
                            "--bind",
                            args.host,
                        ],
                        {**env, "PYTHONUNBUFFERED": "1"},
                        cwd=frontend,
                    ),
                )
            )
            display_host = "<runtime-host>" if args.host in {"0.0.0.0", "::"} else args.host
            open_url = f"http://{display_host}:{args.frontend_port}/"
            print(f"[robot-cleaner] open {open_url}", flush=True)
            print("[robot-cleaner] start robot subsystem packages from another terminal", flush=True)
            print("[robot-cleaner] press Ctrl-C to stop the simulation world", flush=True)

        while True:
            for label, proc in processes:
                if proc.poll() is not None:
                    raise RuntimeError(f"{label} exited with code {proc.returncode}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("[robot-cleaner] interrupted", flush=True)
    finally:
        terminate_all(processes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
