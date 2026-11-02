"""Subsystem.bind_requirement / unbind_requirement over UDS binding_self_set / binding_self_unset."""
from __future__ import annotations

import socket
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from r2kernel.wrapper import _subsystem_self_binding as self_binding
from r2kernel.wrapper.subsystem import Subsystem
from r2kernel.core.control_client import ControlRequestError

ROW = {"source": "station.servo_link", "target": "gpio.servo", "origin": "self", "state": "bound"}
REFUSAL_CODES = (
    "self_binding_disabled", "self_binding_source_ineligible", "self_binding_requirement_invalid",
    "self_binding_operator_owned", "self_binding_remote_forbidden", "self_binding_target_held",
    "self_binding_unmanaged_source", "self_binding_rejected", "self_binding_target_authority_unknown",
    "self_binding_registration_changed",
)


class _Station(self_binding._SelfBindingMixin):
    name = "station"

    def __init__(self) -> None:
        self._runtime = SimpleNamespace(session_id="station-session-1")


class SelfBindingTests(unittest.TestCase):
    def _call(self, response, method="bind_requirement", *args, **kwargs):
        station = _Station()
        args = args or (("servo_link", "gpio.servo") if method == "bind_requirement" else ("servo_link",))
        with patch.object(self_binding, "exchange_control_request", return_value=response) as exchange:
            result = getattr(station, method)(*args, **kwargs)
        return result, exchange

    def test_subsystem_exposes_api(self) -> None:
        self.assertTrue(callable(Subsystem.bind_requirement))
        self.assertTrue(callable(Subsystem.unbind_requirement))

    def test_set_success_returns_row_and_sends_exact_request_once(self) -> None:
        result, exchange = self._call({"ok": True, "res": "ok", "binding": ROW})
        self.assertEqual(result, ROW)
        exchange.assert_called_once()
        self.assertEqual(exchange.call_args.args[0], {
            "cmd": "binding_self_set", "subsystem": "station", "session_id": "station-session-1",
            "requirement": "servo_link", "target": "gpio.servo"})
        self.assertEqual(exchange.call_args.kwargs.get("retries"), 1)

    def test_unset_success_returns_removed_flag(self) -> None:
        for removed in (True, False):
            result, exchange = self._call({"ok": True, "res": "ok", "removed": removed}, "unbind_requirement")
            self.assertIs(result, removed)
            self.assertEqual(exchange.call_args.args[0], {
                "cmd": "binding_self_unset", "subsystem": "station", "session_id": "station-session-1",
                "requirement": "servo_link"})

    def test_policy_refusal_carries_reason_verbatim(self) -> None:
        for code in REFUSAL_CODES:
            with self.subTest(code=code), self.assertRaises(ControlRequestError) as caught:
                self._call({"ok": False, "res": "error", "reason": code, "error": "detail text", "binding": ROW})
            self.assertEqual(caught.exception.reason, code)
            self.assertEqual(caught.exception.error, "detail text")
            self.assertEqual(str(caught.exception), f"{code}: detail text")
            self.assertEqual(caught.exception.binding, ROW)

    def test_pre_dispatch_error_has_empty_reason(self) -> None:
        with self.assertRaises(ControlRequestError) as caught:
            self._call({"status": "error", "error": "subsystem session authentication failed"})
        self.assertEqual(caught.exception.reason, "")
        self.assertEqual(str(caught.exception), "subsystem session authentication failed")

    def test_malformed_success_is_explicit(self) -> None:
        for response, method in (({"ok": True, "res": "ok"}, "bind_requirement"),
                                 ({"ok": True, "res": "ok", "removed": "yes"}, "unbind_requirement"),
                                 (None, "bind_requirement"), ({"res": "ok"}, "bind_requirement")):
            with self.subTest(response=response), self.assertRaises(ControlRequestError):
                self._call(response, method)

    def test_deadline_is_passed_and_expiry_is_timeout_without_retry(self) -> None:
        deadline = time.monotonic() + 5.0
        _, exchange = self._call({"ok": True, "res": "ok", "binding": ROW}, deadline=deadline)
        self.assertEqual(exchange.call_args.kwargs, {"deadline": deadline})
        station = _Station()
        with patch.object(self_binding, "exchange_control_request") as exchange, self.assertRaises(TimeoutError):
            station.bind_requirement("servo_link", "gpio.servo", deadline=time.monotonic() - 1.0)
        exchange.assert_not_called()
        with patch.object(self_binding, "exchange_control_request",
                          side_effect=socket.timeout("Control request deadline exceeded")) as exchange:
            with self.assertRaises(TimeoutError):
                station.bind_requirement("servo_link", "gpio.servo", deadline=time.monotonic() + 0.01)
        exchange.assert_called_once()

    def test_python39_socket_timeout_is_raised_as_timeout_error(self) -> None:
        # Before Python 3.10 socket.timeout is an OSError subclass distinct from TimeoutError.
        class Py39SocketTimeout(OSError):
            pass
        station = _Station()
        for deadline in (None, time.monotonic() + 5.0):
            with self.subTest(deadline=deadline), patch.object(self_binding.socket, "timeout", Py39SocketTimeout), \
                    patch.object(self_binding, "exchange_control_request", side_effect=Py39SocketTimeout("timed out")):
                with self.assertRaises(TimeoutError) as caught:
                    station.bind_requirement("servo_link", "gpio.servo", deadline=deadline)
                self.assertIsInstance(caught.exception.__cause__, Py39SocketTimeout)

    def test_status_error_without_text_names_the_command(self) -> None:
        for response in ({"status": "error"}, {"status": "error", "error": ""}):
            with self.subTest(response=response), self.assertRaises(ControlRequestError) as caught:
                self._call(response)
            self.assertEqual(caught.exception.reason, "")
            self.assertEqual(str(caught.exception), "binding_self_set returned an error without text")

    def test_control_request_error_is_exported_from_core(self) -> None:
        import r2kernel.core as core
        from r2kernel.core import ControlRequestError as exported
        self.assertIs(exported, ControlRequestError)
        self.assertIn("ControlRequestError", core.__all__)

    def test_arguments_are_validated(self) -> None:
        station = _Station()
        with patch.object(self_binding, "exchange_control_request") as exchange:
            for args in (("", "gpio.servo"), ("servo_link", ""), ("servo_link", None)):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    station.bind_requirement(*args)
        exchange.assert_not_called()


if __name__ == "__main__":
    unittest.main()
