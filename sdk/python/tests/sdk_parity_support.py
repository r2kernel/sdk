"""Access to the cases shared with the C++ SDK tests (sdk/testdata/parity)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PARITY_DIR = Path(__file__).resolve().parents[2] / "testdata" / "parity"


def parity_cases(file_name: str) -> list[dict[str, Any]]:
    """The `cases` list of one shared case file."""

    return yaml.safe_load((PARITY_DIR / file_name).read_text(encoding="utf-8"))["cases"]


def parity_case(file_name: str, name: str) -> dict[str, Any]:
    """One named case of a shared case file."""

    for case in parity_cases(file_name):
        if case["name"] == name:
            return case
    raise KeyError(f"{file_name} has no case {name!r}")


def write_case_files(case: dict[str, Any], root: Path) -> Path:
    """Write the case's files under root and return the path the case loads."""

    for relative, text in case.get("files", {}).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root / case["path"]


def deployment_case(name: str, root: Path) -> Path:
    """Write one deployment-validation case under root and return its manifest path."""

    return write_case_files(parity_case("deployment-validation.yaml", name), root)
