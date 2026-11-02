# Brokered Network Tutorial

This tutorial checks the R2Kernel brokered network path:

- the package claims a NIC
- `resources.sandbox.network: brokered` installs the direct network socket filter
- user code requests a UDP socket from `r2kernel-resourced`
- `r2kernel-resourced` creates the socket, binds it to the claimed NIC, and passes
  the FD back to the process

Generate a package for the target host:

```bash
python3 tutorials/advanced/resource_control/brokered_network/bin/make_package.py \
  --out /tmp/r2kernel-brokered-network \
  --interface eth0 \
  --bind-host 0.0.0.0 \
  --target-host 192.0.2.24 \
  --target-port 19090
```

`192.0.2.24` is a documentation-only address. Replace it with an address
reachable from the selected interface on your target network.

Run it through the managed launcher:

```bash
r2kernel launch run /tmp/r2kernel-brokered-network --detach
```

The result file reports whether a direct `AF_INET` socket was blocked and
whether the brokered socket FD was usable. `r2kernel-resourced` should run as the
installed privileged service for `SO_BINDTODEVICE` to be applied reliably.
