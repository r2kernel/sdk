"""Per-channel local-Control v5 sockets and producer-owned payload mappings."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any

from ._control_shm import (
    ControlShmProtocolError,
    close_control_shm_descriptors,
    decode_error_response,
    decode_response_released,
    encode_response_consumed,
    local_control_frame_kind,
    normalize_control_shm_lease,
    receive_control_shm_frame,
    send_control_shm_frame,
)
from ._control_shm_region import (
    CONTROL_SHM_MAX_PAYLOAD_BYTES,
    CONTROL_SHM_PAYLOAD_OFFSET,
    ControlShmBudget,
    ControlShmCapacityError,
    ControlShmPolicy,
    ControlShmRegion,
    _ProcessPreparedMemory,
)


class ControlShmCounters:
    """Small thread-safe diagnostics set that never records payload content."""

    _maximum_error_message_characters = 512

    _names = (
        "channels_created",
        "channels_closed",
        "sockets_opened",
        "frames_sent",
        "frames_received",
        "request_region_creates",
        "request_region_reuses",
        "request_region_grows",
        "request_payload_bytes",
        "payload_reads",
        "payload_writes",
        "payload_bytes_read",
        "payload_bytes_written",
        "response_region_creates",
        "response_region_reuses",
        "response_region_grows",
        "response_payload_bytes",
        "received_mapping_opens",
        "received_mapping_reuses",
        "mapping_closes",
        "fd_sent",
        "fd_received",
        "protocol_errors",
        "capacity_errors",
        "io_errors",
        "errors",
        "prepared_lane_recoveries",
        "prepared_lane_recovery_failures",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values = {name: 0 for name in self._names}
        self._last_error: str | None = None

    def increment(self, name: str, amount: int = 1) -> None:
        if name not in self._values:
            raise KeyError(f"unknown local Control shared-memory counter: {name}")
        with self._lock:
            self._values[name] += int(amount)

    def snapshot(self) -> dict[str, int | str | None]:
        with self._lock:
            return {**self._values, "last_error": self._last_error}

    def reset_lock_after_fork(self) -> None:
        """In a forked child: a lock another parent thread held at the fork stays held."""
        self._lock = threading.Lock()

    @classmethod
    def _bounded_error_message(cls, error: BaseException | str) -> str:
        return str(error)[: cls._maximum_error_message_characters]

    def record_remote_error(self, error: str) -> None:
        """Retain a bounded peer error without classifying it as a local SDK error."""
        with self._lock:
            self._last_error = self._bounded_error_message(error)

    def record_error(self, error: BaseException) -> None:
        with self._lock:
            self._values["errors"] += 1
            if isinstance(error, ControlShmCapacityError):
                self._values["capacity_errors"] += 1
            elif isinstance(error, ControlShmProtocolError):
                self._values["protocol_errors"] += 1
            elif isinstance(error, (OSError, EOFError)):
                self._values["io_errors"] += 1
            self._last_error = self._bounded_error_message(error)


class ControlShmChannel:
    """One persistent caller-side v5 connection with its own request and reply regions."""

    def __init__(
        self,
        *,
        endpoint: str,
        policy: ControlShmPolicy,
        budget: ControlShmBudget,
        counters: ControlShmCounters,
    ):
        self.endpoint = str(endpoint)
        self.policy = policy
        self._budget = budget
        self._counters = counters
        self._socket: socket.socket | None = None
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._pending_release: tuple[int, float, socket.socket] | None = None
        self._release_error: Exception | None = None
        self._reclaimer: threading.Thread | None = None
        self._outgoing_region: ControlShmRegion | None = None
        self._outgoing_descriptor_sent = False
        self._incoming_region: ControlShmRegion | None = None
        self._next_response_ticket = 1
        self._prepared_capacities: tuple[int, int] | None = None
        self._unpublished_timeout_error_id: int | None = None
        # (request capacity, response capacity, result_ready, timeout_ms) of a
        # prepared lane: the bound and contract of its automatic re-preparation.
        self.preparation_parameters: tuple[int, int, bool, int] | None = None
        self._retired_socket: socket.socket | None = None
        # id() of the exception whose exchange retired the socket: only that
        # call takes it (no reference, so its traceback is not retained).
        self._retired_error_id: int | None = None
        self._closed = False
        self._budget.acquire_channel()
        self._counters.increment("channels_created")
        _live_channels.add(self)

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def has_reclamation_error(self) -> bool:
        return self._release_error is not None

    def exchange(
        self,
        *,
        request_builder: Callable[[dict[str, Any] | None], bytes],
        response_decoder: Callable[[bytes], dict[str, Any]],
        timeout_s: float,
        request_payload: bytes | None = None,
        request_timestamp_ns: int = 0,
        response_has_payload: Callable[[dict[str, Any]], bool] | None = None,
        response_ticket_required: bool = False,
        result_ready: bool = False,
        deadline: float | None = None,
        preparation: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        if timeout_s <= 0:
            raise ValueError("local Control v5 timeout must be positive")
        pending_descriptors: list[int] = []
        if deadline is None:
            deadline = time.monotonic() + float(timeout_s)
        if not self._lock.acquire(timeout=self._remaining_timeout(deadline)):
            # Do not close a channel owned by the exchange that currently holds
            # the lock; this timed-out call has not touched the wire.
            raise socket.timeout("local Control v5 channel is busy past the exchange deadline")
        self._unpublished_timeout_error_id = None
        try:
            self._wait_reclaimed_locked(deadline)
            if self._closed:
                raise ControlShmProtocolError("local Control v5 channel is closed")
            wire_attempted = False
            try:
                if preparation is not None:
                    if self._outgoing_region is not None or self._incoming_region is not None:
                        raise ControlShmProtocolError("preparation requires a fresh topic lane")
                    request_capacity, response_capacity = preparation
                    self.policy.region_size_for(response_capacity)
                    self._outgoing_region = ControlShmRegion.create(
                        self.policy.region_size_for(request_capacity), self._budget,
                    )
                    self._counters.increment("request_region_creates")
                    self._outgoing_region.prepare_memory()
                    if result_ready:
                        self._ensure_reclaimer_locked()
                self._connect(deadline)
                if self._prepared_capacities is not None and result_ready and self._reclaimer is None:
                    raise ControlShmProtocolError("prepared lane did not select result-ready completion")
                request_lease = None
                if request_payload is not None:
                    request_lease = self._write_request_payload(request_payload, request_timestamp_ns)
                elif preparation is not None:
                    assert self._outgoing_region is not None
                    request_lease = self._outgoing_region.write_payload(b"", 0)
                request_frame = request_builder(request_lease)
                descriptor_to_send = None
                if request_lease is not None and not self._outgoing_descriptor_sent:
                    assert self._outgoing_region is not None
                    descriptor_to_send = self._outgoing_region.file_descriptor
                assert self._socket is not None
                self._set_remaining_timeout(deadline)
                # Any send attempt may publish a prefix byte or SCM_RIGHTS.
                # Never retain or replay a lane after that boundary.
                wire_attempted = True
                send_control_shm_frame(
                    self._socket,
                    request_frame,
                    descriptor_to_send,
                    deadline=deadline,
                )
                if descriptor_to_send is not None:
                    self._outgoing_descriptor_sent = True
                    self._counters.increment("fd_sent")
                self._counters.increment("frames_sent")
                self._set_remaining_timeout(deadline)
                response_frame, pending_descriptors = receive_control_shm_frame(
                    self._socket, phase="response", deadline=deadline,
                )
                if response_frame is None:
                    raise EOFError("local Control v5 peer closed the channel")
                if pending_descriptors:
                    self._counters.increment("fd_received", len(pending_descriptors))
                self._counters.increment("frames_received")
                if local_control_frame_kind(response_frame) == 255:
                    if pending_descriptors:
                        raise ControlShmProtocolError("local Control v5 error frame carried a descriptor")
                    raise ControlShmProtocolError(decode_error_response(response_frame))
                response = response_decoder(response_frame)
                has_payload = (
                    response_has_payload(response)
                    if response_has_payload is not None
                    else request_payload is not None
                )
                response_lease = response.get("payload_lease")
                if has_payload:
                    if response_lease is None:
                        raise ControlShmProtocolError("successful local Control v5 response omitted its lease")
                    response["payload"] = self._read_response_payload(response_lease, pending_descriptors)
                    pending_descriptors = []
                    self._counters.increment("response_payload_bytes", len(response["payload"]))
                    if preparation is not None:
                        assert self._incoming_region is not None
                        if self._incoming_region.size < preparation[1] + CONTROL_SHM_PAYLOAD_OFFSET:
                            raise ControlShmCapacityError("prepared response mapping is below declared capacity")
                        self._incoming_region.prepare_memory()
                        self._prepared_capacities = preparation
                elif response_lease is not None or pending_descriptors:
                    raise ControlShmProtocolError("local Control v5 response has an unexpected lease or descriptor")
                response_ticket = response.get("response_ticket")
                if response_ticket_required and response.get("ok") and response_ticket is None:
                    raise ControlShmProtocolError("successful local Control v5 response omitted its response ticket")
                if response_ticket is not None:
                    self._consume_response(response_ticket, response_lease, deadline, result_ready=result_ready)
                if response.get("ok") is False:
                    self._counters.record_remote_error(
                        str(response.get("error") or "local Control v5 peer returned ok=false")
                    )
                    self._close_locked()
                self._remaining_timeout(deadline)
                return response
            except Exception as exc:
                if pending_descriptors:
                    close_control_shm_descriptors(pending_descriptors)
                self._counters.record_error(exc)
                if self._can_reuse_after_unpublished_deadline(exc, deadline, wire_attempted):
                    self._unpublished_timeout_error_id = id(exc)
                else:
                    # Late, or Control closed the lane: end of stream, or a send
                    # or receive that failed with EPIPE or ECONNRESET.
                    if wire_attempted and self._prepared_capacities is not None and isinstance(
                        exc, (socket.timeout, EOFError, BrokenPipeError, ConnectionResetError)
                    ):
                        self._retire_socket_locked(exc)
                    self._close_locked()
                raise
        finally:
            self._lock.release()

    def _can_reuse_after_unpublished_deadline(
        self, error: Exception, deadline: float, wire_attempted: bool,
    ) -> bool:
        # _connect commits _socket only after manager peer proof succeeds.
        # Retain that verified connection and its unpublished request mapping
        # while preserving the failed call's original deadline and exception.
        return (
            not wire_attempted
            and isinstance(error, socket.timeout)
            and self._socket is not None
            and not self._closed
            and self._pending_release is None
            and self._release_error is None
            and time.monotonic() >= deadline
        )

    def _retire_socket_locked(self, error: Exception) -> None:
        # A late or peer-closed exchange on a prepared lane: stop sending, keep
        # the descriptor so re-preparation can first wait until Control closes it.
        retired, self._socket = self._socket, None
        if retired is None:
            return
        try:
            retired.shutdown(socket.SHUT_WR)
        except OSError:
            retired.close()
            return
        self._retired_socket = retired
        self._retired_error_id = id(error)

    def take_retired_socket(self, error: BaseException) -> socket.socket | None:
        """Ownership of the failed lane's half-closed socket, for the call whose ``error`` retired it.

        Another call that failed on the same closed lane gets ``None``: recovery
        runs once, in the call whose exchange failed.
        """
        with self._lock:
            if id(error) != self._retired_error_id:
                return None
            return self._take_retired_socket_locked()

    def _take_retired_socket_locked(self) -> socket.socket | None:
        retired, self._retired_socket = self._retired_socket, None
        self._retired_error_id = None
        return retired

    def retains_unpublished_timeout(self, error: Exception | None) -> bool:
        # Registry ownership keeps the original exception live until completion;
        # a scalar identity does not retain its traceback or caller payload.
        return (error is not None and id(error) == self._unpublished_timeout_error_id
                and isinstance(error, socket.timeout) and self._socket is not None
                and not self._closed and self._pending_release is None and self._release_error is None)

    def close(self) -> None:
        with self._lock:
            retired = self._take_retired_socket_locked()
        if retired is not None:
            retired.close()
        try:
            with self._condition:
                if self._socket is not None:
                    # The reclaimer may be blocked in receive outside the lock.
                    try:
                        self._socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass  # Already closed/disconnected during cancellation.
                self._close_locked()
                self._condition.notify_all()
        finally:
            worker = self._reclaimer
            if worker is not None and worker is not threading.current_thread():
                worker.join()

    def abandon_after_fork(self) -> None:
        """In a forked child: drop this inherited lane of the parent without ending it.

        The socket shares its open connection with the parent, so it is closed
        without ``shutdown`` (which would end the parent's lane too). The lock
        is new (a parent thread may have held it at the fork) and the parent's
        release worker does not exist in this process. The lane is closed here.
        """
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._reclaimer = None
        self._pending_release = None
        with self._condition:
            retired = self._take_retired_socket_locked()
            if retired is not None:
                retired.close()
            self._close_locked()

    def wait_reclaimed(self, timeout_s: float) -> None:
        """Wait for reuse permission; expose any failure after result delivery."""
        if timeout_s <= 0:
            raise ValueError("local Control v5 reclamation timeout must be positive")
        deadline = time.monotonic() + timeout_s
        if not self._lock.acquire(timeout=self._remaining_timeout(deadline)):
            raise socket.timeout("local Control v5 channel is busy past the reclamation deadline")
        try:
            self._wait_reclaimed_locked(deadline)
            if self._closed:
                raise ControlShmProtocolError("local Control v5 channel is closed")
        finally:
            self._lock.release()

    def _wait_reclaimed_locked(self, deadline: float) -> None:
        while self._pending_release is not None and not self._closed:
            self._condition.wait(self._remaining_timeout(deadline))
        if self._release_error is not None:
            raise ControlShmProtocolError(
                f"local Control v5 response reclamation failed: {self._release_error}"
            ) from self._release_error

    def _ensure_reclaimer_locked(self) -> None:
        if self._reclaimer is None:
            # Existing clients register close_all at atexit. Python joins
            # non-daemon threads before atexit, so this idle worker must permit
            # that cleanup hook to run; close still cancels and joins it.
            worker = threading.Thread(target=self._reclaim_loop, name="control-release", daemon=True)
            worker.start()
            self._reclaimer = worker

    def _reclaim_loop(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending_release is not None or self._closed)
                if self._closed:
                    return
                assert self._pending_release is not None
                ticket, deadline, local_socket = self._pending_release
            error = None
            try:
                self._receive_release(local_socket, ticket, deadline)
            except Exception as exc:
                error = exc
            with self._condition:
                if self._closed:
                    self._pending_release = None
                    self._condition.notify_all()
                    return
                if error is not None:
                    self._release_error = error
                    self._counters.record_error(error)
                    try:
                        self._close_locked()
                    finally:
                        self._pending_release = None
                        self._condition.notify_all()
                    return
                self._next_response_ticket += 1
                self._pending_release = None
                self._condition.notify_all()

    def _write_request_payload(self, payload: bytes, timestamp_ns: int) -> dict[str, Any]:
        data = bytes(payload)
        if len(data) > CONTROL_SHM_MAX_PAYLOAD_BYTES:
            raise ControlShmCapacityError("local Control payload exceeds the SDK payload limit")
        if self._prepared_capacities is not None and len(data) > self._prepared_capacities[0]:
            raise ControlShmCapacityError("request exceeds the prepared payload capacity")
        previous_region = self._outgoing_region
        desired_size = self.policy.region_size_for(
            len(data),
            previous_region.size if previous_region is not None else 0,
        )
        if previous_region is None or previous_region.size < desired_size:
            if self._prepared_capacities is not None:
                raise ControlShmProtocolError("prepared request region cannot grow")
            replacement = ControlShmRegion.create(desired_size, self._budget)
            self._outgoing_region = replacement
            self._outgoing_descriptor_sent = False
            self._counters.increment("request_region_creates")
            if previous_region is not None:
                self._counters.increment("request_region_grows")
                previous_region.close()
        else:
            self._counters.increment("request_region_reuses")
        assert self._outgoing_region is not None
        lease = self._outgoing_region.write_payload(data, timestamp_ns)
        self._counters.increment("payload_writes")
        self._counters.increment("payload_bytes_written", len(data))
        self._counters.increment("request_payload_bytes", len(data))
        return lease

    def _read_response_payload(self, lease: dict[str, Any], received_descriptors: list[int]) -> bytes:
        normalized = normalize_control_shm_lease(lease)
        if self._prepared_capacities is not None:
            if normalized["length"] > self._prepared_capacities[1]:
                raise ControlShmCapacityError("response exceeds the prepared payload capacity")
            if self._incoming_region is None or self._incoming_region.token != normalized["region_name"]:
                raise ControlShmProtocolError("prepared response mapping cannot change")
        self._validate_region_size(normalized["slot_size"])
        if self._incoming_region is not None and self._incoming_region.token == normalized["region_name"]:
            if received_descriptors:
                raise ControlShmProtocolError("local Control v5 repeated a token with a new descriptor")
            if self._incoming_region.size != normalized["slot_size"]:
                raise ControlShmProtocolError("local Control v5 token changed its mapped region size")
            self._counters.increment("received_mapping_reuses")
        else:
            if len(received_descriptors) != 1:
                raise ControlShmProtocolError("new local Control v5 response token must carry exactly one descriptor")
            received_descriptor = received_descriptors[-1]
            self._drop_incoming_region()
            received_descriptors.pop()
            replacement = ControlShmRegion.from_received_descriptor(
                normalized["region_name"],
                normalized["slot_size"],
                received_descriptor,
                self._budget,
            )
            self._incoming_region = replacement
            self._counters.increment("received_mapping_opens")
        assert self._incoming_region is not None
        payload = self._incoming_region.read_payload(normalized)
        self._counters.increment("payload_reads")
        self._counters.increment("payload_bytes_read", len(payload))
        return payload

    def _validate_region_size(self, region_size: int) -> None:
        if region_size < self.policy.minimum_region_bytes or region_size > self.policy.maximum_region_bytes:
            raise ControlShmProtocolError("local Control v5 lease is outside the negotiated region bounds")

    def _connect(self, deadline: float) -> None:
        if self._socket is not None:
            return
        local_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        local_socket.settimeout(self._remaining_timeout(deadline))
        try:
            local_socket.connect(self.endpoint)
            from .resource import verify_control_server_connection
            verify_control_server_connection(local_socket, deadline=deadline)
        except Exception:
            local_socket.close()
            raise
        self._socket = local_socket
        self._counters.increment("sockets_opened")

    def _remaining_timeout(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("local Control v5 exchange deadline exceeded")
        return remaining

    def _set_remaining_timeout(self, deadline: float) -> None:
        if self._socket is None:
            raise ControlShmProtocolError("local Control v5 socket is not connected")
        self._socket.settimeout(self._remaining_timeout(deadline))

    def _consume_response(
        self,
        response_ticket: int,
        response_lease: dict[str, Any] | None,
        deadline: float,
        *,
        result_ready: bool = False,
    ) -> None:
        if response_ticket != self._next_response_ticket:
            raise ControlShmProtocolError(
                "local Control v5 response ticket does not match the next expected ticket"
            )
        if response_ticket >= 0xFFFFFFFFFFFFFFFF:
            raise ControlShmProtocolError("local Control v5 response ticket sequence is exhausted")
        if response_lease is None:
            raise ControlShmProtocolError("local Control v5 response ticket has no copied payload lease")
        assert self._socket is not None
        if result_ready:
            self._ensure_reclaimer_locked()
        acknowledgement = encode_response_consumed(response_ticket, response_lease)
        self._set_remaining_timeout(deadline)
        send_control_shm_frame(self._socket, acknowledgement, deadline=deadline)
        self._counters.increment("frames_sent")
        if result_ready:
            self._pending_release = (response_ticket, deadline, self._socket)
            self._condition.notify_all()
            return
        self._receive_release(self._socket, response_ticket, deadline)
        self._next_response_ticket += 1

    def _receive_release(self, local_socket: socket.socket, response_ticket: int, deadline: float) -> None:
        local_socket.settimeout(self._remaining_timeout(deadline))
        released_frame, released_descriptors = receive_control_shm_frame(
            local_socket, phase="release", deadline=deadline,
        )
        if released_frame is None:
            raise EOFError("local Control v5 peer closed before releasing the response")
        if released_descriptors:
            self._counters.increment("fd_received", len(released_descriptors))
            close_control_shm_descriptors(released_descriptors)
            raise ControlShmProtocolError("local Control v5 released frame carried a descriptor")
        self._counters.increment("frames_received")
        released_ticket = decode_response_released(released_frame)
        if released_ticket != response_ticket:
            raise ControlShmProtocolError("local Control v5 released ticket does not match the response")

    def _drop_outgoing_region(self) -> None:
        region = self._outgoing_region
        self._outgoing_region = None
        self._outgoing_descriptor_sent = False
        if region is not None:
            region.close()

    def _drop_incoming_region(self) -> None:
        region = self._incoming_region
        self._incoming_region = None
        if region is not None:
            try:
                region.close()
            finally:
                self._counters.increment("mapping_closes")

    def _close_locked(self) -> None:
        self._unpublished_timeout_error_id = None
        if self._closed:
            return
        self._closed = True
        self._condition.notify_all()
        first_error: Exception | None = None
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception as exc:
                first_error = exc
            finally:
                self._socket = None
        try:
            self._drop_outgoing_region()
        except Exception as exc:
            if first_error is None:
                first_error = exc
        finally:
            try:
                self._drop_incoming_region()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
            finally:
                self._budget.release_channel()
                self._counters.increment("channels_closed")
        if first_error is not None:
            raise first_error


def await_control_shm_peer_close(local_socket: socket.socket, deadline: float) -> None:
    """Discard late frames (their descriptors close with them) until Control closes the lane."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("Control did not close the failed lane before the recovery deadline")
        local_socket.settimeout(remaining)
        try:
            if not local_socket.recv(256):
                return
        except ConnectionResetError:
            return
        except socket.timeout:
            continue


class ControlShmChannelRegistry:
    """Keeps independent local Control channels keyed by route and topic."""

    def __init__(self, policy: ControlShmPolicy, budget: ControlShmBudget, counters: ControlShmCounters):
        self.policy = policy
        self.budget = budget
        self.counters = counters
        self._channels: dict[tuple[str, str, str], ControlShmChannel] = {}
        self._pending_preparations: dict[tuple[str, str, str], tuple[tuple[str, int], ControlShmChannel]] = {}
        self._prepared_routes: dict[tuple[str, str, str], tuple[str, int]] = {}
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._shutdown_lock = threading.Lock()
        self._stopped = False
        self._closing_targets: set[str] = set()
        _live_registries.add(self)

    def forget_after_fork(self) -> None:
        """In a forked child: the parent's lanes are not this process's (see ``abandon_after_fork``)."""
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._shutdown_lock = threading.Lock()
        self._channels = {}
        self._pending_preparations = {}
        self._prepared_routes = {}
        self._closing_targets = set()

    def _require_admission_locked(self, target: str) -> None:
        if self._stopped:
            raise ControlShmProtocolError("local Control v5 channel registry is stopped")
        if target in self._closing_targets:
            raise ControlShmProtocolError("local Control v5 target is being rebound")

    def get(self, endpoint: str, target: str, topic: str) -> ControlShmChannel:
        with self._lock:
            self._require_admission_locked(target)
            return self._get_locked(endpoint, target, topic)

    def get_route(
        self, endpoint: str, target: str, topic: str,
        route_provider: Callable[[], tuple[str, int] | None],
    ) -> tuple[ControlShmChannel, tuple[str, int]]:
        """Take the route and lane from the same rebind/shutdown boundary."""
        with self._lock:
            self._require_admission_locked(target)
            route = route_provider()
            if route is None:
                raise ControlShmProtocolError("local Control v5 target route is missing")
            key = (str(endpoint), str(target), str(topic))
            if key in self._pending_preparations:
                raise ControlShmProtocolError("local Control v5 topic preparation is in progress")
            prepared_route = self._prepared_routes.get(key)
            if prepared_route is not None and prepared_route != route:
                channel = self._channels.get(key)
                if channel is not None:
                    channel.close()
                raise ControlShmProtocolError("local Control v5 prepared target route changed")
            if prepared_route is not None:
                channel = self._channels.get(key)
                if channel is None or channel.is_closed:
                    raise ControlShmProtocolError("prepared lane is invalid; rebind before re-preparation")
            return self._get_locked(endpoint, target, topic), route

    def begin_preparation(
        self, endpoint: str, target: str, topic: str,
        route_provider: Callable[[], tuple[str, int] | None],
    ) -> tuple[tuple[str, int], ControlShmChannel]:
        key = (str(endpoint), str(target), str(topic))
        with self._lock:
            self._require_admission_locked(target)
            route = route_provider()
            if route is None:
                raise ControlShmProtocolError("local Control v5 target route is missing")
            if key in self._pending_preparations:
                raise ControlShmProtocolError("local Control v5 topic preparation is already in progress")
            previous = self._channels.get(key)
            if previous is not None:
                if not previous._lock.acquire(blocking=False):
                    raise ControlShmProtocolError("local Control v5 topic lane is busy")
                try:
                    if previous.has_reclamation_error:
                        raise ControlShmProtocolError(
                            f"local Control v5 response reclamation failed: {previous._release_error}"
                        ) from previous._release_error
                    if previous._pending_release is not None:
                        raise ControlShmProtocolError("local Control v5 topic lane is awaiting reclamation")
                    # Retire while still excluding holders of an old lane reference.
                    previous._close_locked()
                finally:
                    previous._lock.release()
                previous.close()
                self._channels.pop(key)
            ticket = (route, ControlShmChannel(
                endpoint=endpoint, policy=self.policy, budget=self.budget, counters=self.counters,
            ))
            self._pending_preparations[key] = ticket
            return ticket

    def finish_preparation(
        self, endpoint: str, target: str, topic: str,
        ticket: tuple[tuple[str, int], ControlShmChannel], *, success: bool,
        route_provider: Callable[[], tuple[str, int] | None],
        update_ready: Callable[[bool], None],
    ) -> None:
        key = (str(endpoint), str(target), str(topic))
        with self._lock:
            if self._pending_preparations.get(key) is not ticket:
                raise ControlShmProtocolError("local Control v5 preparation was invalidated by rebind or shutdown")
            self._require_admission_locked(target)
            self._pending_preparations.pop(key)
            if route_provider() != ticket[0]:
                ticket[1].close()
                raise ControlShmProtocolError("local Control v5 prepared target route changed")
            if success:
                if ticket[1].is_closed or ticket[1]._prepared_capacities is None:
                    ticket[1].close()
                    raise ControlShmProtocolError("local Control v5 preparation did not install ready mappings")
                self._channels[key] = ticket[1]
                self._prepared_routes[key] = ticket[0]
            else:
                ticket[1].close()
            update_ready(success)

    def _get_locked(self, endpoint: str, target: str, topic: str) -> ControlShmChannel:
        key = (str(endpoint), str(target), str(topic))
        closed_keys = [entry_key for entry_key, channel in self._channels.items()
                       if channel.is_closed and not channel.has_reclamation_error
                       and entry_key not in self._prepared_routes]
        for entry_key in closed_keys:
            self._channels.pop(entry_key, None)
        channel = self._channels.get(key)
        if channel is None:
            channel = ControlShmChannel(
                endpoint=endpoint, policy=self.policy,
                budget=self.budget, counters=self.counters,
            )
            self._channels[key] = channel
        return channel

    def find(self, endpoint: str, target: str, topic: str) -> ControlShmChannel | None:
        with self._lock:
            self._require_admission_locked(target)
            return self._channels.get((str(endpoint), str(target), str(topic)))

    def close_target(self, target: str, update_route: Callable[[], None]) -> None:
        with self._lock:
            self._require_admission_locked(target)
            self._closing_targets.add(target)
            channels = [channel for key, channel in self._channels.items() if key[1] == target]
            self._channels = {key: channel for key, channel in self._channels.items() if key[1] != target}
            self._prepared_routes = {key: route for key, route in self._prepared_routes.items() if key[1] != target}
            for preparations in (self._pending_preparations,):
                channels.extend(ticket[1] for key, ticket in preparations.items() if key[1] == target)
                for key in [key for key in preparations if key[1] == target]:
                    preparations.pop(key)
        try:
            self._close_channels(channels)
            with self._lock:
                if self._stopped:
                    raise ControlShmProtocolError("local Control v5 registry stopped during rebind")
                update_route()
        finally:
            with self._changed:
                self._closing_targets.remove(target)
                self._changed.notify_all()

    def remove(
        self,
        endpoint: str,
        target: str,
        topic: str,
        expected_channel: ControlShmChannel,
    ) -> None:
        key = (str(endpoint), str(target), str(topic))
        with self._lock:
            if self._channels.get(key) is expected_channel:
                self._channels.pop(key, None)

    def close_all(self) -> None:
        with self._shutdown_lock:
            with self._changed:
                self._stopped = True
                # A concurrent rebind owns removed channels until it has joined
                # their workers. Shutdown includes those in its completion.
                self._changed.wait_for(lambda: not self._closing_targets)
                channels = list(self._channels.values())
                self._channels.clear()
                self._prepared_routes.clear()
                for preparations in (self._pending_preparations,):
                    channels.extend(ticket[1] for ticket in preparations.values())
                    preparations.clear()
            self._close_channels(channels)

    @staticmethod
    def _close_channels(channels: list[ControlShmChannel]) -> None:
        first_error: Exception | None = None
        for channel in channels:
            try:
                channel.close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def reconfigure(self, policy: ControlShmPolicy, budget: ControlShmBudget) -> None:
        with self._lock:
            channels = list(self._channels.values()) + [
                ticket[1] for preparations in (self._pending_preparations,)
                for ticket in preparations.values()
            ]
            if any(not channel.is_closed for channel in channels):
                if policy != self.policy:
                    raise ControlShmProtocolError("local Control v5 policy changed while channels are active")
                return
            self._channels.clear()
            self._pending_preparations.clear()
            self._prepared_routes.clear()
            self.policy = policy
            self.budget = budget

    def reopen(self) -> None:
        """Explicit lifecycle restart after shutdown has joined all workers."""
        with self._shutdown_lock:
            with self._lock:
                if self._stopped:
                    if self._channels or self._pending_preparations or self._prepared_routes or self._closing_targets:
                        raise ControlShmProtocolError("cannot restart an undrained Control v5 registry")
                    self._stopped = False


# Every lane and registry of this process, so a forked child can drop the
# parent's: the child's exit cleanup must neither shut down the
# parent's lanes nor wait for a lock a parent thread held at the fork.
_live_channels: weakref.WeakSet[ControlShmChannel] = weakref.WeakSet()
_live_registries: weakref.WeakSet[ControlShmChannelRegistry] = weakref.WeakSet()


def _abandon_inherited_lanes() -> None:
    channels, registries = list(_live_channels), list(_live_registries)
    accounting = {id(item): item for channel in channels for item in (channel._budget, channel._counters)}
    for registry in registries:
        accounting.update({id(registry.budget): registry.budget, id(registry.counters): registry.counters})
        registry.forget_after_fork()
    for item in accounting.values():
        item.reset_lock_after_fork()
    first_error: Exception | None = None
    for channel in channels:
        try:
            channel.abandon_after_fork()
        except Exception as exc:  # every inherited lane is still dropped; the first failure is raised
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_abandon_inherited_lanes)


def configure_policy_from_registration(response: dict[str, Any], maximum_region_bytes: int) -> ControlShmPolicy:
    policy_name = response.get("control_local_payload_policy")
    protocol_name = response.get("control_local_protocol")
    if policy_name != "forwarded_shm_v5" or protocol_name != "binary_shm_v5":
        raise ControlShmProtocolError("SDK and runtime local Control shared-memory policies do not match")
    try:
        return ControlShmPolicy(
            minimum_region_bytes=int(response["control_shm_min_region_bytes"]),
            maximum_channels=int(response["control_shm_max_channels"]),
            maximum_owned_bytes=int(response["control_shm_max_owned_bytes"]),
            maximum_mapped_bytes=int(response["control_shm_max_mapped_bytes"]),
            maximum_region_bytes=int(maximum_region_bytes),
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ControlShmProtocolError("runtime returned an invalid local Control shared-memory policy") from exc


def emit_control_shm_summary(
    counters: ControlShmCounters,
    budget: ControlShmBudget,
) -> None:
    if os.environ.get("R2KERNEL_CONTROL_SHM_METRICS", "").strip() != "1":
        return
    event = control_shm_summary(counters, budget)
    import sys

    print(json.dumps(event, separators=(",", ":")), file=sys.stderr, flush=True)


def control_shm_summary(counters: ControlShmCounters, budget: ControlShmBudget) -> dict[str, Any]:
    counters_snapshot = counters.snapshot()
    budget_snapshot = budget.snapshot()
    event = {
        "event": "control_shm_summary",
        "runtime": "python_sdk",
        "mode": "forwarded_shm_v5",
        "policy": "forwarded_shm_v5",
        "active_channels": budget_snapshot["channels"],
        "peak_channels": budget_snapshot["peak_channels"],
        "channels_opened": counters_snapshot["channels_created"],
        "channels_closed": counters_snapshot["channels_closed"],
        "regions_created": counters_snapshot["request_region_creates"] + counters_snapshot["response_region_creates"],
        "region_reuses": counters_snapshot["request_region_reuses"] + counters_snapshot["response_region_reuses"],
        "region_resizes": counters_snapshot["request_region_grows"] + counters_snapshot["response_region_grows"],
        "mapping_opens": counters_snapshot["received_mapping_opens"],
        "mapping_reuses": counters_snapshot["received_mapping_reuses"],
        "mapping_closes": counters_snapshot["mapping_closes"],
        "owned_bytes": budget_snapshot["owned_bytes"],
        "peak_owned_bytes": budget_snapshot["peak_owned_bytes"],
        "mapped_bytes": budget_snapshot["mapped_bytes"],
        "peak_mapped_bytes": budget_snapshot["peak_mapped_bytes"],
        "payload_reads": counters_snapshot["payload_reads"],
        "payload_writes": counters_snapshot["payload_writes"],
        "payload_bytes_read": counters_snapshot["payload_bytes_read"],
        "payload_bytes_written": counters_snapshot["payload_bytes_written"],
        "fd_sent": counters_snapshot["fd_sent"],
        "fd_received": counters_snapshot["fd_received"],
        "protocol_errors": counters_snapshot["protocol_errors"],
        "capacity_errors": counters_snapshot["capacity_errors"],
        "io_errors": counters_snapshot["io_errors"],
        "max_channels": budget.policy.maximum_channels,
        "max_owned_bytes": budget.policy.maximum_owned_bytes,
        "max_mapped_bytes": budget.policy.maximum_mapped_bytes,
        "prepared_bytes": budget_snapshot["prepared_bytes"],
        "peak_prepared_bytes": budget_snapshot["peak_prepared_bytes"],
        "rt_memory_budget_bytes": budget.rt_memory_budget_bytes,
    }
    process_memory = _ProcessPreparedMemory.configured_snapshot()
    event["process_memory_configured"] = process_memory is not None
    if process_memory is not None:
        event["process_rt_memory_budget_bytes"] = process_memory["limit"]
        event["process_prepared_bytes"] = process_memory["bytes"]
        event["process_peak_prepared_bytes"] = process_memory["peak"]
    if counters_snapshot["last_error"] is not None:
        event["last_error"] = counters_snapshot["last_error"]
    return event
