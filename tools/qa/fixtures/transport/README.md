# Transport Fixtures

This directory contains the baseline communication fixtures:

- `event/` for Event
- `control/` for Control
- `control_drive/` for typed Control over the public `api.actuator.drive.v1` contract (cross-host Control)
- `procedure/` for Procedure
- `operation/` for Operation

These fixtures validate the transport/runtime layer itself. Each is
implemented in Python and C++.

Reading rule:

- Event is the stamped stream surface
- Control is the timed request/response surface
- Procedure is bounded synchronous request/response on the control path
- Operation uses the control path for lifecycle control and Event for feedback, result, and state

Shared support code used by these fixtures lives under:

- `tutorials/support/python`
- `tutorials/support/cpp`
- `tutorials/support/build_support.py`, the C++ fixture build CLI
