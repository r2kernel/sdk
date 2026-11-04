"""R2KERNEL_INHERITED_FDS: leader encoding and child-side validation."""
from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from r2kernel.core.inherited_fds import INHERITED_FDS_ENV, InheritedFdError, encode_inherited_fds, inherited_socket
from r2kernel.core.resource import ResourceBrokerClient

SDK_ROOT = Path(__file__).resolve().parents[1]
LISTENER = {"family": "inet", "type": "stream", "listening": True}


def _cloexec(fd: int) -> bool:
    return bool(fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)


class InheritedFdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(self.listener.close)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.set_inheritable(True)
        self.port = self.listener.getsockname()[1]
        self.fd = self.listener.fileno()

    def _env(self, value: str | None):
        env = {key: item for key, item in os.environ.items() if key != INHERITED_FDS_ENV}
        if value is not None:
            env[INHERITED_FDS_ENV] = value
        return patch.dict(os.environ, env, clear=True)

    def _step(self, value: str | None, kind: str = "frontend.api", expect=None) -> str:
        with self._env(value), self.assertRaises(InheritedFdError) as caught:
            inherited_socket(kind, expect or LISTENER)
        self.assertEqual(caught.exception.kind, kind)
        self.assertTrue(str(caught.exception).startswith(f"inherited fd {kind}: {caught.exception.step}: "))
        return caught.exception.step

    def test_env_missing(self) -> None:
        self.assertEqual(self._step(None), "env_missing")

    def test_env_invalid(self) -> None:
        for value in ("{not json", "[3]", '{"bad kind!": 5}', '{"frontend.api": 2}', '{"frontend.api": "7"}',
                      '{"frontend.api": true}', '{"a": 9, "b": 9}'):
            with self.subTest(value=value):
                self.assertEqual(self._step(value), "env_invalid")

    def test_kind_missing(self) -> None:
        self.assertEqual(self._step(json.dumps({"frontend.ws": self.fd})), "kind_missing")

    def test_regular_file_is_not_socket(self) -> None:
        with tempfile.TemporaryFile() as handle:
            self.assertEqual(self._step(json.dumps({"frontend.api": handle.fileno()})), "not_socket")

    def test_closed_fd_is_not_socket(self) -> None:
        spare = os.dup(self.fd)
        os.close(spare)
        self.assertEqual(self._step(json.dumps({"frontend.api": spare})), "not_socket")

    def test_family_type_listening_port_mismatch(self) -> None:
        value = json.dumps({"frontend.api": self.fd})
        self.assertEqual(self._step(value, expect={**LISTENER, "family": "inet6"}), "family_mismatch")
        self.assertEqual(self._step(value, expect={"family": "inet", "type": "dgram", "listening": False}),
                         "type_mismatch")
        self.assertEqual(self._step(value, expect={**LISTENER, "listening": False}), "listening_mismatch")
        self.assertEqual(self._step(value, expect={**LISTENER, "port": self.port + 1 if self.port < 65535 else 1}),
                         "port_mismatch")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unbound:
            self.assertEqual(self._step(json.dumps({"tcp.out": unbound.fileno()}), kind="tcp.out"),
                             "listening_mismatch")
        # Failures leave the fd open and its flags unchanged.
        self.assertFalse(_cloexec(self.fd))
        os.fstat(self.fd)

    def test_success_sets_cloexec_and_returns_same_fd(self) -> None:
        with self._env(encode_inherited_fds({"frontend.api": self.fd})):
            fd = inherited_socket("frontend.api", {**LISTENER, "port": self.port})
            self.assertEqual(fd, self.fd)
            self.assertTrue(_cloexec(fd))
            self.assertEqual(ResourceBrokerClient.inherited_fd("frontend.api", LISTENER), self.fd)
        self.assertEqual(self.listener.getsockname()[1], self.port)

    def test_encode_rules(self) -> None:
        self.assertEqual(encode_inherited_fds({"frontend.api": 7, "can.can0": 8}), '{"can.can0":8,"frontend.api":7}')
        for mapping in ({"": 4}, {"x": 1}, {"a": 5, "b": 5}, {"a b": 5}, {"k": 3.0}):
            with self.subTest(mapping=mapping), self.assertRaises(InheritedFdError) as caught:
                encode_inherited_fds(mapping)
            self.assertEqual(caught.exception.step, "env_invalid")

    def test_duplicate_key_last_value_wins(self) -> None:
        spare = os.dup(self.fd)
        self.addCleanup(os.close, spare)
        with self._env(f'{{"frontend.api": {spare}, "frontend.api": {self.fd}}}'):
            self.assertEqual(inherited_socket("frontend.api", LISTENER), self.fd)

    def test_bool_float_leading_zero_and_out_of_range_fds_are_env_invalid(self) -> None:
        for value in ('{"frontend.api": true}', '{"frontend.api": false}', '{"frontend.api": 7.0}',
                      '{"frontend.api": 1e1}', '{"frontend.api": 07}', '{"frontend.api": 2147483648}',
                      '{"frontend.api": -5}'):
            with self.subTest(value=value):
                self.assertEqual(self._step(value), "env_invalid")
        with self._env(json.dumps({"frontend.api": 2147483647})), self.assertRaises(InheritedFdError) as caught:
            inherited_socket("frontend.api", LISTENER)
        self.assertEqual(caught.exception.step, "not_socket")  # 2^31-1 is a valid encoding

    def test_contradictory_expectation_is_value_error_not_a_step(self) -> None:
        with self._env(json.dumps({"frontend.api": self.fd})):
            for expect in ({"family": "inet", "type": "dgram", "listening": True},
                           {"family": "inet", "type": "raw", "listening": True},
                           {"family": "can", "type": "raw", "listening": False, "port": 1}):
                with self.subTest(expect=expect), self.assertRaises(ValueError) as caught:
                    inherited_socket("frontend.api", expect)
                self.assertNotIsInstance(caught.exception, InheritedFdError)

    def test_encode_failure_is_env_invalid(self) -> None:
        for mapping in ({"frontend.api": True}, {"frontend.api": 2**31}, {"bad kind": 4}, {"a": 4, "b": 4}):
            with self.subTest(mapping=mapping), self.assertRaises(InheritedFdError) as caught:
                encode_inherited_fds(mapping)
            self.assertEqual((caught.exception.step, caught.exception.kind), ("env_invalid", "<encode>"))

    def test_invalid_expectation_is_a_caller_error(self) -> None:
        for expect in ({"family": "other", "type": "stream", "listening": False},
                       {"family": "unix", "type": "stream", "listening": True, "port": 18091},
                       {"family": "inet", "type": "dgram", "listening": True},
                       {"family": "can", "type": "raw", "listening": False, "port": 1},
                       {"family": "inet", "type": "stream"},
                       {**LISTENER, "extra": 1}):
            with self.subTest(expect=expect), self.assertRaises(ValueError):
                inherited_socket("frontend.api", expect)

    def test_child_process_round_trip(self) -> None:
        script = textwrap.dedent(
            """
            import fcntl, json, sys
            from r2kernel.core.inherited_fds import inherited_socket
            fd = inherited_socket("frontend.api", {"family": "inet", "type": "stream", "listening": True,
                                                   "port": int(sys.argv[1])})
            print(json.dumps({"fd": fd, "cloexec": bool(fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)}))
            """
        )
        env = dict(os.environ)
        env[INHERITED_FDS_ENV] = encode_inherited_fds({"frontend.api": self.fd})
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SDK_ROOT), env.get("PYTHONPATH")]))
        completed = subprocess.run([sys.executable, "-c", script, str(self.port)], env=env, pass_fds=(self.fd,),
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"fd": self.fd, "cloexec": True})
        env[INHERITED_FDS_ENV] = encode_inherited_fds({"frontend.api": self.fd})
        wrong_port = subprocess.run([sys.executable, "-c", script, str(self.port + 1 if self.port < 65535 else 1)],
                                    env=env, pass_fds=(self.fd,), capture_output=True, text=True, timeout=60)
        self.assertNotEqual(wrong_port.returncode, 0)
        self.assertIn(f"inherited fd frontend.api: port_mismatch: fd {self.fd} is bound to port {self.port}",
                      wrong_port.stderr)

    def test_unix_frontend_listener_pass_fds_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            endpoint = os.path.join(directory, "frontend.sock")
            listener.bind(endpoint)
            listener.listen(1)
            script = textwrap.dedent("""
                import fcntl, json, socket
                from r2kernel.core.inherited_fds import inherited_socket
                fd = inherited_socket("frontend.api", {"family":"unix", "type":"stream", "listening":True})
                cloexec = bool(fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)
                with socket.socket(fileno=fd) as listener:
                    listener.settimeout(3)
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(3)
                        connection.sendall(b"ACK:" + connection.recv(16))
                print(json.dumps({"fd":fd, "cloexec":cloexec}))
                """)
            env = dict(os.environ)
            env[INHERITED_FDS_ENV] = encode_inherited_fds({"frontend.api": listener.fileno()})
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SDK_ROOT), env.get("PYTHONPATH")]))
            child = subprocess.Popen([sys.executable, "-c", script], env=env, pass_fds=(listener.fileno(),),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(3)
                    connection.connect(endpoint)
                    connection.sendall(b"frontend")
                    self.assertEqual(connection.recv(32), b"ACK:frontend")
                stdout, stderr = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, stderr)
                self.assertEqual(json.loads(stdout), {"fd": listener.fileno(), "cloexec": True})
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
