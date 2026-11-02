from __future__ import annotations

import json
import ctypes
import inspect
import os
import socket
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .topic_buffer import TopicBufferPool, SEQ_READY
from r2kernel.helper.logger import coerce_logger

from . import _keywords as KW
from .constants import LOG_ID_SHM
from .control_client import control_socket_path
from .lock_backend import get_lock_backend, LockBackend, LockHandle
from ._event_registration import _EventRegistrationMixin
from . import _notify_endpoints
from .event_transport import _BUFFER_TOO_SMALL


# Native receive wait per call; stop() waits for the receiver at most this long plus one margin before it
# may close the native client the receiver is using.
_RUST_RECV_TIMEOUT_MS = 500
_RUST_RECV_STOP_MARGIN_S = 0.5


@dataclass(frozen=True)
class EventTimingMetadata:
    observed_at_sync_ns: int
    published_at_sync_ns: int
    received_at_sync_ns: int


class EventClient(_EventRegistrationMixin):
    """Shared-memory event client."""

    def __init__(
        self,
        *,
        subsystem_name: str,
        logger: Any | None = None,
        control_host: str | None = None,
        control_udp_port: int | None = None,
        session_id: str | None = None,
    ):
        del control_host, control_udp_port
        self._subsystem_name = subsystem_name
        self._logger = coerce_logger(logger, default_name="r2kernel_client")
        self._session_id = (
            session_id
            or os.getenv("R2KERNEL_SUBSYSTEM_SESSION_ID")
            or f"{subsystem_name}-{os.getpid()}-{uuid.uuid4().hex}"
        )
        self._pid = os.getpid()
        self._lease_id = os.getenv("R2KERNEL_RESOURCE_LEASE_ID") or None
        self._down_sent = False
        self._lock_backend: LockBackend = get_lock_backend()
        self._prefer_rust = os.getenv("R2KERNEL_FORCE_PYTHON") != "1"
        self._core_mode: str | None = None
        self._rust_core = None
        self._rust_event_handle = None
        self._rust_recv_thread: threading.Thread | None = None
        self._rust_recv_stop = threading.Event()

        # Broker control channel
        self._notify_socket: socket.socket | None = None
        self._notify_endpoint: str | None = None
        self._setup_notify_endpoint()
        self._notify_thread: threading.Thread | None = None
        self._notify_stop = threading.Event()
        self._startup_retry_count = max(1, int(os.getenv("R2KERNEL_STARTUP_RETRY_COUNT", "20")))
        self._startup_retry_delay_s = max(0.01, float(os.getenv("R2KERNEL_STARTUP_RETRY_DELAY_MS", "100")) / 1000.0)
        self._startup_request_timeout_s = max(
            0.1,
            float(os.getenv("R2KERNEL_STARTUP_REQUEST_TIMEOUT_MS", "2000")) / 1000.0,
        )
        self._startup_barrier_cv = threading.Condition()

        # Shared memory
        self._buffer_pool: TopicBufferPool | None = None
        self._publish_slots: dict[str, int] = {}
        self._publish_queue_depths: dict[str, int] = {}
        self._publish_entry_slot_counts: dict[str, int] = {}
        self._topic_max_sizes: dict[str, int] = {}

        # Publish state
        self._publish_topic_generations: dict[str, int] = {}
        self._publish_locks: dict[str, dict[str, LockHandle]] = {}
        env_startup_barrier_ms = os.getenv("R2KERNEL_EVENT_STARTUP_BARRIER_MS")
        self._startup_barrier_env_override = env_startup_barrier_ms is not None
        self._startup_barrier_ms = (
            max(0, int(env_startup_barrier_ms))
            if env_startup_barrier_ms is not None
            else 0
        )
        self._startup_barrier_done: set[str] = set()

        # Subscribe state
        self._subscribe_slots: dict[tuple[str, str], tuple[int, int, int, Callable[[bytes, int], None], str | None]] = {}
        self._subscribe_topic_generations: dict[tuple[str, str], int] = {}
        self._rust_last_event_ts: dict[tuple[str, str], int] = {}
        self._topic_handlers: dict[str, Callable[[bytes, int], None]] = {}
        self._lifecycle_transition_handler: Callable[[dict[str, Any]], None] | None = None
        self._max_subscribe_payload_size = 1024
        self._subscription_threads: dict[tuple[str, str], threading.Thread] = {}
        from .resource import managed_peer_authentication_required
        self._manager_peer_auth_required = managed_peer_authentication_required()
        self._manager_peer_auth_socket = ""
        self._last_registration_response: dict[str, Any] | None = None
        # Accepted Event requests, repeated on a new registration after Control lost this one.
        self._publish_requests: dict[str, tuple[str, int, str, int | None, int | None]] = {}
        self._subscription_requests: dict[str, tuple[str, int, str]] = {}
        self._event_pool_identity: tuple[int, int] | None = None
        self._event_pool_replaced = False

        self._ensure_share_memory()

    def _setup_notify_endpoint(self) -> None:
        base_dir = os.getenv("R2KERNEL_CONTROL_NOTIFY_DIR") or os.path.dirname(control_socket_path())
        if not base_dir or not os.access(base_dir, os.W_OK):
            base_dir = tempfile.gettempdir()
        os.makedirs(base_dir, exist_ok=True)
        _notify_endpoints.remove_stale(base_dir, self._logger)
        endpoint = os.path.join(base_dir, _notify_endpoints.endpoint_name(os.getpid(), uuid.uuid4().hex[:12]))
        try:
            os.unlink(endpoint)
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(endpoint)
        sock.listen(16)
        sock.settimeout(0.5)
        try:
            ipc_group = os.environ.get("R2KERNEL_RUNTIME_IPC_GROUP")
            if ipc_group is not None:
                import grp
                if not ipc_group.strip():
                    raise RuntimeError("Configured runtime IPC group must not be empty")
                group = ipc_group.strip()
                gid = int(group) if group.isascii() and group.isdecimal() else grp.getgrnam(group).gr_gid
                if gid < 0 or gid > 0xFFFFFFFF:
                    raise RuntimeError("Configured runtime IPC group ID is invalid")
                os.chown(endpoint, -1, gid)
            mode = 0o660 if ipc_group is not None or base_dir != tempfile.gettempdir() else 0o666
            os.chmod(endpoint, mode)
        except Exception as error:
            sock.close()
            os.unlink(endpoint)
            raise RuntimeError("Failed to publish notification socket with configured IPC permissions") from error
        self._notify_socket = sock
        self._notify_endpoint = endpoint
        _notify_endpoints.register(endpoint)

    def set_lifecycle_transition_handler(self, handler: Callable[[dict[str, Any]], None] | None) -> None:
        self._lifecycle_transition_handler = handler

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        if self._rust_recv_stop.is_set():
            # stop() closed the notify endpoint and told the backend the subsystem is down; a notify loop
            # started now would wait on a closed socket and never deliver an Event.
            raise RuntimeError(
                "Event transport cannot be restarted after stop(); create a new subsystem client to receive Events again"
            )
        if not self._notify_thread or not self._notify_thread.is_alive():
            self._notify_stop.clear()
            endpoint = f"unix://{self._notify_endpoint}"
            self._logger.debug(
                LOG_ID_SHM,
                f"Starting notify loop on {endpoint}",
            )
            self._notify_thread = threading.Thread(target=self._notify_loop, daemon=True)
            self._notify_thread.start()

    def stop(self):
        self._notify_stop.set()
        # Set before the notify thread is joined: a notification still being processed must not start a
        # new receiver (_ensure_rust_recv_thread refuses once this is set; the client is not restartable).
        self._rust_recv_stop.set()
        self._wake_notify_loop()
        current_thread = threading.current_thread()
        notify = self._notify_thread
        if notify and notify is not current_thread:
            notify.join(timeout=0.5)
        notify_running = bool(notify and notify is not current_thread and notify.is_alive())
        if self._core_mode == "rust" and self._rust_event_handle:
            receiver = self._rust_recv_thread
            if receiver and receiver is not current_thread:
                receiver.join(timeout=_RUST_RECV_TIMEOUT_MS / 1000.0 + _RUST_RECV_STOP_MARGIN_S)
            receiver_running = bool(receiver and receiver is not current_thread and receiver.is_alive())
            if receiver_running or notify_running:
                # Closing frees the native client while the receiver may still be inside
                # r2kernel_event_client_recv_ex or its handler, or the notify thread inside a native
                # subscribe on it (use-after-free); leave it open instead.
                if receiver_running:
                    still_running, thread, waited_s = (
                        "Event receiver", "receiver", _RUST_RECV_TIMEOUT_MS / 1000.0 + _RUST_RECV_STOP_MARGIN_S)
                else:
                    still_running, thread, waited_s = "Event notify thread", "notify", 0.5
                self._logger.error(
                    LOG_ID_SHM,
                    f"{still_running} did not stop within {waited_s:.1f}s (handler still running); "
                    "the native event client handle is retained until process exit because closing it would "
                    f"free memory the {thread} thread is still using",
                )
            else:
                try:
                    self._rust_core.lib.r2kernel_event_client_close(self._rust_event_handle)
                except Exception:
                    pass
            self._rust_event_handle = None
        else:
            for th in list(self._subscription_threads.values()):
                if th is not current_thread:
                    th.join(timeout=0.5)
        self.inform_subsystem_down()
        self._close_notify_endpoint()

    @property
    def session_id(self) -> str:
        return self._session_id

    # ------------------------------------------------------------------
    # Event message API
    # ------------------------------------------------------------------
    def publish_event(
        self,
        *,
        topic: str,
        payload: bytes,
        timestamp: int,
        priority: int = 5,
    ) -> bool:
        publish_slot_index = self._publish_slots.get(topic, None)
        if publish_slot_index is None:
            self._logger.error(LOG_ID_SHM, f"No allocation for topic {topic}; call prepare_publish_topic()")
            return False
        max_size = self._topic_max_sizes.get(topic)
        if isinstance(max_size, int) and len(payload) > max_size:
            self._logger.error(
                LOG_ID_SHM,
                f"Payload too large for {topic}: {len(payload)} > {max_size}",
            )
            return False
        next_generation = self._publish_topic_generations.setdefault(topic, 0)
        if next_generation == 0:
            self._wait_for_publish_subscribers(topic)
        entry_slot_count = self._publish_entry_slot_counts.get(topic, 1)
        queue_depth = self._publish_queue_depths.get(topic, 1)
        target_slot = publish_slot_index + (next_generation % queue_depth) * entry_slot_count
        if self._core_mode not in {"rust", "python"}:
            raise RuntimeError("Event client core mode is not set")
        ok = self._write_publish_payload(topic, target_slot, payload, next_generation, timestamp)
        if ok:
            self._publish_topic_generations[topic] += 1
            self._release_publish_locks(topic)
        return ok

    def _now_ns(self) -> int:
        try:
            if self._rust_core is not None:
                return int(self._rust_core.lib.r2kernel_clock_now_ns())
        except Exception:
            pass
        return time.time_ns()

    def publish_event_typed(
        self,
        *,
        topic: str,
        dtype: str,
        value,
        timestamp: int | None,
        priority: int = 5,
    ) -> bool:
        """Encode a primitive value with the SDK datatype codec and publish it.

        Returns ``False`` for a dtype without a primitive encoder so the caller can use its own encoder.
        """
        from r2kernel.datatype.basetype import get_encoder

        try:
            encoder = get_encoder(str(dtype).lower())
        except Exception:
            return False
        return self.publish_event(
            topic=topic,
            payload=encoder(value),
            timestamp=self._now_ns() if timestamp is None or timestamp < 0 else int(timestamp),
            priority=priority,
        )

    def subscribe_event(self, topic: str, size: int, handler: callable[[bytes], None]):
        accepts_metadata = False
        try:
            parameters = list(inspect.signature(handler).parameters.values())
            accepts_metadata = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in parameters) or len(
                [
                    p
                    for p in parameters
                    if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                ]
            ) >= 3
        except (TypeError, ValueError):
            pass

        if accepts_metadata:
            normalized_handler = handler
        else:
            normalized_handler = lambda payload, timestamp, _timing: handler(payload, timestamp)
        self._topic_handlers[str(topic)] = normalized_handler
        self._max_subscribe_payload_size = max(self._max_subscribe_payload_size, int(size))
        return bool(self._register_subscription(
            subscriber=self._subsystem_name,
            topic=topic,
            size=size,
            mode="event",
        ))

    # ------------------------------------------------------------------
    # Broker registration / slot allocation
    # ------------------------------------------------------------------
    def _wake_notify_loop(self) -> None:
        if self._notify_endpoint:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(0.05)
                    sock.connect(self._notify_endpoint)
            except Exception:
                pass

    def _close_notify_endpoint(self) -> None:
        if self._notify_socket is not None:
            try:
                self._notify_socket.close()
            except Exception:
                pass
            self._notify_socket = None
        if self._notify_endpoint:
            # A forked child closing an inherited client leaves its parent's socket in place.
            if _notify_endpoints.unregister(self._notify_endpoint):
                try:
                    os.unlink(self._notify_endpoint)
                except FileNotFoundError:
                    pass
                except Exception:
                    pass
            self._notify_endpoint = None

    def _write_publish_payload(
        self,
        topic: str,
        slot: int,
        payload: bytes,
        generation: int,
        timestamp: int,
    ) -> bool:
        try:
            self._buffer_pool.publish(slot, payload, generation, timestamp, self._now_ns())
            return True
        except Exception as exc:
            self._logger.error(LOG_ID_SHM, f"Failed to write payload at offset {slot}: {exc}")
            return False

    def _add_event_subscription(
        self,
        topic: str,
        publisher: str,
        slot: int,
        handler: Callable[[bytes, int], None],
        lock_name: str | None = None,
        queue_depth: int = 1,
        entry_slot_count: int = 1,
        replay_existing: bool = False,
    ):
        """Subscribe to one publisher's Event slot.

        Only a subscription that Control confirms preceded this publisher
        (``replay_existing`` in ``notify_publisher``) consumes the publisher's
        bounded initial buffer; a late join starts at the latest Event, as in
        the C++ SDK and the Rust bench client.
        """
        if not publisher or slot is None:
            return
        self._logger.debug(
            LOG_ID_SHM,
            f"Add event subscription topic={topic} publisher={publisher} slot={slot} lock={lock_name}",
        )
        if self._core_mode == "rust":
            # A native subscription that cannot be established is reported at error level and the client
            # stays in the native mode: switching to the Python mode here would split publish and receive
            # across two transports while the native receiver keeps running.
            if not lock_name:
                self._logger.error(
                    LOG_ID_SHM,
                    f"Native Event subscription for topic {topic} from publisher {publisher} arrived without a "
                    "notification lock; Events of this subscription are not received",
                )
                return
            try:
                subscribe_name = "r2kernel_event_client_subscribe_ex"
                if not replay_existing and hasattr(self._rust_core.lib, "r2kernel_event_client_subscribe_latest_ex"):
                    subscribe_name = "r2kernel_event_client_subscribe_latest_ex"
                if hasattr(self._rust_core.lib, subscribe_name):
                    rc = getattr(self._rust_core.lib, subscribe_name)(
                        self._rust_event_handle,
                        str(topic).encode("utf-8"),
                        str(publisher).encode("utf-8"),
                        int(slot),
                        max(1, int(queue_depth)),
                        max(1, int(entry_slot_count)),
                        (lock_name or "").encode("utf-8"),
                    )
                else:
                    rc = self._rust_core.lib.r2kernel_event_client_subscribe(
                        self._rust_event_handle,
                        str(topic).encode("utf-8"),
                        str(publisher).encode("utf-8"),
                        int(slot),
                        (lock_name or "").encode("utf-8"),
                    )
                if rc == 0:
                    self._logger.debug(
                        LOG_ID_SHM,
                        f"Rust event subscription ready topic={topic} publisher={publisher} slot={slot}",
                    )
                    self._ensure_rust_recv_thread()
                else:
                    self._logger.error(
                        LOG_ID_SHM,
                        f"Native Event subscription failed for topic {topic} from publisher {publisher} (rc={rc}); "
                        "Events of this subscription are not received",
                    )
            except Exception as exc:
                self._logger.error(
                    LOG_ID_SHM,
                    f"Native Event subscription failed for topic {topic} from publisher {publisher}: {exc}; "
                    "Events of this subscription are not received",
                )
            return
        key = (str(topic), str(publisher))
        if key in self._subscribe_slots:
            # Update slot/lock for restarted publishers and reset generation.
            prev_slot, prev_depth, prev_entry_slots, _prev_handler, prev_lock = self._subscribe_slots[key]
            self._subscribe_slots[key] = (
                int(slot),
                max(1, int(queue_depth)),
                max(1, int(entry_slot_count)),
                handler,
                lock_name,
            )
            if (
                int(prev_slot) != int(slot)
                or prev_lock != lock_name
                or int(prev_depth) != max(1, int(queue_depth))
                or int(prev_entry_slots) != max(1, int(entry_slot_count))
            ):
                self._subscribe_topic_generations[key] = self._initial_generation(
                    slot, queue_depth, entry_slot_count, replay_existing,
                )
            return
        self._subscribe_slots[key] = (
            int(slot),
            max(1, int(queue_depth)),
            max(1, int(entry_slot_count)),
            handler,
            lock_name,
        )
        self._subscribe_topic_generations[key] = self._initial_generation(
            slot, queue_depth, entry_slot_count, replay_existing,
        )
        th = threading.Thread(target=self._event_subscription_worker, args=(key,), daemon=True)
        self._subscription_threads[key] = th
        th.start()

    def _initial_generation(self, slot: int, queue_depth: int, entry_slot_count: int, replay_existing: bool) -> int:
        """The receive cursor of a new subscription: -1 replays the bounded buffer, else the latest Event."""
        if replay_existing or self._buffer_pool is None:
            return -1
        latest = -1
        depth = max(1, int(queue_depth))
        for depth_index in range(depth):
            snapshot = self._buffer_pool.snapshot(
                int(slot) + depth_index * max(1, int(entry_slot_count)), include_payload=False,
            )
            if snapshot.seq == SEQ_READY and snapshot.timestamp > 0:
                latest = max(latest, snapshot.generation)
        return latest

    def _ensure_rust_recv_thread(self):
        # Never restart the receiver once stop() began. stop() sets the flag before it reads
        # _rust_recv_thread, and the thread is assigned before it starts, so a receiver started in that
        # window sees the flag on its first loop check and exits without receiving.
        if self._rust_recv_stop.is_set():
            return
        if self._rust_recv_thread and self._rust_recv_thread.is_alive():
            return
        self._rust_recv_thread = threading.Thread(target=self._rust_recv_loop, daemon=True)
        self._rust_recv_thread.start()

    def _maybe_bootstrap_rust_subscription(
        self,
        topic: str,
        publisher: str,
        slot: int,
        handler: Callable[[bytes, int], None],
    ):
        if publisher != "stamped_stream_runtime" or self._buffer_pool is None:
            return
        try:
            snapshot = self._buffer_pool.snapshot(slot)
            if snapshot is None or snapshot.timestamp <= 0:
                return
            timestamp = snapshot.timestamp
            payload = snapshot.payload
            published_at = snapshot.published_timestamp
            self._rust_last_event_ts[(str(topic), str(publisher))] = timestamp
            self._logger.debug(
                LOG_ID_SHM,
                f"Rust bootstrap recv topic={topic} publisher={publisher} payload={len(payload)} ts={timestamp}",
            )
            handler(
                payload,
                timestamp,
                EventTimingMetadata(timestamp, published_at, self._now_ns()),
            )
        except Exception as exc:
            self._logger.debug(
                LOG_ID_SHM,
                f"Rust bootstrap recv skipped topic={topic} publisher={publisher}: {exc}",
            )

    def _maybe_bootstrap_publish_lock(
        self,
        topic: str,
        subscriber: str,
        lock: LockHandle,
    ):
        if subscriber not in {"stamped_stream_runtime", "__ws_event_bridge__"} or self._buffer_pool is None:
            return
        slot = self._publish_slots.get(str(topic))
        if slot is None:
            return
        try:
            observed = self._buffer_pool.snapshot(int(slot), include_payload=False)
            timestamp = int(observed.timestamp) if observed is not None else 0
            if timestamp <= 0:
                return
            self._lock_backend.release(lock)
            self._logger.debug(
                LOG_ID_SHM,
                f"Publish bootstrap notify topic={topic} subscriber={subscriber} slot={slot} ts={timestamp}",
            )
        except Exception as exc:
            self._logger.debug(
                LOG_ID_SHM,
                f"Publish bootstrap notify skipped topic={topic} subscriber={subscriber}: {exc}",
            )

    def _wait_for_publish_subscribers(self, topic: str):
        if self._startup_barrier_ms <= 0:
            return
        topic = str(topic)
        deadline = time.monotonic() + (float(self._startup_barrier_ms) / 1000.0)
        with self._startup_barrier_cv:
            if topic in self._startup_barrier_done:
                return
            if self._has_publish_locks(topic):
                self._logger.debug(LOG_ID_SHM, f"Publish startup barrier satisfied topic={topic}")
                self._startup_barrier_done.add(topic)
                return

        if self._notify_thread and self._notify_thread.is_alive():
            with self._startup_barrier_cv:
                remaining_s = deadline - time.monotonic()
                while remaining_s > 0 and not self._has_publish_locks(topic):
                    self._startup_barrier_cv.wait(timeout=remaining_s)
                    remaining_s = deadline - time.monotonic()
                if self._has_publish_locks(topic):
                    self._logger.debug(LOG_ID_SHM, f"Publish startup barrier satisfied topic={topic}")
                else:
                    self._logger.debug(
                        LOG_ID_SHM,
                        f"Publish startup barrier timed out topic={topic} timeout_ms={self._startup_barrier_ms}",
                    )
                self._startup_barrier_done.add(topic)
            return

        while time.monotonic() < deadline:
            remaining_s = deadline - time.monotonic()
            self._wait_for_notification(timeout_s=min(max(remaining_s, 0.0), 0.5))
            with self._startup_barrier_cv:
                if self._has_publish_locks(topic):
                    self._logger.debug(LOG_ID_SHM, f"Publish startup barrier satisfied topic={topic}")
                    self._startup_barrier_done.add(topic)
                    return

        self._logger.debug(
            LOG_ID_SHM,
            f"Publish startup barrier timed out topic={topic} timeout_ms={self._startup_barrier_ms}",
        )
        with self._startup_barrier_cv:
            self._startup_barrier_done.add(topic)

    def _wait_for_notification(self, *, timeout_s: float) -> None:
        msg = self._read_notify_message(timeout_s=timeout_s)
        if msg is None:
            return
        self._process_notify_message(msg)

    def _read_notify_message(self, *, timeout_s: float) -> dict[str, Any] | None:
        sock = self._notify_socket
        if sock is None:
            return None
        previous_timeout = sock.gettimeout()
        try:
            sock.settimeout(max(0.0, timeout_s))
            conn, _ = sock.accept()
            with conn:
                if getattr(self, "_manager_peer_auth_required", True):
                    from .resource import verify_manager_peer_connection
                    verify_manager_peer_connection(conn, getattr(self, "_manager_peer_auth_socket", ""))
                data = b""
                while True:
                    chunk = conn.recv(8192)
                    if not chunk:
                        break
                    data += chunk
        except socket.timeout:
            return None
        except Exception:
            return None
        finally:
            try:
                sock.settimeout(previous_timeout)
            except Exception:
                pass
        try:
            msg = json.loads(data.decode("utf-8"))
        except Exception:
            return None
        return msg if isinstance(msg, dict) else None

    def _rust_recv_loop(self):
        if not self._rust_event_handle:
            return
        lib = self._rust_core.lib
        topic_buf = (ctypes.c_uint8 * 256)()
        pub_buf = (ctypes.c_uint8 * 256)()
        payload_buf_cap = max(1024, int(self._max_subscribe_payload_size))
        payload_buf = (ctypes.c_uint8 * payload_buf_cap)()
        timeout_ms = _RUST_RECV_TIMEOUT_MS
        while not self._rust_recv_stop.is_set():
            needed_cap = max(1024, int(self._max_subscribe_payload_size))
            if needed_cap > payload_buf_cap:
                payload_buf_cap = needed_cap
                payload_buf = (ctypes.c_uint8 * payload_buf_cap)()
            topic_len = ctypes.c_size_t()
            pub_len = ctypes.c_size_t()
            payload_len = ctypes.c_size_t()
            ts = ctypes.c_int64()
            published_ts = ctypes.c_int64()
            received_ts = ctypes.c_int64()
            rc = lib.r2kernel_event_client_recv_ex(
                self._rust_event_handle,
                timeout_ms,
                topic_buf,
                ctypes.sizeof(topic_buf),
                ctypes.byref(topic_len),
                pub_buf,
                ctypes.sizeof(pub_buf),
                ctypes.byref(pub_len),
                ctypes.byref(ts),
                ctypes.byref(published_ts),
                ctypes.byref(received_ts),
                payload_buf,
                ctypes.sizeof(payload_buf),
                ctypes.byref(payload_len),
            )
            if rc != 0:
                if rc == _BUFFER_TOO_SMALL:
                    topic_buf, pub_buf, payload_buf = self._grown_event_receive_buffers(
                        topic_buf, pub_buf, payload_buf, topic_len.value, pub_len.value, payload_len.value)
                    payload_buf_cap = len(payload_buf)
                continue
            topic = bytes(topic_buf[: topic_len.value]).decode("utf-8", errors="ignore")
            publisher = bytes(pub_buf[: pub_len.value]).decode("utf-8", errors="ignore")
            payload = bytes(payload_buf[: payload_len.value])
            key = (str(topic), str(publisher))
            last_ts = self._rust_last_event_ts.get(key)
            if last_ts is not None and int(ts.value) == int(last_ts):
                continue
            self._logger.debug(
                LOG_ID_SHM,
                f"Rust recv topic={topic} publisher={publisher} payload={len(payload)} ts={int(ts.value)}",
            )
            handler = self._topic_handlers.get(str(topic))
            if handler:
                self._rust_last_event_ts[key] = int(ts.value)
                handler(
                    payload,
                    int(ts.value),
                    EventTimingMetadata(
                        int(ts.value),
                        int(published_ts.value),
                        int(received_ts.value),
                    ),
                )

    def _grown_event_receive_buffers(self, topic_buf, pub_buf, payload_buf, topic_len, pub_len, payload_len):
        """Cold path of the receive loop: Core kept an Event that did not fit and reported every required
        length. Each buffer is sized to that length once, so the next receive (the single retry) gets the
        same Event; there is no growth loop. A report without a larger length is an explicit error."""
        buffers = [topic_buf, pub_buf, payload_buf]
        required = [int(topic_len), int(pub_len), int(payload_len)]
        if not any(length > len(buffer) for buffer, length in zip(buffers, required)):
            self._logger.error(
                LOG_ID_SHM,
                "Event receive buffer too small, but Core reported no larger length "
                f"(topic={required[0]}, publisher={required[1]}, payload={required[2]}); the Event was not "
                "delivered",
            )
            return topic_buf, pub_buf, payload_buf
        grown = [buffer if length <= len(buffer) else (ctypes.c_uint8 * length)()
                 for buffer, length in zip(buffers, required)]
        self._logger.debug(
            LOG_ID_SHM,
            f"Event receive buffers resized to topic={len(grown[0])} publisher={len(grown[1])} "
            f"payload={len(grown[2])} bytes",
        )
        return tuple(grown)

    def _event_subscription_worker(self, key: tuple[str, str]):
        info = self._subscribe_slots.get(key)
        if not info:
            return
        slot_index, queue_depth, entry_slot_count, handler, lock_name = info
        lock = self._open_lock(lock_name) if lock_name else None
        while not self._notify_stop.is_set():
            current = self._subscribe_slots.get(key)
            if not current:
                return
            current_slot, cur_queue_depth, cur_entry_slot_count, cur_handler, cur_lock_name = current
            if (
                int(current_slot) != int(slot_index)
                or int(cur_queue_depth) != int(queue_depth)
                or int(cur_entry_slot_count) != int(entry_slot_count)
                or cur_lock_name != lock_name
            ):
                slot_index = int(current_slot)
                queue_depth = max(1, int(cur_queue_depth))
                entry_slot_count = max(1, int(cur_entry_slot_count))
                lock_name = cur_lock_name
                if lock is not None:
                    try:
                        self._lock_backend.close(lock)
                    except Exception:
                        pass
                lock = self._open_lock(lock_name) if lock_name else None
            if lock is not None:
                acquired = self._lock_backend.wait(lock, timeout_s=0.5)
                if not acquired:
                    continue
            current_generation = self._subscribe_topic_generations.setdefault(key, -1)
            latest_generation = -1
            for depth_index in range(max(1, int(queue_depth))):
                candidate_slot = int(slot_index) + depth_index * max(1, int(entry_slot_count))
                snapshot = self._buffer_pool.snapshot(candidate_slot, include_payload=False)
                if snapshot.seq != SEQ_READY or snapshot.timestamp <= 0:
                    continue
                latest_generation = max(latest_generation, snapshot.generation)
            if latest_generation < 0:
                continue
            if latest_generation < current_generation:
                self._subscribe_topic_generations[key] = -1
                current_generation = -1
            if latest_generation <= current_generation:
                continue

            first_generation = max(current_generation + 1, latest_generation - max(1, int(queue_depth)) + 1)
            for generation in range(first_generation, latest_generation + 1):
                candidate_slot = int(slot_index) + (generation % max(1, int(queue_depth))) * max(1, int(entry_slot_count))
                snapshot = self._buffer_pool.snapshot(candidate_slot, expected_generation=generation)
                if snapshot is None or snapshot.timestamp <= 0:
                    continue
                timestamp = snapshot.timestamp
                value = snapshot.payload
                published_at = snapshot.published_timestamp
                # Reinitialization can replace a prepared handler while keeping
                # the same broker slot/lock. Match the Rust receive path's current
                # topic lookup rather than retaining the previous codec attempt.
                current_handler = self._topic_handlers.get(key[0])
                if current_handler is None:
                    self._logger.error(LOG_ID_SHM, f"Event subscription handler is no longer registered: {key[0]}; retiring receiver")
                    return
                current_handler(
                    value,
                    timestamp,
                    EventTimingMetadata(timestamp, published_at, self._now_ns()),
                )
                self._subscribe_topic_generations[key] = generation

    def _process_notify_message(self, msg: dict[str, Any]) -> None:
        if msg.get(KW.COMMAND) == KW.LIFECYCLE_TRANSITION:
            handler = self._lifecycle_transition_handler
            if handler is None:
                self._logger.warning(LOG_ID_SHM, "Lifecycle transition requested but no handler is installed")
                return
            try:
                handler(msg)
            except Exception as exc:
                self._logger.error(LOG_ID_SHM, f"Lifecycle transition handler failed: {exc}")
            return
        if msg.get(KW.COMMAND) == KW.NOTIFY_PUBLISHER:
            topic = msg.get(KW.TOPIC_NAME)
            publisher = msg.get(KW.PUBLISHER_NAME)
            slot = msg.get(KW.PUBLISH_SLOT_INDEX)
            queue_depth = max(1, int(msg.get(KW.QUEUE_DEPTH) or 1))
            entry_slot_count = max(1, int(msg.get(KW.ENTRY_SLOT_COUNT) or 1))
            lock_name = msg.get(KW.LOCK_NAME)
            replay_existing = msg.get("replay_existing") is True
            self._logger.debug(
                LOG_ID_SHM,
                f"Notify publisher topic={topic} publisher={publisher} slot={slot} lock={lock_name} "
                f"replay_existing={replay_existing}",
            )
            handler = self._topic_handlers.get(str(topic))
            if handler:
                self._add_event_subscription(
                    topic,
                    publisher,
                    slot,
                    handler,
                    lock_name,
                    queue_depth=queue_depth,
                    entry_slot_count=entry_slot_count,
                    replay_existing=replay_existing,
                )
            return
        if msg.get(KW.COMMAND) == KW.NOTIFY_SUBSCRIBER:
            topic = msg.get(KW.TOPIC_NAME)
            subscriber = msg.get(KW.SUBSYSTEM_NAME) or msg.get("subscriber")
            lock_name = msg.get(KW.LOCK_NAME)
            self._logger.debug(
                LOG_ID_SHM,
                f"Notify subscriber topic={topic} subscriber={subscriber} lock={lock_name}",
            )
            if topic and lock_name and subscriber:
                lock = self._open_lock(lock_name)
                if lock:
                    with self._startup_barrier_cv:
                        locks = self._publish_locks.setdefault(str(topic), {})
                        previous_lock = locks.get(str(subscriber))
                        locks[str(subscriber)] = lock
                        # Closed under the condition that _release_publish_locks posts under, so the
                        # publisher thread cannot be inside a post on this handle.
                        if previous_lock is not None:
                            self._lock_backend.close(previous_lock)
                        self._maybe_bootstrap_publish_lock(str(topic), str(subscriber), lock)
                        self._startup_barrier_cv.notify_all()

    def _notify_loop(self):
        self._logger.debug(LOG_ID_SHM, "Notify loop started")
        while not self._notify_stop.is_set():
            msg = self._read_notify_message(timeout_s=0.5)
            if msg is None:
                continue
            self._process_notify_message(msg)
