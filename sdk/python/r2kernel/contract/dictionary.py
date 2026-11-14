"""The installed standard dictionary and the reserved-ID rule for package spec roots.

Package spec roots are searched before the installed dictionary, and
unqualified IDs are reserved for that dictionary. A package spec root may
therefore contain a standard ID only as an identical copy of the installed
definition, and it may not add an ID under a standard prefix
(``<kind>.<first segment>`` of any standard ID, such as ``api.locomotion``).
Vendors use their own segment, such as ``api.acme.*``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Iterable

import yaml

from r2kernel.spec_paths import (
    read_spec_text,
    R2KERNEL_CONTRACT_SPEC_ROOTS_ENV,
    R2KERNEL_SPEC_ROOT_ENV,
    installed_spec_roots,
    relative_spec_path,
    spec_snapshot_scope_active,
)

from .loader import ContractSpecError

R2KERNEL_PREFIX_ENV = "R2KERNEL_PREFIX"
SPEC_DOCUMENT_TREES = ("messages", "apis", "components")
_MAX_SPEC_TREE_DEPTH = 16


def installed_dictionary_root() -> Path | None:
    """The complete standard dictionary this process resolves standard IDs from.

    ``R2KERNEL_SPEC_ROOT`` (a complete replacement tree), then
    ``$R2KERNEL_PREFIX/spec`` and the default-prefix spec locations, then the
    dictionary that ships with this SDK.
    """
    candidates: list[Path] = []
    shared_root = os.environ.get(R2KERNEL_SPEC_ROOT_ENV)
    if shared_root:
        candidates.append(Path(shared_root).expanduser())
    prefix = os.environ.get(R2KERNEL_PREFIX_ENV)
    if prefix:
        candidates.append(Path(prefix).expanduser() / "spec")
    candidates.extend(installed_spec_roots())
    for candidate in candidates:
        if _is_dictionary_root(candidate):
            return candidate.resolve()
    return None


def caller_package_spec_roots() -> tuple[Path, ...]:
    """The package spec roots the launcher injected for this process (``R2KERNEL_CONTRACT_SPEC_ROOTS``)."""
    roots: list[Path] = []
    for raw in os.environ.get(R2KERNEL_CONTRACT_SPEC_ROOTS_ENV, "").split(os.pathsep):
        if not raw:
            continue
        root = Path(raw).expanduser()
        if root.is_dir() and root.resolve() not in roots:
            roots.append(root.resolve())
    return tuple(roots)


def validate_package_spec_roots(roots: Iterable[Path], dictionary: Path | None = None) -> None:
    """Refuse package spec roots that redefine a standard ID or add an ID under a standard prefix.

    Raises ContractSpecError naming every violation.
    """
    roots = tuple(roots)
    if not roots:
        return
    dictionary = dictionary if dictionary is not None else installed_dictionary_root()
    if dictionary is None:
        return
    violations: list[str] = []
    prefixes = standard_prefixes(dictionary)
    for root in roots:
        violations.extend(package_spec_root_violations(Path(root), dictionary, prefixes=prefixes))
    if violations:
        raise ContractSpecError("; ".join(violations))


def package_spec_root_violations(
    root: Path, dictionary: Path, *, prefixes: frozenset[str] | None = None
) -> list[str]:
    """The reserved-ID violations of one package spec root against the standard dictionary."""
    if prefixes is None:
        prefixes = standard_prefixes(dictionary)
    documents, problems = _spec_tree(Path(root))
    violations = [f"Package spec root {root}: {problem}" for problem in problems]
    for relative in documents:
        problem = _document_identity_problem(Path(root), relative)
        if problem is not None:
            violations.append(f"Package spec root {root}: {problem}")
        parts = relative.parts
        spec_id = ".".join((*parts[1:-1], Path(parts[-1]).stem))
        standard = dictionary / relative
        if standard.is_file():
            if (root / relative).read_bytes() != standard.read_bytes():
                violations.append(
                    f"Package spec root {root}: {spec_id} differs from the installed standard definition; "
                    "a package spec root may contain a standard ID only as an identical copy"
                )
            continue
        prefix = f"{parts[1]}.{parts[2]}"
        if prefix in prefixes:
            violations.append(
                f"Package spec root {root}: {spec_id} uses the standard prefix {prefix}; "
                f"use a vendor segment such as {parts[1]}.<vendor>.*"
            )
    if not violations:
        problem = _core_refusal(Path(root))
        if problem is not None:
            violations.append(f"Package spec root {root}: {problem}")
    return violations


CORE_REFUSAL = "the runtime core refuses this spec root when it prepares the codecs"


def _core_refusal(root: Path) -> str | None:
    """The core's own verdict on a spec root, or None when it accepts it.

    The checks above name the path of the problems they know. The core's schema
    context, the one codec setup creates, decides last, so a root the core
    refuses never passes validation (cross-audit CONTROL-112-03).
    """
    if spec_snapshot_scope_active():
        # Inside a codec snapshot the core has already accepted these sources.
        return None
    try:
        from r2kernel.interface.prepared_codec import PreparedSchemaContext

        PreparedSchemaContext([root])
    except FileNotFoundError:
        # No core library: codec setup cannot run here either.
        return None
    except RuntimeError as exc:
        return f"{CORE_REFUSAL} ({exc})"
    return None


def standard_prefixes(dictionary: Path) -> frozenset[str]:
    """``<kind>.<first segment>`` of every standard ID of the dictionary."""
    return frozenset(
        f"{relative.parts[1]}.{relative.parts[2]}" for relative in _spec_documents(dictionary)
    )


def _spec_documents(root: Path) -> list[Path]:
    """Relative paths of ``<tree>/<kind>/<segment>/.../<version>.yaml`` documents under root."""
    return _spec_tree(root)[0]


def _spec_tree(root: Path) -> tuple[list[Path], list[str]]:
    """The documents under root and the problems that make the tree unusable.

    Like the core's spec snapshot, the walk refuses rather than skips: a directory
    reached twice (an alias), an entry that cannot be read, a path that leaves the
    root, a tree deeper than the limit, or a YAML document outside
    ``<tree>/<kind>/<segment>/.../<version>.yaml``.
    """
    documents: list[Path] = []
    problems: list[str] = []
    seen: dict[Path, Path] = {}
    try:
        base = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return [], [f"cannot be read: {_reason(exc)}"]

    def walk(relative: Path, depth: int) -> None:
        directory = root / relative
        shown = relative.as_posix()
        if depth > _MAX_SPEC_TREE_DEPTH:
            problems.append(f"{shown} is nested deeper than {_MAX_SPEC_TREE_DEPTH} directories")
            return
        try:
            real = directory.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            problems.append(f"{shown} cannot be read: {_reason(exc)}")
            return
        if real != base and base not in real.parents:
            problems.append(f"{shown} leads outside the package spec root")
            return
        first = seen.get(real)
        if first is not None:
            problems.append(
                f"{shown} is the same directory as {first.as_posix()}; spec directories must not alias each other"
            )
            return
        seen[real] = relative
        try:
            names = sorted(os.listdir(directory))
        except OSError as exc:
            problems.append(f"{shown} cannot be read: {_reason(exc)}")
            return
        files: list[str] = []
        directories: list[str] = []
        for name in names:
            try:
                mode = (directory / name).stat().st_mode
            except OSError as exc:
                problems.append(f"{(relative / name).as_posix()} cannot be read: {_reason(exc)}")
                continue
            if stat.S_ISDIR(mode):
                directories.append(name)
            elif stat.S_ISREG(mode):
                files.append(name)
        for name in files:
            if not name.endswith(".yaml"):
                continue
            child = relative / name
            if len(child.parts) < 4:
                problems.append(f"{child.as_posix()} is not at <tree>/<kind>/<segment>/.../<version>.yaml")
                continue
            documents.append(child)
        for name in directories:
            walk(relative / name, depth + 1)

    for tree in SPEC_DOCUMENT_TREES:
        top = root / tree
        if top.exists() or top.is_symlink():
            walk(Path(tree), 0)
    return documents, problems


def _document_identity_problem(root: Path, relative: Path) -> str | None:
    """Why a message or API document's ``id`` does not match its path, or None.

    The core reads every message and API document of a spec root, referenced or
    not, and refuses one whose ``id`` differs from its path or whose path is not
    the one its ``id`` names; the guard applies both conditions so a validated
    package does not fail at codec setup.
    """
    if relative.parts[0] not in ("messages", "apis"):
        return None
    shown = relative.as_posix()
    expected = ".".join((*relative.parts[1:-1], relative.stem))
    try:
        # The loaders' reader: inside a codec snapshot scope it serves the captured
        # text, which the core already checked; a document the snapshot lacks is not
        # read by any loader either.
        text = read_spec_text(root / relative)
    except UnicodeDecodeError:
        return f"{shown} is not a valid YAML document"
    except (FileNotFoundError, ValueError):
        # Absent from, or outside, the captured snapshot: the snapshot scope decides.
        return None
    except OSError:
        return f"{shown} is not a valid YAML document"
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return f"{shown} is not a valid YAML document"
    declared = document.get("id") if isinstance(document, dict) else None
    if not isinstance(declared, str) or not declared.strip():
        return f"{shown} has no id"
    declared = declared.strip()
    if declared != expected:
        return f"{shown} declares id {declared}; a document at this path must declare {expected}"
    # The path the id names, the way every loader resolves it: a dot inside a
    # directory or file name joins to the same id but names another path.
    try:
        canonical = relative_spec_path(relative.parts[0], declared)
    except ValueError:
        return f"{shown} declares id {declared}, which names no document path"
    if canonical != relative:
        return f"{shown} is not at the path of id {declared}; a document with this id must be at {canonical.as_posix()}"
    return None


def _reason(exc: BaseException) -> str:
    return getattr(exc, "strerror", None) or str(exc)


def _is_dictionary_root(path: Path) -> bool:
    return (path / "defaults.yaml").is_file() and all((path / tree).is_dir() for tree in SPEC_DOCUMENT_TREES)
