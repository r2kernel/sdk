"""Prepared/RT memory admission (R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES), matching the C++ SDK."""
import mmap

import pytest

from r2kernel.core import _control_shm_region as region_module
from r2kernel.core._control_shm_region import (
    ControlShmBudget,
    ControlShmCapacityError,
    ControlShmPolicy,
    ControlShmRegion,
    configured_rt_memory_budget,
)
from r2kernel.core._control_shm_transport import ControlShmCounters, control_shm_summary

PAGE = mmap.PAGESIZE


@pytest.fixture(autouse=True)
def _isolated_process_budget(monkeypatch):
    monkeypatch.setattr(region_module._ProcessPreparedMemory, "_instance", None)
    monkeypatch.setattr(ControlShmRegion, "_lock_memory", lambda self: None)


def test_environment_sets_the_budget_and_rejects_invalid_values(monkeypatch):
    monkeypatch.delenv("R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES", raising=False)
    assert configured_rt_memory_budget() == 64 * 1024 * 1024
    monkeypatch.setenv("R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES", "8192")
    assert configured_rt_memory_budget() == 8192
    assert ControlShmBudget(ControlShmPolicy()).rt_memory_budget_bytes == 8192
    for value in ("", "0", "-1", "12k", " 4096", "４０９６"):
        monkeypatch.setenv("R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES", value)
        with pytest.raises(ValueError, match="R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES"):
            configured_rt_memory_budget()


def test_prepared_regions_are_admitted_page_rounded_and_released_on_close():
    budget = ControlShmBudget(ControlShmPolicy(), rt_memory_budget_bytes=2 * PAGE)
    first = ControlShmRegion.create(PAGE, budget)
    second = ControlShmRegion.create(2 * PAGE, budget)
    try:
        first.prepare_memory()
        first.prepare_memory()
        assert budget.snapshot()["prepared_bytes"] == PAGE
        with pytest.raises(ControlShmCapacityError, match="prepared/RT memory budget exceeded"):
            second.prepare_memory()
        assert budget.snapshot()["prepared_bytes"] == PAGE
        first.close()
        assert budget.snapshot()["prepared_bytes"] == 0
        second.prepare_memory()
        assert budget.snapshot()["prepared_bytes"] == budget.snapshot()["peak_prepared_bytes"] == 2 * PAGE
    finally:
        first.close()
        second.close()
    assert budget.snapshot()["prepared_bytes"] == 0


def test_failed_memory_lock_returns_its_admission(monkeypatch):
    def deny(_region):
        raise PermissionError("memory locking denied")

    monkeypatch.setattr(ControlShmRegion, "_lock_memory", deny)
    budget = ControlShmBudget(ControlShmPolicy(), rt_memory_budget_bytes=PAGE)
    region = ControlShmRegion.create(PAGE, budget)
    try:
        with pytest.raises(PermissionError):
            region.prepare_memory()
        assert budget.snapshot()["prepared_bytes"] == 0
    finally:
        region.close()


def test_the_process_limit_spans_clients(monkeypatch):
    monkeypatch.setenv("R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES", str(PAGE))
    first_client = ControlShmBudget(ControlShmPolicy(), rt_memory_budget_bytes=4 * PAGE)
    second_client = ControlShmBudget(ControlShmPolicy(), rt_memory_budget_bytes=4 * PAGE)
    first = ControlShmRegion.create(PAGE, first_client)
    second = ControlShmRegion.create(PAGE, second_client)
    try:
        first.prepare_memory()
        with pytest.raises(ControlShmCapacityError, match="prepared/RT memory budget exceeded"):
            second.prepare_memory()
        summary = control_shm_summary(ControlShmCounters(), first_client)
        assert summary["prepared_bytes"] == PAGE
        assert summary["rt_memory_budget_bytes"] == 4 * PAGE
        assert summary["process_memory_configured"] is True
        assert (summary["process_rt_memory_budget_bytes"], summary["process_prepared_bytes"]) == (PAGE, PAGE)
    finally:
        first.close()
        second.close()
