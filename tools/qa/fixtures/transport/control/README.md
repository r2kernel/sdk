# Control transport validation fixtures

These QA fixtures exchange raw `bytes` payloads; they do not encode application
messages. The Python and C++ controller reports verify the complete response
payload and response status. They report runtime RTT separately from caller
wall time around `transact_control`, plus p95/p99, failures, integrity errors,
deadline and period violations, achieved rate, process CPU, and peak RSS.

## Maintainer qualification

Provide exact-name subsystem and deployment manifests with a
`Component -> Gateway` binding, and start the gateway before the controller.
These sources are retained protocol fixtures, not an installed public demo.
The private development workspace's periodic real-time validation runner
owns the declared-profile periodic experiment; it retains every attempt,
scheduled completion and skip, without catch-up bursts. A direct fixture run or
successful RTT percentile alone does not qualify a 1 ms completion deadline.

## Building C++ fixtures

The `control` group builds against the SDK sources in this tree by default
(pass the Rust runtime core source with `--core-source <dir>`); the explicit
`--sdk installed` choice is intended only for comparisons and fails if no
installed SDK exists. The other build options are described in
`tutorials/support/README.md`. C++ gateway fixtures require an explicit `--port`. Build and run them against
the same SDK/Core generation as the selected runtime; a source build alone
does not prove provider readiness or periodic performance.

```sh
python3 tutorials/support/build_support.py control --fixture-root tools/qa/fixtures --mode release
```
