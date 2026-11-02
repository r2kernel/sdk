"""Public reset state transitions and Logger resource lifetimes."""
from __future__ import annotations

from contextlib import nullcontext
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from r2kernel.datatype.common import DeviceState
from r2kernel.helper.logger import Logger, logging
from r2kernel.wrapper._subsystem_lifecycle import _LifecycleMixin


class _Lifecycle(_LifecycleMixin):
    def __init__(self, log_dir: str) -> None:
        self._state = DeviceState.READY
        self._lifecycle_transition_lock = threading.RLock()
        self._lifecycle_transition_context = threading.local()
        self._termination_lock = threading.RLock()
        self._termination_flight = None
        self._termination_entry = threading.local()
        self._logger = Logger(name=f"lifecycle-problems-{id(self)}", log_file_folder=log_dir,
                              flag_use_console=False, flag_use_file=False)
        self._runtime = SimpleNamespace(send_lifecycle_status=lambda: None)

    @property
    def logger(self) -> Logger:
        return self._logger

    def on_state_changed(self, state) -> None:
        pass


class LifecycleProblemReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._log_dir = tempfile.TemporaryDirectory()
        self.subject = _Lifecycle(self._log_dir.name)

    def tearDown(self) -> None:
        self.subject.logger.close()
        self._log_dir.cleanup()

    def test_direct_and_managed_reset_share_allowed_states_and_results(self) -> None:
        allowed_states = {
            DeviceState.FAULTED, DeviceState.ERROR, DeviceState.DEGRADED,
            DeviceState.PAUSED, DeviceState.STOPPED,
        }
        for managed in (False, True):
            for initial_state in DeviceState:
                with self.subTest(managed=managed, initial_state=initial_state):
                    self.subject._state = initial_state
                    hook_states = []
                    changes = []
                    self.subject.on_state_changed = changes.append

                    def hook():
                        hook_states.append(self.subject._state)
                        return True

                    self.subject.on_reset = hook
                    result = (self.subject.request_lifecycle_transition("reset") if managed
                              else self.subject.reset())
                    accepted = initial_state in allowed_states
                    if managed:
                        self.assertIs(result, accepted)
                    else:
                        self.assertIsNone(result)
                    self.assertEqual(hook_states, [DeviceState.RESETTING] if accepted else [])
                    self.assertEqual(changes, [DeviceState.RESETTING, DeviceState.READY] if accepted else [])
                    self.assertEqual(self.subject._state, DeviceState.READY if accepted else initial_state)

    def test_direct_and_managed_reset_fail_to_faulted(self) -> None:
        for managed in (False, True):
            for raises in (False, True):
                with self.subTest(managed=managed, raises=raises):
                    self.subject._state = DeviceState.ERROR
                    observed = []
                    self.subject.on_state_changed = observed.append

                    def hook():
                        self.assertEqual(self.subject._state, DeviceState.RESETTING)
                        if raises:
                            raise RuntimeError("reset interlock failed")
                        return False

                    self.subject.on_reset = hook
                    result = (self.subject.request_lifecycle_transition("reset") if managed
                              else self.subject.reset())
                    self.assertIs(result, False if managed else None)
                    self.assertEqual(observed, [DeviceState.RESETTING, DeviceState.FAULTED])
                    self.assertEqual(self.subject._state, DeviceState.FAULTED)

    def test_recursive_reset_is_rejected_without_reentering_the_hook(self) -> None:
        for managed in (False, True):
            with self.subTest(managed=managed):
                self.subject._state = DeviceState.FAULTED
                hook_states = []
                nested_results = []

                def hook():
                    hook_states.append(self.subject._state)
                    nested_results.append(self.subject.reset())
                    nested_results.append(self.subject.request_lifecycle_transition("reset"))
                    return True

                self.subject.on_reset = hook
                result = (self.subject.request_lifecycle_transition("reset") if managed
                          else self.subject.reset())
                self.assertIs(result, True if managed else None)
                self.assertEqual(hook_states, [DeviceState.RESETTING])
                self.assertEqual(nested_results, [None, False])
                self.assertEqual(self.subject._state, DeviceState.READY)

    def test_reset_holds_the_transition_lock_while_the_hook_runs(self) -> None:
        for managed in (False, True):
            with self.subTest(managed=managed):
                self.subject._state = DeviceState.ERROR
                entered, release = threading.Event(), threading.Event()
                results, failures = [], []

                def hook():
                    entered.set()
                    return release.wait(3)

                def reset():
                    try:
                        results.append(self.subject.request_lifecycle_transition("reset") if managed
                                       else self.subject.reset())
                    except Exception as exc:
                        failures.append(exc)

                self.subject.on_reset = hook
                worker = threading.Thread(target=reset, daemon=True)
                worker.start()
                try:
                    self.assertTrue(entered.wait(3), "Reset hook did not start")
                    acquired = self.subject._lifecycle_transition_lock.acquire(blocking=False)
                    if acquired:
                        self.subject._lifecycle_transition_lock.release()
                    self.assertFalse(acquired, "Reset hook ran without the transition lock")
                finally:
                    release.set()
                    worker.join(3)
                self.assertFalse(worker.is_alive(), "Reset did not release the transition lock")
                self.assertEqual(failures, [])
                self.assertEqual(results, [True if managed else None])
                self.assertEqual(self.subject._state, DeviceState.READY)


class TerminationFlightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.subject = _Lifecycle(self.directory.name)
        self.calls = []
        self.failures = []
        self.subject._runtime.stop_heartbeat = lambda: self.calls.append("stop")
        self.subject._runtime.inform_subsystem_down = lambda: self.calls.append("down")

    def tearDown(self) -> None:
        self.subject.logger.close()
        self.directory.cleanup()

    def worker(self, target, *, name=None):
        def run():
            try:
                target()
            except BaseException as exc:
                self.failures.append(exc)
        return threading.Thread(target=run, name=name, daemon=True)

    def observe_follower(self):
        # Instrument this particular flight, not the global Event constructor.
        done = self.subject._termination_flight[1]
        waiting = threading.Event()
        original_wait = done.wait

        def wait(timeout=None):
            waiting.set()
            return original_wait(timeout)

        done.wait = wait
        return waiting

    def test_followers_wait_for_cleanup_recursive_call_is_noop_and_later_call_runs(self):
        hook_entered, release_hook = threading.Event(), threading.Event()
        cleanup_entered, release_cleanup = threading.Event(), threading.Event()
        follower_finished = threading.Event()
        nested_results = []

        def hook():
            self.calls.append("hook")
            nested_results.append(self.subject.terminate())
            hook_entered.set()
            if not release_hook.wait(3):
                raise RuntimeError("hook was not released")
            return True

        def down():
            self.calls.append("down")
            cleanup_entered.set()
            if not release_cleanup.wait(3):
                raise RuntimeError("cleanup was not released")

        self.subject.on_terminate = hook
        self.subject._runtime.inform_subsystem_down = down
        winner = self.worker(self.subject.terminate)
        follower = self.worker(lambda: (self.subject.terminate(), follower_finished.set()))
        winner.start()
        try:
            self.assertTrue(hook_entered.wait(3))
            waiting = self.observe_follower()
            follower.start()
            self.assertTrue(waiting.wait(3))
            release_hook.set()
            self.assertTrue(cleanup_entered.wait(3))
            self.assertFalse(follower_finished.is_set())
        finally:
            release_hook.set()
            release_cleanup.set()
            winner.join(3)
            if follower.ident is not None:
                follower.join(3)
        self.assertFalse(winner.is_alive())
        self.assertFalse(follower.is_alive())
        self.assertTrue(follower_finished.is_set())
        self.assertEqual(self.failures, [])
        self.assertEqual(nested_results, [None])
        self.assertEqual(self.calls, ["hook", "stop", "down"])

        self.subject.terminate()
        self.assertEqual(nested_results, [None, None])
        self.assertEqual(self.calls, ["hook", "stop", "down"] * 2)

    def test_hook_failure_still_waits_for_cleanup_and_releases_follower(self):
        hook_entered, release_hook = threading.Event(), threading.Event()
        cleanup_entered, release_cleanup = threading.Event(), threading.Event()
        follower_finished = threading.Event()

        def hook():
            self.calls.append("hook")
            hook_entered.set()
            if not release_hook.wait(3):
                raise AssertionError("hook was not released")
            raise RuntimeError("hook failure")

        def stop():
            self.calls.append("stop")
            cleanup_entered.set()
            if not release_cleanup.wait(3):
                raise AssertionError("cleanup was not released")

        self.subject.on_terminate = hook
        self.subject._runtime.stop_heartbeat = stop
        winner = self.worker(self.subject.terminate)
        follower = self.worker(lambda: (self.subject.terminate(), follower_finished.set()))
        winner.start()
        try:
            self.assertTrue(hook_entered.wait(3))
            waiting = self.observe_follower()
            follower.start()
            self.assertTrue(waiting.wait(3))
            release_hook.set()
            self.assertTrue(cleanup_entered.wait(3))
            self.assertFalse(follower_finished.is_set())
        finally:
            release_hook.set()
            release_cleanup.set()
            winner.join(3)
            if follower.ident is not None:
                follower.join(3)
        self.assertFalse(winner.is_alive())
        self.assertFalse(follower.is_alive())
        self.assertTrue(follower_finished.is_set())
        self.assertEqual(self.failures, [])
        self.assertEqual(self.calls, ["hook", "stop", "down"])
        self.assertEqual(self.subject._state, DeviceState.ERROR)

    def test_cleanup_exception_is_logged_without_retry_and_follower_is_released(self):
        entered, release = threading.Event(), threading.Event()
        follower_finished = threading.Event()
        problems = []

        def stop():
            self.calls.append("stop")
            entered.set()
            if not release.wait(3):
                raise AssertionError("cleanup was not released")
            raise RuntimeError("stop failure")

        self.subject.on_terminate = lambda: self.calls.append("hook") or True
        self.subject._runtime.stop_heartbeat = stop

        def terminate_and_capture():
            self.subject.terminate()
            problems.append(self.subject.logger.last_problem)

        winner = self.worker(terminate_and_capture)
        follower = self.worker(lambda: (self.subject.terminate(), follower_finished.set()))
        winner.start()
        try:
            self.assertTrue(entered.wait(3))
            waiting = self.observe_follower()
            follower.start()
            self.assertTrue(waiting.wait(3))
            self.assertFalse(follower_finished.is_set())
        finally:
            release.set()
            winner.join(3)
            if follower.ident is not None:
                follower.join(3)
        self.assertFalse(winner.is_alive())
        self.assertFalse(follower.is_alive())
        self.assertTrue(follower_finished.is_set())
        self.assertEqual(self.failures, [])
        self.assertEqual(self.calls, ["hook", "stop"])
        self.assertEqual(self.subject._state, DeviceState.ERROR)
        self.assertEqual(len(problems), 1)
        self.assertIn("stop failure", problems[0])

    def test_down_failure_does_not_repeat_heartbeat_or_down_notification(self):
        def down():
            self.calls.append("down")
            raise RuntimeError("down failure")

        self.subject.on_terminate = lambda: self.calls.append("hook") or True
        self.subject._runtime.inform_subsystem_down = down
        self.subject.terminate()
        self.assertEqual(self.calls, ["hook", "stop", "down"])
        self.assertEqual(self.subject._state, DeviceState.ERROR)
        self.assertIn("down failure", self.subject.logger.last_problem)

    def test_propagated_base_exception_releases_follower_and_allows_later_call(self):
        entered, release = threading.Event(), threading.Event()
        follower_finished = threading.Event()

        def hook():
            self.calls.append("hook")
            entered.set()
            if not release.wait(3):
                raise AssertionError("hook was not released")
            raise KeyboardInterrupt("interrupted")

        self.subject.on_terminate = hook
        winner = self.worker(self.subject.terminate)
        follower = self.worker(lambda: (self.subject.terminate(), follower_finished.set()))
        winner.start()
        try:
            self.assertTrue(entered.wait(3))
            waiting = self.observe_follower()
            follower.start()
            self.assertTrue(waiting.wait(3))
        finally:
            release.set()
            winner.join(3)
            if follower.ident is not None:
                follower.join(3)
        self.assertFalse(winner.is_alive())
        self.assertFalse(follower.is_alive())
        self.assertTrue(follower_finished.is_set())
        self.assertEqual(len(self.failures), 1)
        self.assertIsInstance(self.failures[0], KeyboardInterrupt)
        self.subject.on_terminate = lambda: self.calls.append("later") or True
        self.subject.terminate()
        self.assertEqual(self.calls, ["hook", "later", "stop", "down"])

    def test_same_thread_reentry_during_claim_before_flight_publication(self):
        lock = self.subject._termination_lock
        nested = []

        class ReentrantLock:
            first = True

            def __enter__(self):
                lock.acquire()
                if self.first:
                    self.first = False
                    nested.append(self.subject_terminate())
                return self

            def __exit__(self, *args):
                lock.release()

        tracked_lock = ReentrantLock()
        tracked_lock.subject_terminate = self.subject.terminate
        self.subject._termination_lock = tracked_lock
        self.subject.on_terminate = lambda: self.calls.append("hook") or True
        worker = self.worker(self.subject.terminate)
        worker.start()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.failures, [])
        self.assertEqual(nested, [None])
        self.assertEqual(self.calls, ["hook", "stop", "down"])

    def test_phase_hooks_do_not_wait_on_another_threads_termination(self):
        self.subject._runtime.codec_initialization = lambda **kwargs: nullcontext()
        self.subject._pre_initialize = lambda: None
        self.subject._on_initialize_success = lambda: None
        self.subject._on_pre_start = lambda: None
        for name in ("start", "initialize"):
            with self.subTest(phase=name):
                self.calls.clear()
                self.subject._state = DeviceState.READY
                terminate_entered, release_terminate = threading.Event(), threading.Event()
                phase_finished = threading.Event()
                nested = []

                def terminate_hook():
                    self.calls.append("terminate")
                    terminate_entered.set()
                    if not release_terminate.wait(3):
                        raise AssertionError("termination was not released")
                    return True

                def phase_hook():
                    self.calls.append(name)
                    nested.append(self.subject.terminate())
                    return True

                setattr(self.subject, "on_" + name, phase_hook)
                self.subject.on_terminate = terminate_hook
                winner = self.worker(self.subject.terminate)
                phase = self.worker(lambda: (getattr(self.subject, name)(), phase_finished.set()))
                winner.start()
                try:
                    self.assertTrue(terminate_entered.wait(3))
                    phase.start()
                    self.assertTrue(phase_finished.wait(3))
                    self.assertEqual(nested, [None])
                    self.assertEqual(self.calls, ["terminate", name])
                finally:
                    release_terminate.set()
                    winner.join(3)
                    if phase.ident is not None:
                        phase.join(3)
                self.assertFalse(winner.is_alive())
                self.assertFalse(phase.is_alive())
                self.assertEqual(self.failures, [])
                self.assertEqual(self.calls, ["terminate", name, "stop", "down"])

    def test_phase_hook_terminate_is_noop_without_existing_flight(self):
        self.subject._runtime.codec_initialization = lambda **kwargs: nullcontext()
        self.subject._pre_initialize = lambda: None
        self.subject._on_initialize_success = lambda: None
        self.subject._on_pre_start = lambda: None
        self.subject.on_terminate = lambda: self.calls.append("terminate") or True
        for name in ("start", "initialize"):
            with self.subTest(phase=name):
                self.subject._state = DeviceState.READY
                setattr(self.subject, "on_" + name,
                        lambda: self.calls.append(self.subject.terminate()) or True)
                getattr(self.subject, name)()
        self.assertEqual(self.calls, [None, None])

    def test_transition_hook_reentry_does_not_wait_on_flight_waiting_for_transition(self):
        transition_entered, attempt_admission = threading.Event(), threading.Event()
        nested_finished, release_transition = threading.Event(), threading.Event()
        follower_finished = threading.Event()
        lock = self.subject._lifecycle_transition_lock

        class TrackedLock:
            def __enter__(self):
                if threading.current_thread().name == "termination-winner":
                    attempt_admission.set()
                lock.acquire()
                return self

            def __exit__(self, *args):
                lock.release()

        self.subject._lifecycle_transition_lock = TrackedLock()
        self.subject._state = DeviceState.ERROR

        def reset():
            transition_entered.set()
            if not attempt_admission.wait(3):
                raise AssertionError("termination did not attempt admission")
            self.subject.terminate()
            nested_finished.set()
            return release_transition.wait(3)

        self.subject.on_reset = reset
        self.subject.on_terminate = lambda: self.calls.append("hook") or True
        transition = self.worker(self.subject.reset)
        winner = self.worker(self.subject.terminate, name="termination-winner")
        follower = self.worker(lambda: (self.subject.terminate(), follower_finished.set()))
        transition.start()
        try:
            self.assertTrue(transition_entered.wait(3))
            winner.start()
            self.assertTrue(attempt_admission.wait(3))
            self.assertTrue(nested_finished.wait(3))
            waiting = self.observe_follower()
            follower.start()
            self.assertTrue(waiting.wait(3))
            self.assertEqual(self.calls, [])
            self.assertFalse(follower_finished.is_set())
        finally:
            release_transition.set()
            transition.join(3)
            if winner.ident is not None:
                winner.join(3)
            if follower.ident is not None:
                follower.join(3)
        self.assertFalse(transition.is_alive())
        self.assertFalse(winner.is_alive())
        self.assertFalse(follower.is_alive())
        self.assertTrue(follower_finished.is_set())
        self.assertEqual(self.failures, [])
        self.assertEqual(self.calls, ["hook", "stop", "down"])


class LoggerResourceLifetimeTests(unittest.TestCase):
    def test_close_drains_file_and_recreation_does_not_feed_stale_queues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            name = f"logger-recreation-{id(self)}"
            named_logger = logging.getLogger(name)
            original_handlers = list(named_logger.handlers)
            closed_queues = []
            for generation in range(3):
                with self.subTest(generation=generation):
                    logger = Logger(name=name, log_file_folder=directory, flag_use_console=False)
                    try:
                        closed_queues.append(logger._queue)
                        owned_handler = logger._queue_handler
                        destinations = list(logger._handlers)
                        logger.info("Lifecycle", f"generation-{generation}")
                    finally:
                        logger.close()
                    logger.close()
                    self.assertEqual(list(named_logger.handlers), original_handlers)
                    self.assertTrue(all(handler.stream is None for handler in destinations))
                    logger.info("must-not-write-after-close")
                    # A concurrent producer can retain a handler selected before removal.
                    owned_handler.handle(named_logger.makeRecord(
                        name, logging.INFO, __file__, 0, "late-dispatch-after-close", (), None))
                    named_logger.info("must-not-enqueue-after-close")
                    self.assertTrue(all(record_queue.empty() for record_queue in closed_queues))

            contents = (Path(directory) / f"{name}.log").read_text()
            for generation in range(3):
                self.assertEqual(contents.count(f"generation-{generation}"), 1)
            self.assertNotIn("must-not", contents)
            self.assertNotIn("late-dispatch-after-close", contents)

    def test_same_name_close_preserves_other_live_instance_and_external_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            name = f"logger-shared-{id(self)}"
            first_dir = Path(directory) / "first"
            second_dir = Path(directory) / "second"
            first = Logger(name=name, log_file_folder=first_dir, flag_use_console=False)
            self.addCleanup(first.close)
            second = Logger(name=name, log_file_folder=second_dir, flag_use_console=False)
            self.addCleanup(second.close)
            named_logger = logging.getLogger(name)
            external_stream = open(Path(directory) / "external.log", "w")
            external = logging.StreamHandler(external_stream)
            named_logger.addHandler(external)
            try:
                first.info("before-close")
                first.close()
                first.close()
                self.assertIn(second._queue_handler, named_logger.handlers)
                self.assertIn(external, named_logger.handlers)
                first.info("closed-wrapper-must-not-write")
                second.info("second-still-live")
                named_logger.info("external-still-live")
                second.close()
                self.assertEqual(first._queue.qsize(), 0)
                self.assertEqual(second._queue.qsize(), 0)
                external.flush()
                first_contents = (first_dir / f"{name}.log").read_text()
                second_contents = (second_dir / f"{name}.log").read_text()
                external_contents = (Path(directory) / "external.log").read_text()
                self.assertIn("before-close", first_contents)
                self.assertNotIn("second-still-live", first_contents)
                self.assertIn("before-close", second_contents)
                self.assertIn("second-still-live", second_contents)
                self.assertIn("external-still-live", second_contents)
                self.assertIn("external-still-live", external_contents)
                self.assertNotIn("closed-wrapper-must-not-write", second_contents)
                self.assertNotIn("closed-wrapper-must-not-write", external_contents)
            finally:
                named_logger.removeHandler(external)
                external.close()
                external_stream.close()
                second.close()
                first.close()


if __name__ == "__main__":
    unittest.main()
