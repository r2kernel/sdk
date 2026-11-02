# R2Kernel Spec

This directory is the public, language-neutral contract source for R2Kernel.

It defines:

- message schemas under `messages/`
- API contracts under `apis/`
- public components under `components/`
- default namespace-version resolution under `defaults.yaml`

It does not define transport internals or runtime-only control envelopes.

## Public Contract Scope

The public spec describes subsystem interoperability.

That means it covers:

- shared message families such as `msg.core.common.primitive.v1`,
  `msg.core.spatial.v1`, and `msg.core.kinematics.v1`
- API contracts such as `api.io.digital.output.v1`,
  `api.actuation.trigger.v1`, `api.locomotion.velocity.v1`, and
  `api.motion.manipulator.state.v1`
- public components such as `component.mobile_base.v1`,
  `component.manipulator.v1`, `component.perception.v1`, and
  `component.trigger_actuator.v1`

Runtime-internal message envelopes used by the timed control path are kept
outside this directory.

## Layout

- `messages/`
  - public message schema families
- `apis/`
  - API contracts that declare Events, Controls, Procedures, and
    Operations
- `components/`
  - public components composed of API contracts

Examples:

- `msg.core.spatial.v1` -> `messages/msg/core/spatial/v1.yaml`
- `api.io.digital.output.v1` -> `apis/api/io/digital/output/v1.yaml`
- `api.locomotion.velocity.v1` -> `apis/api/locomotion/velocity/v1.yaml`
- `component.mobile_base.v1` -> `components/component/mobile_base/v1.yaml`

## Namespace Rule

Schema files follow namespace segments as directories, and the version is stored
as the leaf file name. A file's `id` always equals its path.

Examples:

- `msg.core.common.primitive.v1` -> `messages/msg/core/common/primitive/v1.yaml`
- `msg.motion.manipulator.v1` -> `messages/msg/motion/manipulator/v1.yaml`
- `api.motion.manipulator.state.v1` -> `apis/api/motion/manipulator/state/v1.yaml`
- `component.manipulator.v1` -> `components/component/manipulator/v1.yaml`

This keeps versioning local to each namespace instead of forcing one
repository-wide `v1/`, `v2/` split.

## Document Header

Every document starts with the same header:

```yaml
version: 1
id: msg.core.spatial.v1     # or api.*, component.*
kind: message_set           # api_contract, component
```

`defaults.yaml` uses `version: 1` and `kind: namespace_defaults` and has no
`id`.

## Identifier Ownership

Unqualified identifiers (`msg.<domain>.*`, `api.<domain>.*`,
`component.<name>.*`) are reserved for the R2Kernel standard dictionary in this
directory. Standardness is determined by membership in this dictionary, not by
a naming heuristic.

Third parties and products use their own vendor segment:
`<kind>.<vendor>.*`, for example `api.acme.gripper.v1` or
`msg.acme.vision.v1`, published from their own package spec roots. Tutorial
and product-specific contracts belong to their package spec roots. They are not
part of the standard dictionary and do not make public R2Kernel compatibility
claims, and they never become standard by copying an unqualified identifier.

## Public Usage Rule

Use this boundary:

- `msg.*` for datatype references
- `api.*` for endpoint contracts, gateway public interfaces, and component
  conformance APIs
- `component.*` for component-role subsystem public compatibility bundles

## Defaults

`defaults.yaml` stores the release-level default namespace-version map. It
lists every public message namespace exactly once.

That map is a fallback for authoring convenience. Explicit `imports` in a schema
document remain the stronger pin.

## Checks

`scripts/spec/check_public_specs.py` validates the headers, ID-to-path rule,
import and default closure, `defaults.yaml` completeness, and that every
message set is reachable from at least one API contract.

## Current Direction

The current direction is:

- public spec as the source of truth for shared contracts
- runtime implementations built on those contracts
- language-specific bindings layered above the same contract model

Detailed documentation lives under `docs/docs/spec`.
