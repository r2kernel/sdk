from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "subsystems").is_dir() and (parent / "simulator").is_dir())
_LIB = _ROOT / "lib"
_SIMULATOR = _ROOT / "simulator"
for _path in (_LIB, _SIMULATOR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from arena import ArenaSimulator  # noqa: E402


class ArenaServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        quiet: bool = False,
        radar_sample_period_ms: float = 100.0,
        radar_history_capacity: int = 64,
        gnss_fixture_path: Path | None = None,
    ) -> None:
        super().__init__(server_address, ArenaRequestHandler)
        self.arena = ArenaSimulator(
            now_ns=time.time_ns,
            radar_sample_period_ms=radar_sample_period_ms,
            radar_history_capacity=radar_history_capacity,
            gnss_fixture_path=gnss_fixture_path,
        )
        self.quiet = quiet
        self._radar_capture_stop = threading.Event()
        self._radar_capture_thread: threading.Thread | None = None
        self._gnss_capture_stop = threading.Event()
        self._gnss_capture_thread: threading.Thread | None = None

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        try:
            self._start_radar_capture()
            self._start_gnss_capture()
            super().serve_forever(poll_interval=poll_interval)
        finally:
            self._stop_capture_threads()

    def server_close(self) -> None:
        try:
            self._stop_capture_threads()
        finally:
            super().server_close()

    def _stop_capture_threads(self) -> None:
        failures: list[tuple[str, Exception]] = []
        for name, stop in (
            ("GNSS", self._stop_gnss_capture),
            ("radar", self._stop_radar_capture),
        ):
            try:
                stop()
            except Exception as exc:
                failures.append((name, exc))
        if failures:
            details = "; ".join(f"{name}: {error}" for name, error in failures)
            raise RuntimeError(f"simulator capture shutdown did not complete cleanly: {details}") from failures[0][1]

    def _start_radar_capture(self) -> None:
        if self._radar_capture_thread is not None and self._radar_capture_thread.is_alive():
            return
        self._radar_capture_stop.clear()
        self._radar_capture_thread = threading.Thread(
            target=self._radar_capture_loop,
            name="robot-cleaner-radar-capture",
            daemon=True,
        )
        self._radar_capture_thread.start()

    def _stop_radar_capture(self) -> None:
        self._radar_capture_stop.set()
        capture_thread = self._radar_capture_thread
        if capture_thread is not None and capture_thread is not threading.current_thread():
            capture_thread.join(timeout=2.0)
            if capture_thread.is_alive():
                raise RuntimeError("radar capture thread did not stop")
        self._radar_capture_thread = None

    def _radar_capture_loop(self) -> None:
        sample_period_s = self.arena.radar_sample_period_ns / 1_000_000_000.0
        next_capture_monotonic_s = time.monotonic() + sample_period_s
        while not self._radar_capture_stop.is_set():
            delay_s = max(0.0, next_capture_monotonic_s - time.monotonic())
            if self._radar_capture_stop.wait(delay_s):
                return
            try:
                captured_sector_count = self.arena.capture_radar_sectors()
            except Exception as exc:
                self.arena.record_radar_capture_error(exc)
                print(f"[robot-cleaner-sim] radar capture failed: {exc}", file=sys.stderr, flush=True)
                return
            if captured_sector_count == 0:
                next_capture_monotonic_s = time.monotonic() + sample_period_s
            else:
                next_capture_monotonic_s += sample_period_s

    def _start_gnss_capture(self) -> None:
        if self._gnss_capture_thread is not None and self._gnss_capture_thread.is_alive():
            return
        self._gnss_capture_stop.clear()
        self._gnss_capture_thread = threading.Thread(
            target=self._gnss_capture_loop,
            name="robot-cleaner-gnss-capture",
            daemon=True,
        )
        self._gnss_capture_thread.start()

    def _stop_gnss_capture(self) -> None:
        self._gnss_capture_stop.set()
        capture_thread = self._gnss_capture_thread
        if capture_thread is not None and capture_thread is not threading.current_thread():
            capture_thread.join(timeout=2.0)
            if capture_thread.is_alive():
                raise RuntimeError("GNSS capture thread did not stop")
        self._gnss_capture_thread = None

    def _gnss_capture_loop(self) -> None:
        sample_period_s = self.arena.gnss_sample_period_ns / 1_000_000_000.0
        next_capture_monotonic_s = time.monotonic() + sample_period_s
        error_logged = False
        while not self._gnss_capture_stop.is_set():
            delay_s = max(0.0, next_capture_monotonic_s - time.monotonic())
            if self._gnss_capture_stop.wait(delay_s):
                return
            try:
                captured = self.arena.capture_gnss_sample()
            except Exception as exc:
                self.arena.record_gnss_capture_error(exc)
                if not error_logged:
                    print(f"[robot-cleaner-sim] GNSS capture failed: {exc}", file=sys.stderr, flush=True)
                    error_logged = True
                next_capture_monotonic_s = time.monotonic() + sample_period_s
                continue
            error_logged = False
            if not captured:
                next_capture_monotonic_s = time.monotonic() + sample_period_s
            else:
                next_capture_monotonic_s += sample_period_s


class ArenaRequestHandler(BaseHTTPRequestHandler):
    server: ArenaServer

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json({"ok": True, "kind": "robot_cleaner.arena"})
            return
        if self.path == "/state":
            self._send_json(self.server.arena.get_state({}))
            return
        self.send_error(404, "unknown endpoint")

    def do_POST(self) -> None:
        operation = self.path.strip("/")
        handlers = {
            "reset": self.server.arena.reset,
            "spawn_trash": self.server.arena.spawn_trash,
            "get_state": self.server.arena.get_state,
            "apply_drive": self.server.arena.apply_drive,
            "set_arm": self.server.arena.set_arm,
            "set_gripper": self.server.arena.set_gripper,
            "incinerate": self.server.arena.incinerate,
            "scan": self.server.arena.scan,
            "tick": lambda _params: self.server.arena.tick(),
            "start_snapshot": lambda _params: self.server.arena.start_snapshot(),
            "radar_cycle": self.server.arena.read_radar_cycle,
            "gnss_cycle": self.server.arena.read_gnss_cycle,
        }
        handler = handlers.get(operation)
        if handler is None:
            self.send_error(404, "unknown operation")
            return
        try:
            payload = self._read_json()
            self._send_json(handler(payload))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.send_error(400, str(exc))
        except Exception as exc:  # pragma: no cover - server-side diagnostic path.
            self.send_error(500, str(exc))

    def log_message(self, fmt: str, *args: object) -> None:
        if not self.server.quiet:
            super().log_message(fmt, *args)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request JSON must be an object")
        return value

    def _send_json(self, value: dict[str, Any]) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_cors_headers(self) -> None:
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
        self.send_header("access-control-allow-headers", "content-type")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the provided robot cleaner 2D arena simulator.")
    parser.add_argument("--host", default="127.0.0.1", help="Simulator API bind host.")
    parser.add_argument("--port", type=int, default=8092, help="Simulator API port.")
    parser.add_argument("--quiet", action="store_true", help="Suppress HTTP access logs.")
    parser.add_argument(
        "--radar-sample-period-ms",
        type=float,
        default=100.0,
        help="Integrated radar sector acquisition period in milliseconds.",
    )
    parser.add_argument(
        "--radar-history-capacity",
        type=int,
        default=64,
        help="Maximum prepared radar sectors retained for Control consumers.",
    )
    parser.add_argument(
        "--gnss-fixture",
        type=Path,
        default=None,
        help="Path to the explicit geographic origin, clock, and GNSS uncertainty fixture.",
    )
    args = parser.parse_args()

    server = ArenaServer(
        (args.host, args.port),
        quiet=args.quiet,
        radar_sample_period_ms=args.radar_sample_period_ms,
        radar_history_capacity=args.radar_history_capacity,
        gnss_fixture_path=args.gnss_fixture,
    )
    print(f"[robot-cleaner-sim] serving http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
