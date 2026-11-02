"""A handler that outlives stop_operation_server does not break the Operation executor.

Stopping the executor waits 1 s for its workers and then replaces the work queue. A worker whose
handler was still running used to account for its item on the new queue
(``ValueError: task_done() called too many times``), dying with an unhandled exception and, after a
restart, disturbing the new queue's count.
"""
from __future__ import annotations

import threading
import time
import unittest

from test_operation_event_sequence_fixture import CONTRACT, OPERATION, _runtime, _wait


class LateOperationWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.errors: list[BaseException] = []
        previous = threading.excepthook
        threading.excepthook = lambda args: self.errors.append(args.exc_value)
        self.addCleanup(setattr, threading, "excepthook", previous)

    def test_a_late_worker_finishes_on_its_own_queue_and_the_restarted_executor_works(self) -> None:
        runtime = _runtime()
        held, release = threading.Event(), threading.Event()

        def handler(_params, context):
            if not held.is_set():
                context.publish_state("failed")
                held.set()
                release.wait(10)
            return None

        runtime._operation_handlers[OPERATION] = handler

        def start() -> str:
            return runtime._accept_operation(operation=OPERATION, params={}, created_ts=runtime.now_ns(),
                                             request_context=None, result_type=CONTRACT["result_type"])["operation_id"]

        first = start()
        _wait(held.is_set)
        runtime._stop_control_dispatch_server = lambda: None
        runtime._stop_operation_server()  # the handler is still held: its worker outlives the join timeout
        late = [worker for worker in threading.enumerate() if worker.name.startswith("r2kernel-operation-worker")
                and worker.is_alive()]
        self.assertTrue(late, "the held handler's worker should still run")

        runtime._operation_server_enabled = True
        second = start()  # a restarted executor with a new queue
        _wait(lambda: runtime._operation_records[second].state == "succeeded")
        release.set()
        for worker in late:
            worker.join(5)
            self.assertFalse(worker.is_alive(), "the late worker takes the stop meant for it and ends")
        runtime._operation_queue.join()  # the new queue's count is intact
        runtime._stop_operation_executor()
        time.sleep(0.05)
        self.assertEqual(self.errors, [])
        self.assertEqual(runtime._operation_records[first].state, "failed")


if __name__ == "__main__":
    unittest.main()
