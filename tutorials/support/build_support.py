#!/usr/bin/env python3
"""Build Tutorial-owned CMake targets against an SDK-owned CMake package.

Run as a CLI to build one C++ transport fixture group:
``python3 tutorials/support/build_support.py <group> [--fixture-root DIR] ...``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

SDK_MODES = ("auto", "installed", "source")
# Transport fixture groups declared in tutorials/CMakeLists.txt: binaries and default SDK mode.
# Control fixtures build against the SDK sources by default; an installed SDK is an intentional comparison.
FIXTURE_GROUPS: dict[str, tuple[tuple[str, ...], str]] = {
    "event": (("publisher", "subscriber", "fanout_subscriber", "ping", "echo"), "auto"),
    "procedure": (("server", "client"), "auto"),
    "operation": (("server", "client"), "auto"),
    "control": (("gateway", "controller"), "source"),
    "control_drive": (("gateway", "controller"), "source"),
}


def missing_tool_error(tool: str, *, context: str) -> SystemExit:
    return SystemExit(f"Missing required tool for {context}: `{tool}`. Install it and ensure it is on PATH.")


def rust_library_filename() -> str:
    if sys.platform == "darwin":
        return "libr2kernel_core.dylib"
    if sys.platform.startswith("linux"):
        return "libr2kernel_core.so"
    if sys.platform == "win32":
        return "r2kernel_core.dll"
    raise SystemExit(f"Unsupported platform: {sys.platform}")


def cargo_target_dir(core_dir: Path) -> Path:
    """Resolve relative CARGO_TARGET_DIR before changing the Cargo working directory."""
    configured = os.environ.get("CARGO_TARGET_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else core_dir / "target"


def ensure_source_rust_library(core_dir: Path | None, *, profile: str, context: str) -> Path:
    """Always let Cargo evaluate its full fingerprint graph, including build scripts."""
    if core_dir is None or not (core_dir / "Cargo.toml").exists():
        checked = f"Checked: {core_dir}\n" if core_dir is not None else ""
        raise SystemExit(
            f"R2Kernel runtime core source is not available for {context}.\n{checked}"
            "Install R2Kernel first (R2KERNEL_PREFIX), or pass --core-source with the Rust runtime core source directory."
        )
    if not shutil.which("cargo"):
        raise missing_tool_error("cargo", context=context)
    target_dir = cargo_target_dir(core_dir)
    built = target_dir / profile / rust_library_filename()
    cmd = ["cargo", "build", "--locked"]
    if profile == "release":
        cmd.append("--release")
    run(cmd, cwd=core_dir, env={**os.environ, "CARGO_TARGET_DIR": str(target_dir)})
    if not built.exists():
        raise SystemExit(f"Rust library was not produced: {built}")
    return built


def installed_prefix_candidates() -> list[Path]:
    prefixes = [Path("/opt/r2kernel")]
    if configured := os.environ.get("R2KERNEL_PREFIX"):
        prefixes.insert(0, Path(configured).expanduser())
    return list(dict.fromkeys(prefixes))


def resolve_installed_cpp_sdk() -> Path | None:
    """Locate the SDK package in the historical public sdk/cpp install layout."""
    if os.environ.get("R2KERNEL_FORCE_SOURCE_SDK", "").strip().lower() in {"1", "true", "yes", "on"}:
        return None
    for prefix in installed_prefix_candidates():
        for lib in ("lib", "lib64"):
            package = prefix / "sdk" / "cpp" / lib / "cmake" / "r2kernel_cpp"
            if (package / "r2kernel_cppConfig.cmake").is_file():
                return package.resolve()
    return None


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    print(f"$ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=str(cwd), check=True, env=env)


def resolve_cpp_sdk_package(
    root: Path, *, sdk: str, core_source: Path | None, mode: str,
    sdk_build_dir: Path | None, cmake_args: list[str], context: str,
) -> Path:
    """Return a package directory, building a reusable source SDK when selected."""
    if sdk not in SDK_MODES:
        raise SystemExit(f"unknown C++ SDK mode for {context}: {sdk}")
    installed = resolve_installed_cpp_sdk() if sdk in {"auto", "installed"} else None
    if installed is not None:
        return installed
    if sdk == "installed":
        raise SystemExit(f"--sdk installed was requested for {context}, but no installed C++ SDK CMake package was found.")
    core = core_source.resolve() if core_source else None
    library = ensure_source_rust_library(core, profile=mode, context=context)
    assert core is not None  # The Core source is required by ensure_source_rust_library.
    sdk_source = root / "sdk" / "cpp"
    package = (sdk_build_dir or sdk_source / "build" / "fixtures" / mode).resolve()
    config = "Release" if mode == "release" else "Debug"
    run([
        "cmake", "-S", str(sdk_source), "-B", str(package), *cmake_args,
        f"-DCMAKE_BUILD_TYPE={config}", "-DBUILD_TESTING=OFF",
        f"-DR2KERNEL_RUST_CORE_DIR={library.parent}",
        f"-DR2KERNEL_RUST_CORE_LIBRARY={library}",
        f"-DR2KERNEL_RUST_CORE_INCLUDE_DIR={core / 'include'}",
    ], cwd=root)
    run(["cmake", "--build", str(package), "--config", config, "--target", "r2kernel_cpp"], cwd=root)
    return package


def build_cpp_fixtures(
    *, root: Path, fixture_root: Path, fixture_group: str, targets: list[str], sdk: str,
    core_source: Path | None, mode: str, build_dir: Path | None, context: str,
    sdk_build_dir: Path | None = None, cmake_args: list[str] | None = None,
) -> Path:
    """Build declared Tutorial fixture targets into one flat binary directory.

    The default output is ``<fixture_root>/transport/<group>/cpp/build``.
    """
    if not shutil.which("cmake"):
        raise missing_tool_error("cmake", context=context)
    cmake_args = cmake_args or []
    output = (build_dir or fixture_root / "transport" / fixture_group / "cpp" / "build").resolve()
    package = resolve_cpp_sdk_package(
        root, sdk=sdk, core_source=core_source, mode=mode,
        sdk_build_dir=sdk_build_dir, cmake_args=cmake_args, context=context,
    )
    config = "Release" if mode == "release" else "Debug"
    cmake_build = output / "cmake"
    run([
        "cmake", "-S", str(root / "tutorials"), "-B", str(cmake_build), *cmake_args,
        f"-DCMAKE_BUILD_TYPE={config}", "-DBUILD_TESTING=OFF", "-DR2KERNEL_CPP_SOURCE_DIR=",
        f"-Dr2kernel_cpp_DIR={package}", f"-DR2KERNEL_TUTORIAL_FIXTURE_GROUP={fixture_group}",
        f"-DR2KERNEL_TUTORIAL_OUTPUT_DIR={output}",
        f"-DR2KERNEL_TUTORIAL_FIXTURE_ROOT={fixture_root.resolve()}",
    ], cwd=root)
    run([
        "cmake", "--build", str(cmake_build), "--config", config, "--target",
        *[f"r2kernel_tutorial_{fixture_group}_{target}" for target in targets],
    ], cwd=root)
    print(f"Built targets in {output} ({mode})")
    return output


def repo_root() -> Path:
    """The directory holding both ``tutorials/`` and ``sdk/`` (source workspace or public export)."""
    root = Path(__file__).resolve().parents[2]
    if not ((root / "tutorials" / "support").is_dir() and (root / "sdk").is_dir()):
        raise SystemExit(f"could not resolve the tutorials/sdk root from {__file__}")
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build one group of C++ transport fixtures.")
    parser.add_argument("group", choices=sorted(FIXTURE_GROUPS), help="Transport fixture group.")
    parser.add_argument("--target", default="all", help="Binary of the group to build, or all (default).")
    parser.add_argument(
        "--mode", choices=["debug", "release"], default="release",
        help="Build mode for both the C++ binaries and the Rust core link target.",
    )
    parser.add_argument(
        "--sdk", choices=SDK_MODES, default=None,
        help=(
            "auto uses an installed SDK when found (R2KERNEL_FORCE_SOURCE_SDK=1 skips it) and otherwise the SDK "
            "sources in this tree; installed/source select one explicitly. Default: source for the control "
            "groups, auto otherwise."
        ),
    )
    parser.add_argument(
        "--fixture-root", type=Path, default=Path(__file__).resolve().parents[1] / "fixtures",
        help="Fixture directory containing transport/ (default: fixtures/ next to support/; "
             "pass <export>/tools/qa/fixtures for a public export).",
    )
    parser.add_argument(
        "--build-dir", type=Path,
        help="Directory for the built binaries (default: <fixture-root>/transport/<group>/cpp/build).",
    )
    parser.add_argument("--sdk-build-dir", type=Path, help="Reusable source SDK CMake build directory.")
    parser.add_argument(
        "--cmake-arg", action="append", default=[],
        help="CMake configure argument (repeatable; use --cmake-arg=-DNAME=VALUE).",
    )
    parser.add_argument(
        "--core-source", type=Path,
        help="Rust runtime core source directory for builds against SDK sources; Cargo output follows CARGO_TARGET_DIR.",
    )
    args = parser.parse_args(argv)
    binaries, default_sdk = FIXTURE_GROUPS[args.group]
    if args.target != "all" and args.target not in binaries:
        parser.error(f"--target for {args.group} must be one of: all, {', '.join(binaries)}")
    if not (args.fixture_root / "transport" / args.group / "cpp").is_dir():
        parser.error(f"--fixture-root {args.fixture_root} has no transport/{args.group}/cpp directory")
    build_cpp_fixtures(
        root=repo_root(),
        fixture_root=args.fixture_root,
        fixture_group=args.group,
        targets=list(binaries) if args.target == "all" else [args.target],
        sdk=args.sdk or default_sdk,
        core_source=args.core_source,
        mode=args.mode,
        build_dir=args.build_dir,
        sdk_build_dir=args.sdk_build_dir,
        cmake_args=args.cmake_arg,
        context=f"C++ {args.group} fixture build",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
