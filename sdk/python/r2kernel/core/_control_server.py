"""NativeControlClient local handler server: accept loops, connection loop and socket helpers."""

from __future__ import annotations

import os
import grp
import socket
import time
import threading
from typing import Any

from ._control_shm import (
    ControlShmProtocolError,
    encode_error_response as encode_shm_error_response,
    send_control_shm_frame,
)
from ._control_shm_region import CONTROL_SHM_MAX_CHANNELS
from ._control_shm_server import ControlShmServerConnection


DEFAULT_RUNTIME_IPC_SOCKET_MODE = 0o660


def runtime_ipc_socket_mode() -> int:
    """Mode of the local Control handler socket from R2KERNEL_RUNTIME_IPC_SOCKET_MODE.

    Unset or blank means 0660 (owner and IPC group). Otherwise one to four
    octal digits; anything else raises ``ValueError``. The C++ SDK applies the
    same rule.
    """

    text = os.environ.get("R2KERNEL_RUNTIME_IPC_SOCKET_MODE", "").strip()
    if not text:
        return DEFAULT_RUNTIME_IPC_SOCKET_MODE
    if len(text) > 4 or any(ch not in "01234567" for ch in text):
        raise ValueError(f"R2KERNEL_RUNTIME_IPC_SOCKET_MODE must be an octal file mode: {text}")
    return int(text, 8)


class _LocalServerMixin:
    """Local handler server loops, control worker configuration and socket helpers of NativeControlClient."""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _local_server_loop(self) -> None:
        if os.name == "nt":
            return
        assert self._local_endpoint is not None
        self._cleanup_local_endpoint()
        self._new_local_endpoint()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listen_sock:
            listen_sock.bind(self._local_endpoint)
            self._apply_local_ipc_socket_policy(self._local_endpoint)
            listen_sock.listen(16)
            listen_sock.settimeout(0.2)
            self._server_ready.set()
            while not self._server_stop.is_set():
                try:
                    conn, _ = listen_sock.accept()
                except socket.timeout:
                    continue
                except Exception:
                    if self._server_stop.is_set():
                        break
                    continue
                thread = threading.Thread(
                    target=self._local_connection_loop,
                    args=(conn,),
                    daemon=True,
                )
                with self._server_connection_threads_lock:
                    self._server_connection_threads.add(thread)
                thread.start()
        self._cleanup_local_endpoint()

    def configure_control_worker(self, policy: str, priority: int, cpus: list[int]) -> None:
        """Set the actual provider connection-worker profile before admission."""
        policies = {"OTHER": os.SCHED_OTHER, "FIFO": os.SCHED_FIFO, "RR": os.SCHED_RR}
        normalized = str(policy).upper().removeprefix("SCHED_")
        if normalized not in policies:
            raise ValueError("Control worker policy must be OTHER, FIFO or RR")
        scheduler = policies[normalized]
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError("Control worker priority must be an integer")
        if not os.sched_get_priority_min(scheduler) <= priority <= os.sched_get_priority_max(scheduler):
            raise ValueError("Control worker priority is outside the policy range")
        if not cpus or any(isinstance(cpu, bool) or not isinstance(cpu, int) or cpu < 0 for cpu in cpus):
            raise ValueError("Control worker cpus must be a nonempty list of CPU indices")
        with self._server_connection_threads_lock:
            if self._server_connection_threads:
                raise RuntimeError("Control worker profile cannot change while lanes are active")
            self._control_worker_profile = (scheduler, priority, frozenset(cpus))
            self._control_worker_readbacks = []

    def control_worker_status(self) -> list[dict[str, Any]]:
        with self._server_connection_threads_lock:
            return [dict(item, cpus=list(item["cpus"])) for item in getattr(self, "_control_worker_readbacks", [])]

    def _configure_current_control_worker(self) -> None:
        profile = getattr(self, "_control_worker_profile", None)
        if profile is None:
            return
        scheduler, priority, cpus = profile
        os.sched_setaffinity(0, cpus)
        os.sched_setscheduler(0, scheduler, os.sched_param(priority))
        actual_policy = os.sched_getscheduler(0)
        actual_priority = os.sched_getparam(0).sched_priority
        actual_cpus = os.sched_getaffinity(0)
        if (actual_policy, actual_priority, actual_cpus) != (scheduler, priority, cpus):
            raise RuntimeError("Control worker profile readback does not match the requested profile")
        with self._server_connection_threads_lock:
            if len(self._control_worker_readbacks) >= CONTROL_SHM_MAX_CHANNELS:
                self._control_worker_readbacks.pop(0)
            self._control_worker_readbacks.append({
                "tid": threading.get_native_id(), "policy": actual_policy,
                "priority": actual_priority, "cpus": sorted(actual_cpus),
                "ready": True, "error": None, "phase": "profile_applied",
            })

    def _local_connection_loop(self, conn: socket.socket) -> None:
        current = threading.current_thread()
        control_shm_connection = None
        try:
            with conn:
                try:
                    self._configure_current_control_worker()
                except Exception as exc:
                    profile = getattr(self, "_control_worker_profile", None)
                    if profile is not None:
                        with self._server_connection_threads_lock:
                            if len(self._control_worker_readbacks) >= CONTROL_SHM_MAX_CHANNELS:
                                self._control_worker_readbacks.pop(0)
                            self._control_worker_readbacks.append({
                                "tid": threading.get_native_id(), "policy": profile[0],
                                "priority": profile[1], "cpus": sorted(profile[2]),
                                "ready": False, "error": str(exc), "phase": "profile_failed",
                            })
                    conn.settimeout(1.0)
                    send_control_shm_frame(conn, encode_shm_error_response(str(exc)))
                    return
                conn.settimeout(1.0)
                manager_verified = False

                def authenticate_manager(deadline: float) -> None:
                    nonlocal manager_verified
                    if manager_verified or not getattr(self, "_managed_peer_auth_required", True):
                        return
                    from .resource import verify_manager_peer_connection
                    verify_manager_peer_connection(
                        conn, getattr(self, "_managed_peer_auth_socket", ""), deadline=deadline,
                    )
                    manager_verified = True

                control_shm_connection = ControlShmServerConnection(
                    self._control_shm_policy,
                    self._control_shm_budget,
                    self._control_shm_counters,
                )
                while not self._server_stop.is_set():
                    try:
                        request = control_shm_connection.receive_request(
                            conn, authenticate=authenticate_manager if not manager_verified else None,
                        )
                    except socket.timeout:
                        # Handler connections are persistent. A quiet interface must not lose its
                        # channel while another operation is using the subsystem.
                        continue
                    except Exception as exc:
                        try:
                            control_shm_connection.send_error(conn, str(exc))
                        except Exception:
                            pass
                        break
                    if request is None:
                        break
                    if request.get("kind") == "prepare":
                        continue
                    if not self._serve_local_handler_request(
                        conn, control_shm_connection, request, manager_verified,
                    ):
                        break
        finally:
            if control_shm_connection is not None:
                control_shm_connection.close()
            with self._server_connection_threads_lock:
                self._server_connection_threads.discard(current)

    def _serve_local_handler_request(
        self,
        conn: socket.socket,
        control_shm_connection: ControlShmServerConnection,
        request: dict[str, Any],
        manager_verified: bool,
    ) -> bool:
        """Run the handler for one v5 request and send its reply; ``False`` closes the connection."""
        control_shm_response: tuple[int, int, bytes] | None = None
        control_shm_error: str | None = "handler unavailable"
        handler = self._handler
        cost_enabled = self._control_cost_metrics.enabled
        handler_started = None
        handler_elapsed_ns = None
        payload_size = None
        outcome = "handler_unavailable"
        if request.get("kind") == "request" and handler is not None:
            try:
                sender = str(request.get("sender") or "")
                if sender.startswith(("__r2kernel_authority_", "__r2kernel_host_")) and not manager_verified:
                    raise ControlShmProtocolError("Authenticated context requires a verified manager connection")
                payload = bytes(request.get("payload") or b"")
                payload_size = len(payload)
                handler_started = time.perf_counter_ns() if cost_enabled else None
                result = handler(
                    {
                        "sender": sender,
                        "_manager_peer_verified": manager_verified,
                        "topic": str(request.get("topic") or ""),
                        "ts": int(request.get("timestamp_ns") or 0),
                        "period_ms": float(request.get("period_ms") or 0.0),
                        "deadline_ms": float(request.get("deadline_ms") or 0.0),
                        "payload": payload,
                    }
                ) or {}
                if handler_started is not None:
                    handler_elapsed_ns = time.perf_counter_ns() - handler_started
                control_shm_response = (
                    int(result.get("status", 0)),
                    int(result.get("ts", self._now_ns())),
                    bytes(result.get("payload") or b""),
                )
                control_shm_error = None
                outcome = "ok" if control_shm_response[0] == 0 else "handler_status_error"
            except Exception as exc:
                self._control_shm_counters.record_error(exc)
                control_shm_error = str(exc)
                if handler_started is not None:
                    handler_elapsed_ns = time.perf_counter_ns() - handler_started
                outcome = "handler_exception"
        try:
            if control_shm_error is not None:
                control_shm_connection.send_error(conn, control_shm_error)
            else:
                assert control_shm_response is not None
                control_shm_connection.send_response(
                    conn,
                    status=control_shm_response[0],
                    timestamp_ns=control_shm_response[1],
                    payload=control_shm_response[2],
                )
        except Exception as exc:
            self._control_shm_counters.record_error(exc)
            outcome = "local_response_send_error"
        finally:
            if cost_enabled:
                self._control_cost_metrics.record({
                    "phase": "python_local_handler",
                    "target": self._identity or "",
                    "topic": request.get("topic"),
                    "request_timestamp_ns": request.get("timestamp_ns"),
                    "payload_bytes": payload_size,
                    "handler_ns": handler_elapsed_ns,
                    "outcome": outcome,
                })
        return outcome != "local_response_send_error"

    def _apply_local_ipc_socket_policy(self, path: str) -> None:
        """Give the local Control handler socket its IPC group (when it exists) and mode."""
        group = os.environ.get("R2KERNEL_RUNTIME_IPC_GROUP", "r2kernel").strip()
        if group:
            try:
                gid = grp.getgrnam(group).gr_gid
            except KeyError:
                gid = -1
            if gid >= 0:
                os.chown(path, -1, gid)
        os.chmod(path, runtime_ipc_socket_mode())
