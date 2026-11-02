"""The SDK finds runtime files through explicit inputs, never through a source checkout layout."""
from __future__ import annotations

from pathlib import Path

import pytest

from r2kernel.ffi import r2kernel_core as ffi_module
from r2kernel.ffi.r2kernel_core import R2KernelCore
from r2kernel.helper import library_logger


def test_core_library_comes_from_runtime_prefix(monkeypatch, tmp_path):
    lib_name = R2KernelCore._lib_filenames()[0]
    if (Path(ffi_module.__file__).resolve().parent / "lib" / lib_name).exists():
        pytest.skip("a bundled wheel library takes precedence over R2KERNEL_PREFIX")
    library = tmp_path / "runtime" / "lib" / lib_name
    library.parent.mkdir(parents=True)
    library.write_bytes(b"")
    loaded: list[str] = []
    monkeypatch.setenv("R2KERNEL_PREFIX", str(tmp_path))
    monkeypatch.setattr(ffi_module.ctypes, "CDLL", lambda path: loaded.append(path) or object())

    R2KernelCore.__new__(R2KernelCore)._load_library(None)

    assert loaded == [str(library)]


def test_library_log_dir_without_override_or_runtime_prefix_is_user_local(monkeypatch, tmp_path):
    if library_logger._find_runtime_prefix(Path(library_logger.__file__).resolve()) is not None:
        pytest.skip("the SDK runs from an installed runtime prefix")
    monkeypatch.delenv("R2KERNEL_LOG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert library_logger.get_library_log_path() == tmp_path / ".r2kernel" / "log" / "r2kernel.log"


def test_library_log_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("R2KERNEL_LOG_DIR", str(tmp_path / "logs"))

    assert library_logger.get_library_log_path() == tmp_path / "logs" / "r2kernel.log"
