# Event Tutorials

These fixtures demonstrate the native Event transport primitive.

Implemented languages:

- Python
- C++

All variants are direct-run fixtures and support simple benchmark-style runs with:
- variable `topic-count`
- variable `payload-size`
- variable publish period
- bounded run counts with warmup
- final text/JSON latency reports on the subscriber side

Benchmark semantics:
- `count` is the measured message count after warmup.
- actual total publishes = `warmup + count`
- event report fields of interest are `samples`, `dropped`, and latency percentiles
- `dropped` is computed from per-topic sequence gaps
- when the first observed sequence is greater than 1, the missing prefix is
  reported through `prefix_missing` and included in `dropped`

Delivery semantics:
- Event is intended for streaming, telemetry, state, and sensor data rather than command/ack control packets.
- Event delivery uses bounded queues. When a producer outpaces delivery or capacity is exhausted, messages can be dropped; Event does not promise lossless delivery under overload.
- Set `--queue-depth N` on publishers, or `R2KERNEL_EVENT_QUEUE_DEPTH=N`, when validating a specific workload.
- Nonzero `dropped` means the subscriber observed a sequence gap and should be treated as a warning or failure depending on the workload.

Large payload note:
- Event payloads and queue depth must fit the capacity configured in the active runtime `config.yaml`.
- Larger payloads and deeper queues increase memory demand; validate the intended combination under representative load and check delivery reports for drops.

## Run

Python:

```bash
python3 tools/qa/fixtures/transport/event/python/subscriber.py
python3 tools/qa/fixtures/transport/event/python/publisher.py --count 5
```

C++ (build options: `tutorials/support/README.md`):

```bash
# Ubuntu / Debian: sudo apt install -y build-essential cmake
python3 tutorials/support/build_support.py event --fixture-root tools/qa/fixtures --mode release
tools/qa/fixtures/transport/event/cpp/build/subscriber
tools/qa/fixtures/transport/event/cpp/build/publisher --count 5
```

The direct-run pairs remain shared Event integration fixtures for release QA,
cross-host one-way checks, fanout, and ping/echo RTT. They are not a public
one-command benchmark runner.

Cross-host one-way smoke tests gate the publisher before measured traffic.
For **every attempt**, allocate a new private run directory on the publisher
host before starting the publisher; its gate and acknowledgement must not exist.
Use a unique session, publisher name, and topic for each attempt. Never reuse or
delete a gate or acknowledgement from an earlier attempt. The publisher validates
gate absence before registration and acknowledges this session's startup. The
helper requires that live acknowledgement **and** the named publisher's actual
topic registration, as well as readiness, before creating an empty `0600` gate
exclusively. An already-ready receiver path cannot authorize a publisher that
has not validated this attempt's absent gate. Missing or previous-session
acknowledgements, an exited publisher, or a wrong name keep the gate closed.
Both require a real, effective-user-owned `0700` parent directory (no symlink
components); symlinks, directories, FIFOs, hardlinks, nonempty gate files, and
unsafe modes are invalid. A previous success is not authorization for a later
attempt, even if the next API request fails. Existing caller paths are left
intact on refusal. This handshake is startup-only, not a per-message operation.

### StartGate v1 contract

`cpp/start_gate.hpp` (publisher side) and `python/start_gate.py` (publisher
and gate-helper side) implement one contract, checked by the shared scenario
table `start_gate_conformance.json` (`cpp/test_start_gate.cpp`, which takes
the table path as its argument and runs as the CTest
`r2kernel_tutorial_start_gate_test` of the Tutorial or root `tests/qa/cpp` build, and
`python/test_start_gate.py`):

1. Construction requires an absolute gate path in normal form (no empty `//`
   component, no `.` component, no trailing `/`) whose parent is a real
   (no symlink component), effective-user-owned `0700` directory. The parent
   is pinned with `open(O_PATH|O_DIRECTORY|O_NOFOLLOW)` and its device/inode
   recorded; the gate must not exist yet.
2. Every later create, open or stat of the gate and its `.ack` is relative to
   the pinned directory (`openat`/`fstatat(AT_SYMLINK_NOFOLLOW)`; Python
   `dir_fd=`), never by path.
3. After each create, the new descriptor must be a regular, effective-user
   owned, `0600` file with one link, and the parent path must still resolve to
   the pinned directory. The acknowledgement is created exclusively and locked
   (`flock LOCK_EX`) immediately, before its mode is checked and its content
   is written; the lock is held for the publisher's lifetime. It holds
   `r2kernel-event-start-v1`, the session and the publisher name. The helper
   treats an empty acknowledgement (created, not yet written) as not
   acknowledged yet and never probes its lock; an unlocked acknowledgement with
   content means the publisher exited.
4. Any mismatch is an explicit error with a contract code
   (`StartGateError.code` / `code()`): `invalid_path`, `parent_unsafe`,
   `directory_changed`, `gate_exists`, `gate_unsafe`, `gate_inaccessible`,
   `invalid_identity`, `ack_exists`, `ack_unsafe`, `ack_write_failed`,
   `gate_closed` (any use after Python `close()` or of a moved-from C++ gate),
   and on the helper side `publisher_exited` and `not_acknowledged`. The helper
   reports an acknowledgement it cannot open safely as `ack_unsafe` and an
   existing gate in `open()` as `gate_exists`, never as a raw `OSError`.
5. Both implementations run these checks in the same order. Neither removes a
   caller path. The C++ fixture treats an empty gate path as "no gate".
6. A gated C++ publisher or ping whose gate refuses at startup prints
   `<Publisher|Ping> start gate refused [<code>]: <message>` on stderr and
   exits with status 3 before registration.

Installed-release smoke: from a checkout/export containing these fixtures, use
the installed Python SDK/runtime (the C++ build above also prefers the installed
SDK). Keep the receiver running on the receiving host, establish the SSH tunnel
in a separate shell, then run the publisher and gate helper in the **same**
publisher-host shell so they share this attempt's directory:

```bash
# publisher host, original shell: prepare this attempt before starting either fixture
set -o pipefail
session=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
run_dir=$(mktemp -d /tmp/r2kernel-event.XXXXXXXX)
cat >"$run_dir/publisher.subsystem.yaml" <<EOF
kind: r2kernel.subsystem
version: 1
subsystem:
  name: cross_pub_$session
  role: component
provides:
  event_stream:
    contract: tutorial.transport.event_stream.v1
    visibility: internal
    endpoints:
      publishes: [cross_evt_$session]
EOF
printf 'Use this session on the receiver: %s\n' "$session"

# receiver host, separate shell: paste the session printed above
read -r -p "Attempt session: " session
receiver_run=$(mktemp -d /tmp/r2kernel-event-receiver.XXXXXXXX)
cat >"$receiver_run/subscriber.subsystem.yaml" <<EOF
kind: r2kernel.subsystem
version: 1
subsystem:
  name: cross_sub_$session
  role: component
requires:
  event_stream:
    contract: tutorial.transport.event_stream.v1
    visibility: internal
    required: false
    cardinality: one
    endpoints:
      publishes: [cross_evt_$session]
EOF
python3 tools/qa/fixtures/transport/event/python/subscriber.py \
  --name "cross_sub_$session" --topic-prefix "cross_evt_$session" --expected-count 10 \
  --manifest "$receiver_run/subscriber.subsystem.yaml"

# publisher host, separate shell: protect the receiver's API headers with SSH
ssh -N -L 127.0.0.1:18002:127.0.0.1:8001 <subscriber-ssh-host>

# publisher host, original prepared shell: no gate cleanup/reuse
python3 tools/qa/fixtures/transport/event/python/publisher.py \
  --name "cross_pub_$session" --topic-prefix "cross_evt_$session" --count 10 \
  --manifest "$run_dir/publisher.subsystem.yaml" \
  --start-gate-file "$run_dir/ready" --start-gate-session "$session" \
  >"$run_dir/publisher.log" 2>&1 &
publisher_pid=$!
python3 tools/qa/fixtures/transport/event/python/wait_event_path.py \
  --sender-status-url http://127.0.0.1:8001/api/v1/control/status \
  --receiver-status-url http://127.0.0.1:18002/api/v1/control/status \
  --scope cross-host \
  --sender-api-key-file /run/user/$UID/r2kernel-sender.key \
  --receiver-api-key-file /run/user/$UID/r2kernel-receiver.key \
  --topic-prefix "cross_evt_$session" --publisher-name "cross_pub_$session" \
  --require-time-sync \
  --stable-polls 3 \
  --start-gate-file "$run_dir/ready" \
  --start-gate-session "$session" \
  --report-format json \
  --report-path "$run_dir/gate.json" | tee "$run_dir/gate.log"
gate_status=$?
if [ "$gate_status" -ne 0 ]; then
  kill "$publisher_pid"
  wait "$publisher_pid" || true
  echo "Gate refused; retain $run_dir for diagnostics and start a fresh attempt." >&2
  exit "$gate_status"
fi
wait "$publisher_pid"
```

Inspect the subscriber's final delivery report and `$run_dir/gate.json`; no
publisher traffic should precede successful gating. The gate report must show
`publisher_startup_acknowledged=true`, `publisher_status_observed=true`,
`publisher_ready=true`, this attempt's `start_gate_session`, and
both data-plane ready topic sets, `transport_required=true`, `transport_ready=true`,
and two ready `transport_checks` for the opposite native peer. Two accepted clock
checks, no topology fallback, and `latency_evidence_accepted=true` are additionally
required before accepting one-way measurements with `--require-time-sync`.
For C++ publishers, pass the identical per-run gate path and
`--start-gate-session`; C++ ping/echo RTT gates follow the same startup
contract. The QA matrix allocates private run
directories, binds sessions and publisher registrations, and opens gates
exclusively too. Ungated local fixtures
remain supported but are not accepted cross-host measurements.

Each declared manifest must match that attempt's subsystem name and topics.
Python fixtures accept `--manifest`; retained C++ fixtures take the same
declaration through `R2KERNEL_SUBSYSTEM_MANIFEST`. Keep normal manifest and
endpoint enforcement enabled; a closed gate from a process that failed SDK
initialization is not a valid live-publisher refusal check.

Keep API security and required federation authentication enabled. Provision each
side's API key into the indicated file using your credential provisioning tool;
the helper requires a regular file owned by its effective user with no group or
other permissions (for example mode `0600`). Key contents never belong in URLs,
query strings, command arguments, shell history, logs, or readiness reports.
Each side defaults to `Authorization: Bearer`; select
`--sender-auth-header X-R2Kernel-API-Key` or its receiver counterpart if needed.
Use HTTPS for non-loopback status requests, or an approved protected transport;
plain HTTP does not encrypt headers. Redirects are refused, and a denied or
unavailable endpoint keeps the gate closed. Do not disable authentication or
use topology fallback to bypass a secured readiness failure.

With `--start-gate-file`, `--start-gate-session` and `--publisher-name` are
required: the name must identify this run's registered publisher, not just a
previously visible subsystem. Without a gate, `--publisher-name` remains an
optional observation: an absent publisher does not block path diagnostics, but
an observed publisher must list the requested topics. `--subscriber-name`
remains an optional receiver diagnostic.
`--scope cross-host` requires `--receiver-status-url`; `auto` chooses cross-host
when a receiver URL is supplied and local otherwise. Explicit `local` retains
local subscription semantics. Cross-host readiness requires ready topics from both
sides: the publisher-side `forward_ready_topics` and the subscriber-side
`inbound_ready_topics`. It also requires each host's
`stamped_stream_runtime_status.runtime.applied.active_peers` and
`subscriptions_synced` to contain the other endpoint's
`stamped_stream_runtime_status.local_host`. These current applied fields describe
native peer presence and peer subscription synchronization; the top-level
`connections` list is only desired topology and cannot satisfy them. Missing
identity or applied peer observations fail closed, even when local reader/sink
metrics and ready topics are present. When available, data-plane tick metrics
must also show sender and receiver readiness.

JSON reports expose `transport_required`, `transport_ready`, and
`transport_checks`. Each cross-host check has `label` (`sender` or `receiver`),
`peer_id` (the opposite endpoint's native identity), booleans `active_peers`,
`connected`, `subscriptions_synced`, and `ready`. Readiness is the conjunction of
`active_peers` and `subscriptions_synced`. `connected` is diagnostic: it lists
connections initiated during the most recent apply, so it can be false for
healthy established or accepted inbound peers. Local scope has no transport
requirement, an empty check list, and `transport_ready=true`; local subscription
checks still apply.

Topology-only fields are reported for debugging. `--allow-topology-fallback`
selects topology diagnostic topics but never approves cross-host readiness:
`ready=false`, exit status 1 after the bounded timeout, no start-gate creation,
and `latency_evidence_accepted=false`, even with mutually established peers and
accepted clocks. It is never selected automatically. Without that flag,
cross-host success returns exit status 0 and opens a requested gate only after
all current-run publisher and path checks, consecutive `--stable-polls`, and
any requested clock checks pass.

The helper polls existing public status only; it does not send probe Events,
repair a failed native handshake, prove delivery, or continuously protect an
already-open gate against subsequent peer loss. Inspect the subscriber's actual
delivery report independently; readiness alone is not evidence of a native
transport fix.

If a repeated same-control run reports an unexpected ready topic, inspect
`forward_ready_topic_details` or `inbound_ready_topic_details` in
`/api/v1/control/status`. Those detail fields show the participant lifecycle and
last-seen age behind each ready topic.

For readiness-only data-path smoke, omit `--require-time-sync`; do not treat the
resulting one-way latency samples as accepted cross-host performance data.
