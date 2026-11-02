"""StartGate v1 conformance: runs the shared scenario table against the Python fixture gate."""
from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from start_gate import StartGate, StartGateError  # noqa: E402

TABLE = Path(__file__).resolve().parent.parent / "start_gate_conformance.json"


def _private_dir(path: Path) -> Path:
    path.mkdir()
    os.chmod(path, 0o700)
    return path


def _ack_lock_free(path: Path) -> bool:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


class _Scenario:
    def __init__(self, root: Path, spec: dict):
        self.spec = spec
        self.base = _private_dir(root / spec["name"])
        self.run = _private_dir(self.base / "run")
        self.gate_path = str(self.run / "ready")
        self.gate: StartGate | None = None

    def constructed(self) -> StartGate:
        if self.gate is None:
            raise AssertionError("scenario step needs a constructed gate")
        return self.gate

    def replace_run_directory(self) -> None:
        self.run.rename(self.base / "old-run")
        _private_dir(self.run)

    def helper_probe(self) -> None:
        """The helper polls between the publisher's exclusive create and its lock."""
        helper = StartGate(self.gate_path)
        try:
            if helper.acknowledged("current-run", "publisher"):
                raise AssertionError("an unwritten acknowledgement must not count as acknowledged")
        finally:
            helper.close()

    def setup(self, action: str) -> None:
        ready = self.run / "ready"
        if action == "construct":
            self.gate = StartGate(self.gate_path)
        elif action == "acknowledge":
            self.constructed().acknowledge("current-run", "publisher")
        elif action == "create_gate":
            ready.touch()
            os.chmod(ready, 0o600)
        elif action == "create_ack":
            (self.run / "ready.ack").touch()
        elif action == "create_private_empty_ack":
            ack = self.run / "ready.ack"
            ack.touch()
            os.chmod(ack, 0o600)
        elif action == "helper_probe_after_ack_open":
            self.constructed().set_after_ack_open_for_test(self.helper_probe)
        elif action == "create_ack_symlink":
            (self.run / "ready.ack").symlink_to(self.base / "elsewhere")
        elif action == "replace_run_directory":
            self.replace_run_directory()
        elif action == "replace_run_directory_after_ack_create":
            self.constructed().set_after_ack_create_for_test(self.replace_run_directory)
        elif action == "symlink_parent":
            (self.base / "linked-run").symlink_to(self.run, target_is_directory=True)
            self.gate_path = str(self.base / "linked-run" / "ready")
        elif action == "chmod_parent_0750":
            os.chmod(self.run, 0o750)
        elif action == "chown_parent_foreign":
            os.chown(self.run, 65534, 65534)
        elif action == "relative_gate_path":
            self.gate_path = "relative/ready"
        elif action == "dot_component_gate_path":
            self.gate_path = f"{self.run}/./ready"
        elif action == "double_slash_gate_path":
            self.gate_path = f"{self.base}//run/ready"
        elif action == "trailing_slash_gate_path":
            self.gate_path = f"{self.run}/ready/"
        elif action in {"release_publisher", "close_publisher"}:
            self.constructed().close()
        elif action.startswith("gate_kind:"):
            kind = action.split(":", 1)[1]
            if kind == "symlink":
                ready.symlink_to(self.base / "missing")
            elif kind == "directory":
                ready.mkdir()
            elif kind == "fifo":
                os.mkfifo(ready, 0o600)
            else:
                ready.write_text("stale" if kind == "content" else "")
                os.chmod(ready, 0o644 if kind == "public" else 0o600)
                if kind == "hardlink":
                    os.link(ready, self.run / "other")
        else:
            raise AssertionError(f"unknown setup action {action}")

    def step(self, step: str) -> None:
        if step == "construct":
            self.gate = StartGate(self.gate_path)
        elif step == "acknowledge":
            self.constructed().acknowledge("current-run", "publisher")
        elif step == "acknowledge_invalid_session":
            self.constructed().acknowledge("bad/session", "publisher")
        elif step == "acknowledge_invalid_publisher":
            self.constructed().acknowledge("current-run", "publisher name")
        elif step == "opened":
            self.constructed().opened()
        elif step == "ack_lock_free":
            if not _ack_lock_free(self.run / "ready.ack"):
                raise AssertionError("exited publisher kept the lock")
        elif step == "consumer_acknowledged":
            helper = StartGate(self.gate_path)
            try:
                if not helper.acknowledged("current-run", "publisher"):
                    raise StartGateError("not_acknowledged", "no matching acknowledgement")
            finally:
                helper.close()
        elif step == "consumer_acknowledged_other_name":
            helper = StartGate(self.gate_path)
            try:
                if not helper.acknowledged("current-run", "other-publisher"):
                    raise StartGateError("not_acknowledged", "no matching acknowledgement")
            finally:
                helper.close()
        elif step == "consumer_open_twice":
            helper = StartGate(self.gate_path)
            try:
                helper.open("current-run", "publisher")
                helper.open("current-run", "publisher")
            finally:
                helper.close()
        elif step == "acknowledge_then_open":
            gate = self.constructed()
            gate.acknowledge("current-run", "publisher")
            ack = self.run / "ready.ack"
            assert ack.read_bytes() == b"r2kernel-event-start-v1\ncurrent-run\npublisher\n"
            metadata = ack.lstat()
            assert (metadata.st_mode & 0o7777) == 0o600 and metadata.st_nlink == 1
            assert not _ack_lock_free(ack), "live publisher must hold the acknowledgement lock"
            assert not gate.opened()
            helper = StartGate(self.gate_path)
            try:
                helper.open("current-run", "publisher")
            finally:
                helper.close()
            assert gate.opened()
        else:
            raise AssertionError(f"unknown step {step}")

    def observe(self) -> str:
        try:
            for action in self.spec["setup"]:
                self.setup(action)
            self.step(self.spec["step"])
        except StartGateError as exc:
            return exc.code
        finally:
            if self.gate is not None:
                self.gate.close()
        return "ok"


def _scenarios(*, requires_root: bool) -> list[dict]:
    table = json.loads(TABLE.read_text(encoding="utf-8"))
    return [spec for spec in table["scenarios"] if bool(spec.get("requires_root")) == requires_root]


class StartGateConformanceTest(unittest.TestCase):
    # Root-only rows are a separate test: a skip inside the subTest loop would end
    # the whole loop under pytest < 9 and silently drop every later scenario.
    def _run_scenarios(self, scenarios: list[dict]) -> None:
        self.assertTrue(scenarios, "conformance table has no scenarios for this test")
        observed = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for spec in scenarios:
                with self.subTest(spec["name"]):
                    observed.append(spec["name"])
                    self.assertEqual(_Scenario(root, spec).observe(), spec["expect"])
        self.assertEqual(observed, [spec["name"] for spec in scenarios])

    def test_shared_scenarios(self):
        self._run_scenarios(_scenarios(requires_root=False))

    @unittest.skipUnless(os.geteuid() == 0, "requires root")
    def test_root_only_scenarios(self):
        self._run_scenarios(_scenarios(requires_root=True))

    def test_errors_remain_value_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                StartGate(Path(directory) / "missing-parent" / "ready")


if __name__ == "__main__":
    unittest.main()
