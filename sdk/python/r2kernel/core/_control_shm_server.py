"""Server-side reusable Control SHM connection handling."""

from __future__ import annotations

import select
import socket
import time
from collections.abc import Callable
from typing import Any, TYPE_CHECKING

from ._control_shm import (
    ControlShmProtocolError,
    close_control_shm_descriptors,
    decode_handler_request,
    decode_handler_prepare_request,
    encode_error_response,
    encode_handler_response,
    encode_handler_prepare_response,
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
)

if TYPE_CHECKING:
    from ._control_shm_transport import ControlShmCounters


class ControlShmServerConnection:
    """Per-accepted-socket receiver/request mapping and server-owned reply mapping."""

    def __init__(self, policy: ControlShmPolicy, budget: ControlShmBudget, counters: ControlShmCounters):
        self.policy = policy
        self._budget = budget
        self._counters = counters
        self._request_region: ControlShmRegion | None = None
        self._response_region: ControlShmRegion | None = None
        self._response_descriptor_sent = False
        self._prepared_capacities: tuple[int, int] | None = None
        self._prepared_identity: tuple[str, str] | None = None
        self._closed = False
        self._request_deadline: float | None = None
        self._budget.acquire_channel()
        self._counters.increment("channels_created")

    def receive_request(
        self, local_socket: socket.socket, *,
        authenticate: Callable[[float], None] | None = None,
    ) -> dict[str, Any] | None:
        try:
            idle_timeout = local_socket.gettimeout()
            idle_deadline = None if idle_timeout is None else time.monotonic() + idle_timeout
            while True:
                remaining = None if idle_deadline is None else max(0.0, idle_deadline - time.monotonic())
                try:
                    readable, _, _ = select.select([local_socket], [], [], remaining)
                except InterruptedError:
                    continue
                if not readable:
                    raise socket.timeout("local Control v5 handler is idle")
                break
            frame_started_at = time.monotonic()
            frame, received_descriptors = receive_control_shm_frame(
                local_socket,
                allow_idle_prefix_wait=False,
            )
        except socket.timeout:
            raise
        except Exception as exc:
            self._counters.record_error(exc)
            raise
        if frame is None:
            if received_descriptors:
                close_control_shm_descriptors(received_descriptors)
                raise ControlShmProtocolError("local Control v5 peer closed with an attached descriptor")
            return None
        if received_descriptors:
            self._counters.increment("fd_received", len(received_descriptors))
        self._counters.increment("frames_received")
        try:
            preparing = local_control_frame_kind(frame) == 13
            request = decode_handler_prepare_request(frame) if preparing else decode_handler_request(frame)
            deadline = frame_started_at + request["remaining_ns"] / 1_000_000_000.0
            self._request_deadline = deadline
            if authenticate is not None:
                try:
                    authenticate(deadline)
                except socket.timeout as exc:
                    raise ControlShmProtocolError("manager proof exceeded the handler deadline") from exc
            self._check_request_deadline()
            if preparing and (self._request_region is not None or self._response_region is not None):
                raise ControlShmProtocolError("handler preparation requires a fresh lane")
            if self._prepared_identity is not None and (request["sender"], request["topic"]) != self._prepared_identity:
                raise ControlShmProtocolError("prepared handler lane identity changed")
            lease = normalize_control_shm_lease(request["payload_lease"])
            if self._prepared_capacities is not None:
                if lease["length"] > self._prepared_capacities[0]:
                    raise ControlShmCapacityError("request exceeds the prepared payload capacity")
                if self._request_region is None or self._request_region.token != lease["region_name"]:
                    raise ControlShmProtocolError("prepared handler request mapping cannot change")
            self._validate_region_size(lease["slot_size"])
            if self._request_region is not None and self._request_region.token == lease["region_name"]:
                if received_descriptors:
                    raise ControlShmProtocolError("local Control v5 repeated a request token with a descriptor")
                if self._request_region.size != lease["slot_size"]:
                    raise ControlShmProtocolError("local Control v5 request token changed its mapped region size")
                self._counters.increment("received_mapping_reuses")
            else:
                if len(received_descriptors) != 1:
                    raise ControlShmProtocolError(
                        "new local Control v5 request token must carry exactly one descriptor"
                    )
                received_descriptor = received_descriptors[-1]
                self._drop_request_region()
                received_descriptors.pop()
                replacement = ControlShmRegion.from_received_descriptor(
                    lease["region_name"],
                    lease["slot_size"],
                    received_descriptor,
                    self._budget,
                )
                self._request_region = replacement
                self._counters.increment("received_mapping_opens")
            if received_descriptors:
                raise ControlShmProtocolError("local Control v5 request carried extra descriptors")
            assert self._request_region is not None
            if preparing:
                capacities = (request["request_capacity"], request["response_capacity"])
                self.policy.region_size_for(capacities[0])
                if self._request_region.size < capacities[0] + CONTROL_SHM_PAYLOAD_OFFSET:
                    raise ControlShmCapacityError("prepared request mapping is below declared capacity")
                self._request_region.prepare_memory()
                self._response_region = ControlShmRegion.create(
                    self.policy.region_size_for(capacities[1]), self._budget,
                )
                self._counters.increment("response_region_creates")
                self._response_region.prepare_memory()
                response_lease = self._response_region.write_payload(b"", 0)
                send_control_shm_frame(
                    local_socket, encode_handler_prepare_response(response_lease),
                    self._response_region.file_descriptor,
                    deadline=deadline,
                )
                self._check_request_deadline()
                self._response_descriptor_sent = True
                self._prepared_capacities = capacities
                self._prepared_identity = (request["sender"], request["topic"])
                self._counters.increment("fd_sent")
                self._counters.increment("frames_sent")
                return request
            request["payload"] = self._request_region.read_payload(lease)
            self._check_request_deadline()
            self._counters.increment("payload_reads")
            self._counters.increment("payload_bytes_read", len(request["payload"]))
            self._counters.increment("request_payload_bytes", len(request["payload"]))
            return request
        except Exception as exc:
            if received_descriptors:
                close_control_shm_descriptors(received_descriptors)
            self._counters.record_error(exc)
            raise

    def send_response(
        self,
        local_socket: socket.socket,
        *,
        status: int,
        timestamp_ns: int,
        payload: bytes,
    ) -> None:
        self._check_request_deadline()
        data = bytes(payload)
        if len(data) > CONTROL_SHM_MAX_PAYLOAD_BYTES:
            raise ControlShmCapacityError("local Control payload exceeds the SDK payload limit")
        if self._prepared_capacities is not None and len(data) > self._prepared_capacities[1]:
            raise ControlShmCapacityError("response exceeds the prepared payload capacity")
        previous_region = self._response_region
        desired_size = self.policy.region_size_for(
            len(data),
            previous_region.size if previous_region is not None else 0,
        )
        if previous_region is None or previous_region.size < desired_size:
            if self._prepared_capacities is not None:
                raise ControlShmProtocolError("prepared handler response region cannot grow")
            replacement = ControlShmRegion.create(desired_size, self._budget)
            self._response_region = replacement
            self._response_descriptor_sent = False
            self._counters.increment("response_region_creates")
            if previous_region is not None:
                self._counters.increment("response_region_grows")
                previous_region.close()
        else:
            self._counters.increment("response_region_reuses")
        assert self._response_region is not None
        descriptor = self._response_region.write_payload(data, timestamp_ns)
        frame = encode_handler_response(status, timestamp_ns, descriptor)
        descriptor_to_send = not self._response_descriptor_sent
        send_control_shm_frame(
            local_socket,
            frame,
            self._response_region.file_descriptor if descriptor_to_send else None,
            deadline=self._request_deadline,
        )
        if descriptor_to_send:
            self._counters.increment("fd_sent")
        self._response_descriptor_sent = True
        self._counters.increment("payload_writes")
        self._counters.increment("payload_bytes_written", len(data))
        self._counters.increment("frames_sent")
        self._counters.increment("response_payload_bytes", len(data))

    def _check_request_deadline(self) -> None:
        if self._request_deadline is not None and time.monotonic() >= self._request_deadline:
            raise ControlShmProtocolError("local Control v5 handler deadline exceeded")

    def send_error(self, local_socket: socket.socket, message: str) -> None:
        send_control_shm_frame(local_socket, encode_error_response(message))
        self._counters.increment("frames_sent")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._drop_request_region()
        finally:
            try:
                self._drop_response_region()
            finally:
                self._budget.release_channel()
                self._counters.increment("channels_closed")

    def _validate_region_size(self, region_size: int) -> None:
        if region_size < self.policy.minimum_region_bytes or region_size > self.policy.maximum_region_bytes:
            raise ControlShmProtocolError("local Control v5 lease is outside the negotiated region bounds")

    def _drop_request_region(self) -> None:
        region = self._request_region
        self._request_region = None
        if region is not None:
            try:
                region.close()
            finally:
                self._counters.increment("mapping_closes")

    def _drop_response_region(self) -> None:
        region = self._response_region
        self._response_region = None
        self._response_descriptor_sent = False
        if region is not None:
            region.close()
