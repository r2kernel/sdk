from __future__ import annotations

import pytest

from r2kernel.core.native_control import NativeControlClient
import r2kernel.core.native_control as native_control_module


class TutorialClock:
    def __init__(self) -> None:
        self.current_ns = 1_700_000_000_000_000_000

    def now_ns(self) -> int:
        return self.current_ns

    def advance(self, duration_ns: int) -> None:
        self.current_ns += duration_ns


def _client(clock: TutorialClock) -> NativeControlClient:
    client = object.__new__(NativeControlClient)
    client._ready = {"component.remote": True}
    client._now_ns = clock.now_ns
    client._managed_transact_request = lambda **_kwargs: {"status": 0, "payload": b"response"}
    return client


@pytest.mark.parametrize(
    ("elapsed_ns", "expected_overrun"),
    [
        (50_000_000, False),
        (125_000_000, True),
    ],
)
def test_overrun_uses_the_synchronized_clock_used_by_subsystem_now_ns(
    monkeypatch: pytest.MonkeyPatch,
    elapsed_ns: int,
    expected_overrun: bool,
) -> None:
    clock = TutorialClock()
    client = _client(clock)
    last_tick_ns = clock.now_ns()
    assert last_tick_ns > 1_000_000_000_000_000_000

    # The OS monotonic value has an unrelated epoch and cannot be subtracted
    # from the synchronized timestamp supplied by the tutorial-style caller.
    monkeypatch.setattr(native_control_module.time, "monotonic_ns", lambda: 5_000_000_000)
    clock.advance(elapsed_ns)

    result = client._transact_raw(
        target="component.remote",
        topic="velocity_command",
        payload=b"request",
        period_ms=100.0,
        deadline_ms=100.0,
        last_tick_ns=last_tick_ns,
    )

    assert result["ok"] is True
    assert result["overrun"] is expected_overrun
