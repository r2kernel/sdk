from __future__ import annotations
import time
from typing import TYPE_CHECKING, Any, Callable

from . import _keywords as KW
from r2kernel.helper.logger import coerce_logger

from .clock import configure_clock_from_status
from .native_control import NativeControlClient
from .event_client import EventClient
from .constants import LOCAL_HOST
from .resource import BrokeredFdResult, BrokeredSocketResult, ResourceBrokerClient

if TYPE_CHECKING:
    import asyncio
    import ssl


class R2KernelClient:
    """Facade for the Event and Control transports."""

    def __init__(
        self,
        *,
        subsystem_name: str,
        logger: Any | None = None,
        control_host: str = LOCAL_HOST,
        control_udp_port: int | None = None,
        session_id: str | None = None,
    ):
        self._subsystem_name = subsystem_name
        self._logger = coerce_logger(logger, default_name="r2kernel_client")
        self._event = EventClient(
            subsystem_name=subsystem_name,
            logger=self._logger,
            control_host=control_host,
            control_udp_port=control_udp_port,
            session_id=session_id,
        )
        self._control = NativeControlClient(
            logger=self._logger,
            identity=subsystem_name,
            control_host=control_host,
            control_udp_port=control_udp_port,
            session_id=self._event.session_id,
        )
        self._apply_control_registration(self._event.last_registration_response)
        self._log_core_status()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        self.start_event_transport()

    def stop(self):
        self._control.stop()
        self.stop_event_transport()

    @property
    def session_id(self) -> str:
        return self._event.session_id

    @property
    def notify_endpoint(self) -> str | None:
        return getattr(self._event, "_notify_endpoint", None)

    def inform_subsystem_down(self) -> None:
        self._event.inform_subsystem_down()

    @property
    def down_reported(self) -> bool:
        return self._event.down_reported

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def _log_core_status(self) -> None:
        event_core = getattr(self._event, "_core_mode", "unknown")
        message = f"core_backend event={event_core} control=native"
        self._logger.info("R2KernelClient", message)

    # ------------------------------------------------------------------
    # Event message API
    # ------------------------------------------------------------------
    def start_event_transport(self) -> None:
        self._control.start()
        self._event.start()

    def stop_event_transport(self) -> None:
        self._event.stop()

    def set_lifecycle_transition_handler(self, handler: Callable[[dict[str, Any]], None] | None) -> None:
        self._event.set_lifecycle_transition_handler(handler)

    def publish_event(
        self,
        *,
        topic: str,
        payload: bytes,
        timestamp: int,
        priority: int = 5,
    ) -> bool:
        return self._event.publish_event(
            topic=topic,
            payload=payload,
            timestamp=timestamp,
            priority=priority,
        )

    def publish_event_typed(
        self,
        *,
        topic: str,
        dtype: str,
        value,
        timestamp: int | None,
        priority: int = 5,
    ) -> bool:
        return self._event.publish_event_typed(
            topic=topic,
            dtype=dtype,
            value=value,
            timestamp=timestamp,
            priority=priority,
        )

    def subscribe_event(self, topic: str, size: int, handler: callable[[bytes], None]):
        return self._event.subscribe_event(topic, size, handler)

    # ------------------------------------------------------------------
    # Control transport
    # ------------------------------------------------------------------
    def get_control_port(self) -> int | None:
        return self._control.port

    def bind_control(self, port: int | None = None, host: str = "0.0.0.0") -> int:
        return self._control.bind(port=port, host=host)

    def start_control_server(self, handler: Callable[..., dict]) -> None:
        self._control.start_server(handler=handler)

    def ensure_control_server_registered(self) -> bool:
        return self._control.ensure_control_registered()

    def stop_control_server(self) -> None:
        self._control.stop_server()

    def connect_control(self, name: str, host: str, port: int) -> None:
        self._control.connect(name=name, host=host, port=port, identity=self._subsystem_name)

    def open_udp_socket(
        self,
        *,
        interface: str,
        bind_host: str = "0.0.0.0",
        bind_port: int = 0,
    ) -> BrokeredSocketResult:
        return ResourceBrokerClient().open_udp_socket(
            interface=interface,
            bind_host=bind_host,
            bind_port=bind_port,
        )

    def open_uplink_tcp(
        self,
        host: str,
        port: int,
        *,
        timeout_s: float = 10.0,
        tls: bool = False,
        server_hostname: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> BrokeredSocketResult:
        return ResourceBrokerClient().open_uplink_tcp(
            host,
            port,
            timeout_s=timeout_s,
            tls=tls,
            server_hostname=server_hostname,
            ssl_context=ssl_context,
        )

    async def open_uplink_connection(
        self,
        host: str,
        port: int,
        *,
        timeout_s: float = 10.0,
        tls: bool = False,
        server_hostname: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        limit: int = 2**16,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await ResourceBrokerClient().open_uplink_connection(
            host,
            port,
            timeout_s=timeout_s,
            tls=tls,
            server_hostname=server_hostname,
            ssl_context=ssl_context,
            limit=limit,
        )

    def open_tcp_socket(
        self,
        *,
        interface: str,
        bind_host: str = "0.0.0.0",
        bind_port: int = 0,
        connect_host: str | None = None,
        connect_port: int | None = None,
        listen: bool = False,
        backlog: int = 128,
    ) -> BrokeredSocketResult:
        return ResourceBrokerClient().open_tcp_socket(
            interface=interface,
            bind_host=bind_host,
            bind_port=bind_port,
            connect_host=connect_host,
            connect_port=connect_port,
            listen=listen,
            backlog=backlog,
        )

    def open_frontend_tcp_listener(
        self,
        *,
        kind: str = "api",
        bind_host: str | None = None,
        bind_port: int | None = None,
        backlog: int = 128,
    ) -> BrokeredSocketResult:
        return ResourceBrokerClient().open_frontend_tcp_listener(
            kind=kind,
            bind_host=bind_host,
            bind_port=bind_port,
            backlog=backlog,
        )

    def open_can_socket(
        self,
        *,
        interface: str,
        receive_own_messages: bool = False,
        can_fd_frames: bool = False,
        nonblocking: bool = True,
    ) -> BrokeredSocketResult:
        return ResourceBrokerClient().open_can_socket(
            interface=interface,
            receive_own_messages=receive_own_messages,
            can_fd_frames=can_fd_frames,
            nonblocking=nonblocking,
        )

    def open_device(
        self,
        path: str,
        *,
        read: bool = True,
        write: bool = True,
        nonblocking: bool = True,
    ) -> BrokeredFdResult:
        return ResourceBrokerClient().open_device(
            path,
            read=read,
            write=write,
            nonblocking=nonblocking,
        )

    def handshake_control(self, target: str, timeout_ms: int = 1000) -> bool:
        return self._control.handshake(target=target, timeout_ms=timeout_ms)

    def prepare_control(
        self, target: str, topic: str, request_capacity: int, response_capacity: int,
        timeout_ms: int = 1000, result_ready: bool = False, *,
        _deadline: float | None = None,
    ) -> bool:
        return self._control.prepare_control(
            target, topic, request_capacity, response_capacity, timeout_ms, result_ready,
            _deadline=_deadline,
        )

    def configure_control_worker(self, policy: str, priority: int, cpus: list[int]) -> None:
        self._control.configure_control_worker(policy, priority, cpus)

    def control_worker_status(self) -> list[dict[str, Any]]:
        return self._control.control_worker_status()

    def send_control(
        self,
        *,
        target: str,
        topic: str,
        type: str,
        value,
        timestamp: int,
        period_ms: float,
        deadline_ms: float,
        priority: int | None = None,
    ) -> bool:
        return self._control.send(
            target=target,
            topic=topic,
            type=type,
            value=value,
            timestamp=timestamp,
            period_ms=period_ms,
            deadline_ms=deadline_ms,
            priority=priority,
        )

    def recv_control(self, target: str, type: str, timeout_ms: int = 0) -> dict | None:
        return self._control.recv(target=target, type=type, timeout_ms=timeout_ms)


    def transact_control(
        self,
        *,
        target: str,
        topic: str,
        type: str,
        value,
        period_ms: float,
        deadline_ms: float,
        last_tick_ns: int | None = None,
        priority: int | None = None,
        _deadline: float | None = None,
    ) -> dict:
        return self._control.transact(
            target=target,
            topic=topic,
            type=type,
            value=value,
            period_ms=period_ms,
            deadline_ms=deadline_ms,
            last_tick_ns=last_tick_ns,
            priority=priority,
            **({"_deadline": _deadline} if _deadline is not None else {}),
        )

    def transact_control_result(
        self, *, target: str, topic: str, type: str, value,
        period_ms: float, deadline_ms: float,
        last_tick_ns: int | None = None, priority: int | None = None,
        _deadline: float | None = None,
    ) -> dict:
        return self._control.transact_result_ready(
            target=target, topic=topic, type=type, value=value,
            period_ms=period_ms, deadline_ms=deadline_ms,
            last_tick_ns=last_tick_ns, priority=priority,
            **({"_deadline": _deadline} if _deadline is not None else {}),
        )

    def wait_control_reclaimed(self, target: str, topic: str, timeout_ms: int = 1000) -> None:
        self._control.wait_control_reclaimed(target, topic, timeout_ms)

    # ------------------------------------------------------------------
    # Control registration / slot allocation
    # ------------------------------------------------------------------
    def register_subsystem(self) -> dict | None:
        response = self._event.register_subsystem()
        self._apply_control_registration(response)
        return response

    def reregister_subsystem(self) -> dict[str, Any]:
        """Register again once with the same session (after Control lost the registration); raises on failure."""
        response = self._event.reregister_subsystem()
        self._apply_control_registration(response)
        return response

    def restore_event_registrations(self) -> int:
        return self._event.restore_event_registrations()

    def request_publish_slots(
        self,
        subsystem: str,
        topic: str,
        size: int,
        mode: str = "event",
        priority: int | None = None,
        queue_depth: int | None = None,
    ) -> bool:
        return self._event.request_publish_slots(subsystem, topic, size, mode, priority, queue_depth)

    def register_publish_slot(
        self,
        *,
        topic: str,
        size: int,
        slot_index: int,
        lock_infos: list[dict] | None = None,
        mode: str = "event",
        queue_depth: int = 1,
        entry_slot_count: int = 1,
    ) -> None:
        self._event.register_publish_slot(
            topic=topic,
            size=size,
            slot_index=slot_index,
            lock_infos=lock_infos,
            mode=mode,
            queue_depth=queue_depth,
            entry_slot_count=entry_slot_count,
        )

    def resolve_control_target(
        self, target: str, *, deadline: float | None = None,
    ) -> dict[str, Any] | None:
        timeout_s = 1.0 if deadline is None else deadline - time.monotonic()
        if timeout_s <= 0:
            raise TimeoutError("Control target resolution deadline exceeded")
        response = self._event.request_control(
            {
                KW.COMMAND: KW.RESOLVE_CONTROL_TARGET,
                KW.SUBSYSTEM_NAME: target,
            },
            timeout_s=timeout_s,
            retries=3 if deadline is None else 1,
            **({"deadline": deadline} if deadline is not None else {}),
        ) or {}
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Control target resolution deadline exceeded")
        if response.get("res") != "ok":
            return None
        return response

    def refresh_clock_status(self, *, timeout_s: float = 1.0) -> dict[str, Any] | None:
        response = self._event.request_control(
            {
                KW.COMMAND: KW.REQUEST_CLOCK_STATUS,
            },
            timeout_s=timeout_s,
            retries=1,
        ) or {}
        if response.get("res") != "ok":
            return None
        self._apply_control_registration(response)
        clock_status = response.get("clock_status")
        return clock_status if isinstance(clock_status, dict) else response

    def _apply_control_registration(self, response: dict | None) -> None:
        if not response:
            return
        try:
            configure_clock_from_status(response)
        except Exception as exc:
            self._logger.debug("Clock", f"Failed to apply control clock config: {exc}")
        if not hasattr(self._control, "configure_from_control_registration"):
            return
        try:
            self._control.configure_from_control_registration(response)
        except Exception as exc:
            self._logger.debug("Control", f"Failed to apply Control config: {exc}")
