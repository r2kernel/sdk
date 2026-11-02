# r2kernel-client

Primary source-of-truth for the R2Kernel client SDK layer.

Current scope:

- Python client wrapper
- C++ client wrapper
- datatype adapters
- lifecycle helpers
- client launch/CLI entry points
- managed UDP, TCP, CAN, and device broker helpers for launcher-owned package processes
  (`ResourceBrokerClient` in both SDKs)

The Python and C++ SDKs expose the same subsystem surface (manifests, bindings
and bound calls, configuration document, Control/Procedure/Operation, timing
headers, frame lookup, resource broker). Names differ only where a language
requires it, and both SDKs read the same
`R2KERNEL_*` environment variables where they apply. See the per-SDK READMEs and
the documentation's Subsystem SDK Reference.

## Layout
- `python/`: Python package and launcher/CLI entry points
- `cpp/`: C++ SDK and CMake package sources
- `testdata/parity/`: cases both SDK test suites run to keep their decisions
  identical (manifest and deployment validation, manifest discovery, Control
  handler socket mode, Protocol host authority)

## Local connection authority

Production Linux SDK connections verify the actual Unix server PID and process
birth identity through the root resource broker. Handler, Event and notification
connections verify the backend manager once when accepting a connection; warm
Control requests do not query the broker for every packet. Standalone SDK clients
can request this read-only proof without a package lease. A registration response
cannot turn verification off.

For explicit standalone development with `resource_service.mode=disabled`, set
`R2KERNEL_RESOURCE_SERVICE_MODE=disabled` in the SDK environment as well. This
mode permits the raw development transport and does not authenticate source
names. Authority-prefixed contexts are rejected on unverified connections.
Managed launch supplies the resolved mode and broker socket automatically.

Verified remote Control preserves the callback's source subsystem name. Python
request contexts expose `source_authority` and the `Subsystem` offers
`current_control_source_authority()`; C++ callbacks use the free function
`current_control_source_authority()` without changing existing request layouts.
Protocol Procedure and Operation use a separate authenticated host identity:
the sender is `host:<peer>`, Python contexts expose `host_authority` (also
`Subsystem.current_protocol_host_authority()`), and C++ callbacks use
`current_protocol_host_authority()`. The host context follows the
Operation's executor callback and is cleared when that callback returns. These
request-local values do not authenticate names supplied inside application data.
Registered typed Procedure callers retain the strict prepared schema comparison;
source-less typed Procedure calls remain unsupported. Console plain Procedure
and Operation retain their existing management authorization contract.
