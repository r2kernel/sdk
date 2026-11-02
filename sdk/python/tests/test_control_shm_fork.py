"""A forked child leaves its parent's local Control v5 lanes alone.

Regression: the child of ``os.fork`` ran the inherited exit cleanup
(``close_all`` at atexit), whose ``shutdown(SHUT_RDWR)`` on the inherited lane socket ended the
parent's lane too (the parent then got EPIPE), and the child hung when a parent thread held a
lane lock at the fork. Real processes: a parent with a lane on a socket pair, a forked child.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

SDK_ROOT = Path(__file__).resolve().parents[1]

# The parent: one registry lane on a socket pair whose other end plays Control. The registry's
# close_all runs at exit, as the native Control client registers it. The child forks in one of the
# scenarios; the parent then reports whether its lane survived.
FORK_PARENT = textwrap.dedent('''
    import atexit, json, os, signal, socket, sys, threading
    from r2kernel.core import _control_shm_transport as transport
    scenario = sys.argv[1]
    policy = transport.ControlShmPolicy()
    registry = transport.ControlShmChannelRegistry(
        policy, transport.ControlShmBudget(policy), transport.ControlShmCounters())
    atexit.register(registry.close_all)
    channel = registry.get("control.sock", "provider", "echo")
    lane, control_side = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    channel._socket = lane
    holding, release = threading.Event(), threading.Event()
    if scenario == "held":
        # A parent thread is inside an exchange and a shutdown at the fork.
        def hold():
            with channel._lock, registry._shutdown_lock:
                holding.set()
                release.wait()
        threading.Thread(target=hold, daemon=True).start()
        holding.wait()
    pid = os.fork()
    if pid == 0:
        signal.alarm(5)  # a hang becomes SIGALRM, not a stuck test
        if scenario in ("close", "held"):
            registry.close_all()
            channel.close()
        if scenario == "held":
            os._exit(3 if channel._lock.locked() else 0)
        sys.exit(0)  # runs the inherited atexit close_all
    _, status = os.waitpid(pid, 0)
    release.set()
    control_side.setblocking(False)
    try:
        control_eof = control_side.recv(1) == b""
    except BlockingIOError:
        control_eof = False
    try:
        lane.send(b"x")
        parent_send = "ok"
    except OSError as error:
        parent_send = type(error).__name__
    print(json.dumps({"child_status": status, "control_eof": control_eof, "parent_send": parent_send,
                      "parent_lane_closed": channel.is_closed}))
    sys.stdout.flush()
    os._exit(0)
''')


class ForkedChildLeavesParentLanesTests(unittest.TestCase):
    def _run(self, scenario: str) -> dict:
        process = subprocess.run([sys.executable, "-c", FORK_PARENT, scenario], cwd=SDK_ROOT,
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout.strip().splitlines()[-1])

    def _assert_parent_lane_survives(self, result: dict) -> None:
        self.assertEqual(result["child_status"], 0, f"the child did not exit cleanly: {result}")
        self.assertFalse(result["control_eof"], f"the child shut down the parent's lane: {result}")
        self.assertEqual(result["parent_send"], "ok", result)
        self.assertFalse(result["parent_lane_closed"], result)

    def test_child_exit_cleanup_leaves_the_parent_lane(self) -> None:
        self._assert_parent_lane_survives(self._run("exit"))

    def test_child_explicit_close_leaves_the_parent_lane(self) -> None:
        self._assert_parent_lane_survives(self._run("close"))

    def test_child_does_not_wait_for_a_lock_held_at_the_fork(self) -> None:
        self._assert_parent_lane_survives(self._run("held"))


if __name__ == "__main__":
    unittest.main()
