from __future__ import annotations

import ctypes
import json
import threading
from typing import Any

from r2kernel.helper.logger import coerce_logger
from r2kernel.ffi import R2KernelCore
from .sandbox import sandbox_socket_denials

_MODE_TO_ID = {
    "event": 1,
    "rtcs": 2,
}

_MODE_FROM_ID = {
    1: "event",
    2: "rtcs",
}

_NOT_FOUND = -2
_BUFFER_TOO_SMALL = -4
_PEER_ID_CAPACITY = 512


class EventTransport:
    """Thin wrapper of the Core federation Event transport (``r2kernel_event_transport_*``).

    The forward set (topics this transport sends to a peer) is written only by the peer's own subscription
    requests received on the native session, so a send-only user must still pump :meth:`recv_event` for
    every connected peer. When a buffer is too small, Core keeps the accepted peer or the received event and
    reports the required sizes; ``accept_peer`` and ``recv_event`` retry once with exactly those sizes (a
    payload above the caller's ``payload_cap`` is refused and stays queued for a later call).

    Threads: the Core handle is not thread-safe, so calls on it are serialized by this transport's lock.
    ``accept_peer`` and ``apply_state`` wait on the network (accept wait, handshake, outbound connects)
    without that lock and take it only to install a session or finish the apply, so they never delay
    ``recv_event(timeout_ms=0)``, ``enqueue_event`` or ``flush_n`` in other threads by those waits.
    """
    def __init__(
        self,
        *,
        local_host_id: str,
        queue_capacity: int = 1024,
        connect_timeout_ms: int = 250,
        io_timeout_ms: int = 200,
        logger: Any | None = None,
    ) -> None:
        self._logger = coerce_logger(logger, default_name="r2kernel.event_transport")
        self._core = R2KernelCore()
        self._lock = threading.RLock()
        # Serializes whole applies (begin, connects, finish) without blocking the handle lock.
        self._apply_lock = threading.Lock()
        self._io_timeout_ms = int(io_timeout_ms)
        self._handle = ctypes.c_void_p()
        with self._lock:
            rc = self._core.lib.r2kernel_event_transport_create(
                str(local_host_id).encode("utf-8"),
                int(queue_capacity),
                int(connect_timeout_ms),
                int(io_timeout_ms),
                ctypes.byref(self._handle),
            )
        if rc != 0 or not self._handle.value:
            raise RuntimeError(f"Failed to create event transport rc={rc}")

    def close(self) -> None:
        with self._lock:
            if self._handle and self._handle.value:
                self._core.lib.r2kernel_event_transport_close(self._handle)
                self._handle = ctypes.c_void_p()

    def bind(self, host: str, port: int) -> int:
        out_port = ctypes.c_uint16()
        with self._lock:
            rc = self._core.lib.r2kernel_event_transport_bind(
                self._handle,
                str(host).encode("utf-8"),
                int(port),
                ctypes.byref(out_port),
            )
        if rc != 0:
            denials = sandbox_socket_denials()
            detail = f": {'; '.join(denials)}" if denials else ""
            raise RuntimeError(f"Failed to bind event transport rc={rc}{detail}")
        return int(out_port.value)

    def apply_state(self, *, forward_topics: dict[str, str], peers: list[dict[str, Any]]) -> dict[str, Any]:
        """Apply the desired peer set and return the Core apply report.

        Each peer is ``{"peer_id", "host", "port", "should_connect", "subscriptions"}``. ``should_connect``
        true connects whenever no session exists; false is accept-only. ``subscriptions`` are this side's
        requests to the peer. ``remote_subscriptions`` is not accepted: a peer's subscriptions arrive only as
        its own native requests, and a non-empty value raises ``ValueError`` before anything is applied.

        The outbound connects run without this transport's lock; the lock is held only to start and to finish
        the apply. Applies from several threads run one at a time.

        Report: ``connected``/``disconnected`` list the peers this apply connected or disconnected;
        ``subscriptions_synced`` lists peers that acknowledged this transport's latest subscription request
        on the current session; ``subscription_ack_pending`` lists ``{peer_id, generation, pending_ms}`` still
        unacknowledged. If the report does not fit, the apply is repeated once with exactly the size
        Core reported; the returned report then describes the repeated apply and also lists, ahead of its own,
        the ``connected``/``disconnected`` transitions of the apply whose report did not fit.
        """
        for peer in peers or []:
            if isinstance(peer, dict) and peer.get("remote_subscriptions"):
                raise ValueError(
                    f"peer {peer.get('peer_id')!r}: remote_subscriptions is not accepted; the forward set is "
                    "filled only from the peer's own subscription requests on the native session"
                )
        payload = {
            "forward_topics": [
                {"topic": str(topic), "mode": str(mode)}
                for topic, mode in sorted((forward_topics or {}).items())
            ],
            "peers": peers,
        }
        encoded = json.dumps(payload).encode("utf-8")
        out_cap = max(4096, len(encoded) * 2)
        with self._apply_lock:
            rc, out_buf, out_len = self._apply_once(encoded, out_cap)
            if rc == _BUFFER_TOO_SMALL:
                required = int(out_len.value)
                if required <= out_cap:
                    raise RuntimeError(
                        f"event transport apply_state reported buffer-too-small without a larger required size "
                        f"(required={required}, capacity={out_cap})"
                    )
                out_cap = required
                rc, out_buf, out_len = self._apply_once(encoded, out_cap)
        if rc == _BUFFER_TOO_SMALL:
            raise RuntimeError(
                f"event transport apply_state report still did not fit after one retry "
                f"(required={int(out_len.value)}, capacity={out_cap})"
            )
        if rc != 0:
            raise RuntimeError(f"Failed to apply event transport state rc={rc}")
        return json.loads(bytes(out_buf[: out_len.value]).decode("utf-8"))

    def _apply_once(self, encoded: bytes, out_cap: int) -> tuple[int, Any, ctypes.c_size_t]:
        """One apply: begin under the lock, outbound connects without it, finish under the lock."""
        lib = self._core.lib
        out_len = ctypes.c_size_t()
        out_buf = (ctypes.c_uint8 * out_cap)()
        plan = ctypes.c_void_p()
        with self._lock:
            self._require_open()
            rc = lib.r2kernel_event_transport_apply_begin_json(self._handle, encoded, len(encoded),
                                                               ctypes.byref(plan))
        if rc != 0:
            return rc, out_buf, out_len
        try:
            lib.r2kernel_event_apply_plan_connect(plan)
            with self._lock:
                self._require_open()
                finishing, plan = plan, None  # apply_finish takes ownership of the plan
                rc = lib.r2kernel_event_transport_apply_finish(self._handle, finishing, out_buf,
                                                               ctypes.sizeof(out_buf), ctypes.byref(out_len))
        finally:
            if plan is not None:
                lib.r2kernel_event_apply_plan_close(plan)
        return rc, out_buf, out_len

    def _require_open(self) -> None:
        if not (self._handle and self._handle.value):
            raise RuntimeError("event transport is closed")

    def accept_peer(self, timeout_ms: int | None = None) -> str | None:
        """Accept one session and return the peer id.

        Waits at most ``timeout_ms`` for a connection (``None``: the transport's ``io_timeout_ms``; ``0``: only
        a connection already waiting), then runs its whole handshake within ``io_timeout_ms`` of the accept. The
        wait and the handshake run without this transport's lock; the lock is held only to install the session.

        Returns None when no connection arrived or its handshake did not complete in time, and when the
        connection duplicated a live session of the same peer: Core closed the duplicate by duplicate
        resolution (``R2KERNEL_EVENT_TRANSPORT_ALREADY_CONNECTED``) and the peer stays connected on its
        existing session.
        """
        if timeout_ms is not None and int(timeout_ms) < 0:
            raise ValueError(f"timeout_ms must be None or >= 0, got {timeout_ms}")
        wait_ms = self._io_timeout_ms if timeout_ms is None else int(timeout_ms)
        already_connected = 1  # R2KERNEL_EVENT_TRANSPORT_ALREADY_CONNECTED
        lib = self._core.lib
        setup = ctypes.c_void_p()
        with self._lock:
            self._require_open()
            rc = lib.r2kernel_event_transport_session_setup(self._handle, ctypes.byref(setup))
        if rc != 0:
            raise RuntimeError(f"Failed to prepare event transport accept rc={rc}")
        session = ctypes.c_void_p()
        try:
            rc = lib.r2kernel_event_session_setup_accept(setup, ctypes.c_int64(wait_ms), ctypes.byref(session))
        finally:
            lib.r2kernel_event_session_setup_close(setup)
        if rc == _NOT_FOUND:
            return None
        if rc != 0:
            raise RuntimeError(f"Failed to accept event transport peer rc={rc}")
        try:
            peer_id = self._session_peer_id(session)
            with self._lock:
                self._require_open()
                installing, session = session, None  # install_session takes ownership of the session
                rc = lib.r2kernel_event_transport_install_session(self._handle, installing)
        finally:
            if session is not None:
                lib.r2kernel_event_session_close(session)
        if rc == already_connected:
            return None
        if rc != 0:
            raise RuntimeError(f"Failed to accept event transport peer rc={rc}")
        return peer_id

    def _session_peer_id(self, session: ctypes.c_void_p) -> str:
        capacity = _PEER_ID_CAPACITY
        for attempt in range(2):
            out_len = ctypes.c_size_t()
            out_buf = (ctypes.c_uint8 * capacity)()
            rc = self._core.lib.r2kernel_event_session_peer_id(session, out_buf, ctypes.sizeof(out_buf),
                                                               ctypes.byref(out_len))
            # The session keeps its peer id on buffer-too-small; retry once with the reported size.
            if rc != _BUFFER_TOO_SMALL or attempt or int(out_len.value) <= capacity:
                break
            capacity = int(out_len.value)
        if rc == _BUFFER_TOO_SMALL:
            raise RuntimeError(
                f"accepted event transport peer id needs {int(out_len.value)} bytes and did not fit "
                f"capacity {capacity} after one retry"
            )
        if rc != 0:
            raise RuntimeError(f"Failed to read accepted event transport peer id rc={rc}")
        return bytes(out_buf[: out_len.value]).decode("utf-8")

    def enqueue_event(
        self,
        *,
        topic: str,
        publisher: str,
        priority: int,
        payload: bytes,
        timestamp_ns: int,
        mode: str,
        published_at_sync_ns: int | None = None,
    ) -> bool:
        payload = bytes(payload)
        published_at_sync_ns = (
            int(timestamp_ns)
            if published_at_sync_ns is None
            else int(published_at_sync_ns)
        )
        with self._lock:
            rc = self._core.lib.r2kernel_event_transport_enqueue_event_ex(
                self._handle,
                str(topic).encode("utf-8"),
                str(publisher).encode("utf-8"),
                int(priority),
                payload,
                len(payload),
                int(timestamp_ns),
                published_at_sync_ns,
                _MODE_TO_ID[str(mode)],
            )
        if rc == _NOT_FOUND:
            return False
        if rc != 0:
            raise RuntimeError(f"Failed to enqueue event transport event rc={rc}")
        return True

    def flush_n(self, limit: int) -> int:
        """Send at most ``limit`` queued events and return how many were sent.

        Each send ends within ``io_timeout_ms``: a peer that has not taken a whole event by then is disconnected,
        its queued events are discarded and ``RuntimeError`` is raised. No send starts once the call has run for
        ``io_timeout_ms``, so one call holds this transport's lock for at most about twice ``io_timeout_ms``.
        """
        out_flushed = ctypes.c_size_t()
        with self._lock:
            rc = self._core.lib.r2kernel_event_transport_flush_n(
                self._handle,
                int(limit),
                ctypes.byref(out_flushed),
            )
        if rc != 0:
            raise RuntimeError(f"Failed to flush event transport queue rc={rc}")
        return int(out_flushed.value)

    def recv_event(
        self,
        peer_id: str,
        payload_cap: int = 16 * 1024 * 1024,
        timeout_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """Receive one inbound event from ``peer_id`` (None when none is queued).

        Also processes the peer's subscription requests and acknowledgements, so call it for every
        connected peer even when only sending. ``timeout_ms=None`` waits up to the transport's
        ``io_timeout_ms``; ``timeout_ms=0`` does not wait on the network: it applies the frames the peer already
        delivered (at most 64 setup frames per call; the next call continues) and returns the first event or None,
        at the cost of one non-blocking socket read when nothing arrived (use it to pump send-only peers). A
        positive ``timeout_ms`` waits at most that long. The Core handle is not thread-safe, so this transport's
        lock is held for the whole call including the wait: a blocking call delays ``enqueue_event``/``flush_n``
        from other threads by up to that wait. A ``timeout_ms=0`` call holds the lock only briefly, but it first
        waits for the lock behind a concurrent ``flush_n`` (at most about twice ``io_timeout_ms``) or a concurrent
        blocking ``recv_event`` (up to that call's wait).
        A signal handled during the wait neither ends it nor closes the session; Python runs its handler
        (for example raising ``KeyboardInterrupt`` on Ctrl+C) after the wait returns, within ``timeout_ms``.
        Receive buffers are allocated outside the lock and reused across calls (no per-call payload allocation).
        When the topic or publisher buffer is too small, Core keeps the event and this call retries once with the
        reported sizes. A payload larger than ``payload_cap`` raises and the event stays queued for a call with a
        larger ``payload_cap``.
        """
        if timeout_ms is not None and int(timeout_ms) < 0:
            raise ValueError(f"timeout_ms must be None or >= 0, got {timeout_ms}")
        peer = str(peer_id).encode("utf-8")
        payload_cap = int(payload_cap)
        # Reused buffers: grown (allocated) here, outside the lock; used and copied out only under the lock.
        buffers = getattr(self, "_recv_buffers", None)
        if buffers is None or len(buffers["payload"]) < payload_cap:
            buffers = {
                "topic": buffers["topic"] if buffers else (ctypes.c_uint8 * 512)(),
                "publisher": buffers["publisher"] if buffers else (ctypes.c_uint8 * 512)(),
                "payload": (ctypes.c_uint8 * max(payload_cap, 1))(),
            }
            self._recv_buffers = buffers
        lib = self._core.lib
        topic_len = ctypes.c_size_t()
        publisher_len = ctypes.c_size_t()
        payload_len = ctypes.c_size_t()
        mode = ctypes.c_uint16()
        priority = ctypes.c_uint16()
        sequence = ctypes.c_uint64()
        timestamp_ns = ctypes.c_int64()
        published_at_sync_ns = ctypes.c_int64()
        topic_cap = publisher_cap = 0
        event = None
        with self._lock:
            for attempt in range(2):
                topic_buf, publisher_buf, payload_buf = buffers["topic"], buffers["publisher"], buffers["payload"]
                topic_cap, publisher_cap = len(topic_buf), len(publisher_buf)
                outs = (
                    ctypes.byref(mode),
                    ctypes.byref(priority),
                    ctypes.byref(sequence),
                    ctypes.byref(timestamp_ns),
                    ctypes.byref(published_at_sync_ns),
                )
                if timeout_ms is None:
                    rc = lib.r2kernel_event_transport_recv_event_ex(
                        self._handle, peer, *outs,
                        topic_buf, topic_cap, ctypes.byref(topic_len),
                        publisher_buf, publisher_cap, ctypes.byref(publisher_len),
                        payload_buf, payload_cap, ctypes.byref(payload_len),
                    )
                else:
                    # Explicit ctypes scalars: correct with or without declared argtypes.
                    rc = lib.r2kernel_event_transport_recv_event_timeout(
                        self._handle, peer, ctypes.c_int64(int(timeout_ms)), *outs,
                        topic_buf, ctypes.c_size_t(topic_cap), ctypes.byref(topic_len),
                        publisher_buf, ctypes.c_size_t(publisher_cap), ctypes.byref(publisher_len),
                        payload_buf, ctypes.c_size_t(payload_cap), ctypes.byref(payload_len),
                    )
                if rc == 0:
                    event = {
                        "mode": _MODE_FROM_ID.get(int(mode.value), "event"),
                        "priority": int(priority.value),
                        "sequence": int(sequence.value),
                        "timestamp_ns": int(timestamp_ns.value),
                        "observed_at_sync_ns": int(timestamp_ns.value),
                        "published_at_sync_ns": int(published_at_sync_ns.value),
                        "topic": bytes(topic_buf[: topic_len.value]).decode("utf-8"),
                        "publisher": bytes(publisher_buf[: publisher_len.value]).decode("utf-8"),
                        "payload": bytes(payload_buf[: payload_len.value]),
                    }
                if rc != _BUFFER_TOO_SMALL or attempt or int(payload_len.value) > payload_cap:
                    break
                # Rare: a topic or publisher above the reused buffers; grow them for this and later calls.
                if int(topic_len.value) > topic_cap:
                    buffers["topic"] = (ctypes.c_uint8 * int(topic_len.value))()
                if int(publisher_len.value) > publisher_cap:
                    buffers["publisher"] = (ctypes.c_uint8 * int(publisher_len.value))()
        if rc == _NOT_FOUND:
            return None
        if rc == _BUFFER_TOO_SMALL:
            if int(payload_len.value) > payload_cap:
                raise RuntimeError(
                    f"event transport event from {peer_id}: payload needs {int(payload_len.value)} bytes, above "
                    f"payload_cap {payload_cap}; Core keeps the event for a recv_event call with a larger payload_cap"
                )
            raise RuntimeError(
                f"event transport event from {peer_id} did not fit after one retry "
                f"(topic {int(topic_len.value)}/{topic_cap}, publisher {int(publisher_len.value)}/{publisher_cap} bytes)"
            )
        if rc != 0:
            raise RuntimeError(f"Failed to recv event transport event rc={rc}")
        return event
