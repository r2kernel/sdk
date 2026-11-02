from __future__ import annotations

import sys
from pathlib import Path


_SPEC_ENV_KEYS = ("R2KERNEL_CONTRACT_SPEC_ROOTS", "R2KERNEL_SPEC_PATH")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def repo_python_sdk_candidates() -> list[Path]:
    root = repo_root()
    candidates: list[Path] = []
    override = sys_path_override()
    if override:
        candidates.extend(override)
    candidates.extend(
        [
            root / "sdk" / "python",
            Path("/opt/r2kernel/sdk/python/site-packages"),
            Path("/opt/r2kernel/sdk/python"),
            root.parent / "r2kernel-sdk" / "sdk" / "python",
        ]
    )
    sdk_root = Path("/opt/r2kernel/sdk/python")
    candidates.extend(sorted(sdk_root.glob("r2kernel_client-*.data/purelib")))
    expanded: list[Path] = []
    for candidate in candidates:
        if (candidate / "r2kernel").exists():
            expanded.append(candidate)
        expanded.extend(sorted(candidate.glob("r2kernel_client-*.data/purelib")))
        site_packages = candidate / "site-packages"
        if site_packages.exists():
            expanded.append(site_packages)
    return expanded


def sys_path_override() -> list[Path]:
    import os

    value = os.environ.get("R2KERNEL_SDK_PYTHONPATH", "")
    if not value:
        return []
    return [Path(item).expanduser() for item in value.split(":") if item]


def repo_python_sdk() -> Path:
    root = repo_root()
    candidates = (
        *repo_python_sdk_candidates(),
        root / "sdk" / "python",
        Path("/opt/r2kernel/sdk/python"),
        root.parent / "r2kernel-sdk" / "sdk" / "python",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def ensure_repo_python_sdk() -> Path | None:
    ensure_repo_spec_root()
    existing = []
    seen: set[str] = set()
    for sdk_root in repo_python_sdk_candidates():
        if not sdk_root.exists():
            continue
        value = str(sdk_root)
        if value in seen:
            continue
        seen.add(value)
        existing.append(sdk_root)
    first_existing = existing[0] if existing else None
    for sdk_root in existing:
        value = str(sdk_root)
        sys.path[:] = [item for item in sys.path if item != value]
    for sdk_root in reversed(existing):
        sys.path.insert(0, str(sdk_root))
    return first_existing


def ensure_repo_spec_root() -> Path | None:
    spec_root = repo_root() / "spec"
    if not spec_root.exists():
        return None
    for key in _SPEC_ENV_KEYS:
        _prepend_env_path(key, spec_root)
    return spec_root


def _prepend_env_path(key: str, path: Path) -> None:
    import os

    value = str(path)
    existing = [item for item in os.environ.get(key, "").split(os.pathsep) if item]
    if existing and existing[0] == value:
        return
    existing = [item for item in existing if item != value]
    os.environ[key] = os.pathsep.join([value, *existing])


def missing_python_sdk_message() -> str:
    return (
        "Python SDK could not be imported. Install a compatible wheel from "
        "`/opt/r2kernel/sdk/python/` into your current Python environment, or use "
        "an editable `r2kernel-sdk` source checkout."
    )


def python_tutorial_environment_message(missing_module: str | None = None) -> str:
    if missing_module and missing_module != "r2kernel":
        prefix = f"Missing Python dependency `{missing_module}`. "
    else:
        prefix = "Python SDK or its Python dependencies could not be imported. "
    return (
        prefix
        + "Source the installed runtime environment and retry:\n"
        + "  . /opt/r2kernel/env.sh\n"
        + "  python3 -c 'import r2kernel; print(\"R2Kernel Python SDK ready\")'\n"
        + "For an isolated development shell, install the wheel explicitly:\n"
        + "  . /opt/r2kernel/env.sh\n"
        + "  unset PYTHONPATH\n"
        + "  /usr/bin/python3 -m venv ~/r2kernel-tutorials\n"
        + "  . ~/r2kernel-tutorials/bin/activate\n"
        + "  python -m pip install /opt/r2kernel/sdk/python/r2kernel_client-*.whl\n"
        + "Then rerun the tutorial."
    )
