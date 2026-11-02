# Tutorial private contracts

These package-local contracts describe typed private subsystem bindings. They
are outside the official R2Kernel dictionary and do not define public robot slots.

`api.tutorial.gateway.device_gateway.digital_io.v1` describes the gripper's two
Control calls. Both manifest interfaces retain `visibility: internal` and their
explicit endpoint lists. The declared request and response types allow the SDK to
prepare the caller and provider codecs before Ready and advertise the same
contract identity for binding negotiation.

The `sync_tutorial_specs.py` maintenance script copies this API into the gripper
and Gateway package `spec/` roots. Both package manifests declare
`contracts.spec_roots: [spec]`, so ordinary package launch supplies those roots
to the Python and native codec loaders. Run the synchronization script after changing the source, and use its
`--check` mode during release validation.
