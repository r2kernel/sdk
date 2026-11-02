# Tutorial Support

This directory contains internal helper code used by runnable tutorials.

- `python/`
  - shared runtime helpers
  - measurement/report helpers
  - timing helper utilities
- `cpp/`
  - shared measurement utilities used by selected transport fixtures
- `build_support.py`
  - the C++ transport fixture build CLI

`tutorials/support` is not a public tutorial category. User-facing guided entry
points are under `tutorials/`.

Low-level declaration and transport fixtures live under `tutorials/fixtures` in
the source workspace and under `tools/qa/fixtures` in the public export. They
are kept for release regression, language comparison, and focused protocol
validation rather than for the guided cleaner tutorial.

## C++ build targets

`tutorials/CMakeLists.txt` owns the header-only `r2kernel::tutorial_support`
target, all `r2kernel_tutorial_<group>_<binary>` fixture executables, the
`r2kernel_tutorial_fixtures` aggregate, and the UNIX
`r2kernel_tutorial_start_gate_test` CTest. Configure it with
`-Dr2kernel_cpp_DIR=<SDK build directory or installed lib/cmake/r2kernel_cpp>`
to consume the SDK package, or explicitly
`-DR2KERNEL_CPP_SOURCE_DIR=<SDK source directory>` to compose an SDK source
target. No Tutorial target compiles the SDK implementation.
`R2KERNEL_TUTORIAL_FIXTURE_ROOT` explicitly names the fixture directory containing
`transport/`. Its workspace default is `tutorials/fixtures`; direct CMake
configuration of a public export must pass
`-DR2KERNEL_TUTORIAL_FIXTURE_ROOT=<export>/tools/qa/fixtures`. CMake does not
probe for alternative layouts.

## C++ fixture build CLI

```bash
python3 tutorials/support/build_support.py <group> [--fixture-root <dir>] [--target all|<binary>] [--mode release|debug]
```

`<group>` is one of `event`, `procedure`, `operation`, `control`, and
`control_drive`. `--fixture-root` defaults to `fixtures/` next to this
directory (the source workspace); a public export passes
`--fixture-root tools/qa/fixtures`. The CLI requires CMake and a C++17
toolchain.

`--sdk auto` (the default for `event`, `procedure`, and `operation`) reuses the
installed SDK under `R2KERNEL_PREFIX` or `/opt/r2kernel` when available;
`R2KERNEL_FORCE_SOURCE_SDK=1` skips it. `--sdk source` (the default for
`control` and `control_drive`) builds against the SDK sources and needs the
Rust runtime core source passed with `--core-source <dir>`; only that mode needs
`cargo`, which always evaluates Core freshness and writes to `CARGO_TARGET_DIR`.
Source mode then builds the SDK's `r2kernel_cpp` target in the shared
`sdk/cpp/build/fixtures/<mode>` cache; `--sdk-build-dir <dir>` overrides that
reusable SDK build directory. `--sdk installed` fails when no installed SDK
exists. `--cmake-arg=-DNAME=VALUE` supplies repeatable configuration inputs
(for example a toolchain file or the SDK's yaml-cpp settings). Separate
incompatible toolchains/configurations into separate build directories.

Executables go directly into `--build-dir` (default
`<fixture-root>/transport/<group>/cpp/build/`); the Tutorial CMake state lives
under that directory's `cmake/`.
