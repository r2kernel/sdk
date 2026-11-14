# Tutorial package-local contracts

These package-local contracts describe typed bindings between tutorial
subsystems. They are outside the official R2Kernel dictionary and do not define
robot slots.

`api.tutorial.gateway.device_gateway.digital_io.v1` describes the gripper's two
Control calls. The Gateway's `digital_io` interface and the gripper's
`digital_io` requirement both declare `visibility: public`, so an authenticated
federation binding can export it. The contract lists the Controls, so neither
manifest repeats an endpoint list. The declared request and response types allow
the SDK to prepare the caller and provider codecs before Ready and advertise the
same contract identity for binding negotiation.

The `sync_tutorial_specs.py` maintenance script copies this API into the gripper
and Gateway package `spec/` roots. Both package manifests declare
`contracts.spec_roots: [spec]`, so ordinary package launch supplies those roots
to the Python and native codec loaders. Run the synchronization script after changing the source, and use its
`--check` mode during release validation.
