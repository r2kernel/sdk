from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch


TUTORIALS = Path(__file__).resolve().parents[1]
ROOT = TUTORIALS.parent
for path in (
    ROOT / "sdk" / "python",
    TUTORIALS / "lib",
    TUTORIALS / "subsystems" / "app.cleaner.teleop",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app_base import CleanerApplicationBase  # noqa: E402
from standard_types import BOOL, COMMAND_ACK, EMPTY, STOP_REQUEST, VELOCITY_COMMAND  # noqa: E402
from teleop_app import TeleopCleanerApp  # noqa: E402


class ProcedureOnlyBinding:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    def procedure(self, name: str, params: Any = None, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, params, kwargs))
        return {"ok": True, "payload": {"accepted": True, "reason": "ok"}}


class AppBodyMotionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = CleanerApplicationBase.__new__(CleanerApplicationBase)
        self.app._name = "app.cleaner.teleop"
        self.app._body_motion = ProcedureOnlyBinding()

    def test_velocity_reference_uses_procedure_and_no_app_timing(self) -> None:
        result = self.app._drive(vx=1.5, vy=-1.5, wz=0.4)

        self.assertEqual(result, {"accepted": True, "reason": "ok"})
        name, params, kwargs = self.app._body_motion.calls[-1]
        self.assertEqual(name, "set_velocity_reference")
        self.assertEqual(
            params,
            {
                "twist": {
                    "linear": {"x": 1.0, "y": -1.0, "z": 0.0},
                    "angular": {"x": 0.0, "y": 0.0, "z": 0.4},
                },
                "source": "app.cleaner.teleop",
            },
        )
        self.assertEqual(kwargs["timeout_ms"], 1000)
        self.assertEqual(kwargs["request_type"], VELOCITY_COMMAND)
        self.assertEqual(kwargs["response_type"], COMMAND_ACK)
        self.assertNotIn("timing", kwargs)
        self.assertNotIn("period_ms", kwargs)
        self.assertNotIn("deadline_ms", kwargs)
        self.assertNotIn("dt", inspect.signature(CleanerApplicationBase._drive).parameters)

    def test_external_teleop_dt_is_rejected_with_actionable_error(self) -> None:
        app = TeleopCleanerApp.__new__(TeleopCleanerApp)

        with self.assertRaisesRegex(ValueError, "Remove it; command timing is managed by the Component"):
            app._command({"action": "drive", "vx": 0.2, "dt": 0.35})

    def test_stop_remains_a_procedure(self) -> None:
        result = self.app._stop_drive()

        self.assertEqual(result, {"accepted": True, "reason": "ok"})
        name, params, kwargs = self.app._body_motion.calls[-1]
        self.assertEqual(name, "stop")
        self.assertEqual(params, {})
        self.assertEqual(kwargs["request_type"], STOP_REQUEST)
        self.assertEqual(kwargs["response_type"], COMMAND_ACK)

    def test_arm_query_error_is_reported_immediately_without_timeout(self) -> None:
        self.app._manipulation = Mock()
        self.app._manipulation.operation.return_value = {"operation_id": "arm-operation-1"}
        self.app.binding_target = Mock(return_value="component.manipulation")
        self.app.query_operation = Mock(return_value={"ok": False, "error_message": "result codec rejected"})
        self.app.cancel_operation = Mock()

        with patch("app_base.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "move_j query failed for arm-operation-1: result codec rejected"):
                self.app._move_arm(0.5)

        self.app.query_operation.assert_called_once_with(target="component.manipulation", operation_id="arm-operation-1", timeout_ms=1000)
        sleep.assert_not_called()
        self.app.cancel_operation.assert_not_called()

    def test_burn_preserves_acknowledged_true_and_false_without_replay(self) -> None:
        for fired in (True, False):
            with self.subTest(fired=fired):
                self.app._trash_disposal = Mock()
                self.app._trash_disposal.procedure.return_value = {"ok": True, "result": {"value": fired}}

                self.assertEqual(self.app._burn(), {"fired": fired})
                self.app._trash_disposal.procedure.assert_called_once_with(
                    "trigger", {}, timeout_ms=1500, request_type=EMPTY, response_type=BOOL
                )

    def test_burn_propagates_failed_envelope_instead_of_domain_false(self) -> None:
        for response, message in (
            ({"ok": False, "error_message": "response ownership lost"}, "response ownership lost"),
            ({"ok": False, "error_code": "control_channel_status"}, "control_channel_status"),
            ({"ok": False, "transport": {"error": "Control caller deadline exceeded"}},
             "Control caller deadline exceeded"),
            ({"ok": False, "error_message": "expired", "result": {"value": False}}, "expired"),
            ({"status": 1, "error_message": "handler failed"}, "handler failed"),
        ):
            with self.subTest(response=response):
                self.app._trash_disposal = Mock()
                self.app._trash_disposal.procedure.return_value = response

                with self.assertRaisesRegex(RuntimeError, message):
                    self.app._burn()
                self.app._trash_disposal.procedure.assert_called_once()

    def test_burn_rejects_missing_or_non_boolean_acknowledgement(self) -> None:
        for result in ({}, {"value": "false"}, {"value": 0}, None):
            with self.subTest(result=result):
                self.app._trash_disposal = Mock()
                self.app._trash_disposal.procedure.return_value = {"ok": True, "result": result}

                with self.assertRaisesRegex(RuntimeError, "trigger Procedure returned no boolean value"):
                    self.app._burn()
                self.app._trash_disposal.procedure.assert_called_once()

    def test_burn_preserves_transport_exception_without_replay(self) -> None:
        self.app._trash_disposal = Mock()
        error = TimeoutError("Control request deadline exceeded")
        self.app._trash_disposal.procedure.side_effect = error

        with self.assertRaises(TimeoutError) as caught:
            self.app._burn()

        self.assertIs(caught.exception, error)
        self.app._trash_disposal.procedure.assert_called_once()

    def test_teleop_burn_reports_procedure_failure_without_fired_field(self) -> None:
        app = TeleopCleanerApp.__new__(TeleopCleanerApp)
        app._trash_disposal = Mock()
        app._trash_disposal.procedure.return_value = {"ok": False, "error_message": "handler response failed"}
        app._set_status = Mock(return_value={})
        app._publish_status = Mock()

        response = app._command({"action": "burn"})

        self.assertEqual(response["result"], {
            "ok": False, "action": "burn", "error": "handler response failed", "error_type": "RuntimeError",
        })
        self.assertNotIn("fired", response["result"])
        app._trash_disposal.procedure.assert_called_once()
