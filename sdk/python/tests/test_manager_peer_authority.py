"""Peer-proof contract tests; actual root broker service tests run separately on Linux."""
from __future__ import annotations

import io
import json
import os
import socket
import struct
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from r2kernel.core.resource import managed_peer_authentication_required, verify_manager_peer_connection
from r2kernel.core.native_control import NativeControlClient
from r2kernel.core.event_client import EventClient
from r2kernel.helper.logger import coerce_logger


class Candidate:
    def getsockopt(self, *_args):
        return struct.pack("3i", 123, 1000, 1000)


class Broker:
    def __init__(self, *, uid=0, authorized=True):
        self.uid = uid
        self.request = None
        self.chunks = [json.dumps({"ok": True, "result": {"authorized": authorized}}).encode(), b""]
    def __enter__(self): return self
    def __exit__(self, *_args): pass
    def settimeout(self, _timeout): pass
    def connect(self, _path): pass
    def shutdown(self, _mode): pass
    def getsockopt(self, *_args): return struct.pack("3i", 456, self.uid, 0)
    def sendall(self, request): self.request = json.loads(request)
    def recv(self, _limit): return self.chunks.pop(0)


@unittest.skipUnless(hasattr(socket, "SO_PEERCRED"), "Linux kernel credentials required")
class ManagerPeerProofTest(unittest.TestCase):
    def verify(self, broker):
        # /proc starttime is field 22: index 19 after the command's closing ')'.
        stat = "123 (command with spaces) " + " ".join(["S"] + ["0"] * 18 + ["42"])
        with patch("r2kernel.core.resource.socket.socket", return_value=broker), patch("builtins.open", return_value=io.StringIO(stat)):
            verify_manager_peer_connection(Candidate(), "/run/root-proof.sock")

    def test_actual_kernel_pid_and_birth_are_sent_to_root_broker(self):
        broker = Broker()
        self.verify(broker)
        self.assertEqual(broker.request, {"op": "verify_control_manager_peer", "pid": 123, "process_start_ticks": 42})

    def test_nonroot_broker_is_rejected_before_request(self):
        broker = Broker(uid=1000)
        with self.assertRaisesRegex(RuntimeError, "root-owned"):
            self.verify(broker)
        self.assertIsNone(broker.request)

    def test_broker_denial_is_explicit(self):
        with self.assertRaisesRegex(RuntimeError, "without backend manager"):
            self.verify(Broker(authorized=False))


class DevelopmentPolicyTest(unittest.TestCase):
    def test_only_explicit_development_mode_disables_proof(self):
        with patch.dict(os.environ, {}, clear=True): self.assertTrue(managed_peer_authentication_required())
        with patch.dict(os.environ, {"R2KERNEL_RESOURCE_SERVICE_MODE": "disabled"}): self.assertFalse(managed_peer_authentication_required())
        with patch.dict(os.environ, {"R2KERNEL_RESOURCE_SERVICE_MODE": "nonsense"}):
            with self.assertRaisesRegex(RuntimeError, "Invalid"): managed_peer_authentication_required()

    def test_server_response_cannot_downgrade_production_sdk(self):
        client = object.__new__(NativeControlClient)
        with patch.dict(os.environ, {"R2KERNEL_RESOURCE_SERVICE_MODE": "required"}):
            with self.assertRaisesRegex(RuntimeError, "cannot accept disabled"):
                client.configure_from_control_registration({"manager_peer_auth_required": False})


@unittest.skipUnless(os.name == "posix", "Unix notification sockets required")
class NotificationSocketPermissionTest(unittest.TestCase):
    def test_configured_ipc_group_is_published_on_socket(self):
        import grp
        group = grp.getgrgid(os.getgid())
        for configured in [group.gr_name, str(group.gr_gid)]:
            with self.subTest(group=configured), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "R2KERNEL_CONTROL_NOTIFY_DIR": directory,
                "R2KERNEL_RUNTIME_IPC_GROUP": configured,
            }):
                client = object.__new__(EventClient)
                client._logger = coerce_logger(None, default_name="test")
                client._setup_notify_endpoint()
                try:
                    metadata = os.stat(client._notify_endpoint)
                    self.assertEqual(metadata.st_gid, group.gr_gid)
                    self.assertEqual(metadata.st_mode & 0o777, 0o660)
                finally:
                    client._notify_socket.close()
                    os.unlink(client._notify_endpoint)

    def test_unknown_group_rejects_startup_and_removes_socket(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "R2KERNEL_CONTROL_NOTIFY_DIR": directory,
            "R2KERNEL_RUNTIME_IPC_GROUP": "r2kernel-test-group-must-not-exist",
        }):
            client = object.__new__(EventClient)
            client._logger = coerce_logger(None, default_name="test")
            with self.assertRaisesRegex(RuntimeError, "configured IPC permissions"):
                client._setup_notify_endpoint()
            self.assertEqual(list(Path(directory).iterdir()), [])
