from __future__ import annotations

import threading
import time
import unittest

from r2kernel.core.common_control import (
    ControlContractError,
    ControlCycle,
    ControlGuard,
    ControlPolicy,
)


class _FakeClock:
    def __init__(self) -> None:
        self.sync_ns = 1_000_000_000
        self.monotonic_ns = 50_000

    def synchronized(self) -> int:
        return self.sync_ns

    def monotonic(self) -> int:
        return self.monotonic_ns

    def advance(self, amount_ns: int) -> None:
        self.sync_ns += amount_ns
        self.monotonic_ns += amount_ns


def _policy(
    refresh_mode: str = "maintain_state",
    *,
    max_update_gap_ns: int = 500,
    max_command_age_ns: int = 100,
    max_request_bytes: int = 100,
    max_response_bytes: int = 20,
    max_pending_units: int | None = None,
) -> ControlPolicy:
    return ControlPolicy(
        refresh_mode=refresh_mode,
        max_update_gap_ns=max_update_gap_ns,
        max_command_age_ns=max_command_age_ns,
        max_request_bytes=max_request_bytes,
        max_response_bytes=max_response_bytes,
        max_pending_units=max_pending_units,
    )


class CommonControlGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _FakeClock()
        self.invalidated: list[tuple[int, str]] = []
        self.guard = ControlGuard(
            bound_owner_id="component-a",
            policy=_policy(),
            sync_clock_ns=self.clock.synchronized,
            monotonic_clock_ns=self.clock.monotonic,
            on_held_command_invalidated=lambda revision, reason: self.invalidated.append((revision, reason)),
        )
        self.guard.activate(generation=1)

    def tearDown(self) -> None:
        self.guard.close()

    def _accept(
        self,
        sequence: int,
        intent: str = "command",
        state_revision: int = 1,
        *,
        generation: int = 1,
        source_identity: str = "component-a",
        request_size_bytes: int = 10,
        valid_for_ns: int = 1_000,
        execute_at_sync_ns: int | None = None,
    ):
        return self.guard.accept(
            ControlCycle(generation, sequence, intent, state_revision),
            source_identity=source_identity,
            generated_at_sync_ns=self.clock.sync_ns,
            valid_until_sync_ns=self.clock.sync_ns + valid_for_ns,
            request_size_bytes=request_size_bytes,
            execute_at_sync_ns=execute_at_sync_ns,
        )

    def test_cycle_fields_are_strict_nonnegative_int64(self) -> None:
        with self.assertRaises(ControlContractError):
            ControlCycle(True, 1, "command", 1)
        with self.assertRaises(ControlContractError):
            ControlCycle(1, 1 << 63, "command", 1)
        with self.assertRaises(ControlContractError):
            ControlCycle.from_mapping({"generation": 1, "sequence": 1, "intent": "sample"})
        with self.assertRaises(ControlContractError):
            ControlCycle.from_mapping(
                {"generation": 1, "sequence": 1, "intent": "sample", "state_revision": 0, "owner": "component-a"}
            )

    def test_only_bound_owner_and_provider_activated_generation_are_accepted(self) -> None:
        original_deadline = self.guard.snapshot()["lease_deadline_monotonic_ns"]
        with self.assertRaisesRegex(ControlContractError, "bound owner"):
            self._accept(1, source_identity="component-b")
        with self.assertRaisesRegex(ControlContractError, "generation is not active"):
            self._accept(1, generation=2)
        self.assertEqual(self.guard.snapshot()["active_generation"], 1)
        self.assertEqual(self.guard.snapshot()["lease_deadline_monotonic_ns"], original_deadline)

        self.guard.revoke(generation=1)
        self.guard.activate(generation=2)
        with self.assertRaisesRegex(ControlContractError, "generation is not active"):
            self._accept(1, generation=1)

    def test_cycle_sequence_must_increase_and_bad_cycle_does_not_renew_lease(self) -> None:
        first = self._accept(1)
        first_deadline = self.guard.snapshot()["lease_deadline_monotonic_ns"]
        with self.assertRaisesRegex(ControlContractError, "strictly increase"):
            self._accept(1)
        self.assertEqual(self.guard.snapshot()["lease_deadline_monotonic_ns"], first_deadline)
        self.guard.dispatch_if_valid(first, lambda: "submitted")
        with self.assertRaisesRegex(ControlContractError, "committed only once"):
            self.guard.dispatch_if_valid(first, lambda: self.fail("replayed effect ran"))

    def test_expiry_is_rechecked_after_prepare_before_effect(self) -> None:
        permit = self._accept(1, valid_for_ns=25)
        self.clock.advance(26)
        effects: list[str] = []

        with self.assertRaisesRegex(ControlContractError, "expired before effect dispatch"):
            self.guard.dispatch_if_valid(permit, lambda: effects.append("submitted"))

        self.assertEqual(effects, [])

    def test_future_scheduled_execution_is_explicitly_rejected(self) -> None:
        with self.assertRaisesRegex(ControlContractError, "immediate execution only"):
            self._accept(1, execute_at_sync_ns=self.clock.sync_ns + 1)
        self.assertIsNone(self.guard.snapshot()["last_sequence"])

    def test_command_refresh_and_sample_exchange_accept_only_their_intents(self) -> None:
        command_guard = ControlGuard(
            bound_owner_id="component-a",
            policy=_policy("command_refresh"),
            sync_clock_ns=self.clock.synchronized,
            monotonic_clock_ns=self.clock.monotonic,
        )
        command_guard.activate(generation=1)
        with self.assertRaisesRegex(ControlContractError, "command_refresh accepts command"):
            command_guard.accept(
                ControlCycle(1, 1, "keep", 1),
                source_identity="component-a",
                generated_at_sync_ns=self.clock.sync_ns,
                valid_until_sync_ns=self.clock.sync_ns + 100,
                request_size_bytes=1,
            )
        command_guard.close()

        sample_guard = ControlGuard(
            bound_owner_id="camera-source",
            policy=_policy("sample_exchange"),
            sync_clock_ns=self.clock.synchronized,
            monotonic_clock_ns=self.clock.monotonic,
        )
        sample_guard.activate(generation=4)
        permit = sample_guard.accept(
            ControlCycle(4, 1, "sample", 0),
            source_identity="camera-source",
            generated_at_sync_ns=self.clock.sync_ns,
            valid_until_sync_ns=self.clock.sync_ns + 100,
            request_size_bytes=5,
        )
        self.assertEqual(sample_guard.dispatch_if_valid(permit, lambda: "observed"), "observed")
        sample_guard.revoke(generation=4)
        sample_guard.activate(generation=5)
        initial_lease = sample_guard.snapshot()["lease_deadline_monotonic_ns"]
        with self.assertRaisesRegex(ControlContractError, "sample state_revision must be zero"):
            sample_guard.accept(
                ControlCycle(5, 1, "sample", 1),
                source_identity="camera-source",
                generated_at_sync_ns=self.clock.sync_ns,
                valid_until_sync_ns=self.clock.sync_ns + 100,
                request_size_bytes=5,
            )
        self.assertEqual(sample_guard.snapshot()["lease_deadline_monotonic_ns"], initial_lease)
        sample_guard.close()

    def test_keep_renews_control_lease_but_not_held_command_age(self) -> None:
        command = self._accept(1, state_revision=7)
        self.guard.dispatch_if_valid(command, lambda: "command submitted")
        self.clock.advance(50)
        keep = self._accept(2, intent="keep", state_revision=7)
        self.guard.dispatch_if_valid(keep, lambda: "kept")
        refreshed_lease_deadline = self.guard.snapshot()["lease_deadline_monotonic_ns"]

        self.clock.advance(51)
        self.assertTrue(self.guard.watchdog_tick())
        self.assertEqual(self.guard.snapshot()["active_generation"], 1)
        self.assertIsNone(self.guard.snapshot()["held_state_revision"])
        self.assertGreater(refreshed_lease_deadline, self.clock.monotonic_ns)
        self.assertEqual(self.invalidated, [(7, "command_expired")])

        with self.assertRaisesRegex(ControlContractError, "held command"):
            self._accept(3, intent="keep", state_revision=7)

    def test_state_revision_never_moves_back_after_expiry(self) -> None:
        command = self._accept(1, state_revision=9)
        self.guard.dispatch_if_valid(command, lambda: None)
        self.clock.advance(101)
        self.guard.watchdog_tick()

        with self.assertRaisesRegex(ControlContractError, "increase monotonically"):
            self._accept(2, state_revision=9)

    def test_expired_prepared_effect_is_not_replayed_after_generation_change(self) -> None:
        permit = self._accept(1, valid_for_ns=50)
        self.guard.revoke(generation=1, reason="binding_changed")
        self.guard.activate(generation=2)
        calls: list[str] = []

        with self.assertRaisesRegex(ControlContractError, "no longer active"):
            self.guard.dispatch_if_valid(permit, lambda: calls.append("submitted"))

        self.assertEqual(calls, [])

    def test_request_and_response_limits_are_enforced_without_implicit_response_scope(self) -> None:
        before = self.guard.snapshot()["lease_deadline_monotonic_ns"]
        with self.assertRaisesRegex(ControlContractError, "request exceeds"):
            self._accept(1, request_size_bytes=101)
        with self.assertRaisesRegex(ControlContractError, "response exceeds"):
            self.guard.validate_response_size(21)
        self.guard.validate_response_size(20)
        self.assertEqual(self.guard.snapshot()["lease_deadline_monotonic_ns"], before)

    def test_malformed_traffic_does_not_prevent_expiry(self) -> None:
        calls: list[str] = []
        bounded_guard = ControlGuard(
            bound_owner_id="component-a",
            policy=_policy(max_pending_units=1),
            sync_clock_ns=self.clock.synchronized,
            monotonic_clock_ns=self.clock.monotonic,
        )
        bounded_guard.activate(generation=1)
        permit = bounded_guard.accept(
            ControlCycle(1, 1, "command", 1),
            source_identity="component-a",
            generated_at_sync_ns=self.clock.sync_ns,
            valid_until_sync_ns=self.clock.sync_ns + 1_000,
            request_size_bytes=10,
        )
        bounded_guard.register_pending(permit, cancel=lambda reason: calls.append(reason))
        self.clock.advance(501)
        with self.assertRaisesRegex(ControlContractError, "request exceeds"):
            bounded_guard.accept(
                ControlCycle(1, 2, "command", 2),
                source_identity="component-a",
                generated_at_sync_ns=self.clock.sync_ns,
                valid_until_sync_ns=self.clock.sync_ns + 1_000,
                request_size_bytes=101,
            )

        self.assertEqual(bounded_guard.snapshot()["active_generation"], None)
        self.assertEqual(calls, ["request_expired"])
        self.assertEqual(bounded_guard.snapshot()["pending_effects"], 0)
        bounded_guard.close()

    def test_pending_effects_need_explicit_capacity_and_cannot_be_registered_twice(self) -> None:
        permit = self._accept(1)
        with self.assertRaisesRegex(ControlContractError, "requires a selected max_pending_units"):
            self.guard.register_pending(permit, cancel=lambda reason: None)

        bounded_guard = ControlGuard(
            bound_owner_id="component-a",
            policy=_policy(max_pending_units=1),
            sync_clock_ns=self.clock.synchronized,
            monotonic_clock_ns=self.clock.monotonic,
        )
        bounded_guard.activate(generation=1)
        bounded_permit = bounded_guard.accept(
            ControlCycle(1, 1, "command", 1),
            source_identity="component-a",
            generated_at_sync_ns=self.clock.sync_ns,
            valid_until_sync_ns=self.clock.sync_ns + 100,
            request_size_bytes=10,
        )
        handle = bounded_guard.register_pending(bounded_permit, cancel=lambda reason: None)
        with self.assertRaisesRegex(ControlContractError, "registered for deferred"):
            bounded_guard.register_pending(bounded_permit, cancel=lambda reason: None)
        bounded_guard.cancel_pending(handle)
        with self.assertRaisesRegex(ControlContractError, "registered for deferred"):
            bounded_guard.register_pending(bounded_permit, cancel=lambda reason: None)
        bounded_guard.close()

    def test_foreign_guard_handle_cannot_cancel_or_dispatch_same_token(self) -> None:
        calls: list[str] = []
        first_guard = ControlGuard(
            bound_owner_id="component-a",
            policy=_policy(max_pending_units=1),
            sync_clock_ns=self.clock.synchronized,
            monotonic_clock_ns=self.clock.monotonic,
        )
        second_guard = ControlGuard(
            bound_owner_id="component-a",
            policy=_policy(max_pending_units=1),
            sync_clock_ns=self.clock.synchronized,
            monotonic_clock_ns=self.clock.monotonic,
        )
        first_guard.activate(generation=1)
        second_guard.activate(generation=1)
        first_permit = first_guard.accept(
            ControlCycle(1, 1, "command", 1),
            source_identity="component-a",
            generated_at_sync_ns=self.clock.sync_ns,
            valid_until_sync_ns=self.clock.sync_ns + 1_000,
            request_size_bytes=10,
        )
        second_permit = second_guard.accept(
            ControlCycle(1, 1, "command", 1),
            source_identity="component-a",
            generated_at_sync_ns=self.clock.sync_ns,
            valid_until_sync_ns=self.clock.sync_ns + 1_000,
            request_size_bytes=10,
        )
        first_handle = first_guard.register_pending(first_permit, cancel=lambda reason: calls.append("first:" + reason))
        second_handle = second_guard.register_pending(second_permit, cancel=lambda reason: calls.append("second:" + reason))
        self.assertEqual(first_handle.token, second_handle.token)

        self.assertFalse(first_guard.cancel_pending(second_handle))
        with self.assertRaisesRegex(ControlContractError, "not active"):
            first_guard.dispatch_pending(second_handle, lambda: self.fail("foreign effect ran"))
        self.assertEqual(first_guard.snapshot()["pending_effects"], 1)
        self.assertEqual(second_guard.snapshot()["pending_effects"], 1)
        self.assertTrue(first_guard.cancel_pending(first_handle))
        self.assertEqual(calls, ["first:application_cancelled"])
        first_guard.close()
        second_guard.close()

    def test_watchdog_recomputes_after_registering_short_request_expiry(self) -> None:
        cancelled = threading.Event()
        reasons: list[str] = []
        real_guard = ControlGuard(
            bound_owner_id="camera-source",
            policy=_policy("sample_exchange", max_update_gap_ns=10_000_000_000, max_pending_units=1),
        )
        real_guard.activate(generation=1)
        real_guard.start_watchdog()
        now_sync_ns = time.time_ns()
        permit = real_guard.accept(
            ControlCycle(1, 1, "sample", 0),
            source_identity="camera-source",
            generated_at_sync_ns=now_sync_ns,
            valid_until_sync_ns=now_sync_ns + 100_000_000,
            request_size_bytes=10,
        )

        def cancel(reason: str) -> None:
            reasons.append(reason)
            cancelled.set()

        real_guard.register_pending(permit, cancel=cancel)
        self.assertTrue(cancelled.wait(timeout=2.0))
        self.assertEqual(reasons, ["request_expired"])
        self.assertEqual(real_guard.snapshot()["active_generation"], 1)
        real_guard.close()

    def test_pending_sample_is_cancelled_at_its_request_expiry_without_expiring_owner(self) -> None:
        sample_guard = ControlGuard(
            bound_owner_id="camera-source",
            policy=_policy("sample_exchange", max_update_gap_ns=500, max_pending_units=2),
            sync_clock_ns=self.clock.synchronized,
            monotonic_clock_ns=self.clock.monotonic,
        )
        sample_guard.activate(generation=1)
        permit = sample_guard.accept(
            ControlCycle(1, 1, "sample", 0),
            source_identity="camera-source",
            generated_at_sync_ns=self.clock.sync_ns,
            valid_until_sync_ns=self.clock.sync_ns + 25,
            request_size_bytes=10,
        )
        cancelled: list[str] = []
        handle = sample_guard.register_pending(permit, cancel=lambda reason: cancelled.append(reason))
        self.clock.advance(26)

        self.assertTrue(sample_guard.watchdog_tick())
        self.assertEqual(cancelled, ["request_expired"])
        self.assertEqual(sample_guard.snapshot()["active_generation"], 1)
        with self.assertRaisesRegex(ControlContractError, "not active"):
            sample_guard.dispatch_pending(handle, lambda: self.fail("expired effect ran"))
        sample_guard.close()

    def test_expired_held_state_notifies_provider_callback_even_while_control_lease_is_live(self) -> None:
        permit = self._accept(1, state_revision=4)
        self.guard.dispatch_if_valid(permit, lambda: None)
        self.clock.advance(101)

        self.assertTrue(self.guard.watchdog_tick())
        self.assertEqual(self.invalidated, [(4, "command_expired")])
        self.assertEqual(self.guard.snapshot()["active_generation"], 1)

    def test_dispatch_and_revoke_are_serialized_at_the_commit_boundary(self) -> None:
        sample_guard = ControlGuard(
            bound_owner_id="camera-source",
            policy=_policy("sample_exchange"),
            sync_clock_ns=self.clock.synchronized,
            monotonic_clock_ns=self.clock.monotonic,
        )
        sample_guard.activate(generation=1)
        permit = sample_guard.accept(
            ControlCycle(1, 1, "sample", 0),
            source_identity="camera-source",
            generated_at_sync_ns=self.clock.sync_ns,
            valid_until_sync_ns=self.clock.sync_ns + 100,
            request_size_bytes=10,
        )
        effect_entered = threading.Event()
        release_effect = threading.Event()
        revoke_entered = threading.Event()
        revoke_finished = threading.Event()

        def effect() -> str:
            effect_entered.set()
            if not release_effect.wait(timeout=1.0):
                raise AssertionError("test did not release effect callback")
            return "submitted"

        def dispatch() -> None:
            sample_guard.dispatch_if_valid(permit, effect)

        def revoke() -> None:
            revoke_entered.set()
            sample_guard.revoke(generation=1)
            revoke_finished.set()

        dispatch_thread = threading.Thread(target=dispatch)
        dispatch_thread.start()
        self.assertTrue(effect_entered.wait(timeout=1.0))
        revoke_thread = threading.Thread(target=revoke)
        revoke_thread.start()
        self.assertTrue(revoke_entered.wait(timeout=1.0))
        self.assertFalse(revoke_finished.is_set())
        release_effect.set()
        dispatch_thread.join(timeout=1.0)
        revoke_thread.join(timeout=1.0)

        self.assertFalse(dispatch_thread.is_alive())
        self.assertFalse(revoke_thread.is_alive())
        self.assertTrue(revoke_finished.is_set())
        sample_guard.close()

    def test_policy_factory_requires_every_selected_binding_value_within_provider_bound(self) -> None:
        common = {
            "version": 1,
            "request_effect": "exchange_sample",
            "response_scope": "sample_observed",
            "period_policy": "binding_required",
            "execution_policy": "finite_expiry_commit_check",
            "refresh_mode": "sample_exchange",
            "ownership_policy": "explicit_binding_generation",
            "ordering": "strictly_increasing_cycle",
            "repeat_policy": "no_automatic_replay",
            "failure": "report_domain_status",
            "limits": {
                "source": "binding",
                "required_fields": [
                    "max_update_gap_ns",
                    "max_command_age_ns",
                    "max_request_bytes",
                    "max_response_bytes",
                    "max_pending_units",
                ],
                "provider_bounds": "constrain_selected_values",
            },
        }
        selected = {
            "max_update_gap_ns": 50,
            "max_command_age_ns": 40,
            "max_request_bytes": 100,
            "max_response_bytes": 80,
            "max_pending_units": 4,
        }
        provider = {key: value * 2 for key, value in selected.items()}

        resolved = ControlPolicy.from_common_control(common, selected_limits=selected, provider_limits=provider)
        self.assertEqual(resolved.max_pending_units, 4)

        provider["max_request_bytes"] = 99
        with self.assertRaisesRegex(ControlContractError, "exceeds the provider bound"):
            ControlPolicy.from_common_control(common, selected_limits=selected, provider_limits=provider)


if __name__ == "__main__":
    unittest.main()
