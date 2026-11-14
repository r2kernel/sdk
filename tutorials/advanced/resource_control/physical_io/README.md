# Physical I/O Validation

These probes validate the boundary between R2Kernel resource claims and actual Linux
I/O behavior.

The tutorials are intentionally generated at test time because device paths and
NIC names differ across targets. Use `make_packages.py` to create temporary
managed gateway packages for the current host. The UDP sender packages
explicitly select `resources.sandbox.network: advisory` because these probes
exercise direct Linux socket binding and routing. This exception is available
only to gateways, permits general direct networking, and needs a root approval
of each generated package with `r2kernel-resourced --grant-advisory-network`:

```bash
python3 tutorials/advanced/resource_control/physical_io/bin/make_packages.py \
  --out /tmp/r2kernel-physical-io-packages \
  --device /dev/ttyUSB0 \
  --native-iface eth0 \
  --native-source 192.0.2.20 \
  --usb-iface eth1 \
  --usb-source 192.0.2.32 \
  --target 192.0.2.24 \
  --port 19080
```

The `192.0.2.x` values are documentation-only addresses. Replace them with
addresses reachable on the corresponding test interfaces.

## Device Node Probe

`device_open_probe.py` opens the claimed device and writes a JSON result with
the process UID/GID, supplementary groups, device mode, and open result. A
positive appliance test should show:

- direct unmanaged user open fails after the udev policy is applied
- `r2kernel launch run --detach` succeeds after `r2kernel-resourced` grants the R2Kernel
  device group to the managed package process
- a second managed package claiming the same device is rejected while the first
  lease is live

## NIC Packet Probe

`udp_receiver.py` records packet source addresses. `udp_sender.py` sends packets
from a chosen source IP. The generated packages include positive and negative
probes:

- `nic_claim_native_send_native`: claims the native NIC and sends from the
  native source IP
- `nic_claim_usb_send_usb`: claims the USB NIC and sends from the USB source IP
- `nic_claim_usb_send_native`: claims the USB NIC but sends from the native
  source IP
- `nic_claim_native_send_usb`: claims the native NIC but sends from the USB
  source IP

The mismatch probes are expected to reveal whether launcher-level NIC claims are
only reservations or actual packet-path enforcement. With the current resource
service, managed launch rejects conflicting claims, but packet routing still
follows normal Linux socket binding and routing rules.

When multiple NICs are attached to the same IPv4 subnet, Linux may select the
primary route and may answer ARP for another local interface address on that
route. In that topology, binding a socket to the USB NIC source address is not
the same as hard-isolating traffic to the USB NIC. Use packet capture on the
receiving host, or place NICs on separate subnets/VLANs, when validating actual
egress/ingress paths.
