import os

import pytest

from rt_profile import MAX_SAMPLES, PeriodicWindow, validate_profile


def sample(slot, scheduled, started, returned, *, ok=True, integrity=True,
           sdk_started=None, sdk_returned=None):
    sdk_started = started if sdk_started is None else sdk_started
    sdk_returned = returned if sdk_returned is None else sdk_returned
    return {"sequence": slot, "slot": slot, "scheduled_ns": scheduled,
            "started_ns": started, "returned_ns": returned, "ok": ok,
            "integrity_ok": integrity, "sdk_started_ns": sdk_started,
            "sdk_returned_ns": sdk_returned, "caller_wall_ns": sdk_returned - sdk_started,
            "task_wall_ns": returned - started,
            "sdk_rtt_ns": None, "error": "" if ok and integrity else "explicit failure"}


def test_request_generation_and_integrity_validation_are_in_task_completion_only():
    window = PeriodicWindow(0, 300, 100, 50)
    window.record(sample(0, 0, 0, 150, sdk_started=40, sdk_returned=50))
    report = window.report()
    assert report["samples"][0]["task_wall_ns"] == 150
    assert report["samples"][0]["caller_wall_ns"] == 10
    assert report["completion_deadline_violations"] == 1
    assert report["caller_deadline_violations"] == 0
    assert report["skipped_slots"] == 1
    assert window.due(150) == (2, 200)


def test_absolute_window_skips_elapsed_slots_without_catchup():
    window = PeriodicWindow(100, 1000, 100, 80)
    assert window.due(100) == (0, 100)
    window.record(sample(0, 100, 100, 350))
    # Completion at 350 cannot cause overdue slots at 200/300 to burst.
    assert window.due(350) == (3, 400)
    assert window.due(650) == (5, 600)
    assert window.skipped == 4
    window.record(sample(5, 600, 650, 700))
    assert window.due(700) == (6, 700)
    assert window.due(1100) is None
    assert window.skipped + window.attempted == window.slots


def test_exact_tick_and_exact_window_end_boundaries():
    window = PeriodicWindow(100, 200, 100, 100)
    assert window.due(99) == (0, 100)
    window.record(sample(0, 100, 100, 200))
    assert window.due(200) == (1, 200)
    window.record(sample(1, 200, 200, 300))
    assert window.due(300) is None
    report = window.report()
    assert report["performance_passed"]
    assert report["completed_after_window"] == 0
    assert report["completion_deadline_violations"] == 0
    assert report["caller_deadline_violations"] == 0


def test_failures_integrity_and_deadline_phases_are_independent():
    window = PeriodicWindow(0, 400, 100, 50)
    window.record(sample(0, 0, 40, 70, ok=False))
    assert window.due(100) == (1, 100)
    window.record(sample(1, 100, 100, 151, integrity=False))
    assert window.due(200) == (2, 200)
    window.record(sample(2, 200, 200, 220))
    assert window.due(300) == (3, 300)
    window.record(sample(3, 300, 350, 401))
    assert window.due(401) is None
    report = window.report()
    assert report["attempted"] == 4
    assert report["successes"] == 2
    assert report["failures"] == 2
    assert report["completion_deadline_violations"] == 3
    assert report["caller_deadline_violations"] == 2
    assert report["completed_after_window"] == 1
    assert len(report["samples"]) == 4
    assert not report["functional_passed"]
    assert not report["performance_passed"]


def test_failed_rtt_absence_and_successful_zero_rtt_remain_distinct():
    window = PeriodicWindow(0, 200, 100, 100)
    failed = sample(0, 0, 0, 10, ok=False)
    window.record(failed)
    successful = sample(1, 100, 100, 120)
    successful["sdk_rtt_ns"] = 0
    window.record(successful)
    report = window.report()
    assert report["failures"] == 1
    assert report["successes"] == 1
    assert report["samples"][0]["sdk_rtt_ns"] is None
    assert report["samples"][0]["ok"] is False
    assert report["samples"][0]["error"] == "explicit failure"
    assert report["samples"][1]["sdk_rtt_ns"] == 0
    assert report["samples"][1]["ok"] is True
    assert report["samples"][1]["error"] == ""


def test_all_successful_attempts_do_not_hide_skipped_slots():
    window = PeriodicWindow(0, 1000, 100, 100)
    assert window.due(300) == (3, 300)
    window.record(sample(3, 300, 300, 320))
    assert window.due(1000) is None
    report = window.report()
    assert report["functional_passed"]
    assert not report["performance_passed"]
    assert report["skipped_slots"] == 9
    assert report["achieved_rate_hz"] == 1_000_000


@pytest.mark.parametrize("duration,period,deadline", [(0, 1, 1), (1, 0, 1), (1, 1, 0),
                                                       (MAX_SAMPLES + 1, 1, 1)])
def test_invalid_pacing_and_unbounded_sample_storage_are_rejected(duration, period, deadline):
    with pytest.raises(ValueError):
        PeriodicWindow(0, duration, period, deadline)


@pytest.mark.parametrize("policy,priority", [("other", 1), ("fifo", 0), ("rr", 100), ("invalid", 0)])
def test_invalid_scheduler_profile_is_rejected(policy, priority):
    with pytest.raises(ValueError):
        validate_profile(policy, priority, None, False)


def test_invalid_affinity_and_implicit_isolation_are_rejected():
    cpu = min(os.sched_getaffinity(0))
    for csv in ("", f"{cpu},{cpu}", f"{cpu},", "-1"):
        with pytest.raises(ValueError):
            validate_profile("other", 0, csv, False)
    with pytest.raises(ValueError):
        validate_profile("other", 0, None, True)
