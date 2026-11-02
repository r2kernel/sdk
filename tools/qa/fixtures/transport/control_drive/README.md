# Public-contract Control fixtures (`api.actuator.drive.v1`)

These QA fixtures exchange typed Control over the public
`api.actuator.drive.v1` contract. Control binds a requirement across hosts only
to a provider interface whose visibility is public, so the transport language
comparison's cross-host Control cases (`--include-control`) run these
fixtures; the raw-payload fixtures in `../control` provide an
internal interface and are refused (`provider_visibility_forbidden`).

- `gateway` (role `gateway`) provides the `control` interface and registers
  the contract's one control, `drive_command`, typed
  (`msg.actuator.drive.v1/DriveCommand` -> `DriveState`, timing support
  `required`).
- `controller` (role `component`) requires it as `gateway` and sends timed
  requests through the deployment binding (or `--target`).

Python and C++ take the same options and write the same report schema as the
`../control` fixtures (`kind: control`, `role`, latency summary, `failures`,
`extra`), so a driver can swap them.

## Payload

`--payload-size BYTES` sets the drive vector length: one channel per 21 request
data bytes (uint32 channel, uint8 mode, two float64 values), at least one. The
report records the vector length as `extra.channels` and the encoded request
size as `extra.request_encoded_bytes`. Request `seq` with `n` channels carries
`channels[i] = i`, `mode[i] = (seq + i) & 0xff`, `target[i] = seq + 0.25 i`
and `feedforward[i] = 0.5 i`; the gateway answers `channels`,
`position = target`, `velocity = feedforward` and
`enabled[i] = (seq + i) % 2 == 0`. The gateway rejects a request that differs
from this pattern (`integrity_errors`), and the controller counts a response
that differs from it as a failure. `python/drive_payload.py` and
`cpp/drive_payload.hpp` hold the shared definitions.

## Timing

Every request is timed: `generated_at_sync_ns` is taken just before the call
and the request is valid for `--validity-ms` (default 30000). The gateway
rejects an expired request, so the window must cover the clock offset between
the hosts. The gateway's latency is the request's `sent_at_sync_ns` to its
acceptance (`extra.latency_scope`); across hosts it includes the clock offset.
The Python controller reports the runtime RTT from `transact_control`; the C++
controller reports caller wall time around `transact_control`
(`extra.rtt_measurement_scope`).

## Options

Gateway: `--port` (required for C++), `--name`, `--manifest`,
`--expected-count`, `--warmup`, `--payload-size` (restricts the accepted vector
length; default any), `--quiet`, `--report-format text|json`, `--report-path`.

Controller: `--name`, `--manifest`, `--deployment-manifest`, `--binding`,
`--target`, `--payload-size`, `--period-us | --period-ms`, `--count`,
`--warmup`, `--timeout-ms`, `--deadline-ms`, `--validity-ms`, `--priority`,
`--ready-path` with `--start-gate-path` (and `--gate-timeout-s` in Python,
`--gate-timeout-ms` in C++), `--quiet`, `--report-format`, `--report-path`.
`--topic-count 1`, `--topic-prefix drive_command` and
`--request-mode round-robin` are accepted for parity with `../control`.

With a start gate the controller writes the ready file before resolving the
route or sending any request and waits for the gate file, so a cross-host
driver can create the runtime binding
(`POST /api/v1/control/bindings {"source": "<controller>.gateway", "target": "<gateway>.control"}`)
first. Warmup requests follow the gate; their failures are reported as
`extra.warmup_failures` and excluded from `failures`.

## Building the C++ fixtures

The build follows `../control/README.md`: source SDK by default (pass the Rust
runtime core source with `--core-source <dir>`), `--sdk installed` only for an
intentional comparison, binaries in `transport/control_drive/cpp/build/` unless
`--build-dir <dir>`.

```sh
python3 tutorials/support/build_support.py control_drive --fixture-root tools/qa/fixtures --mode release
```

## Local run

With a running Control, start the gateway, then the controller; the default
deployment `r2kernel.deployment.yaml` binds `drive_controller` to
`drive_gateway` (and the `_cpp` pair likewise):

```sh
python3 python/gateway.py --expected-count 100 --warmup 10 --report-format json --report-path /tmp/gw.json --quiet &
python3 python/controller.py --count 100 --warmup 10 --period-us 1000 --report-format json --report-path /tmp/ctl.json --quiet
```

A successful local run shows typed Control works on one host; it does not show
cross-host binding or periodic performance.
