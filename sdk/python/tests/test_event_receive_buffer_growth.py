"""The native Event receive loop grows its buffers once to the lengths Core reports with BufferTooSmall.

The fake library follows the Core contract: a buffer too small for the front Event keeps it queued and
reports every required length; the receive loop retries once with buffers of exactly those lengths.
"""
from __future__ import annotations

import ctypes
import threading
import unittest
from types import SimpleNamespace

from r2kernel.core.event_client import EventClient

_BUFFER_TOO_SMALL = -4
_NOT_FOUND = -2


class _KeepingEventLib:
    """recv_ex with the Core keep-and-report contract for one queued Event."""

    def __init__(self, client: EventClient, topic: bytes, publisher: bytes, payload: bytes,
                 report_lengths: bool = True) -> None:
        self.client = client
        self.queue = [(topic, publisher, payload)]
        self.report_lengths = report_lengths
        self.calls: list[tuple[int, int, int]] = []

    def r2kernel_event_client_recv_ex(self, handle, timeout_ms, topic_buf, topic_cap, topic_len, pub_buf, pub_cap,
                                      pub_len, ts, published_ts, received_ts, payload_buf, payload_cap,
                                      payload_len):
        if not self.queue or len(self.calls) >= 8:  # bounded: a loop that never grows must still end
            self.client._rust_recv_stop.set()
            return _NOT_FOUND
        self.calls.append((topic_cap, pub_cap, payload_cap))
        topic, publisher, payload = self.queue[0]
        if len(topic) > topic_cap or len(publisher) > pub_cap or len(payload) > payload_cap:
            if self.report_lengths:
                topic_len._obj.value, pub_len._obj.value, payload_len._obj.value = (
                    len(topic), len(publisher), len(payload))
            else:
                self.queue.pop(0)  # an older Core consumed the Event without reporting lengths
            return _BUFFER_TOO_SMALL
        self.queue.pop(0)
        ctypes.memmove(topic_buf, topic, len(topic))
        ctypes.memmove(pub_buf, publisher, len(publisher))
        ctypes.memmove(payload_buf, payload, len(payload))
        topic_len._obj.value, pub_len._obj.value, payload_len._obj.value = len(topic), len(publisher), len(payload)
        ts._obj.value = published_ts._obj.value = received_ts._obj.value = 7
        return 0


class _Logger:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, _log_id, message) -> None:
        self.errors.append(message)

    def debug(self, *_args, **_kwargs) -> None:
        pass


def _client() -> EventClient:
    client = object.__new__(EventClient)
    client._logger = _Logger()
    client._rust_event_handle = object()
    client._rust_recv_stop = threading.Event()
    client._max_subscribe_payload_size = 1024
    client._rust_last_event_ts = {}
    client._topic_handlers = {}
    return client


class EventReceiveBufferGrowthTests(unittest.TestCase):
    def test_oversized_event_is_delivered_after_one_retry_with_reported_lengths(self) -> None:
        client = _client()
        topic, publisher, payload = b"t" * 300, b"publisher", b"p" * 5000
        lib = _KeepingEventLib(client, topic, publisher, payload)
        client._rust_core = SimpleNamespace(lib=lib)
        delivered: list[tuple[bytes, int]] = []
        client._topic_handlers[topic.decode()] = lambda data, ts, _meta: delivered.append((data, ts))
        client._rust_recv_loop()
        self.assertEqual(delivered, [(payload, 7)])
        self.assertEqual(lib.calls, [(256, 256, 1024), (300, 256, 5000)])
        self.assertEqual(client._logger.errors, [])

    def test_too_small_without_reported_lengths_is_an_explicit_error(self) -> None:
        client = _client()
        lib = _KeepingEventLib(client, b"t" * 300, b"publisher", b"x", report_lengths=False)
        client._rust_core = SimpleNamespace(lib=lib)
        client._rust_recv_loop()
        self.assertEqual(lib.calls, [(256, 256, 1024)])
        self.assertEqual(client._logger.errors, [
            "Event receive buffer too small, but Core reported no larger length (topic=0, publisher=0, "
            "payload=0); the Event was not delivered"])


if __name__ == "__main__":
    unittest.main()
