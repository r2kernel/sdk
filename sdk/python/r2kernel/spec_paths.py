from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


R2KERNEL_SPEC_ROOT_ENV = "R2KERNEL_SPEC_ROOT"
R2KERNEL_SPEC_PATH_ENV = "R2KERNEL_SPEC_PATH"
R2KERNEL_CONTRACT_SPEC_ROOTS_ENV = "R2KERNEL_CONTRACT_SPEC_ROOTS"

_PACKAGE_SPEC_ROOT = Path(__file__).resolve().parent / "specs"
_REPO_SPEC_ROOT = Path(__file__).resolve().parents[3] / "spec"
_frozen_sources: ContextVar[object | None] = ContextVar('r2kernel_frozen_spec_sources', default=None)
_scoped_roots: ContextVar[tuple[Path, ...] | None] = ContextVar('r2kernel_scoped_spec_roots', default=None)


class _FrozenSources:
    def __init__(self, context, roots):
        self.context = context
        self.roots = tuple(Path(root).resolve() for root in roots)
        self.virtual_root = Path('/__r2kernel_snapshot_specs__')
        self.texts: dict[str, str | None] = {}

    def text(self, relative):
        key = str(relative)
        if key not in self.texts:
            self.texts[key] = self.context.spec_text(key)
        return self.texts[key]

    def relative(self, path):
        resolved = Path(path).resolve()
        for root in (*self.roots, self.virtual_root):
            try:
                return resolved.relative_to(root)
            except ValueError:
                continue
        raise ValueError(f'Spec path is outside the initialization snapshot: {path}')


@contextmanager
def frozen_spec_sources(context, roots):
    """Contract loaders and codecs see the exact same captured source contents."""
    token = _frozen_sources.set(_FrozenSources(context, roots))
    try:
        yield
    finally:
        _frozen_sources.reset(token)


@contextmanager
def package_spec_sources(roots):
    """Inspect one package without importing another package's private roots."""
    candidates: list[Path] = []
    for root in roots:
        _push_path(candidates, Path(root).resolve())
    # Launchers prepend the caller's private contract roots to SPEC_PATH. Keep
    # independently configured dictionary paths while removing that caller set.
    caller_roots = {
        Path(root).expanduser().resolve()
        for root in os.environ.get(R2KERNEL_CONTRACT_SPEC_ROOTS_ENV, '').split(os.pathsep) if root
    }
    for raw in os.environ.get(R2KERNEL_SPEC_PATH_ENV, '').split(os.pathsep):
        if raw:
            root = Path(raw).expanduser().resolve()
            if root not in caller_roots:
                _push_path(candidates, root)
    # The singular root configures the shared dictionary.
    shared_root = os.environ.get(R2KERNEL_SPEC_ROOT_ENV)
    if shared_root:
        _push_path(candidates, Path(shared_root).expanduser().resolve())
    _push_standard_spec_roots(candidates)
    selected = tuple(Path(root).resolve() for root in candidates)
    frozen = _frozen_sources.get()
    if frozen is not None and selected != frozen.roots:
        raise RuntimeError('Package spec scope differs from the captured codec snapshot; '
                           'load the deployment before codec initialization')
    token = _scoped_roots.set(selected)
    try:
        yield
    finally:
        _scoped_roots.reset(token)


def spec_snapshot_scope_active() -> bool:
    """Whether loaders currently read a captured codec snapshot."""
    return _frozen_sources.get() is not None


def read_spec_text(path: Path) -> str:
    frozen = _frozen_sources.get()
    if frozen is None:
        return path.read_text()
    text = frozen.text(frozen.relative(path))
    if text is None:
        raise FileNotFoundError(f'Spec absent from initialization snapshot: {path}')
    return text


def spec_path_exists(path: Path) -> bool:
    frozen = _frozen_sources.get()
    return path.exists() if frozen is None else frozen.text(frozen.relative(path)) is not None


def spec_root_candidates() -> tuple[Path, ...]:
    frozen = _frozen_sources.get()
    if frozen is not None:
        return frozen.roots
    scoped = _scoped_roots.get()
    if scoped is not None:
        return scoped
    candidates: list[Path] = []

    env_contract_roots = os.environ.get(R2KERNEL_CONTRACT_SPEC_ROOTS_ENV)
    if env_contract_roots:
        for path in env_contract_roots.split(os.pathsep):
            if path:
                _push_path(candidates, Path(path).expanduser())

    env_path = os.environ.get(R2KERNEL_SPEC_PATH_ENV)
    if env_path:
        for path in env_path.split(os.pathsep):
            if path:
                _push_path(candidates, Path(path).expanduser())

    env_root = os.environ.get(R2KERNEL_SPEC_ROOT_ENV)
    if env_root:
        _push_path(candidates, Path(env_root).expanduser())

    # Only the working directory itself, as in the runtime core and the C++ SDK;
    # its ancestors are not searched (cross-audit SDK-112-03).
    cwd = Path.cwd()
    _push_path(candidates, cwd / "r2kernel-spec")
    _push_path(candidates, cwd / "spec")

    _push_standard_spec_roots(candidates)
    return tuple(candidates)


def installed_spec_roots() -> tuple[Path, ...]:
    """The existing installed, bundle-adjacent, checkout and packaged spec roots, in search order."""
    candidates: list[Path] = []
    _push_standard_spec_roots(candidates)
    return tuple(candidates)


def _push_standard_spec_roots(candidates: list[Path]) -> None:
    _push_path(candidates, Path("/opt/r2kernel/spec"))
    _push_path(candidates, Path("/opt/r2kernel/runtime/spec"))

    exe_dir = Path(sys.executable).resolve().parent
    _push_path(candidates, exe_dir / "spec")
    _push_path(candidates, exe_dir.parent / "spec")
    _push_path(candidates, exe_dir.parent / "runtime" / "spec")

    _push_path(candidates, _REPO_SPEC_ROOT)
    _push_path(candidates, _PACKAGE_SPEC_ROOT)


def resolve_spec_path(relative: str | Path) -> Path | None:
    relative_path = Path(relative)
    frozen = _frozen_sources.get()
    if frozen is not None:
        return frozen.virtual_root / relative_path if frozen.text(relative_path) is not None else None
    for root in spec_root_candidates():
        candidate = root / relative_path
        if candidate.exists():
            return candidate
    return None


def relative_spec_path(top_level: str, spec_id: str) -> Path:
    parts = [part for part in str(spec_id or "").split(".") if part]
    if len(parts) < 2:
        raise ValueError(f"Invalid spec id: {spec_id}")
    version = parts[-1]
    namespace_parts = parts[:-1]
    return Path(top_level).joinpath(*namespace_parts, f"{version}.yaml")


def _push_path(candidates: list[Path], path: Path) -> None:
    if path.exists() and path not in candidates:
        candidates.append(path)
