# R2Kernel SDK

This repository contains public R2Kernel SDK source for release `v1.0.0`.

For bug reports, feature requests, and usage questions, use the shared R2Kernel
issue tracker linked in [CONTRIBUTING.md](CONTRIBUTING.md).

Included source:

- `sdk/python`
- `sdk/cpp`
- `tutorials`
- `tools/qa/fixtures`

The runtime core binary is installed by the R2Kernel installer. C ABI headers required by
the C++ SDK are provided under `runtime/core/include` for source builds and are passed to
CMake explicitly, but the runtime implementation is delivered as installer-managed binaries.

`<prefix>` below is the R2Kernel install prefix (default `/opt/r2kernel`).

## Python

```bash
. <prefix>/env.sh
/usr/bin/python3 -m venv .venv
. .venv/bin/activate
unset PYTHONPATH
python -m pip install -e sdk/python
```

Source the runtime environment before activating the development venv. Activation
selects shell Python; it does not change the managed service/package interpreter.
Clearing `PYTHONPATH` removes the installed SDK injection so the editable checkout
is imported. Re-add unrelated custom import paths explicitly if needed.

## C++

Install R2Kernel first, then point the C++ SDK at the installed runtime library and the
C ABI headers in this checkout:

```bash
cmake -S sdk/cpp -B build/r2kernel-cpp \
  -DR2KERNEL_RUST_CORE_DIR=<prefix>/runtime/lib \
  -DR2KERNEL_RUST_CORE_INCLUDE_DIR="$PWD/runtime/core/include"
cmake --build build/r2kernel-cpp
```

## Tutorials

Normal tutorial use does not require this source checkout. The installer places
guided tutorials under `<prefix>/tutorials` and QA fixtures under
`<prefix>/runtime/tools/qa/fixtures`.

Use the installer-managed Python for normal installed tutorial runs; a second
venv and another SDK wheel install are not required. Start the control backend
before running transport or managed-runtime tutorials if its managed service is
not already running:

```bash
. <prefix>/env.sh
r2kernel-control --config "$R2KERNEL_PREFIX/runtime/config.yaml"
```
