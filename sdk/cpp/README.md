# sdk/cpp

C++ r2kernel-client SDK for R2Kernel.

This SDK combines:
- `r2kernel/core/*`: control-facing runtime client
- `r2kernel/wrapper/*`: subsystem base components, declaration metadata, and surface registration helpers

The transport implementation is provided by the installed R2Kernel
communication library `libr2kernel_comm` (CMake target `r2kernel::comm`), but
C++ users consume a single SDK project.

## Layout
- `include/r2kernel/core`: public runtime client API
- `include/r2kernel/wrapper`: `Subsystem`, logger, declaration and contract-validation API
- `src/core`: control/event client implementation
- `src/wrapper`: wrapper implementation

## Declaration Model
`Subsystem` is the single integration point. Use `subsystem.yaml` as the
canonical declaration surface for provided and required interfaces.

```cpp
r2kernel::wrapper::SubsystemOptions options;
options.name = "component.mobile";
options.manifest_path = "subsystem.yaml";

MyComponentSubsystem subsystem(std::move(options));
```

When a package is launched by R2Kernel, the launcher sets
`R2KERNEL_SUBSYSTEM_MANIFEST` and `R2KERNEL_BINDINGS` automatically. A C++ subsystem
can also pass `SubsystemOptions::manifest_path` and
`SubsystemOptions::launch_bindings` directly for custom startup code.

Manifest-provided public `api.*` and `component.*` contracts are added to the
runtime declaration set and are reported in heartbeat status as
`manifest`, `interfaces`, and `bindings`. The Web Console Runtime graph uses
that status to draw C++ provider cards and binding edges in the same way it does
for Python subsystems. Capability contracts are not supported: a manifest
interface that names a `capability.*` contract, or uses a `capability:` key, is
rejected when the manifest loads.

The manifest is the only source of declared API and component contracts, as
in the Python SDK (there is no code-level `declare_api_contract` or
`declare_component`). Endpoints are registered with:

- `register_event_publish(...)`
- `register_event_subscribe(...)`
- `register_control_endpoint(...)`
- `register_procedure(...)`
- `register_operation(...)`

Initialization validates the declared component/API-contract set against the
registered contract metadata. It then applies the Python SDK's runtime contract
type rule (`validate_runtime_contract_types()`): every registered Control
endpoint, Procedure and Operation, internal ones included, needs scalar or
resolvable message type metadata; `R2KERNEL_CONTRACT_POLICY=strict` (default)
fails initialization, `warn` logs warnings and `off` skips it. Low-level binary
Control servers started with `start_control_server(handler)` count as
hosted control surfaces for contract conformance when topic-level typed
metadata is not registered.

## Lifecycle and Event registration

Initialization, start and requested lifecycle transitions are serialized.
Their hooks may wait for a worker that registers an Event: `on_initialize`
stages those registrations before codec validation and resource acquisition.
Initialization stops staging before it reports its result, so Event
registrations that start after `initialize()` returns observe the completed
state.

While a thread is registering an Event on a Subsystem, a direct synchronous
lifecycle request by that same thread on that Subsystem is refused promptly.
This includes reentry from a custom `CoreClient` during acknowledgement or
resource acquisition, including nested registrations. Boolean transitions return
`false`; direct `start()`, `reset()` and `terminate()` leave state unchanged,
and `initialize()` throws. The SDK does not retry lifecycle effects automatically.
Requests from other threads are not refused solely because registration is
active; they follow the normal lifecycle serialization policy.

Overlapping `terminate()` calls on one Subsystem share one attempt: other
threads wait for its hook and teardown to finish, then return without repeating
it. A later non-overlapping call starts a new attempt. Waiters do not receive
the owner's failure; inspect the owner result, lifecycle state and logs. A recursive
`terminate()` from a lifecycle or termination hook on the same thread is
refused. A termination attempt waits for another thread's active lifecycle
transition; initialization does not overlap active teardown. The lifecycle
transition mutex is released before waiting for registrations or joining
transport or callback threads.

`terminate()` closes Operation admission and ends running Operations as the
Python `stop_operation_server()` does: they get 1 s to finish, then each
unfinished Operation is asked to cancel and a `cancel_requested` state event is
published for it. `terminate()` then waits for admitted Operation workers to
exit before `on_terminate()` and Event transport shutdown. A handler still
running after its Operation finished also sees `is_cancel_requested()`, but
the finished Operation keeps its state. `stop_operation_server()` follows the
same steps (1 s, cancel requests, then up to 0.25 s more) without waiting for
workers. A handler that does not return can therefore block termination
indefinitely; it should observe `OperationContext::is_cancel_requested()`.
Direct `terminate()` from a worker or synchronous Operation admission callback
on the same Subsystem is refused and logged. Do not make a handler wait for a
thread terminating that Subsystem.

Call `terminate()` before destroying the derived Subsystem or any objects its
handlers capture. The base destructor runs too late to protect derived members.

`terminate()` is not async-signal-safe. A native signal handler must notify a
normal thread instead of calling it directly.


Do not synchronously wait in a registration callback or lifecycle hook for a
worker that requests a lifecycle transition on the same Subsystem: that request
can wait on work owned by the waiting thread and deadlock. Such cross-thread
reentry is unsupported and cannot be detected by the same-thread guard. This
does not preclude a lifecycle hook from waiting for a worker to register an Event.


`reset()` and `request_lifecycle_transition("reset")` share the Python SDK's
admission and hook policy. Only Faulted, Error, Degraded, Paused and Stopped
admit reset; the hook runs through Resetting, then Ready on success or Faulted
on `false` or an exception. Reset retains transport and heartbeat ownership,
and does not bypass a failed initialization's start guard. Created, Ready and
Operating reject reset
without running its hook.

## Control completion

`Subsystem::transact_control(...)` and the core `R2KernelClient::transact_control(...)`
use synchronous completion: they return after the runtime confirms
`RESPONSE_RELEASED`.

`transact_control_result(...)` accepts the same three payload/options overloads
and returns an owned response after validating its lease/ticket and sending
`RESPONSE_CONSUMED`. The core equivalent is `R2KernelClient::transact_control_result(...)`.
A persistent receiver per target/topic lane validates `RESPONSE_RELEASED` using
the original transaction deadline. `wait_control_reclaimed(target, topic,
timeout_ms)` waits for that lane and throws on a release failure or wait timeout.
The wrapper defaults its wait timeout to 1000 ms; the core requires it explicitly.

```cpp
auto response = subsystem.transact_control_result(
    "gateway.servo", "servo_control", request, 1.0F, 1000.0F, 1000);
if (response) use_response(response->payload);
subsystem.wait_control_reclaimed("gateway.servo", "servo_control", 1000);
```

Each lane permits one outstanding release and no queued requests. The next
transaction waits for reclamation within its own budget before writing request
memory; concurrent foreground calls on the same lane fail explicitly. Other
lanes progress independently. A release failure closes the lane and remains
visible to explicit waits and subsequent transactions, without invalidating an
already returned response or replaying its request. An explicit reconnect retires
idle, successfully reclaimed lanes and permits fresh peer verification; a busy
lane or a sticky release failure rejects reconnect/preparation as well. Recreate
the client to retire a failed receiver; reconnect is not an error-clearing shim.
Stopping the client shuts down pending sockets and joins their receivers.

The native client uses only the Control SHM transport (`binary_shm_v5`, frame
version 5); other frame versions are rejected before payload mapping or handler
dispatch. Requests carry `remaining_ns`. Alternate `CoreClient` implementations
without explicit support reject the response-ready APIs; they do not silently
use synchronous completion.

The public split `send_control(..., deadline_ms)` / `recv_control(target, type, timeout_ms)`
API uses
one caller-origin transport deadline, including its optional handshake. A finite
positive `deadline_ms` is the complete send budget (1500ms is not silently capped
at 1000ms); otherwise the budget is 1000ms. Shorter deadlines remain shorter.
The native send completes the exchange synchronously and stores an owned response;
receive takes that response once, without additional broker I/O or a new timeout
budget. Its `timeout_ms` cannot extend the original exchange. Timeout or ambiguous
delivery is never retried automatically. `type` is the response payload type, as
in Python: empty or `bytes` keeps the payload bytes, a message reference
(`<contract>/<Message>`) replaces them with the decoded JSON text, and a payload
that does not decode throws (Python returns no payload). The core
`R2KernelClient::recv_control(target, timeout_ms)` stays byte-level.

`bind_control(port = 0, host = "0.0.0.0")` returns the bound port (the requested
port until Control assigns one) and throws when the role cannot serve native
Control or no core client is bound. `resolve_control_target(target)` returns
`std::optional<ControlRoute>`, the fields of Control's reply as the Python SDK
returns them (`host`, `port`, `route_kind`, advertised `controls`/`procedures`/
`operations`, the contract and codec maps as JSON text, and the whole reply in
`json`), or `std::nullopt` when Control has no usable route.

`prepare_procedure(target, procedure, request_capacity, response_capacity,
timeout_ms = 1000, result_ready = false, typed = true)` and the matching
`prepare_operation(...)` check that the target advertises the name (a
Procedure must not be one of its Control endpoints) and prepare the typed
(`procedure.call_typed/<name>`, `operation.start_typed/<name>`) or untyped
(`procedure.call`, `operation.start`) lane within one `timeout_ms` budget.

Untyped requests name the endpoint in `procedure` (`procedure_request`) and
`operation` (`operation_start_request`); Operation query responses and
feedback, result and state messages carry it in `operation` as well, the field
that fills `OperationQueryResponse::operation`. Unknown keys are ignored, so a
request without its name key is refused with `invalid_argument` (`missing
procedure name` or `missing operation name`), as in Python.

Operation feedback, result and state events use the Python SDK's binary event
format. When the Operation declares a message type for the event
(`OperationOptions::feedback_type`, `result_type`, `state_type`), the payload
JSON is encoded with that type through the prepared codec, as in Python.
Otherwise the JSON text is sent as given, which includes the default state type
`string`. A terminal state (`succeed`, `fail`, `canceled`) has no payload, and
its result follows as a result event only when it is not `null`. A cancel
request, or `stop_operation_server()`, publishes `cancel_requested` unless the
Operation has finished. As in Python, `queued` and `running` are not published:
they are the states of the start reply and of a query. An Operation canceled
before its handler starts ends `canceled` without running the handler.
Subscribers receive an `OperationEventMessage`:

- `payload_json` holds the payload as JSON text: a typed payload decoded by its
  declared `payload_type`, raw bytes as a JSON string of lowercase hex, or
  `"null"` when there is no payload.
- `payload_encoding` (`none`, `json`, `typed`, `raw`) tells a raw-bytes payload
  from a JSON string; `payload_bytes` keeps the raw bytes.
- The subscriber must register the declared message types it receives.

### Typed payloads without JSON

The JSON overloads convert each typed payload between JSON text and the codec
bytes, which costs about 0.8-2.3 microseconds per event (table below). A typed
value can skip JSON:

- `OperationContext::publish_feedback`, `publish_state`, `publish_result`,
  `succeed`, `fail` and `canceled` also take a `CodecByteView`: the payload
  already encoded with the declared message type, for example
  `PreparedCodec::encode_nodes` output or bytes received earlier. The bytes are
  published as given, so the event is the one the JSON overload writes for the
  same value. They are not validated again; a declared type that is not a
  message throws `std::invalid_argument`. An Operation query decodes a stored
  encoded result or state payload to JSON only when it is asked.
- `subscribe_operation_feedback`, `subscribe_operation_result` and
  `subscribe_operation_state` take `OperationSubscribeOptions`. With
  `payload_json = false`, typed and raw payloads arrive only in `payload_bytes`
  (a typed one as encoded with `payload_type`) and `payload_json` is empty for
  them; read them with `PreparedCodec::decode_nodes` into a reused
  `CodecWorkspace`. JSON payloads still arrive as JSON text.
- A typed Procedure registered with `ProcedureOptions::native_payload` receives
  `ProcedureRequest::params_payload` and returns
  `ProcedureResponse::result_payload`, both encoded with the declared types.
  `call_procedure(target, procedure, params_payload, options)` sends encoded
  params and returns the encoded result in `result_payload` (`result_json`
  stays `"null"`); `options` must name both types. Native handlers also serve
  JSON callers, and encoded calls also reach JSON handlers: the SDK converts
  only on the side that uses JSON.

```cpp
const auto pose = subsystem.prepared_codec("msg.core.spatial.v1/Pose");
CodecWorkspace workspace;  // one per thread, reused
context.publish_feedback(pose.encode_nodes(nodes.data(), nodes.size(), workspace));
```

Event payloads (`publish_event`, Event callbacks) and Control endpoints with
`ControlEndpointOptions::native_payload` or a `ByteBuffer` request were already
bytes. Typed Operation start params stay JSON: they are converted once per
Operation, inside the JSON `operation.start` protocol request.

In-process cost (Release, `r2kernel_cpp_operation_event_payload_benchmark` and
`r2kernel_cpp_procedure_payload_benchmark`, minimum over batches, one host):

| Path | Pose feedback event | Float32Vector(32) state event |
| --- | --- | --- |
| JSON payload, no declared type | encode 33 ns, receive 68 ns | encode 37 ns, receive 83 ns |
| Typed, JSON API | encode 802 ns, receive 878 ns | encode 1230 ns, receive 2278 ns |
| Typed, encoded bytes | encode 42 ns, receive 69 ns | encode 45 ns, receive 77 ns |
| Typed, from/to codec nodes | encode 81 ns, receive 122 ns | encode 92 ns, receive 122 ns |

The extra cost of the node rows is the codec itself (`encode_nodes`,
`decode_nodes`). A Pose echo Procedure round trip in process takes about 2.7
microseconds on the untyped JSON lane, 5.1 microseconds typed with JSON, and
1.3 microseconds typed with encoded payloads (1.5 with codec nodes).

`start_operation(target, operation, params_json, timeout_ms)` takes an optional
timeout: unset, the provider receives `null` and the caller waits 1000 ms, as
in Python. `Subsystem::publish_event(topic, payload, timestamp_ns)` publishes with
the registered publication's priority; the core client keeps an explicit
`priority` (negative selects the registered one).

## Prepared local Control memory

`Subsystem::prepare_control(target, topic, request_capacity, response_capacity,
timeout_ms = 1000, result_ready = false)` and the equivalent `R2KernelClient`
method return `true` only after the live provider has authenticated its manager,
mapped the request region, allocated its declared response region, and the caller
has installed that response mapping. Failures throw with the manager's original
cause; failed responses must carry no lease, descriptor, or trailing metadata.
Preparation does not invoke
the application handler, create a response ticket, or send consumption/release
acknowledgements. `handshake_control` is a route check, not
provider readiness.

Resolve/connect the target, then prepare on the same caller thread with its
intended scheduling policy before entering the periodic loop:

```cpp
subsystem.prepare_control("gateway.servo", "servo_control", 64, 64, 1000, true);
// The same target/topic lane supports either completion API.
auto response = subsystem.transact_control_result(
    "gateway.servo", "servo_control", request, 1.0F, 1.0F, 1000);
subsystem.wait_control_reclaimed("gateway.servo", "servo_control", 1000);
```

Capacities count complete transported payload bytes, including any timing envelope,
not page-rounded SHM region sizes. Prepared lanes reject excess request/response
bytes or changed mappings; they never grow their declared capacities. Changing a
healthy prepared lane requires an explicit idle target rebind/new lane. Select
`result_ready=true` when
preparing if the loop will use response-ready completion: its persistent reclaimer
is created before readiness, not on first TX. Prepared first TX reuses the same
connection, mappings, metadata and worker; it performs no new proof/map/grow/thread
startup. Owned callback requests and returned response vectors retain their
existing allocation and reclamation behavior.

Production v5 lanes prefault their metadata/payload mappings. FIFO/RR caller and provider threads require successful
memory locking during preparation. Keep a prepared lane's worker/caller scheduling
profile fixed; establish a new lane after a policy change. Ordinary unprepared
transactions remain supported and perform their required cold setup within the
transaction's absolute deadline.

`R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES` sets the prepared/locked memory
admission limit (default 64MiB). It applies to each client and to the combined
clients in a process, using page-rounded mapping sizes. The process limit is
fixed at the first negotiation or admission; set the environment before
initializing clients. This budget does not grant OS permission to lock memory.
RT applications need an adequate `RLIMIT_MEMLOCK`/systemd `LimitMEMLOCK` as
well as their scheduling grant.

Preparation uses a separate cold absolute deadline, including request allocation,
connect, root-broker proof, provider preparation and caller response mapping.
Ordinary transactions charge route/codec conversion and transport against their
invocation budget; the caller forwards nanosecond remaining time without replacing
the request's period/deadline metadata. Automatic Procedure/Operation decoding also
checks the original budget and reports an overrun rather than returning late success.
Application-explicit response decoding is a separate invocation. None of these
contracts guarantees a physical packet frequency or makes application code
allocation-free.

Before a provider worker starts, `configure_control_worker(policy, priority, cpus)`
on `Subsystem` or `R2KernelClient` declares a Linux profile by name (`OTHER`,
`FIFO` or `RR`, case-insensitive, optional `SCHED_` prefix), a priority within that
policy's range and a nonempty CPU list. Each actual accepted dataworker applies and reads back that
profile once, before peer proof or payload mapping. Setup/readback failure closes
the lane and fails preparation. `control_worker_status() const` returns cold
`ControlWorkerStatus` records with the Python fields (`tid`, `policy` as the OS
policy value, `priority`, `cpus`, `ready`, `error` (empty when ready) and `phase`:
`profile_applied`, `profile_failed`, or `profile_observed` for an unconfigured
worker) for the actual dataworker threads.
This status means OS setup completed, not provider readiness; only successful
`prepare_control` confirms mappings/authentication. No profile syscall is added to
prepared packet processing, and worker configuration cannot change once a worker
has started.

Established lanes cache their immutable source/target/topic and endpoint identity.
Reconnect invalidates the selected target's idle lanes. A codec/session generation
change (including wrapper reinitialization), global endpoint change, or failed
preparation preserves a prepared-lane invalidation tombstone: ordinary same-size,
oversized and result-ready calls all reject before allocating a replacement lane,
mapping memory, connecting or starting a reclaimer. Recover an idle, successfully
reclaimed invalidated lane with explicit `prepare_control`, or explicitly rebind
then prepare it; a normal transaction cannot silently fall back to cold setup.
Exception: when a published exchange on a prepared lane misses its deadline or
Control closes or resets the connection (end of stream before a frame, or a
send that fails with EPIPE or a receive that fails with ECONNRESET), the
failing call (never a later ordinary call)
waits for Control to close the failed connection and re-prepares the lane with
its original capacities, completion mode and preparation timeout, bounded by
that timeout; the failing call still throws. A deadline miss that expires before
anything reaches Control fails only that exchange and keeps the lane. A failed
re-preparation leaves the tombstone. Route, codec, binding, protocol and
capacity failures, and a transaction failure that Control reports in its reply
(it then closes the connection), are not re-prepared automatically. The late exchange is still
a failed exchange. The Control SHM summary counts automatic re-preparations that
succeeded as `prepared_lane_recoveries` and those that failed as
`prepared_lane_recovery_failures`.
Same-lane exclusion, reclamation barriers and sticky release failures still apply.
Each request still clones its callable
to preserve the existing behavior of mutable by-value captures; only normalized
registration metadata and prepared codec/session state are reused.

Set `R2KERNEL_CONTROL_SHM_TIMING_METRICS=1` before initializing a client to enable
diagnostic response/reclamation durations in its existing Control SHM summary.
Response-ready duration starts at the public `R2KernelClient` transaction call
and ends when it owns the validated response; wrapper encoding and JSON
decoding are outside this duration. Release
duration ends at validated `RESPONSE_RELEASED`, including receiver scheduling
when using result-ready completion. The reclamation-barrier duration measures
the next call waiting for an earlier release. Additional duration marks are disabled by
default. Core's separate first-response and reclaim-stage counters describe
its own frame-read/write boundaries, not SDK or physical-device completion.
Independent aggregate maxima cannot be subtracted to attribute one transaction.

## Requirement self-binding

`Subsystem::bind_requirement(requirement, target, deadline)` binds the
subsystem's own declared requirement to a local provider (`"<provider>.<interface>"`)
when the control backend runs with `control.self_binding.mode: local`. It
returns the bound binding row as JSON. A refusal throws `ControlRequestError`
whose `reason()` is the backend reason code (for example
`self_binding_operator_owned` or `self_binding_target_held`) and whose message
is `"<reason>: <error>"`; an authentication or malformed-request error has an
empty reason (an error reply without text names the command). A timeout, a
transport failure or a success reply without `res: ok` throws
`std::runtime_error`. Nothing is retried. A blank requirement or target throws
`std::invalid_argument` before any request. `deadline` is an optional absolute
`std::chrono::steady_clock::time_point`; without it each control-socket operation
is bounded by 1 s, as in the Python SDK, and an expired deadline fails before
any request.
`unbind_requirement(requirement, deadline)` removes only this subsystem's
self-binding and returns whether one existed. A self-binding ends when the
registration ends or re-registers, and the SDK does not request it again: call
`bind_requirement` from `on_start`. Both calls require the native core client:
before `initialize()` they throw `std::logic_error` naming the missing
initialization, and with another `CoreClient` implementation a
`std::logic_error` naming the native client.

## Manifests, bindings and bound calls

`subsystem_manifest()` and `deployment_manifest()` return the read-only models
of `r2kernel/wrapper/manifest.hpp` (`SubsystemManifest` with its
`SpatialDeclaration`, `InterfaceDeclaration`, `BindingReference`,
`BoundInterface`, `DeploymentManifest::resolve`, `DeploymentInstance`,
`RobotPlatformManifest`, `ApplicationManifest`, `ManifestError`). The same
header has the loaders of the Python SDK: `load_subsystem_manifest`,
`load_robot_platform_manifest`, `load_application_manifest`,
`load_deployment_manifest` and `validate_required_bindings_for_subsystem`.

Construction finds the subsystem manifest from `SubsystemOptions::manifest_path`
(else `R2KERNEL_SUBSYSTEM_MANIFEST`), then `<name>.subsystem.yaml` and
`subsystem.yaml` two directories above the user data path and in the working
directory, and the deployment manifest from
`SubsystemOptions::deployment_manifest_path` (else
`R2KERNEL_DEPLOYMENT_MANIFEST`), then `r2kernel.deployment.yaml` next to the
subsystem manifest, in its parent directory and in the working directory. The
subsystem manifest is required. Both manifests are validated exactly as in the
Python SDK, with the same messages: roles and contracts, spatial declarations,
robot slots, application profiles, Control binding groups and the spatial
topology. Construction fails when a binding does not resolve; without a
deployment manifest, a required interface that has no launch binding logs a
warning and the subsystem starts in `WaitingBinding`. Python's `requires` and
`interface` fields are named `requirements` and `interface_name`, and its
free-form mappings (`kinematics`, `parts`, `frontend`, slot `metadata`) are
JSON text (`*_json`).

`launch_bindings()`, `runtime_bindings()`, `effective_bindings()`,
`resolve_binding(requirement, deadline)`, `resolve_runtime_binding(requirement,
deadline)` and `binding_target(requirement, deadline)` follow the Python SDK:
a bound runtime binding wins, an explicit runtime unbind throws, and otherwise
the launch binding, then the deployment binding, applies. With a deadline
(`std::chrono::steady_clock::time_point`) only a ready runtime binding is
accepted. `transact_bound_control`, `call_bound_procedure` and
`start_bound_operation` spend one invocation budget on that strict resolution
and the call: a binding that is not ready or a spent budget throws
`std::runtime_error` and sends nothing. `declared_runtime_endpoints(kind)`
lists the endpoints the manifest allows (sorted). `GatewaySubsystem` adds
`bound_control_sessions`, `bound_control_authority` (`BoundControlAuthority`),
`validate_control_authority`, `bound_control_owner` and
`bound_control_session`, which query Control's binding status on each call and
fail closed.

`lookup_frame_transform(source, target)` returns Control's frame lookup reply
as JSON text, and `spatial_status_payload()` the reported transforms.

## Configuration document

`config_data()`, `config_get(path, default_json)`, `config_set(value_json,
path, save)`, `config_reload()` and `config_save()` read and write
`<user_data_path>/config`, the YAML file of the Python SDK, created as `{}` at
construction when missing. Values are JSON text. A string path is dotted or
slash-delimited; a `std::vector<std::string>` names literal keys. Scalars are
typed as PyYAML reads them (YAML 1.1) and the file is written in the Python
layout, so both SDKs read each other's file back to the same values
(timestamps stay strings in C++).

## Resource broker

`r2kernel::core::ResourceBrokerClient` (`r2kernel/core/resource_broker_client.hpp`)
requests brokered descriptors from `r2kernel-resourced` with
`R2KERNEL_RESOURCE_LEASE_ID` and `R2KERNEL_RESOURCE_SERVICE_SOCKET`:
`open_udp_socket`, `open_tcp_socket`, `open_frontend_tcp_listener`,
`open_can_socket`, `open_uplink_tcp` (option structs) and
`open_device(path, options)` return `BrokeredSocketResult` / `BrokeredFdResult`
(a close-on-exec descriptor the caller owns and the broker's result as
`metadata_json`); `inherited_fd` is `inherited_socket`. Each request is sent
once within 5 s (`open_uplink_tcp`: its timeout plus 2 s). A refusal throws
`ResourceBrokerError`, a `std::runtime_error` whose `code()` is the broker's
stable error code; an invalid reply throws `std::runtime_error`. Both close
every received descriptor. `R2KernelClient` offers the same `open_*` calls.
Low-level shared memory is the C ABI (`r2kernel_shm_publish`,
`r2kernel_shm_read_snapshot`, `r2kernel_shm_reset_slot` in `r2kernel_comm.h`);
there is no C++ counterpart of Python's `TopicBufferPool`.

## Status and custom core clients

Heartbeat and lifecycle status updates carry the same keys as the Python SDK,
including `reported_lifecycle` (the subsystem's own lifecycle state, which Control
reports unless a binding state takes precedence). A `CoreClient` other than the
native `R2KernelClient` receives the source-wrapped topic Control routes on
(`__r2kernel_source_v1__:<hex source>:<topic>`) for `send_control`,
`transact_control`, `transact_control_result`, `wait_control_reclaimed` and
`prepare_control`, so it can forward requests to Control unchanged.

## Timing headers

`build_timing_request_header`, `normalize_timing_request_header`,
`build_timing_response_header`, `pack_timing_request`, `unpack_timing_request`,
`pack_timing_response` and `unpack_timing_response` match the Python SDK
helpers. Headers, execution reports, payloads and wire objects are JSON object
text with the Python dict shapes; the request header takes its keyword arguments
from `ControlTimingOptions`, and the unpack functions return a `TimingEnvelope`
(`header_json`, `payload_ref`, `payload_json`). Malformed input throws
`std::invalid_argument`.

## Timed request late policy

`ControlTimingOptions::late_policy` is exactly `drop`, `reject` or
`execute_immediately` (the default; wire codes 0, 1 and 2). Any other value, including an empty
string, a different case or surrounding spaces, throws `std::invalid_argument` before the request
is sent. A received timed request with another late-policy code fails to decode and is refused
before the endpoint handler runs; it is never treated as `execute_immediately`. The handler reads the
validated value as `request.timing.late_policy`; applying it to a late request is the handler's
decision.

## Event inline delivery

If the SDK cannot verify the Control peer of the Event inline connection during `publish()`, it
disables inline delivery for that registration and logs once `Event inline delivery disabled for
endpoint <endpoint> until the subsystem registers again: publish on topic <topic> failed:
<reason>; Events are delivered through the lock notification instead`. That `publish()` and every
later one still succeed through the lock notification and do not retry the peer verification.

## Inherited brokered sockets

A package leader that received brokered sockets can pass them to its own
child processes. The leader builds the `R2KERNEL_INHERITED_FDS` value with
`r2kernel::encode_inherited_fds({{"frontend.api", fd}})`
(`r2kernel/core/inherited_fds.hpp`), sets it in the child's environment and keeps
those descriptors open across `exec`. The child calls
`r2kernel::inherited_socket(kind, expectation)`: it checks that the descriptor
is a socket with the expected family, type, listening state and (optionally)
local port, sets `FD_CLOEXEC` and returns it. Every mismatch throws
`r2kernel::InheritedFdError` with `step()` naming the failed check; the helper
never falls back to a broker request and never closes the descriptor on failure.

`open_frontend_tcp_listener()` returns an AF_UNIX stream listener for a broker-owned
loopback TCP relay. The result metadata has `domain: unix`, `transport: unix_listener`
and `bind_host`/`bind_port` for the TCP endpoint. The SDK validates both the metadata
and descriptor. Use `InheritedSocketFamily::unix_socket`, stream and listening=true
when passing it to a child; Unix expectations cannot include a port. Call the
broker result's `close()` to shut down the relay. A child holding the inherited
listener must call `shutdown(fd, SHUT_RDWR)` before `close(fd)`; closing only one
descriptor leaves the broker's copy alive until lease cleanup.

## Build

Install native build tools first:

```bash
sudo apt install -y build-essential cmake
```

Build against the installed `libr2kernel_comm` from the R2Kernel runtime
prefix (`<prefix>` below is the R2Kernel installation prefix):

```bash
cmake -S sdk/cpp -B build/r2kernel-client-cpp \
  -DR2KERNEL_RUST_CORE_DIR=<prefix>/runtime/lib
cmake --build build/r2kernel-client-cpp
```

CMake looks for the C ABI headers (`r2kernel_comm.h` and
`r2kernel_comm_control.h`) next to that library directory, including the
installed C++ SDK under `<prefix>/sdk/cpp/include`. Without an installed C++ SDK,
or to use the headers that ship with this checkout under `runtime/core/include`,
set the header directory explicitly:

```bash
cmake -S sdk/cpp -B build/r2kernel-client-cpp \
  -DR2KERNEL_RUST_CORE_DIR=<prefix>/runtime/lib \
  -DR2KERNEL_RUST_CORE_INCLUDE_DIR="$PWD/runtime/core/include"
```

The SDK declares `r2kernel_cpp` and its consumer target `r2kernel::cpp`.
Both the configured build directory and an installation are CMake packages:

```cmake
find_package(r2kernel_cpp CONFIG REQUIRED)
target_link_libraries(my_client PRIVATE r2kernel::cpp)
```

Pass `-Dr2kernel_cpp_DIR=/absolute/path/to/build/r2kernel-client-cpp`
to reuse a source build, or point it at the installation's
`lib/cmake/r2kernel_cpp`. The target carries the SDK/Core public headers and
Core link dependency; consumers never need SDK implementation source lists.
On ELF platforms the imported shared Core dependency links by basename, including
Rust libraries without `DT_SONAME`; the installed package must not retain a Core
build-directory path in `DT_NEEDED`. Mach-O uses its shared-library install-name
semantics; Windows retains its import-library handling.
`-DCMAKE_BUILD_TYPE=Debug` or `Release` selects a single-configuration build;
multi-configuration generators also require `cmake --build ... --config Debug`
or `Release`. `-DBUILD_TESTING=OFF` builds the SDK without its own tests.
When `../testdata/parity` is present, the tests also run the manifest,
socket-mode and host-authority cases the Python SDK tests run
(`sdk_parity_cases_test`, `control_local_policy_test`).

Tutorial fixtures and StartGate tests are declared by Tutorial CMake, not
silently discovered by SDK configuration. The repository's cross-project
manifest binding check is owned by `tests/qa/cpp` and links the declared SDK and
Tutorial support targets.


## Install
```bash
cmake --install build/r2kernel-client-cpp --prefix /your/prefix
```

Installed contents include:
- C++ headers under `include/r2kernel/...`
- `libr2kernel_cpp.*` (on Linux the chain `libr2kernel_cpp.so` ->
  `libr2kernel_cpp.so.1` -> `libr2kernel_cpp.so.1.0.0`)
- the communication library `libr2kernel_comm` the SDK links to
- CMake package files under `lib/cmake/r2kernel_cpp`

### Library versioning and exported symbols

1.0.0 is the C++ SDK ABI baseline. The shared library carries `SOVERSION 1`
(`DT_SONAME` `libr2kernel_cpp.so.1`), so applications record that name; an
ABI-incompatible release changes the SOVERSION. Copy or package the whole
symlink chain, not only the unversioned development link.

The library is compiled with hidden default visibility. Only declarations
marked `R2KERNEL_CPP_API` (`include/r2kernel/export.hpp`) are exported; SDK
implementation details under `src/` are not part of the ABI.

Linux release SDK archives link yaml-cpp into `libr2kernel_cpp.so` as an internal
static dependency. Source rebuilds still require yaml-cpp development headers on
the build host.

## Environment

The C++ SDK reads the SDK-facing variables of the Python SDK where they apply:
`R2KERNEL_CONTROL_SOCKET`, `R2KERNEL_CONTROL_NOTIFY_DIR`, `R2KERNEL_SUBSYSTEM_MANIFEST`,
`R2KERNEL_BINDINGS`, `R2KERNEL_DEPLOYMENT_MANIFEST`, `R2KERNEL_SUBSYSTEM_SESSION_ID`,
`R2KERNEL_RESOURCE_LEASE_ID`,
`R2KERNEL_CONTRACT_POLICY`, `R2KERNEL_STARTUP_RETRY_COUNT`,
`R2KERNEL_STARTUP_RETRY_DELAY_MS`, `R2KERNEL_STARTUP_REQUEST_TIMEOUT_MS` (registration
request timeout, default 2000), `R2KERNEL_EVENT_STARTUP_BARRIER_MS`,
`R2KERNEL_EVENT_QUEUE_DEPTH`, `R2KERNEL_CONTROL_REGISTRATION_CHECK_SEC` and
`R2KERNEL_CLOCK_STATUS_REFRESH_SEC` (seconds, default 5, at least 1),
`R2KERNEL_RUNTIME_IPC_GROUP` (default `r2kernel` for the Control handler socket,
applied when the group exists), `R2KERNEL_RUNTIME_IPC_SOCKET_MODE` (octal mode of the
Control handler socket, default `0660`), `R2KERNEL_CONTROL_SHM_METRICS`, `R2KERNEL_RESOURCE_SERVICE_MODE`,
`R2KERNEL_RESOURCE_SERVICE_SOCKET`, `R2KERNEL_INHERITED_FDS` and the sandbox
variables. Contract spec roots (`R2KERNEL_CONTRACT_SPEC_ROOTS`, `R2KERNEL_SPEC_ROOT`,
`R2KERNEL_SPEC_PATH`) are resolved by the runtime core library; the deployment
manifest loader also reads them to scope a package's `contracts.spec_roots`, as
the Python SDK does. `R2KERNEL_CONTROL_RT_MEMORY_BUDGET_BYTES` is read by both
SDKs; `R2KERNEL_CONTROL_SHM_TIMING_METRICS` is C++-only.

## Runtime backend
- Linux uses the semaphore backend by default. This is the recommended production configuration.


Named private Raw Control endpoints must be registered during `on_initialize`, before endpoint validation, and activated with `start_control_server()` during `on_start`. `register_raw_control_endpoint(topic, handler)` receives the unchanged `ControlRequest` value, including its original timestamp, sender and payload. Existing callback authority getters retain their scope. The endpoint must be declared on an internal interface with raw byte request/response types or an untyped private contract. A typed API endpoint, SDK Protocol topic, or endpoint already registered on another plane is rejected. Re-registering the same Raw name deliberately replaces its callback; Raw and typed registrations cannot replace each other. Registration does not introduce a typed codec or disable manager verification. The aggregate handler passed to `start_control_server(handler)` receives every request that neither a registered endpoint nor an SDK Protocol topic handles.

## Typed Procedure failures

Untimed typed Procedure success remains the declared response bytes with
transport status zero. If the handler returns `ok = false`, reports a nonzero
`status_code`, or throws, the provider returns a nonzero transport status and
diagnostic bytes instead of encoding a success-shaped response. The public typed
caller receives `ok = false`, `error_code = "procedure_transport_failed"`, and the
provider diagnostic in `error_message`; failure payloads are not decoded using
the success schema. Timed replies retain their existing response metadata and
application status contract. No new envelope or public API/ABI is introduced.

## App uplink (outbound connections)

A managed `role: app` package opens an outbound TCP connection with
`ResourceBrokerClient::open_uplink_tcp(const UplinkTcpOptions&)`, also on
`R2KernelClient`. The root broker resolves `host` (a DNS name or an IP literal
without brackets), applies the host's uplink policy and connects within
`timeout` (default 10 s, 1 ms to 120 s). The broker shortens a longer timeout
to the host's `max_connect_timeout_ms`, which defaults to 30 s. The SDK waits
`timeout` plus 2 s for the reply and sends the request once. The result's
`socket` is a connected, close-on-exec AF_UNIX stream that the broker relays to
the TCP connection. `metadata_json` has `kind` (`uplink_tcp`), `connection_id`,
`lease_id`, the normalized `host` (a name in lowercase without a trailing dot,
or the IP literal in canonical text: IPv4 dotted decimal, IPv6 compressed),
`port`, `remote_address` and `interface`. The broker closes the stream (the app
reads EOF) when the lease ends, after a policy change, or when a gateway claims
the interface. The checks match the Python SDK's `open_uplink_tcp`: a reply
whose kind, lease, connection ID, host, port or descriptor does not match
throws `std::runtime_error` after closing the descriptor, and invalid options
throw `std::invalid_argument` before any request. TLS is left to the caller's
library.

Whether apps may connect out is a host setting, not a package one. The uplink
mode is `auto` (the default: any interface that no gateway uses), `explicit`
(listed interfaces only) or `off`. It is chosen at installation
(`--app-uplink`) and changed with `r2kernel-resourced --set-app-uplink`, which
applies after a resourced restart. A package declares nothing. Device
networks, loopback, the host's own addresses, link-local, multicast and
broadcast destinations are always blocked, and the host may add a destination
allowlist.

A refusal throws `r2kernel::core::ResourceBrokerError`; `what()` is the
broker's text and `code()` is stable:

| `code()` | Meaning |
| --- | --- |
| `invalid_request` | bad host, port or timeout |
| `uplink_role_refused` | the package role is not `app` |
| `app_uplink_off` | the host's uplink is `off` or not configured |
| `uplink_unavailable` | the broker cannot determine the device networks, so it refuses |
| `destination_not_allowed` | the host has an allowlist and nothing matches |
| `destination_blocked` | every address is blocked or has no eligible route; the message gives the reason per address |
| `resolve_failed` | the name did not resolve |
| `connect_timeout` | resolving or connecting used up the timeout |
| `connect_failed` | every connect attempt failed; the message has the last error |
| `uplink_limit` | a connection limit of the host or the lease is reached |

```cpp
#include <sys/socket.h>

#include <iostream>
#include <string>

#include "r2kernel/core/resource_broker_client.hpp"

r2kernel::core::UplinkTcpOptions options;
options.host = "robot-2.example.net";
options.port = 8001;
try {
    auto uplink = r2kernel::core::ResourceBrokerClient().open_uplink_tcp(options);
    const std::string request =
        "GET /api/status HTTP/1.1\r\nHost: robot-2.example.net:8001\r\nConnection: close\r\n\r\n";
    ::send(uplink.socket, request.data(), request.size(), MSG_NOSIGNAL);
    char reply[4096];
    const ssize_t received = ::recv(uplink.socket, reply, sizeof(reply), 0);
    if (received > 0) std::cout.write(reply, received);
    uplink.close();
} catch (const r2kernel::core::ResourceBrokerError& refused) {
    // For example "app_uplink_off" or "destination_blocked".
    std::cerr << "uplink refused: " << refused.code() << ": " << refused.what() << '\n';
}
```

The package's sandbox (`resources.sandbox.network: brokered`) refuses every
socket except AF_UNIX, so a library that opens its own TCP socket fails with
`EPERM`. Use libraries that accept a connected descriptor, for example
OpenSSL with `SSL_set_fd(ssl, uplink.socket)`, the expected server name for SNI
and certificate verification. The uplink carries bytes only: API
authentication and the remote service's authorization remain the
application's. The host setting, the blocked destinations and the
fleet-control pattern are described in
[App Network Access](../../../docs/docs/operate/app-outbound-connections.md).

## License

The SDK source is licensed under Apache-2.0. The `libr2kernel_comm` binary
bundled with the SDK packages is distributed under the R2Kernel Runtime License
until its source is published.
