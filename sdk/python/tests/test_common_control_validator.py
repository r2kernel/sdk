from __future__ import annotations

import unittest

from r2kernel.interface.model import ControlEndpointSpec, InterfaceSpec
from r2kernel.interface.validator import validate_interface_spec_definition


def _common_control(
    *,
    refresh_mode: str = "maintain_state",
    response_scope: str = "endpoint_effect_completed",
    required_fields: list[str] | None = None,
) -> dict:
    return {
        "version": 1,
        "request_effect": "apply_output_command",
        "response_scope": response_scope,
        "period_policy": "binding_required",
        "execution_policy": "finite_expiry_commit_check",
        "refresh_mode": refresh_mode,
        "ownership_policy": "explicit_binding_generation",
        "ordering": "strictly_increasing_cycle",
        "repeat_policy": "no_automatic_replay",
        "failure": "report_domain_status",
        "limits": {
            "source": "binding",
            "required_fields": required_fields or [
                "max_update_gap_ns",
                "max_command_age_ns",
                "max_request_bytes",
                "max_response_bytes",
            ],
            "provider_bounds": "constrain_selected_values",
        },
    }


def _spec(common_control: object | None) -> InterfaceSpec:
    semantics = {"legacy_note": "kept for compatibility"}
    if common_control is not None:
        semantics["common_control"] = common_control
    return InterfaceSpec(
        id="api.test.control.v1",
        name="Control API",
        kind="api_contract",
        frames={"policy": "endpoint_declared"},
        timing={"clock_domain": "r2kernel_sync"},
        state_model={"owner": "provider"},
        failure_semantics={"transport": "runtime_status"},
        authority={"owner": "provider"},
        limits={"source": "binding"},
        conformance={"level": "public"},
        controls=(
            ControlEndpointSpec(
                topic="apply_command",
                request="Request",
                response="Response",
                timing_support="required",
                semantics=semantics,
            ),
        ),
    )


class CommonControlValidatorTests(unittest.TestCase):
    def test_legacy_control_without_new_declaration_remains_accepted(self) -> None:
        self.assertEqual(validate_interface_spec_definition(_spec(None)), ())

    def test_valid_command_and_sample_profiles_are_accepted(self) -> None:
        command_report = validate_interface_spec_definition(_spec(_common_control()))
        sample_metadata = _common_control(
            refresh_mode="sample_exchange",
            response_scope="sample_observed",
            required_fields=[
                "max_update_gap_ns",
                "max_command_age_ns",
                "max_request_bytes",
                "max_response_bytes",
                "max_pending_units",
            ],
        )
        sample_report = validate_interface_spec_definition(_spec(sample_metadata))
        self.assertEqual(command_report, ())
        self.assertEqual(sample_report, ())

    def test_unknown_and_missing_common_policy_fields_are_rejected(self) -> None:
        common = _common_control()
        common["execution_policy"] = "best_effort"
        common["silent_retry"] = True
        del common["repeat_policy"]

        codes = {issue.code for issue in validate_interface_spec_definition(_spec(common))}

        self.assertIn("incomplete_common_control", codes)
        self.assertIn("unknown_common_control_fields", codes)
        self.assertIn("unsupported_common_control_policy", codes)

    def test_boolean_and_float_versions_are_not_accepted_as_integer_one(self) -> None:
        for invalid_version in (True, 1.0):
            with self.subTest(version=invalid_version):
                common = _common_control()
                common["version"] = invalid_version
                codes = {issue.code for issue in validate_interface_spec_definition(_spec(common))}
                self.assertIn("unsupported_common_control_version", codes)

    def test_sample_observed_requires_sample_exchange(self) -> None:
        common = _common_control(response_scope="sample_observed")

        codes = {issue.code for issue in validate_interface_spec_definition(_spec(common))}

        self.assertIn("incompatible_common_control_policy", codes)

    def test_limits_require_concrete_binding_fields_and_provider_cap_policy(self) -> None:
        common = _common_control()
        common["limits"]["required_fields"] = ["max_update_gap_ns", "max_request_bytes"]
        common["limits"]["source"] = "provider"

        codes = {issue.code for issue in validate_interface_spec_definition(_spec(common))}

        self.assertIn("incomplete_common_control_required_limits", codes)
        self.assertIn("invalid_common_control_limit_source", codes)

    def test_unknown_limit_entries_and_duplicate_required_fields_are_rejected(self) -> None:
        common = _common_control()
        common["limits"]["required_fields"] = [
            "max_update_gap_ns",
            "max_update_gap_ns",
            "max_command_age_ns",
            "max_request_bytes",
            "max_response_bytes",
            "unbounded_queue",
        ]
        common["limits"]["example_limit"] = 12

        codes = {issue.code for issue in validate_interface_spec_definition(_spec(common))}

        self.assertIn("duplicate_common_control_required_limit", codes)
        self.assertIn("unknown_common_control_required_limits", codes)
        self.assertIn("unknown_common_control_limit_fields", codes)

    def test_request_effect_requires_stable_snake_case_identifier(self) -> None:
        common = _common_control()
        common["request_effect"] = "Apply Command Now"

        codes = {issue.code for issue in validate_interface_spec_definition(_spec(common))}

        self.assertIn("invalid_common_control_request_effect", codes)

    def test_non_mapping_common_control_is_rejected(self) -> None:
        codes = {
            issue.code
            for issue in validate_interface_spec_definition(_spec(["not", "a", "mapping"]))
        }

        self.assertIn("invalid_common_control", codes)


if __name__ == "__main__":
    unittest.main()
