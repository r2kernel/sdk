"""Publisher lock-handle lifetime: a notify-thread replacement must never close a handle mid-post.

Regression for the Python Event publisher SIGSEGV at startup: the backend sends `notify_subscriber`
right after a publish slot is allocated, the notify thread replaced the subscriber's lock handle and
closed the old one while the publishing thread was posting it (native use-after-free).
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import unittest
from pathlib import Path

from r2kernel.core import _keywords as KW
from r2kernel.core.event_client import EventClient
from r2kernel.helper.logger import coerce_logger

SDK_ROOT = Path(__file__).resolve().parents[1]


class _Handle:
    def __init__(self, name: str):
        self.name = name
        self.posting = False
        self.closed = False


class _RecordingBackend:
    """Lock backend whose post blocks until the test lets it finish, recording unsafe closes."""

    def __init__(self):
        self.post_started = threading.Event()
        self.finish_post = threading.Event()
        self.closed_while_posting: list[str] = []
        self.posted_after_close: list[str] = []

    def open(self, name):
        return _Handle(name)

    def release(self, handle):
        if handle.closed:
            self.posted_after_close.append(handle.name)
        handle.posting = True
        self.post_started.set()
        self.finish_post.wait(timeout=5.0)
        handle.posting = False

    def close(self, handle):
        if handle.posting:
            self.closed_while_posting.append(handle.name)
        handle.closed = True


def _client(backend) -> EventClient:
    client = object.__new__(EventClient)
    client._startup_barrier_cv = threading.Condition()
    client._lock_backend = backend
    client._logger = coerce_logger(None, default_name="lock-lifetime-test")
    client._buffer_pool = None
    client._publish_slots = {}
    client._publish_locks = {"topic": {"sub": backend.open("/lock")}}
    return client


class PublishLockLifetimeTests(unittest.TestCase):
    def test_replacement_waits_for_in_flight_post(self):
        backend = _RecordingBackend()
        client = _client(backend)
        original = client._publish_locks["topic"]["sub"]
        publisher = threading.Thread(target=client._release_publish_locks, args=("topic",))
        publisher.start()
        self.assertTrue(backend.post_started.wait(timeout=5.0))
        message = {KW.COMMAND: KW.NOTIFY_SUBSCRIBER, KW.TOPIC_NAME: "topic", KW.SUBSYSTEM_NAME: "sub",
                   KW.LOCK_NAME: "/lock"}
        notifier = threading.Thread(target=client._process_notify_message, args=(message,))
        notifier.start()
        notifier.join(timeout=0.2)
        # The replacement must not complete (and must not close the handle) while the post is in flight.
        self.assertTrue(notifier.is_alive())
        self.assertFalse(original.closed)
        backend.finish_post.set()
        publisher.join(timeout=5.0)
        notifier.join(timeout=5.0)
        self.assertFalse(publisher.is_alive() or notifier.is_alive())
        self.assertEqual(backend.closed_while_posting, [])
        self.assertTrue(original.closed)
        replacement = client._publish_locks["topic"]["sub"]
        self.assertIsNot(replacement, original)
        backend.post_started.clear()
        client._release_publish_locks("topic")
        self.assertEqual(backend.posted_after_close, [])

    def test_native_lock_replacement_race_does_not_crash(self):
        """Real Core lock handles: before the fix this crashed with SIGSEGV in r2kernel_lock_post."""
        script = textwrap.dedent(
            """
            import faulthandler, os, sys, threading, uuid
            faulthandler.enable()
            from r2kernel.core import _keywords as KW
            from r2kernel.core.event_client import EventClient
            from r2kernel.core.lock_backend import RustLockBackend, get_lock_backend
            from r2kernel.helper.logger import coerce_logger
            backend = get_lock_backend()
            if not isinstance(backend, RustLockBackend):
                raise SystemExit(f"native lock backend unavailable: {type(backend).__name__}")
            name = f"/r2k_lock_lifetime_{os.getpid()}_{uuid.uuid4().hex[:8]}"
            owner = backend.create(name)
            assert owner is not None
            client = object.__new__(EventClient)
            client._startup_barrier_cv = threading.Condition()
            client._lock_backend = backend
            client._logger = coerce_logger(None, default_name="race")
            client._buffer_pool = None
            client._publish_slots = {}
            client._publish_locks = {"topic": {"sub": backend.open(name)}}
            stop = threading.Event()
            def publisher():
                while not stop.is_set():
                    client._release_publish_locks("topic")
            def drain():
                while not stop.is_set():
                    backend.try_acquire(owner)
            message = {KW.COMMAND: KW.NOTIFY_SUBSCRIBER, KW.TOPIC_NAME: "topic", KW.SUBSYSTEM_NAME: "sub",
                       KW.LOCK_NAME: name}
            threads = [threading.Thread(target=publisher), threading.Thread(target=drain)]
            for thread in threads:
                thread.start()
            try:
                for _ in range(int(sys.argv[1])):
                    client._process_notify_message(message)
            finally:
                stop.set()
                for thread in threads:
                    thread.join()
                for lock in client._publish_locks["topic"].values():
                    backend.close(lock)
                backend.close(owner)
                backend.unlink(name)
            """
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SDK_ROOT), env.get("PYTHONPATH")]))
        env.pop("R2KERNEL_FORCE_PYTHON", None)
        completed = subprocess.run([sys.executable, "-c", script, "5000"], env=env, capture_output=True,
                                   text=True, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr[-4000:])


if __name__ == "__main__":
    unittest.main()
