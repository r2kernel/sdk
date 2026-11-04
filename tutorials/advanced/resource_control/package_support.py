"""Shared helpers for the resource-control tutorial package generators."""

from __future__ import annotations

import shutil
from pathlib import Path


def quote(value: str) -> str:
    """Double-quoted YAML scalar."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_gateway_manifest(package_dir: Path, name: str) -> None:
    """Mark hardware outlet probes as gateways, never as role-free packages."""
    (package_dir / "subsystem.yaml").write_text(
        "kind: r2kernel.subsystem\nversion: 1\nsubsystem:\n"
        f"  name: {quote(name)}\n  role: gateway\n",
        encoding="utf-8",
    )


def sdk_python_dir(script: str, repo_root: str | None) -> Path:
    """Python SDK source directory for the generated package's ``PYTHONPATH``.

    ``repo_root`` is the checkout containing ``sdk/python`` (the public SDK
    repository root). Without it, the checkout that contains this tutorial is
    used. A root without ``sdk/python/r2kernel`` is refused.
    """
    if repo_root:
        roots = [Path(repo_root).resolve()]
    else:
        roots = [parent for parent in Path(script).resolve().parents if (parent / "sdk" / "python").is_dir()][:1]
    for root in roots:
        sdk = root / "sdk" / "python"
        if (sdk / "r2kernel").is_dir():
            return sdk
    checked = ", ".join(str(root) for root in roots) or f"the parents of {script}"
    raise SystemExit(f"Python SDK sources (sdk/python/r2kernel) not found under {checked}; pass --repo-root.")


def prepare_package(out: str, script_dir: Path, probe: str) -> Path:
    """Fresh package directory with ``bin/<probe>`` copied from ``script_dir``."""
    package_dir = Path(out)
    bin_dir = package_dir / "bin"
    if package_dir.exists():
        shutil.rmtree(package_dir)
    bin_dir.mkdir(parents=True)
    shutil.copy2(script_dir / probe, bin_dir / probe)
    (bin_dir / probe).chmod(0o755)
    return package_dir
