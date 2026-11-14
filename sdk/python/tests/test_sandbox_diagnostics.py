"""Brokered-sandbox EPERM diagnostics and the start-hook False report."""
from __future__ import annotations

import errno
import os
import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from r2kernel.core import sandbox
from r2kernel.datatype.common import DeviceState
from r2kernel.helper.logger import Logger
from r2kernel.wrapper._subsystem_lifecycle import LOG_ID_LIFECYCLE, _LifecycleMixin

_REAL_SOCKET = socket.socket


_RAW_SOCKET_PERMITTED = os.geteuid() == 0


def _socket_eperm() -> None:
    """Raises a genuine EPERM from the socket() call: an ICMP raw socket needs CAP_NET_RAW."""
    probe = _REAL_SOCKET(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    probe.close()
    raise AssertionError("raw sockets are permitted here; run these tests without CAP_NET_RAW")


def _seccomp_like(*denied_families):
    """socket.socket replacement that fails like the brokered seccomp filter for the given families."""
    def factory(family=-1, type=-1, proto=-1, fileno=None):
        if family in denied_families:
            _socket_eperm()
        return _REAL_SOCKET(family, type, proto) if fileno is None else _REAL_SOCKET(family, type, proto, fileno)
    return factory


def _caught(call) -> BaseException:
    try:
        call()
    except BaseException as exc:  # noqa: BLE001 - the test inspects the raised exception
        return exc
    raise AssertionError("expected an exception")


@unittest.skipIf(_RAW_SOCKET_PERMITTED, "root may create raw sockets; a socket() EPERM cannot be produced")
class SocketCreationEpermTests(unittest.TestCase):
    def test_only_socket_creation_eperm_is_explained(self) -> None:
        socket_eperm = _caught(_socket_eperm)
        self.assertEqual(getattr(socket_eperm, "errno", None), errno.EPERM)
        other_eperm = _caught(lambda: os.kill(1, 0))  # EPERM from kill(), not from socket()
        self.assertEqual(getattr(other_eperm, "errno", None), errno.EPERM)
        with patch.dict(os.environ, {sandbox.SANDBOX_NETWORK_ENV: "brokered"}, clear=True), \
                patch.object(sandbox.socket, "socket", _seccomp_like(socket.AF_INET, socket.AF_INET6)):
            self.assertIsNotNone(sandbox.explain_permission_error(socket_eperm))
            self.assertIsNone(sandbox.explain_permission_error(other_eperm))
            self.assertIsNone(sandbox.explain_permission_error(PermissionError(errno.EPERM, "raised by code")))
            wrapped = RuntimeError("scheduling setup failed")
            wrapped.__cause__ = other_eperm
            self.assertIsNone(sandbox.explain_permission_error(wrapped))


@unittest.skipIf(_RAW_SOCKET_PERMITTED, "root may create raw sockets; a socket() EPERM cannot be produced")
class SandboxDenialTests(unittest.TestCase):
    def test_no_sandbox_env_means_no_explanation(self) -> None:
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(sandbox.socket, "socket", _seccomp_like(socket.AF_INET, socket.AF_INET6)):
            self.assertEqual(sandbox.sandbox_socket_denials(), [])
            self.assertIsNone(sandbox.explain_permission_error(_caught(_socket_eperm)))

    def test_brokered_network_denial_is_named_with_alternatives(self) -> None:
        with patch.dict(os.environ, {sandbox.SANDBOX_NETWORK_ENV: "brokered"}, clear=True), \
                patch.object(sandbox.socket, "socket", _seccomp_like(socket.AF_INET, socket.AF_INET6)):
            (message,) = sandbox.sandbox_socket_denials()
            self.assertEqual(message, (
                "socket(AF_INET/AF_INET6) denied by resources.sandbox.network=brokered; for outbound TCP use "
                "the app uplink (open_uplink_tcp), request only sockets authorized by this package's grants, "
                "use a declared frontend listener or R2Kernel interfaces, or validate a leader-passed "
                "descriptor (R2KERNEL_INHERITED_FDS)"))
            wrapped = RuntimeError("readiness failed")
            wrapped.__cause__ = _caught(_socket_eperm)
            self.assertEqual(sandbox.explain_permission_error(wrapped), message)
            self.assertIsNone(sandbox.explain_permission_error(PermissionError(errno.EACCES, "denied")))

    def test_brokered_can_denial(self) -> None:
        can = getattr(socket, "AF_CAN", 29)
        with patch.dict(os.environ, {sandbox.SANDBOX_CAN_ENV: "brokered"}, clear=True), \
                patch.object(sandbox.socket, "socket", _seccomp_like(can)):
            (message,) = sandbox.sandbox_socket_denials()
            self.assertTrue(message.startswith("socket(AF_CAN) denied by resources.sandbox.can=brokered;"))

    def test_env_without_actual_denial_is_not_reported(self) -> None:
        with patch.dict(os.environ, {sandbox.SANDBOX_NETWORK_ENV: "brokered"}, clear=True):
            self.assertEqual(sandbox.sandbox_socket_denials(), [])

    def test_network_sandbox_also_explains_can_without_separate_can_setting(self) -> None:
        can = getattr(socket, "AF_CAN", 29)
        with patch.dict(os.environ, {sandbox.SANDBOX_NETWORK_ENV: "brokered"}, clear=True), \
                patch.object(sandbox.socket, "socket", _seccomp_like(can)):
            (message,) = sandbox.sandbox_socket_denials()
            self.assertTrue(message.startswith("socket(AF_CAN) denied by resources.sandbox.network=brokered;"))


class _Lifecycle(_LifecycleMixin):
    def __init__(self, log_dir: str, hook) -> None:
        self._state = DeviceState.READY
        self._logger = Logger(name=f"sandbox-diag-{id(self)}", log_file_folder=log_dir, flag_use_console=False)
        self._runtime = SimpleNamespace(send_lifecycle_status=lambda: None)
        self.on_start = hook
        self.logged: list[str] = []
        original_error = self._logger.error

        def capture(*args, **kwargs):
            self.logged.append(" ".join(str(a) for a in args))
            original_error(*args, **kwargs)
        self._logger.error = capture

    @property
    def logger(self) -> Logger:
        return self._logger

    def on_state_changed(self, state) -> None:
        pass


class StartHookDiagnosticsTests(unittest.TestCase):
    def _run(self, hook) -> _Lifecycle:
        with tempfile.TemporaryDirectory() as log_dir:
            subject = _Lifecycle(log_dir, hook)
            try:
                subject._run_phase(label="start", hook=subject.on_start, success_state=DeviceState.OPERATING,
                                   failure_state=DeviceState.ERROR)
            finally:
                subject.logger.close()
        self.assertEqual(subject._state, DeviceState.ERROR)
        return subject

    def test_false_without_errors_says_no_exception(self) -> None:
        subject = self._run(lambda: False)
        self.assertEqual(subject.logged[-1], f"{LOG_ID_LIFECYCLE} Start failed: start hook returned False (no exception)")

    def test_false_reports_last_error_logged_during_the_hook(self) -> None:
        def hook():
            subject_holder[0].logger.error("Resource", "frontend listener request refused")
            return False
        subject_holder: list[_Lifecycle] = []
        original = _Lifecycle.__init__

        def init(self, log_dir, hook_):
            original(self, log_dir, hook_)
            subject_holder.append(self)
        with patch.object(_Lifecycle, "__init__", init):
            subject = self._run(hook)
        self.assertEqual(subject.logged[-1], (
            f"{LOG_ID_LIFECYCLE} Start failed: start hook returned False (no exception); "
            "last warning or error logged during start: [Resource] frontend listener request refused"))

    @unittest.skipIf(_RAW_SOCKET_PERMITTED, "root may create raw sockets; a socket() EPERM cannot be produced")
    def test_eperm_in_start_is_explained_under_brokered_sandbox(self) -> None:
        def hook():
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with patch.dict(os.environ, {sandbox.SANDBOX_NETWORK_ENV: "brokered"}), \
                patch.object(sandbox.socket, "socket", _seccomp_like(socket.AF_INET, socket.AF_INET6)):
            subject = self._run(hook)
        self.assertTrue(subject.logged[-1].startswith(
            f"{LOG_ID_LIFECYCLE} Start failed with EPERM: socket(AF_INET/AF_INET6) denied by "
            "resources.sandbox.network=brokered;"), subject.logged)


if __name__ == "__main__":
    unittest.main()
