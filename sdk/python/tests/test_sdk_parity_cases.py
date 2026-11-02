"""Cases shared with the C++ SDK tests (sdk/testdata/parity).

The C++ SDK runs the same files from tests/control_local_policy_test.cpp and
tests/sdk_parity_cases_test.cpp; both SDKs must reach the same decisions.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from sdk_parity_support import PARITY_DIR, parity_cases, write_case_files

if not PARITY_DIR.is_dir():
    pytest.skip(f"shared SDK parity cases are not available: {PARITY_DIR}", allow_module_level=True)

from r2kernel.core.native_control import NativeControlClient  # noqa: E402
from r2kernel.manifest import (  # noqa: E402
    ManifestError,
    load_application_manifest,
    load_deployment_manifest,
    load_robot_platform_manifest,
    load_subsystem_manifest,
)
from r2kernel.wrapper.subsystem import Subsystem  # noqa: E402
from test_transport_authority import _runtime  # noqa: E402


def _ids(cases: list[dict[str, Any]]) -> list[str]:
    return [case["name"] for case in cases]


SOCKET_MODE_CASES = parity_cases("ipc-socket-mode.yaml")
HOST_AUTHORITY_CASES = parity_cases("host-authority.yaml")
VALIDATION_CASES = parity_cases("manifest-validation.yaml") + parity_cases("deployment-validation.yaml")
SEARCH_CASES = parity_cases("manifest-search.yaml")
LOADERS = {
    "subsystem": load_subsystem_manifest,
    "robot": load_robot_platform_manifest,
    "application": load_application_manifest,
    "deployment": load_deployment_manifest,
}


@pytest.mark.parametrize("case", SOCKET_MODE_CASES, ids=_ids(SOCKET_MODE_CASES))
def test_control_handler_socket_mode(case: dict[str, Any], tmp_path: Path) -> None:
    path = tmp_path / "handler.sock"
    path.write_text("", encoding="utf-8")
    path.chmod(0o600)
    environment = {"R2KERNEL_RUNTIME_IPC_GROUP": "r2kernel-parity-absent-group"}
    if case["env"] is not None:
        environment["R2KERNEL_RUNTIME_IPC_SOCKET_MODE"] = case["env"]
    client = object.__new__(NativeControlClient)
    with patch.dict(os.environ, environment):
        if case["env"] is None:
            os.environ.pop("R2KERNEL_RUNTIME_IPC_SOCKET_MODE", None)
        if "error" in case:
            with pytest.raises(ValueError) as refused:
                client._apply_local_ipc_socket_policy(str(path))
            assert case["error"] in str(refused.value)
            return
        client._apply_local_ipc_socket_policy(str(path))
    assert f"{stat.S_IMODE(path.stat().st_mode):04o}" == case["mode"]


@pytest.mark.parametrize("case", HOST_AUTHORITY_CASES, ids=_ids(HOST_AUTHORITY_CASES))
def test_protocol_host_authority(case: dict[str, Any]) -> None:
    runtime = _runtime()
    observed: list[tuple[Any, str]] = []

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        observed.append((runtime.current_protocol_host_authority(), request["sender"]))
        return {"ts": 1, "status": 0, "payload": b""}

    runtime._control_handler = handler
    request = {"topic": "anything", "ts": 1, "payload": b"", "sender": case["sender"],
               "_manager_peer_verified": case["verified"]}
    if "error" in case:
        with pytest.raises(ValueError) as refused:
            runtime._dispatch_control_request(request)
        assert case["error"] in str(refused.value)
        assert observed == []
    else:
        runtime._dispatch_control_request(request)
        assert observed == [(case["authority"], case["handler_sender"])]
    assert runtime.current_protocol_host_authority() is None


@pytest.mark.parametrize("case", VALIDATION_CASES, ids=_ids(VALIDATION_CASES))
def test_manifest_validation(case: dict[str, Any], tmp_path: Path) -> None:
    path = write_case_files(case, tmp_path)
    load = LOADERS[case["load"]]
    if case["expect"] == "accept":
        load(path)
        return
    with pytest.raises(ManifestError) as refused:
        load(path)
    assert case["error"] in str(refused.value)


class _SearchProbe(Subsystem):
    def on_initialize(self) -> bool:
        return True

    def on_start(self) -> bool:
        return True

    def on_terminate(self) -> bool:
        return True

    def on_reset(self) -> bool:
        return True


class _OfflineRuntime:
    def __init__(self, **_kwargs: Any) -> None:
        pass


@pytest.mark.parametrize("case", SEARCH_CASES, ids=_ids(SEARCH_CASES))
def test_manifest_search_order(case: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path.resolve()
    for relative in case["files"]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith("subsystem.yaml"):
            path.write_text(
                "kind: r2kernel.subsystem\nversion: 1\nsubsystem: {name: probe, role: component}\n", encoding="utf-8"
            )
        else:
            path.write_text("kind: r2kernel.deployment\nversion: 1\nsubsystems: {}\n", encoding="utf-8")
    (root / case["cwd"]).mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(root / case["cwd"])
    monkeypatch.setenv("HOME", str(root / "home"))
    for variable in ("R2KERNEL_SUBSYSTEM_MANIFEST", "R2KERNEL_DEPLOYMENT_MANIFEST", "R2KERNEL_BINDINGS"):
        monkeypatch.delenv(variable, raising=False)
    for variable, value in (case.get("env") or {}).items():
        monkeypatch.setenv(variable, value.replace("<root>", str(root)))
    user_data = case["user_data"]
    options: dict[str, Any] = {"user_data_path": user_data if user_data.startswith("./") else str(root / user_data)}
    for option in ("manifest_path", "deployment_manifest_path"):
        if case.get(option):
            options[option] = case[option].replace("<root>", str(root))
    with patch("r2kernel.wrapper.subsystem.SubsystemRuntime", _OfflineRuntime):
        if case.get("error"):
            with pytest.raises(RuntimeError) as refused:
                _SearchProbe(name="probe", **options)
            assert case["error"] in str(refused.value)
            return
        subsystem = _SearchProbe(name="probe", **options)
    assert subsystem.subsystem_manifest.path == root / case["subsystem_manifest"]
    deployment = subsystem.deployment_manifest
    if case.get("deployment_manifest"):
        assert deployment is not None and deployment.path == root / case["deployment_manifest"]
    else:
        assert deployment is None
