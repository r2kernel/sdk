# Raw Control runtime fixture

`r2kernel_cpp_raw_control_runtime_fixture` is a manual live fixture, not a CTest.
It complements the offline CTests (`r2kernel_cpp_raw_control_metadata_test`,
`r2kernel_cpp_subsystem_registration_test`, `r2kernel_cpp_event_registration_rollback_test`
and `r2kernel_cpp_subsystem_lifecycle_test`), which use a recording client and do
not verify actual backend negotiation.

The fixture requires an actual local `r2kernel-control` using production Control
and Event transports. All four modes enforce strict declarations and disable
unsafe dynamic endpoints. Set the ordinary installed/test runtime paths for that
backend (for example `R2KERNEL_CONTROL_SOCKET`, and `R2KERNEL_CONTRACT_SPEC_ROOTS`
when specs are not the shipped ones). Use the same environment for every fixture
process.

After starting the backend, run these pairs. Wait for each provider's `Operating`
line before starting its caller. Providers exit after 40 seconds by default; the
optional last argument sets that lifetime.

```sh
r2kernel_cpp_raw_control_runtime_fixture gateway /path/to/sdk/cpp/tests/raw_control_runtime_fixtures 60
r2kernel_cpp_raw_control_runtime_fixture control-caller /path/to/sdk/cpp/tests/raw_control_runtime_fixtures

r2kernel_cpp_raw_control_runtime_fixture operation-provider /path/to/sdk/cpp/tests/raw_control_runtime_fixtures 60
r2kernel_cpp_raw_control_runtime_fixture operation-caller /path/to/sdk/cpp/tests/raw_control_runtime_fixtures
```

The Control caller genuinely prepares its 64-byte request/response lane before its
first transaction, checks that reprepare and a 65-byte request fail, and verifies
that neither preparation nor rejected requests invoke the provider callback.
The callback's response timestamp is a fixture invocation counter for these
assertions. Six bytes containing zero and non-ASCII values remain unchanged through
the publicly declared `servo_control` raw server; result-ready completion and ownership
also reuse the prepared lane. The gateway provides a non-spec internal interface
and must initialize.
An explicit target rebind then prepares 65-byte capacities and admits that payload
without losing a response retained by the application from the original lane.
Inspect backend status to confirm `servo_control` is advertised as `bytes`/`bytes`,
while its declared interface contract remains `api.actuator.servo.v1`.

The Operation caller subscribes after Ready to canonical remote feedback/result/state
transport topics, then verifies accepted start, delivery of all three Event kinds,
matching operation id, and a `succeeded` query with one feedback message. It does
not rely on sleeping between start and subscription. The provider registers its
three Event publisher resources only when the Operation starts after Ready.

Record the process exit codes/stdout, backend errors, and the runtime/core/SDK
build provenance as evidence when the fixture is run.
