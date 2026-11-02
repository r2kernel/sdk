from __future__ import annotations

import unittest

from r2kernel.interface.model import InterfaceSpec, OperationEndpointSpec
from r2kernel.interface.validator import validate_interface_specs


class _OperationRuntime:
    def get_publish_info(self):
        return []

    def get_subscribe_info(self):
        return []

    def get_control_info(self):
        return []

    def get_procedure_info(self):
        return []

    def get_operation_info(self):
        return ["move"]

    def get_control_contract_info(self):
        return []

    def get_procedure_contract_info(self):
        return []

    def get_operation_contract_info(self):
        return [
            (
                "move",
                {
                    "request_type": "string",
                    "feedback_type": "string",
                    "result_type": "string",
                    "state_type": "string",
                    "timing_support": "none",
                },
            )
        ]


class InterfaceValidatorTests(unittest.TestCase):
    def test_operation_timing_support_mismatch_reports_issue_without_crashing(self) -> None:
        spec = InterfaceSpec(
            id="api.test.operation.v1",
            name="test",
            operations=(
                OperationEndpointSpec(
                    name="move",
                    request="string",
                    feedback="string",
                    result="string",
                    state="string",
                    timing_support="required",
                ),
            ),
        )

        report = validate_interface_specs((spec,), _OperationRuntime())

        self.assertFalse(report.ok)
        self.assertEqual(report.issues[0].code, "operation_timing_support_mismatch")
        self.assertIn("operation 'move' timing_support", report.issues[0].message)


if __name__ == "__main__":
    unittest.main()
