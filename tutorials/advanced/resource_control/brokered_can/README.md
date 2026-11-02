# Brokered CAN Probe

This tutorial checks the R2Kernel SocketCAN resource path:

- `resources.devices.can` reserves a CAN netdev such as `can0`.
- `resources.sandbox.can: brokered` installs the direct `AF_CAN` socket filter.
- `ResourceBrokerClient.open_can_socket()` asks `r2kernel-resourced` to open and bind the CAN socket, then returns the FD through `SCM_RIGHTS`.

Generate a package:

```bash
python3 tutorials/advanced/resource_control/brokered_can/bin/make_package.py \
  --out /tmp/r2kernel-brokered-can \
  --interface can0 \
  --repo-root "$PWD"
```

Run it through the managed launcher path:

```bash
r2kernel launch run /tmp/r2kernel-brokered-can --detach
```

The result JSON records whether a direct `AF_CAN` socket was blocked and whether the brokered CAN FD was usable.
