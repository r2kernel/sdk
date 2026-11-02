from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from r2kernel.core.control_cost_metrics import ControlCostMetrics
from r2kernel.core.native_control import NativeControlClient


def test_control_cost_metrics_flushes_bounded_recent_samples(capsys) -> None:
    metrics = ControlCostMetrics(enabled=True, capacity=2)
    metrics.record({"phase": "outbound", "request": 1})
    metrics.record({"phase": "outbound", "request": 2})
    metrics.record({"phase": "outbound", "request": 3})

    metrics.flush()

    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert rows[0]["event"] == "control_cost_summary"
    assert rows[0]["total_samples"] == 3
    assert rows[0]["retained_samples"] == 2
    assert rows[0]["overwritten_samples"] == 1
    assert [row["request"] for row in rows[1:]] == [2, 3]


def test_disabled_control_cost_metrics_emit_nothing(capsys) -> None:
    metrics = ControlCostMetrics(enabled=False)
    metrics.record({"phase": "outbound"})
    metrics.flush()

    assert capsys.readouterr().err == ""


def test_enabled_control_cost_metrics_flush_on_normal_process_exit() -> None:
    script = (
        "from r2kernel.core.control_cost_metrics import ControlCostMetrics; "
        "metrics = ControlCostMetrics(enabled=True, capacity=2); "
        "metrics.record({'phase': 'outbound', 'request': 9})"
    )
    python_package_root = str(Path(__file__).resolve().parents[1])
    environment = os.environ.copy()
    current_python_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (python_package_root, current_python_path) if item
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=True,
        env=environment,
        text=True,
    )

    rows = [json.loads(line) for line in result.stderr.splitlines()]
    assert rows[0]["event"] == "control_cost_summary"
    assert rows[0]["total_samples"] == 1
    assert rows[1]["event"] == "control_cost_sample"
    assert rows[1]["request"] == 9


def test_control_cost_metrics_bounds_dynamic_labels(capsys) -> None:
    metrics = ControlCostMetrics(enabled=True, capacity=1)
    metrics.record({"target": "x" * 200, "topic": "y" * 200})

    sample = metrics._samples[0]
    assert len(sample["target"]) == 128
    assert len(sample["topic"]) == 128
    metrics.flush()
    capsys.readouterr()


def test_transact_metrics_distinguish_remote_status_from_transport_error(capsys) -> None:
    client = object.__new__(NativeControlClient)
    client._control_cost_metrics = ControlCostMetrics(enabled=True, capacity=2)
    client._pending_cost_context = {}
    client._get_encoder = lambda _dtype: lambda _value: b"request"
    client._get_decoder = lambda _dtype: lambda payload: payload

    responses = iter(
        [
            {
                "ok": False,
                "error": "control_channel_status",
                "status": 7,
                "response": {"status": 7},
            },
            {"ok": False, "error": "timeout"},
        ]
    )
    client._transact_raw = lambda **_kwargs: next(responses)

    for _ in range(2):
        client.transact(
            target="target",
            topic="topic",
            type="bytes",
            value=b"request",
            period_ms=10.0,
            deadline_ms=20.0,
        )

    samples = list(client._control_cost_metrics._samples)
    assert samples[0]["outcome"] == "remote_status_error"
    assert samples[0]["response_status"] == 7
    assert samples[1]["outcome"] == "transport_error"
    assert samples[1]["response_status"] is None
    client._control_cost_metrics.flush()
    capsys.readouterr()
