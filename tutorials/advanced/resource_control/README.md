# Resource Control Tutorials

This directory contains managed component packages for the R2Kernel launcher and
resource service.

The CPU package demonstrates the minimal managed-launch flow (`<prefix>` is the
install prefix, `/opt/r2kernel` by default):

```bash
. "<prefix>/env.sh"
r2kernel package validate tutorials/advanced/resource_control/cpu_package
r2kernel package build tutorials/advanced/resource_control/cpu_package --out /tmp/r2kernel-cpu-demo.r2kernelpkg --force
r2kernel dev resourced --config "<prefix>/runtime/config.yaml"
r2kernel resource inventory --config "<prefix>/runtime/config.yaml"
r2kernel launch run /tmp/r2kernel-cpu-demo.r2kernelpkg --config "<prefix>/runtime/config.yaml"
```

The package claims CPU core `1` exclusively. `r2kernel-resourced` rejects another
live exclusive claim on the same core, and `r2kernel launch` applies CPU affinity in
the child process before the package entrypoint is executed.

Serial and USB device tutorials should use `resources.devices.paths` or
`resources.devices.serial`. In appliance deployments, install
`r2kernel-device-policy-install` so normal users cannot open those device nodes
directly; `r2kernel-resourced` grants the R2Kernel device group only to managed package
processes with an accepted device claim.

NIC claims should normally use `mode: shared` with a named network policy. Use
`mode: exclusive` only for dedicated links that must not be shared by another
managed component.

`brokered_network/` contains an SDK-level resource lock probe. It generates a
package with `resources.sandbox.network: brokered`; direct `AF_INET` socket
creation is blocked by the launcher sandbox, and user code obtains a UDP socket
or TCP socket FD from `r2kernel-resourced` instead.

`brokered_can/` contains the SocketCAN version of the brokered outlet probe.
It claims `resources.devices.can`, blocks direct `AF_CAN` socket creation with
`resources.sandbox.can: brokered`, and obtains the bound CAN socket FD from
`r2kernel-resourced`.

`brokered_device/` contains the equivalent managed device FD probe. It uses
`resources.sandbox.devices: managed`, so device access is delivered as an
accepted lease-owned FD instead of a broad supplementary device group. The
directory also includes a Linux TTY smoke script that temporarily protects a
selected `/dev/tty...` node, checks direct open blocking, and verifies brokered
FD delivery.

`physical_io/` contains validation probes for real device nodes and multi-NIC
packet paths. Those tutorials generate temporary packages for the current host,
then check whether a managed package can open a claimed device or send UDP
traffic from a selected source interface.
