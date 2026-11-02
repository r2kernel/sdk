# Declare Tutorials

This directory contains declaration-first fixtures built on the canonical spec model:

- `device/`
  - declaration and validation fixtures for canonical `component.*` components

These fixtures focus on:

- `API contract`
- `component`
- `declare + validate`

The `device/` fixtures use gateway examples and require a running local
control backend from the installed runtime.

For Python tutorials, source the installed runtime environment first (`<prefix>`
is the install prefix, `/opt/r2kernel` by default):

```bash
. <prefix>/env.sh
python3 -c 'import r2kernel; print("R2Kernel Python SDK ready")'
```

For an isolated development shell, install the wheel into a virtual
environment. When working from an editable public SDK checkout, run
`pip install -e sdk/python` from the repository root instead.

They are separate from `transport/`, which validates runtime communication
primitives.
