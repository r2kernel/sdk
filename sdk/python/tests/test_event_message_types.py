from __future__ import annotations

import unittest
import threading
import fcntl
import mmap
import tempfile
import os
import struct
from types import SimpleNamespace
from typing import Any

from r2kernel.core.event_client import EventClient, EventTimingMetadata
from r2kernel.core.subsystem_runtime import EventTransportManager
from r2kernel.core.topic_buffer import _Flock, _NamedShmAccess, _PyTopicBufferPool, SEQ_READY, SEQ_WRITING


class _Logger:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, _log_id: str, message: str) -> None:
        self.errors.append(message)

    def warning(self, _log_id: str, message: str) -> None:
        self.warnings.append(message)

    def debug(self, _log_id: str, _message: str) -> None:
        pass


class _Owner:
    name = "event_owner"


class _Client:
    def __init__(self) -> None:
        self.publish_requests: list[dict[str, Any]] = []
        self.published: list[dict[str, Any]] = []
        self.subscriptions: list[dict[str, Any]] = []

    def request_publish_slots(self, **kwargs: Any) -> bool:
        self.publish_requests.append(kwargs)
        return True

    def publish_event_typed(self, **_kwargs: Any) -> bool:
        raise AssertionError("spec message refs must use the message codec path, not primitive typed publish")

    def publish_event(self, **kwargs: Any) -> bool:
        self.published.append(kwargs)
        return True

    def subscribe_event(self, **kwargs: Any) -> bool:
        self.subscriptions.append(kwargs)
        return True

    def start_event_transport(self) -> None:
        pass

    def stop_event_transport(self) -> None:
        pass


class EventMessageTypeTests(unittest.TestCase):
    def test_python_receiver_retires_without_invoking_removed_handler(self) -> None:
        client = self._bare_event_client()
        key = ('probe', 'publisher')
        client._notify_stop = threading.Event()
        client._subscribe_topic_generations = {}
        client._logger = _Logger()

        def removed_handler(*_arguments):
            self.fail('removed broker handler was invoked')

        client._subscribe_slots = {key: (0, 1, 1, removed_handler, None)}
        client._buffer_pool = self._local_buffer()
        client._now_ns = lambda: 103
        client._event_subscription_worker(key)
        self.assertEqual(client._subscribe_topic_generations[key], -1)
        self.assertTrue(any('no longer registered: probe' in error for error in client._logger.errors))

    def test_python_receiver_uses_reinitialized_handler_with_unchanged_slot(self) -> None:
        client = self._bare_event_client()
        key = ('probe', 'publisher')
        client._notify_stop = threading.Event()
        client._subscribe_topic_generations = {}
        received = []

        def obsolete_handler(*_arguments):
            self.fail('unchanged broker slot retained obsolete prepared handler')

        def current_handler(payload, timestamp, timing):
            received.append((payload, timestamp, timing.published_at_sync_ns))
            client._notify_stop.set()

        client._subscribe_slots = {key: (0, 1, 1, obsolete_handler, None)}
        client._topic_handlers['probe'] = current_handler
        client._buffer_pool = self._local_buffer()
        client._now_ns = lambda: 103
        client._event_subscription_worker(key)
        self.assertEqual(received, [(b'retained', 101, 102)])
        self.assertEqual(client._subscribe_topic_generations[key], 0)

    def _local_buffer(self, slot_size=81, slot_count=2, initial_payload=b'retained'):
        # Real local file-backed mmap/OFD locks, without named SHM or a native
        # library. Only construction is bypassed; all buffer APIs are real.
        storage = tempfile.TemporaryFile()
        storage.truncate(slot_size * slot_count)
        mapping = mmap.mmap(storage.fileno(), slot_size * slot_count)
        self.addCleanup(storage.close)
        self.addCleanup(mapping.close)
        pool = object.__new__(_PyTopicBufferPool)
        pool.slot_size, pool.slot_count = slot_size, slot_count
        pool.buffer_size = slot_size * slot_count
        pool._shm = SimpleNamespace(_fd=storage.fileno(), buf=mapping)
        pool._access = _NamedShmAccess(storage.fileno(), slot_size, slot_count)
        pool.publish(0, initial_payload, 0, 101, 102)
        return pool

    def test_python_buffer_single_call_state_and_rejected_publication(self):
        pool = self._local_buffer(slot_size=73)
        pool.publish(1, b'123456789', 7, 201, 202)
        before = pool.snapshot(1)
        with self.assertRaises(IndexError):
            pool.publish(1, b'1234567890', 8, 301, 302)
        with self.assertRaises(struct.error):
            pool.publish(1, b'bad', 8, 1 << 63, 302)
        with self.assertRaises(TypeError):
            pool.publish(1, 'bad', 8, 301, 302)
        self.assertEqual(pool.snapshot(1), before)
        with self.assertRaises(IndexError):
            pool.snapshot(2)
        with self.assertRaises(struct.error):
            pool.snapshot(1, expected_generation=-1)
        with pool._access(1):
            pool._shm.buf[pool.slot_size] = SEQ_WRITING
        self.assertIsNone(pool.snapshot(1))
        self.assertEqual(pool.snapshot(1, include_payload=False).seq, SEQ_WRITING)
        # Raw WRITING has no retained owner: another thread can use this slot.
        completed = threading.Event()
        worker = threading.Thread(target=lambda: (pool.publish(1, b'new', 8, 301, 0), completed.set()), daemon=True)
        worker.start()
        self.assertTrue(completed.wait(2))
        worker.join(2)
        snapshot = pool.snapshot(1)
        self.assertEqual((snapshot.payload, snapshot.generation, snapshot.timestamp,
                          snapshot.prev_timestamp, snapshot.published_timestamp),
                         (b'new', 8, 301, 201, 301))
        self.assertEqual(pool.read_payload(0), b'retained')

    def test_event_publication_preserves_previous_and_published_timestamp(self):
        client = self._bare_event_client()
        client._logger = _Logger()
        client._buffer_pool = pool = self._local_buffer()
        client._now_ns = lambda: 303
        self.assertFalse(client._write_publish_payload('probe', 0, b'x' * 18, 1, 202))
        self.assertEqual(pool.read_payload(0), b'retained')
        self.assertTrue(client._write_publish_payload('probe', 0, b'new', 1, 202))
        snapshot = pool.snapshot(0)
        self.assertEqual((snapshot.payload, snapshot.generation, snapshot.timestamp,
                          snapshot.prev_timestamp, snapshot.published_timestamp),
                         (b'new', 1, 202, 101, 303))

    def test_primitive_typed_publish_uses_the_datatype_codec_and_snapshot_publication(self):
        client = self._bare_event_client()
        client._logger = _Logger()
        client._buffer_pool = pool = self._local_buffer()
        client._now_ns = lambda: 303
        client._core_mode = "python"
        client._publish_slots = {"probe": 0}
        client._topic_max_sizes = {"probe": 17}
        client._publish_topic_generations = {"probe": 1}
        client._publish_entry_slot_counts = {}
        client._publish_queue_depths = {}
        client._release_publish_locks = lambda _topic: None
        self.assertTrue(client.publish_event_typed(topic="probe", dtype="int32_array", value=[1, -2], timestamp=None))
        snapshot = pool.snapshot(0)
        self.assertEqual((snapshot.payload, snapshot.generation, snapshot.timestamp),
                         (bytes.fromhex("0000000200000001fffffffe"), 1, 303))
        self.assertFalse(client.publish_event_typed(topic="probe", dtype="msg.unknown/Type", value={}, timestamp=1))

    def test_empty_snapshots_expected_generation_and_reset(self):
        pool = self._local_buffer(slot_size=64, initial_payload=b'')
        pool.publish(1, b'', 9, 401, -1)
        self.assertIsNone(pool.snapshot(1, expected_generation=8))
        value = pool.snapshot(1, expected_generation=9)
        self.assertEqual((value.payload, value.length, value.published_timestamp), (b'', 0, 401))
        self.assertIsNone(pool.snapshot(1, include_payload=False).payload)
        pool.reset_slot(1)
        value = pool.snapshot(1)
        self.assertEqual((value.seq, value.payload, value.generation, value.timestamp,
                          value.prev_timestamp, value.published_timestamp),
                         (SEQ_READY, b'', 0, 0, 0, 0))

    def test_full_copy_rejects_generation_overwritten_after_header_scan(self):
        pool = self._local_buffer()
        observed = pool.snapshot(0, include_payload=False)
        pool.publish(0, b'replacement', observed.generation + 1, 201, 202)
        self.assertIsNone(pool.snapshot(0, expected_generation=observed.generation))
        current = pool.snapshot(0, expected_generation=observed.generation + 1)
        self.assertEqual((current.payload, current.timestamp), (b'replacement', 201))

    def test_corrupt_length_releases_slot_and_cannot_copy_adjacent_payload(self):
        pool = self._local_buffer()
        with pool._access(0):
            struct.pack_into('<I', pool._shm.buf, 8, 0xffffffff)
        with self.assertRaises(IndexError):
            pool.snapshot(0)
        with self.assertRaises(IndexError):
            pool.snapshot(0, include_payload=False)
        pool.publish(0, b'recovered', 1, 201, 202)
        self.assertEqual(pool.read_payload(0), b'recovered')

    def test_same_handle_different_slot_snapshot_does_not_wait(self):
        pool = self._local_buffer(slot_size=73)
        pool.publish(1, b'other', 1, 201, 202)
        result = []
        completed = threading.Event()
        def read_other_slot():
            result.append(pool.snapshot(1))
            completed.set()
        worker = threading.Thread(target=read_other_slot, daemon=True)
        with pool._access(0):
            worker.start()
            independent = completed.wait(2)
        worker.join(2)
        self.assertTrue(independent, 'different slots shared a local ownership mutex')
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0].payload, b'other')

    def test_concurrent_publication_snapshots_keep_payload_and_metadata_coherent(self):
        pool = self._local_buffer(slot_size=129)
        pool.publish(1, struct.pack('<Q', 0), 0, 100, 0)
        started, finished = threading.Event(), threading.Event()
        errors = []
        def write_versions():
            try:
                started.set()
                for generation in range(1, 501):
                    payload = struct.pack('<Q', generation) + bytes([generation % 256]) * (generation % 48)
                    pool.publish(1, payload, generation, generation + 100, -1)
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        worker = threading.Thread(target=write_versions, daemon=True)
        worker.start()
        self.assertTrue(started.wait(2))
        for _ in range(1000):
            value = pool.snapshot(1)
            generation = value.generation
            self.assertEqual(value.payload, struct.pack('<Q', generation) + bytes([generation % 256]) * (generation % 48))
            self.assertEqual(value.length, len(value.payload))
            self.assertEqual(value.timestamp, generation + 100)
            self.assertEqual(value.published_timestamp, value.timestamp)
            self.assertEqual(value.prev_timestamp, generation + 99 if generation else 0)
            if finished.is_set():
                break
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_strided_queue_receiver_reads_candidates_without_owning_other_slots(self):
        client = self._bare_event_client()
        key = ('probe', 'publisher')
        client._notify_stop = threading.Event()
        client._subscribe_topic_generations = {key: 1}
        client._buffer_pool = pool = self._local_buffer(slot_size=81, slot_count=4)
        pool.publish(1, b'second', 2, 202, 302)
        pool.publish(3, b'third', 3, 203, 303)
        client._now_ns = lambda: 403
        received, errors = [], []
        completed = threading.Event()
        def handler(payload, timestamp, timing):
            received.append((payload, timestamp, timing.published_at_sync_ns))
            if len(received) == 2:
                client._notify_stop.set()
        def receive():
            try:
                client._event_subscription_worker(key)
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()
        client._topic_handlers['probe'] = handler
        client._subscribe_slots = {key: (1, 2, 2, handler, None)}
        worker = threading.Thread(target=receive, daemon=True)
        with pool._access(0):
            worker.start()
            independent = completed.wait(2)
        client._notify_stop.set()
        worker.join(2)
        self.assertTrue(independent)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(received, [(b'second', 202, 302), (b'third', 203, 303)])
        self.assertEqual(client._subscribe_topic_generations[key], 3)

    def test_python_receiver_delivers_restarted_generation(self):
        client = self._bare_event_client()
        key = ('probe', 'publisher')
        client._notify_stop = threading.Event()
        client._subscribe_topic_generations = {key: 5}
        client._buffer_pool = self._local_buffer()
        client._now_ns = lambda: 103
        received = []

        def handler(payload, timestamp, timing):
            received.append((payload, timestamp, timing.published_at_sync_ns))
            client._notify_stop.set()

        client._topic_handlers['probe'] = handler
        client._subscribe_slots = {key: (0, 1, 1, handler, None)}
        client._event_subscription_worker(key)
        self.assertEqual(received, [(b'retained', 101, 102)])
        self.assertEqual(client._subscribe_topic_generations[key], 0)

    def test_slot_ranges_exclude_independent_opens_and_release_after_exception(self):
        with tempfile.NamedTemporaryFile() as first:
            first.truncate(146)
            with open(first.name, 'r+b') as second:
                access = _NamedShmAccess(first.fileno(), 73, 2)
                same = _Flock(fcntl.F_WRLCK, os.SEEK_SET, 73, 73, 0)
                other = _Flock(fcntl.F_WRLCK, os.SEEK_SET, 0, 73, 0)
                with self.assertRaisesRegex(ValueError, 'operation failed'):
                    with access(1):
                        with self.assertRaises(BlockingIOError):
                            fcntl.fcntl(second.fileno(), fcntl.F_OFD_SETLK, bytes(same))
                        fcntl.fcntl(second.fileno(), fcntl.F_OFD_SETLK, bytes(other))
                        other.type = fcntl.F_UNLCK
                        fcntl.fcntl(second.fileno(), fcntl.F_OFD_SETLK, bytes(other))
                        raise ValueError('operation failed')
                fcntl.fcntl(second.fileno(), fcntl.F_OFD_SETLK, bytes(same))
                same.type = fcntl.F_UNLCK
                fcntl.fcntl(second.fileno(), fcntl.F_OFD_SETLK, bytes(same))

    @staticmethod
    def _bare_event_client() -> EventClient:
        client = object.__new__(EventClient)
        client._subsystem_name = "event_owner"
        client._topic_handlers = {}
        client._max_subscribe_payload_size = 0
        client._register_subscription = lambda **_kwargs: True
        return client

    def test_event_subscription_preserves_legacy_two_argument_callback(self) -> None:
        client = self._bare_event_client()
        received: list[tuple[bytes, int]] = []

        self.assertTrue(client.subscribe_event("probe", 32, lambda payload, timestamp: received.append((payload, timestamp))))
        timing = EventTimingMetadata(10, 20, 30)
        client._topic_handlers["probe"](b"payload", 10, timing)

        self.assertEqual(received, [(b"payload", 10)])

    def test_event_subscription_delivers_transport_timing_metadata(self) -> None:
        client = self._bare_event_client()
        received: list[EventTimingMetadata] = []

        self.assertTrue(
            client.subscribe_event(
                "probe",
                32,
                lambda _payload, _timestamp, timing: received.append(timing),
            )
        )
        timing = EventTimingMetadata(10, 20, 30)
        client._topic_handlers["probe"](b"payload", 10, timing)

        self.assertEqual(received, [timing])

    def test_register_publish_accepts_full_spec_message_id(self) -> None:
        logger = _Logger()
        client = _Client()
        manager = EventTransportManager(logger=logger, owner=_Owner(), client=client)  # type: ignore[arg-type]

        ok = manager.register_publish(
            topic="probe_servo",
            type="msg.actuator.servo.v1/ServoControlRequest",
            size=4096,
        )

        self.assertTrue(ok)
        self.assertEqual(logger.errors, [])
        self.assertEqual(client.publish_requests[0]["size"], 4096)
        self.assertIn(("probe_servo", "msg.actuator.servo.v1/ServoControlRequest"), manager.get_publish_info())

    def test_register_subscribe_accepts_full_spec_message_id(self) -> None:
        logger = _Logger()
        client = _Client()
        manager = EventTransportManager(logger=logger, owner=_Owner(), client=client)  # type: ignore[arg-type]

        manager.register_subscribe(
            topic="probe_camera",
            type="msg.sensor.camera.v1/CameraCycleResponse",
            size=262144,
        )

        self.assertEqual(logger.errors, [])
        self.assertEqual(client.subscriptions[0]["size"], 262144)
        self.assertIn(("probe_camera", "msg.sensor.camera.v1/CameraCycleResponse"), manager.get_subscribe_info())

    def test_publish_full_spec_message_id_uses_message_codec(self) -> None:
        try:
            from r2kernel.interface.codec import RustMessageCodec

            codec = RustMessageCodec()
            codec.get_message_schema("msg.core.common.primitive.v1/StringValue")
        except Exception as exc:
            self.skipTest(f"Rust message codec is not available: {exc}")

        logger = _Logger()
        client = _Client()
        manager = EventTransportManager(logger=logger, owner=_Owner(), client=client)  # type: ignore[arg-type]
        ok = manager.register_publish(
            topic="probe_text",
            type="msg.core.common.primitive.v1/StringValue",
            size=1024,
        )
        self.assertTrue(ok)

        published = manager.publish_event(topic="probe_text", value={"value": "hello"}, timestamp=123)

        self.assertTrue(published)
        self.assertEqual(logger.errors, [])
        payload = client.published[0]["payload"]
        self.assertEqual(codec.decode_message("msg.core.common.primitive.v1/StringValue", payload), {"value": "hello"})


if __name__ == "__main__":
    unittest.main()
