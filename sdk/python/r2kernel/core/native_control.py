from __future__ import annotations

import atexit
import os
import socket
import time
import threading
from typing import Any, Callable

from r2kernel.ffi import R2KernelCore
from r2kernel.helper.logger import coerce_logger
from ._control_shm import ControlShmProtocolError
from ._control_shm_region import (
    CONTROL_SHM_MAX_CHANNELS,
    CONTROL_SHM_MAX_MAPPED_BYTES,
    CONTROL_SHM_MAX_OWNED_BYTES,
    CONTROL_SHM_MAX_REGION_BYTES,
    CONTROL_SHM_MIN_REGION_BYTES,
    ControlShmBudget,
    ControlShmPolicy,
)
from ._control_shm_transport import (
    ControlShmChannelRegistry,
    ControlShmCounters,
    configure_policy_from_registration,
    control_shm_summary,
    emit_control_shm_summary,
)
from .control_cost_metrics import ControlCostMetrics
from . import _notify_endpoints
from ._control_managed import _ManagedControlMixin
from ._control_messaging import _MessagingMixin
from ._control_server import _LocalServerMixin

# The only local handler protocol Control registers (forwarded_shm_v5 payloads).
LOCAL_HANDLER_PROTOCOL = "binary_shm_v5"


class NativeControlClient(
    _ManagedControlMixin,
    _MessagingMixin,
    _LocalServerMixin,
):
    """Control client/server backed by the native core."""

    def __init__(
        self,
        *,
        logger: Any | None = None,
        identity: str | None = None,
        control_host: str = "127.0.0.1",
        control_udp_port: int | None = None,
        session_id: str | None = None,
    ):
        del control_host, control_udp_port
        self._logger = coerce_logger(logger, default_name="r2kernel_control")
        self._core = R2KernelCore()
        self._lib = self._core.lib
        from .resource import managed_peer_authentication_required
        self._managed_peer_auth_required = managed_peer_authentication_required()
        self._managed_peer_auth_socket = ""
        self._identity = identity
        self._session_id = session_id
        self._client = self._create_client(identity)
        self._server_port: int | None = None
        self._server_thread: threading.Thread | None = None
        self._server_connection_threads: set[threading.Thread] = set()
        self._server_connection_threads_lock = threading.Lock()
        self._pending_target_guard = threading.Lock()
        self._pending_target_reservations: set[str] = set()
        self._server_stop = threading.Event()
        self._server_ready = threading.Event()
        self._handler: Callable[[dict], dict] | None = None
        self._ready: dict[str, bool] = {}
        self._encoder_cache: dict[str, Any] = {}
        self._decoder_cache: dict[str, Any] = {}
        self._control_cost_metrics = ControlCostMetrics()
        self._pending_cost_context: dict[str, dict[str, Any]] = {}
        self._control_shm_summary_logged = False
        self._control_shared_buffer_slot_size = CONTROL_SHM_MAX_REGION_BYTES
        self._control_shm_policy = ControlShmPolicy(
            minimum_region_bytes=CONTROL_SHM_MIN_REGION_BYTES,
            maximum_channels=CONTROL_SHM_MAX_CHANNELS,
            maximum_owned_bytes=CONTROL_SHM_MAX_OWNED_BYTES,
            maximum_mapped_bytes=CONTROL_SHM_MAX_MAPPED_BYTES,
            maximum_region_bytes=self._control_shared_buffer_slot_size,
        )
        self._control_shm_budget = ControlShmBudget(self._control_shm_policy)
        self._control_shm_counters = ControlShmCounters()
        self._control_shm_channel_registry = ControlShmChannelRegistry(
            self._control_shm_policy,
            self._control_shm_budget,
            self._control_shm_counters,
        )
        self._control_shm_negotiated = False
        self._local_control_endpoint: str | None = None
        self._requested_host = "0.0.0.0"
        self._requested_port = 0
        self._local_endpoint: str | None = None
        self._routes: dict[str, tuple[str, int]] = {}
        self._pending_responses: dict[str, dict] = {}
        self._last_transport_error: str | None = None
        self._startup_retry_count = max(1, int(os.getenv("R2KERNEL_STARTUP_RETRY_COUNT", "20")))
        self._startup_retry_delay_s = max(0.01, float(os.getenv("R2KERNEL_STARTUP_RETRY_DELAY_MS", "100")) / 1000.0)
        atexit.register(self._close_control_resources)

    def _now_ns(self) -> int:
        try:
            return int(self._lib.r2kernel_clock_now_ns())
        except Exception:
            return time.time_ns()

    def _get_encoder(self, dtype: str):
        encoder = self._encoder_cache.get(dtype)
        if encoder is not None:
            return encoder
        from r2kernel.datatype.basetype import get_encoder

        encoder = get_encoder(dtype)
        self._encoder_cache[dtype] = encoder
        return encoder

    def _get_decoder(self, dtype: str):
        decoder = self._decoder_cache.get(dtype)
        if decoder is not None:
            return decoder
        from r2kernel.datatype.basetype import get_decoder

        decoder = get_decoder(dtype)
        self._decoder_cache[dtype] = decoder
        return decoder

    def _create_client(self, identity: str | None):
        import ctypes

        out = ctypes.c_void_p()
        identity_arg = None if not identity else str(identity).encode("utf-8")
        rc = self._lib.r2kernel_control_client_create(identity_arg, ctypes.byref(out))
        if rc != 0 or not out.value:
            raise RuntimeError("Failed to create Control client (native)")
        return out

    # ------------------------------------------------------------------
    # Server
    # ------------------------------------------------------------------
    @property
    def port(self) -> int | None:
        return self._server_port

    def configure_from_control_registration(self, response: dict[str, Any] | None) -> None:
        if not response:
            return
        required = response.get("manager_peer_auth_required")
        if isinstance(required, bool):
            from .resource import managed_peer_authentication_required
            self._managed_peer_auth_required = managed_peer_authentication_required()
            if self._managed_peer_auth_required and not required:
                raise RuntimeError("Production SDK cannot accept disabled manager authentication")
            self._managed_peer_auth_socket = str(response.get("resource_service_socket") or "")
        slot_size = response.get("control_shared_buffer_slot_size")
        control_endpoint = response.get("control_local_endpoint")
        if isinstance(slot_size, int) and slot_size > 0:
            self._control_shared_buffer_slot_size = slot_size
        if isinstance(control_endpoint, str) and control_endpoint:
            self._local_control_endpoint = control_endpoint
        policy = configure_policy_from_registration(
            response,
            self._control_shared_buffer_slot_size,
        )
        if policy != self._control_shm_policy:
            active_channels = self._control_shm_budget.snapshot()["channels"]
            if active_channels:
                raise ControlShmProtocolError(
                    "runtime local Control shared-memory policy changed while SDK channels are active"
                )
            budget = ControlShmBudget(policy)
            self._control_shm_channel_registry.reconfigure(policy, budget)
            self._control_shm_policy = policy
            self._control_shm_budget = budget
        self._control_shm_negotiated = True

    def control_shm_statistics(self) -> dict[str, Any]:
        """Return a snapshot of local Control v5 counters and current budgets."""
        return control_shm_summary(self._control_shm_counters, self._control_shm_budget)

    def _require_control_shm_negotiated(self) -> None:
        if not getattr(self, "_control_shm_negotiated", False):
            raise ControlShmProtocolError(
                "runtime did not negotiate forwarded_shm_v5 local Control policy"
            )

    def _managed_control_ready(self) -> bool:
        return bool(self._local_control_endpoint)

    def _register_managed_handler(self) -> int:
        if os.name == "nt":
            raise RuntimeError("control-managed Control is not implemented on Windows yet")
        if not self._local_endpoint:
            self._new_local_endpoint()
        response = self._exchange_control_request(
            {
                "cmd": "register_control_handler",
                "subsystem": self._identity or "control_subsystem",
                "endpoint": self._local_endpoint,
                "local_protocol": LOCAL_HANDLER_PROTOCOL,
                "bind_host": self._requested_host or "0.0.0.0",
                "port": int(self._requested_port),
                "session_id": self._session_id,
            },
            timeout_s=1.0,
            retries=self._startup_retry_count,
            retry_delay_s=self._startup_retry_delay_s,
        ) or {}
        if response.get("res") != "ok":
            raise RuntimeError(f"failed to register control-managed Control handler: {response}")
        self.configure_from_control_registration(response)
        assigned_port = int(response.get("control_port") or 0)
        if assigned_port <= 0:
            raise RuntimeError(f"invalid control-managed Control port: {response}")
        return assigned_port

    def bind(self, port: int | None = None, host: str = "0.0.0.0") -> int:
        self._requested_host = str(host or "0.0.0.0")
        self._requested_port = int(port or 0)
        return int(self._server_port or self._requested_port)

    def start_server(self, handler: Callable[[dict], dict]) -> None:
        self._handler = handler
        if self._server_thread and self._server_thread.is_alive():
            return
        if os.name == "nt":
            raise RuntimeError("control-managed Control is not implemented on Windows yet")
        self._cleanup_local_endpoint()
        self._new_local_endpoint()
        self._server_stop.clear()
        self._server_ready.clear()
        self._server_thread = threading.Thread(target=self._local_server_loop, daemon=True)
        self._server_thread.start()
        if not self._server_ready.wait(timeout=1.0):
            raise RuntimeError("failed to start local Control handler endpoint")
        # Registered from the caller's thread: the socket goes at exit or SIGTERM like the notify endpoints.
        _notify_endpoints.register(self._local_endpoint)
        self._server_port = self._register_managed_handler()

    def ensure_control_registered(self) -> bool:
        if not self._server_thread or not self._server_thread.is_alive() or self._handler is None:
            return False
        try:
            assigned_port = self._register_managed_handler()
        except Exception as exc:
            self._logger.debug("Control", f"control handler re-registration failed: {exc}")
            return False
        if assigned_port != self._server_port:
            self._logger.info(
                "Control",
                f"control handler re-registered on relay port {assigned_port}",
            )
        self._server_port = assigned_port
        return True

    def stop_server(self) -> None:
        self._server_stop.set()
        if self._local_endpoint and os.name != "nt":
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(0.05)
                    sock.connect(self._local_endpoint)
            except Exception:
                pass
        if self._server_thread:
            self._server_thread.join(timeout=0.5)
        self._server_thread = None
        with self._server_connection_threads_lock:
            connection_threads = list(self._server_connection_threads)
        for thread in connection_threads:
            thread.join(timeout=1.1)
        self._cleanup_local_endpoint()
        self._server_port = None
        self._control_cost_metrics.flush()

    # ------------------------------------------------------------------
    # Client
    # ------------------------------------------------------------------
    def connect(self, name: str, host: str, port: int, identity: str | None = None) -> None:
        _ = identity
        route = (str(host), int(port))
        if not self._route_is_valid(*route):
            raise ValueError(f"invalid Control route: {route!r}")
        if self._routes.get(name) == route:
            return
        def update_route() -> None:
            self._routes[name] = route
            self._ready[name] = False
        self._control_shm_channel_registry.close_target(name, update_route)
        if self._logger:
            self._logger.debug("Control", f"Control client route name={name} tcp://{host}:{int(port)}")

    def handshake(self, target: str, timeout_ms: int = 1000) -> bool:
        return self._managed_handshake(target, timeout_ms)

    def start(self) -> None:
        self._control_shm_channel_registry.reopen()

    def stop(self) -> None:
        self.stop_server()
        self._close_control_resources()
        self._routes.clear()
        self._pending_responses.clear()
        reservations = getattr(self, "_pending_target_reservations", None)
        if reservations is not None:
            with self._pending_target_guard:
                reservations.clear()
        self._pending_cost_context.clear()
        self._ready.clear()

    def _close_control_resources(self) -> None:
        try:
            self._control_shm_channel_registry.close_all()
        finally:
            if not getattr(self, "_control_shm_summary_logged", False):
                emit_control_shm_summary(self._control_shm_counters, self._control_shm_budget)
            self._control_shm_summary_logged = True
