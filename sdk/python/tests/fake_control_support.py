"""Fake Control for SDK registration tests: in-memory registrations over a real Unix socket."""
from __future__ import annotations

import json
import os
import socket
import threading
import time

SLOT_SIZE = 256
SLOT_COUNT = 64
MISSING_REGISTRATION = "status requires an existing authenticated registration"


class RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def _record(self, level: str, *args, **kwargs) -> None:
        message = kwargs.get("message", args[-1] if args else "")
        with self._lock:
            self.records.append((level, str(message)))

    def debug(self, *args, **kwargs) -> None:
        self._record("debug", *args, **kwargs)

    def info(self, *args, **kwargs) -> None:
        self._record("info", *args, **kwargs)

    def warning(self, *args, **kwargs) -> None:
        self._record("warning", *args, **kwargs)

    def error(self, *args, **kwargs) -> None:
        self._record("error", *args, **kwargs)

    def messages(self, level: str) -> list[str]:
        with self._lock:
            return [message for record_level, message in self.records if record_level == level]


class FakeControl:
    """In-memory Control: registrations vanish on restart, like the real backend's in-memory state."""

    def __init__(self, socket_path: str, pool_name: str) -> None:
        self.socket_path = socket_path
        self.pool_name = pool_name
        self.requests: list[dict] = []
        self.registrations: dict[str, dict] = {}
        self.generation = 0
        # A registration reply held this long (a slow Control); register_started is set when one arrives.
        self.register_delay_s = 0.0
        self.register_started = threading.Event()
        # Refusal reasons answered, in order, to the next registrations.
        self.registration_refusals: list[str] = []
        self._lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self.start()

    def start(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.socket_path)
        listener.listen(16)
        listener.settimeout(0.1)
        self._listener = listener
        self._thread = threading.Thread(target=self._serve, args=(listener,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        with self._lock:
            self.registrations.clear()
            self.generation += 1

    def requests_since(self, index: int) -> list[dict]:
        with self._lock:
            return list(self.requests[index:])

    def request_count(self) -> int:
        with self._lock:
            return len(self.requests)

    def _serve(self, listener: socket.socket) -> None:
        while self._listener is listener:
            try:
                connection, _ = listener.accept()
            except (socket.timeout, OSError):
                continue
            with connection:
                connection.settimeout(2.0)
                chunks = []
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                request = json.loads(b"".join(chunks))
                if request.get("cmd") == "register_subsystem" and self.register_delay_s:
                    self.register_started.set()
                    time.sleep(self.register_delay_s)
                try:
                    connection.sendall(json.dumps(self._handle(request)).encode())
                except OSError:
                    pass

    def _handle(self, request: dict) -> dict:
        with self._lock:
            request = dict(request, _generation=self.generation)
            self.requests.append(request)
            command = request.get("cmd")
            if command == "register_subsystem" and self.registration_refusals:
                reason = self.registration_refusals.pop(0)
                return {"status": "error", "error": f"subsystem registration refused ({reason})", "reason": reason}
            if command == "register_subsystem":
                self.registrations[request["subsystem"]] = request
                return {"res": "ok", "topic_buffer_pool_name": self.pool_name,
                        "topic_buffer_pool_slot_size": SLOT_SIZE, "topic_buffer_pool_slot_count": SLOT_COUNT}
            if command == "update_subsystem_status":
                status = request.get("param") or {}
                current = self.registrations.get(status.get("subsystem"))
                if current is None:
                    return {"status": "error", "error": MISSING_REGISTRATION}
                if current.get("session_id") != status.get("session_id"):
                    return {"status": "error", "error": "subsystem status caller does not own the registration"}
                return {"ok": True, "res": "ok"}
            if command in ("request_publish_slots", "register_subscription"):
                if request.get("subsystem") not in self.registrations:
                    return {"status": "error", "error": "operation requires an existing authenticated registration"}
                if command == "request_publish_slots":
                    return {"res": "ok", "publish_slot_index": 4 + self.generation, "queue_depth": 1,
                            "entry_slot_count": 1, "subscribers": []}
                return {"res": "ok", "publishers": []}
            if command == "clock_status":
                return {"res": "ok"}
            if command == "inform_subsystem_down":
                self.registrations.pop(request.get("subsystem"), None)
                return {"res": "ok"}
            return {"status": "error", "error": f"unsupported {command}"}
