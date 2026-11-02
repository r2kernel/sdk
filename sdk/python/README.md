# r2kernel-client

Unified Python R2Kernel SDK for public R2Kernel source tutorials and installed runtime use.

This package provides:

- `r2kernel.core`: Event, Control, Procedure and Operation transports and Control backend-facing runtime/client APIs
- `r2kernel.core.ResourceBrokerClient`: managed UDP/TCP/device FD requests for
  packages launched by `r2kernel-launch`
- `r2kernel.wrapper`: subsystem lifecycle base class with direct register/call helpers
- `r2kernel.ffi`: Rust core bindings
- `r2kernel.datatype`: shared datatype encoders/decoders
- `r2kernel.interface`: packaged spec inventory access and helper bindings
- `r2kernel.contract`: canonical profile/subsystem-class declaration loader and validator
- `r2kernel.manifest`: mandatory subsystem/deployment manifest loader and binding resolver
- `r2kernel.helper`: config/logging helpers

## Utility conversions

Import unit conversions explicitly from `r2kernel.utility.common`:

```python
from r2kernel.datatype.coordinate import Orientation, Position, Pose
from r2kernel.datatype.manipulator import JointValues
from r2kernel.utility.common import to_meter_radian_kg, to_millimeter_degree_g

pose_mm_deg = Pose(Position(1000, 0, 500), Orientation(180, 0, 90))
pose_si = to_meter_radian_kg(pose_mm_deg)  # m and radians, returns a new Pose
pose_restored = to_millimeter_degree_g(pose_si)

joints = JointValues([180, 250], names=("hinge", "slide"))
joints_si = to_meter_radian_kg(joints, jointtype="RP")  # pi rad, 0.25 m
joints_restored = to_millimeter_degree_g(joints_si, jointtype="RP")
```

Supported datatypes are `Position`, `Orientation`, `Pose`, `JointValues`,
`TaskWayPoint`, `TCP`, `Payload`, and `ToolSettings`. Joint vectors require exactly
one `R` (revolute, degree/radian) or `P` (prismatic, mm/m) per value; invalid or
missing joint types fail rather than dropping axes. Custom joint names survive
conversion. `TaskWayPoint` converts its pose and radius, retaining `vel`, `acc`,
and `cornering_vel` as supplied. `TCP` converts position and orientation;
`Payload` converts mass (g/kg), center of mass (mm/m), and inertia
(g·mm²/kg·m²). `ToolSettings` converts its nested payload and TCP. Conversions
do not mutate their inputs and round trips retain float32 datatype precision.
The removed `WayPoint`, `ToolOffset`, and `GcodeSegment` models are not supported
through compatibility aliases.

`r2kernel.utility.common` also provides `offset_pose` and
`blend_pose` in the caller's existing units. Rotation and homogeneous-transform
helpers in `r2kernel.utility.rotation_transform` take radians, not degrees;
homogeneous-transform translations use meters. They need SciPy, which is the
optional `rotation` extra: `pip install 'r2kernel-client[rotation]'`.

With the SDK installed in your active Python environment, you can check mixed
joint-unit conversion without starting the runtime:

```bash
python3 - <<'PY'
import math
from r2kernel.datatype.manipulator import JointValues
from r2kernel.utility.common import to_meter_radian_kg

joints = JointValues([180, 250], names=("hinge", "slide"))
converted = to_meter_radian_kg(joints, jointtype="RP")
assert math.isclose(converted.hinge, math.pi, abs_tol=1e-6)
assert math.isclose(converted.slide, 0.25, abs_tol=1e-6)
print(converted)
PY
```

## Public Repo Usage

When you clone the public R2Kernel repo, prepare a local Python environment from
the repo root first:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e sdk/python
```

The public cleaner tutorial expects the R2Kernel runtime to be installed under
an install prefix, written `<prefix>` below (`/opt/r2kernel` by default, or the
prefix named by `R2KERNEL_PREFIX`).

Start the installed control backend in one terminal:

```bash
. <prefix>/env.sh
r2kernel control --config <prefix>/config.yaml
```

Then run the installed cleaner tutorial from another terminal:

```bash
cd <prefix>
python3 tutorials/run_sim_world.py
```

If you want to install the packaged wheel into a separate Python environment,
use the installer-delivered artifact under `<prefix>/sdk/python/` when it
matches your Python ABI.

## Layering
- `r2kernel.core` owns transport/runtime responsibilities:
  - event transport
  - RPC transport
  - Control transport (Procedure and Operation run on it)
  - control backend heartbeat/runtime wiring
  - brokered UDP, TCP, CAN, and device FDs issued by `r2kernel-resourced`
- `r2kernel.wrapper` owns lifecycle/state/config plus manifest-backed subsystem validation
- `r2kernel.contract` owns canonical subsystem-role contracts:
  - profile/subsystem-class inventory loading
  - class-to-profile expansion
  - manifest declaration validation during subsystem initialization
- `r2kernel.interface` provides packaged spec inventory access and helper bindings
- subsystem startup is manifest-first; the SDK resolves logical bindings from
  `r2kernel.deployment.yaml` before application code talks to a concrete provider

Brokered socket/device requests submit each operation once and use one five-second
absolute deadline for connection, submission, and response reception. A response
must be a complete newline-terminated JSON frame of at most 1 MiB and contain
exactly one FD on success. Fragmented responses are supported; malformed,
truncated, oversized, failed, or incomplete responses close all received FDs.
The SDK never automatically resubmits a rejected or ambiguously delivered
resource operation. The returned socket or `BrokeredFdResult` owns the successful
FD; the caller must close it.

Production local Control uses `forwarded_shm_v5` / `binary_shm_v5` and frame
version 5 only. Upgrade the runtime and all participating SDKs together; other
frame versions and policy/protocol labels are rejected.

For provider-ready production Control v5, call
`prepare_control(target, topic, request_capacity, response_capacity,
timeout_ms=1000, result_ready=False)` after the provider is started and the
binding is live, before the periodic loop. Capacities are encoded payload bytes,
including any typed or Control envelope, not Python object sizes. Preparation uses a
separate cold absolute timeout covering resolution and provider readiness. It
never invokes the application callback. Success means both caller and provider
are ready at the declared capacities and completion mode. `handshake_control`
remains a route check, not this preparation contract.

Prepared transactions use the same `(target, topic)` lane and reject payloads
outside either declared capacity; they do not create sockets, mappings, workers
or grow regions. Route/binding invalidation fails explicitly rather than silently
falling back to a cold transaction. A deadline miss that expires before anything
reaches Control fails only that exchange and keeps the prepared lane. A late
exchange after publication, or a connection Control closed or reset (end of
stream before a frame, or a send that fails with `BrokenPipeError` or a receive
that fails with `ConnectionResetError`), fails that exchange without replay and
re-prepares the same lane once, in the call whose exchange failed (another call
that fails on the same closed lane does not run the recovery), with its original
capacities, completion mode and preparation timeout, after Control has closed
the failed connection; the failing call raises its error after that bounded re-preparation
(at most the lane's preparation timeout). If re-preparation fails, the lane stays
invalid until an explicit preparation or rebind. Route, codec, binding, protocol
and capacity failures, and a transaction failure that Control reports in its
reply (it then closes the connection), are not re-prepared automatically. The late exchange is
still a failed exchange. The client's local Control v5 counters
(`control_shm_statistics()`) count automatic re-preparations that succeeded as
`prepared_lane_recoveries` and those that failed as
`prepared_lane_recovery_failures`. Explicit preparation can replace an idle lane
with a fresh one; in-flight calls, pending reclamation and retained reclamation
errors are not discarded. Ordinary unprepared transactions remain supported.
Production caller deadlines include route lookup and encoding and carry remaining
time through transport/proof/reclamation; a late successful decode raises a
deadline error rather than reporting successful on-time completion.
Route resolution during preparation or a production transaction makes a single
request using the same absolute deadline, including broker proof, request encoding,
response reads and decoding. It does not start independent one-second waits or
retry sleeps. Standalone target-resolution calls retain their existing retry
behavior when no strict caller deadline is supplied.

Provider applications may call `configure_control_worker(policy, priority, cpus)`
before accepting Control lanes. `policy` is `OTHER`, `FIFO` or `RR`; `cpus` is a
nonempty list of CPU indices. The profile is applied and read back on the actual
handler connection thread before authentication or mapping. Failure closes the
lane and preparation fails. `control_worker_status()` returns bounded cold
diagnostics with native `tid`, numeric OS `policy`, `priority`, `cpus`, `ready`,
`error` and `phase`. Its `ready` means profile readiness only; successful
`prepare_control` is the provider/mapping readiness boundary. Scheduling permission
and memory-lock limits must be provisioned by the deployment; the SDK does not
change host policy or grant itself privileges.

Prepared lanes fault in and lock their mappings. `R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES`
sets the prepared/locked memory admission limit (default 64 MiB). It applies to each
client and to the combined clients in a process, using page-rounded mapping sizes; the
process limit is fixed at the first preparation, so set the environment before creating
clients. A preparation beyond the budget fails with `ControlShmCapacityError` before
locking memory. The budget does not grant OS permission to lock memory: RT applications
also need an adequate `RLIMIT_MEMLOCK` (systemd `LimitMEMLOCK`).

Typed Procedure/Operation callers can use `prepare_procedure(target, procedure,
request_capacity, response_capacity, ...)` or `prepare_operation(target, operation,
request_capacity, response_capacity, ...)`, both with `typed=True` by default.
Set `typed=False` for existing untyped call/start envelopes. Only existing
reserved runtime transport topics receive this Procedure/Operation role treatment;
arbitrary Control endpoint preparation remains Gateway/Component-only.

For production Control v5, `Subsystem.transact_control_result(...)` takes the same
arguments as `transact_control(...)` and returns copied response data after sending
the consumption acknowledgement. A persistent worker receives the release
confirmation. Call `wait_control_reclaimed(target, topic, timeout_ms=1000)` when
reuse confirmation matters. The next same-lane transaction waits for reclamation
before writing; independent lanes do not share that wait. Reclamation errors stay
visible until the client is recreated or the route is explicitly changed. Existing
`transact_control` still waits for release. Both completions use owned payloads;
response-ready timing measures response delivery, not full reclamation or physical
actuator completion. Explicit `stop()` cancels and joins workers; existing atexit
cleanup also remains usable.


Named private Raw Control endpoints must be registered during `on_initialize`, before endpoint validation, and activated with `start_control_server()` during `on_start`. `register_raw_control_endpoint(topic, handler)` receives the full request dictionary, including its original `ts`, sender and opaque payload. A second optional argument receives the verified request context. The endpoint must be declared on an internal interface with raw byte request/response types or an untyped private contract. A typed API endpoint, SDK Protocol topic, or endpoint already registered on another plane is rejected. Re-registering the same Raw name deliberately replaces its callback; Raw and typed registrations cannot replace each other. Registration does not introduce a typed codec or disable manager verification. A handler passed to `start_control_server(handler)` receives the requests for topics without a registered endpoint.

While a Control, Procedure or Operation callback runs, `current_control_source_authority()`
and `current_protocol_host_authority()` on the `Subsystem` return a copy of the request's
manager-verified transport authority, or `None` for a request without one (no SDK-declared
name is authenticated). The Control source authority has `peer`, `epoch`, `source`,
`source_session`, `source_registration`, `source_reference`, `target`, `target_session`,
`target_registration`, `interface`, `binding_revision` and `topic`; the Protocol host
authority has `kind`, `peer` and `epoch`. Operation handlers run with the host authority
captured when the Operation was accepted and no Control source authority. Handlers that
take a context argument also receive these values as `context.source_authority` and
`context.host_authority`.

Untyped Procedure and Operation messages name the endpoint in `procedure` (`procedure_request`)
and `operation` (`operation_start_request`, Operation query responses, and the feedback, result
and state dictionaries delivered to Operation subscribers). Unknown keys are ignored, so a request
without its name key is refused with `invalid_argument` (`missing procedure name` or
`missing operation name`).

State queries and cancellation flags do not wait for Operation Event topic allocation. The cancellation RPC retains its synchronous state-event publication step and may still wait for that allocation. Existing deadlines and error handling remain unchanged.

### Self-binding a requirement

`Subsystem.bind_requirement(requirement, target, *, deadline=None)` asks Control to bind
this subsystem's own declared requirement to `target` (`"<provider_subsystem>.<interface>"`)
and returns the installed binding row; `unbind_requirement(requirement, *, deadline=None)`
removes only that self-binding and returns whether a row was removed. Control accepts
it only when `control.self_binding.mode` is `local` and its policy checks pass. A blank
`requirement` or `target` raises `ValueError` before any request. A refusal raises
`ControlRequestError` (`from r2kernel.core import ControlRequestError`) with the backend
`reason` code and message verbatim (`"<reason>: <error>"`); an error before dispatch has an
empty `reason`. An expired `deadline` (keyword-only, absolute `time.monotonic()` value;
when omitted, each socket operation times out after 1 s) raises the built-in
`TimeoutError` on every supported Python version.
Each call sends one request. The SDK never re-requests a self-binding after
re-registration; call `bind_requirement` again, for example in `on_start`.

### Brokered sockets inherited from the package leader

A leader that received brokered sockets can pass them to its child processes
(helpers in `r2kernel.core.resource`):
`env[INHERITED_FDS_ENV] = encode_inherited_fds({"frontend.api": fd})` with
`subprocess.Popen(..., env=env, pass_fds=(fd,))`. The child obtains the socket with
`inherited_socket(kind, expect)` (also `ResourceBrokerClient.inherited_fd`), where
`expect` gives `family` (`inet`, `inet6`, `can`), `type` (`stream`, `dgram`, `raw`),
`listening` and optionally `port`. It checks the environment value, that the fd is a
socket, its family, type, listening state and bound port, in that order. A mismatch raises
`InheritedFdError` naming the failed step and leaves the fd unchanged; there is no broker
fallback. On success the fd gets `FD_CLOEXEC` and is returned to the caller.

### Brokered sandbox and start diagnostics

With `resources.sandbox.network: brokered` the package's whole process tree cannot
create AF_INET/AF_INET6 sockets (`EPERM`); local Control and Event use Unix sockets and
are unaffected. When a lifecycle hook fails with an `EPERM` raised by creating a socket
(`socket.socket`) while `R2KERNEL_SANDBOX_NETWORK=brokered` (or
`R2KERNEL_SANDBOX_CAN=brokered`) is set and the process is actually denied, the SDK logs
which socket families are denied and the alternatives: a brokered socket or the leader
pass_fds pattern. An `EPERM` from any other call is not explained this way. A hook that
returns `False` is logged as `"<Phase> failed: <phase> hook returned False (no exception)"`,
followed by `"; last warning or error logged during <phase>: ..."` when that hook's own
thread logged one while it ran. A refused transition (pause, resume, stop, fault, degrade,
reset) is logged as `"Lifecycle transition <name> was not applied"` with the same suffix.

### Unprepared message codecs

Before `initialize()`, or without a managed Subsystem, typed values use the generic
codec. Each call does one native conversion and resolves the message against the spec
roots again, so spec edits show up on the next call. The runtime logs a one-time
warning when it uses this path. For repeated use, initialize the Subsystem or use
`PreparedSchemaContext`/`PreparedCodec`.

### Timed request late policy

`late_policy` is exactly `drop`, `reject` or `execute_immediately` (omitted or `None`:
`execute_immediately`; wire codes 0, 1 and 2). Any other value, including an empty string, a
different case or surrounding spaces, raises `ValueError` before the request is sent. A received
timed request with another late-policy code fails to decode and is refused before the endpoint
handler runs; it is never treated as `execute_immediately`. The handler reads the validated value as
`context.late_policy`; applying it to a late request is the handler's decision.

### Native Event client

A native Event subscription that cannot be established (refused, no notification lock, or a
native error) is logged at error level and the client stays in its native mode. If the native
Event client cannot be created at registration, the SDK logs `Native Event client creation
failed (...)` at error level and the client publishes and receives Events in its Python mode from
then on. After `stop_event_transport()` (or the client's
`stop()`), starting the Event transport again raises `RuntimeError`; create a new subsystem
client to receive Events again.

### Federation Event transport wrapper

`r2kernel.core.EventTransport.apply_state(forward_topics, peers)` takes peers as
`{peer_id, host, port, should_connect, subscriptions}`. `should_connect: false` means
accept-only. `remote_subscriptions` is refused with `ValueError`: a peer's subscriptions
reach the forward set only as that peer's own requests on the native session. Keep calling
`recv_event` for every connected peer even when only sending, because that call processes
those requests. `recv_event(peer, timeout_ms=0)` does not wait on the network, but it takes the
transport lock, so it can wait behind a concurrent `flush_n` (at most about twice
`io_timeout_ms`) or a concurrent blocking `recv_event` (up to that call's wait). In the report, `subscriptions_synced` lists peers that acknowledged the
latest subscription request on the current session; `subscription_ack_pending` lists the
others. When the report does not fit, the apply is
repeated once with exactly the size Core reported; the returned report describes the repeated
apply and also lists, ahead of its own, the connected/disconnected transitions of the apply
whose report did not fit. When a peer id, topic or publisher does not fit, Core keeps the
accepted peer or the event and `accept_peer` / `recv_event` retry once with exactly the
reported size. A payload larger than `payload_cap` raises an error
that names the required size; the event stays queued for a `recv_event` call on the same
peer with a larger `payload_cap`.

## Environment variables

The SDK reads these variables; launchers set the deployment-specific ones.

| Variable | Default | Meaning |
| --- | --- | --- |
| `R2KERNEL_PREFIX` | `/opt/r2kernel` | Installed runtime prefix used to find the native core library. |
| `R2KERNEL_CONTROL_SOCKET` | `/run/r2kernel/control.sock` | Control backend Unix socket. |
| `R2KERNEL_CONTROL_NOTIFY_DIR` | directory of the Control socket | Directory for the client's notification endpoint. |
| `R2KERNEL_RUNTIME_IPC_GROUP` | `r2kernel` for the handler socket (applied when the group exists) | Group that owns the SDK's local IPC sockets. |
| `R2KERNEL_RUNTIME_IPC_SOCKET_MODE` | `0660` | Octal mode (one to four octal digits) of the local Control handler socket. |
| `R2KERNEL_SUBSYSTEM_SESSION_ID` | generated | Session identity sent at registration. |
| `R2KERNEL_SUBSYSTEM_MANIFEST` | `<name>.subsystem.yaml` or `subsystem.yaml` two directories above the user data path, then in the working directory | Subsystem manifest path when `manifest_path` is not given. |
| `R2KERNEL_DEPLOYMENT_MANIFEST` | `r2kernel.deployment.yaml` next to the subsystem manifest, in its parent directory, then in the working directory | Deployment manifest path when `deployment_manifest_path` is not given. |
| `R2KERNEL_BINDINGS` | none | JSON object of launch bindings (`requirement` to `provider.interface`). |
| `R2KERNEL_STARTUP_RETRY_COUNT` | `20` | Registration and Control startup attempts. |
| `R2KERNEL_STARTUP_RETRY_DELAY_MS` | `100` | Delay between startup attempts. |
| `R2KERNEL_STARTUP_REQUEST_TIMEOUT_MS` | `2000` | Timeout of one startup Control request. |
| `R2KERNEL_CONTROL_REGISTRATION_CHECK_SEC` | `5.0` (minimum 1) | Interval of the Control handler registration check that re-registers a missing route. |
| `R2KERNEL_CLOCK_STATUS_REFRESH_SEC` | `5.0` (minimum 1) | Interval of the Control clock status refresh. |
| `R2KERNEL_EVENT_QUEUE_DEPTH` | by slot size: 64, 16 or 4 | Event queue depth when a topic does not set one. |
| `R2KERNEL_EVENT_STARTUP_BARRIER_MS` | value from Control registration, else 0 | Wait before the first Event publication of a topic; overrides Control's value. |
| `R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES` | `67108864` (64 MiB) | Prepared/locked Control memory per client and per process. |
| `R2KERNEL_CONTROL_SHM_METRICS` | off | `1` prints a Control SHM summary to stderr when the client stops. |
| `R2KERNEL_CONTROL_COST_METRICS` | off | `1` records bounded Control stage samples and prints them when the client stops. |
| `R2KERNEL_CONTROL_COST_METRICS_MAX_SAMPLES` | `256` (1 to 4096) | Sample ring size for cost metrics. |
| `R2KERNEL_RESOURCE_SERVICE_SOCKET` | `/run/r2kernel/resourced.sock` | Resource broker socket. |
| `R2KERNEL_RESOURCE_SERVICE_MODE` | `required` | `required`, `auto` or `disabled` (development only) manager peer proof. |
| `R2KERNEL_RESOURCE_LEASE_ID` | none | Resource lease of a managed package, sent with registration and broker requests. |
| `R2KERNEL_INHERITED_FDS` | none | Brokered descriptors passed from the package leader (`inherited_socket`). |
| `R2KERNEL_SANDBOX_NETWORK`, `R2KERNEL_SANDBOX_CAN` | none | `brokered` enables the sandbox `EPERM` diagnostics. |
| `R2KERNEL_SPEC_ROOT` | packaged specs | Shared message/spec dictionary root. |
| `R2KERNEL_SPEC_PATH` | none | Additional spec roots (path list). |
| `R2KERNEL_CONTRACT_SPEC_ROOTS` | none | Caller-private contract roots excluded when inspecting another package. |
| `R2KERNEL_CONTRACT_POLICY` | `strict` | Runtime contract metadata check: `strict`, `warn` or `off`. |
| `R2KERNEL_LOG_DIR` | `<prefix>/log`, else `~/.r2kernel/log` | Library log directory. |
| `R2KERNEL_FORCE_PYTHON` | off | `1` selects the pure-Python Event, topic-buffer and lock implementations instead of the native core (tests and diagnostics). |
