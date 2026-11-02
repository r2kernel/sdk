# Brokered Device Tutorial

This tutorial checks the device FD broker path for managed packages:

- the package claims a `/dev/...` node
- `resources.sandbox.devices: managed` keeps the process out of the R2Kernel device
  access group
- direct `open()` can be checked with `--expect-direct-block`
- user code requests the device FD from `r2kernel-resourced`
- `r2kernel-resourced` verifies the live lease and passes the opened FD back with
  `SCM_RIGHTS`

Use a real protected serial device for appliance validation. `/dev/null` is
useful only as a smoke target because the host normally allows every user to
open it directly.

```bash
python3 tutorials/advanced/resource_control/brokered_device/bin/make_package.py \
  --out /tmp/r2kernel-brokered-device \
  --device /dev/ttyUSB0 \
  --expect-direct-block

r2kernel launch run /tmp/r2kernel-brokered-device --detach
```

For a full TTY permission-and-broker smoke test on a Linux host with an unused
UART node, run:

```bash
tutorials/advanced/resource_control/brokered_device/bin/run_tty_smoke.sh \
  --device /dev/ttyAMA10 \
  --prefix <prefix>
```

The script uses the installed runtime binaries under `--prefix` (default:
`R2KERNEL_PREFIX`, `/opt/r2kernel` or `~/.local/r2kernel`). For binaries outside
an installed prefix, pass `--config`, `--resourced` and `--launch` explicitly.

The script temporarily moves the selected TTY node to the R2Kernel device group,
expects direct `open()` to fail in the managed package, verifies brokered
`open_device()` succeeds, and restores the original node owner/group/mode on
exit.
