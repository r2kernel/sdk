# Component Declare Tutorial

This tutorial set demonstrates declaration and validation for canonical
`component.*` components.

Tutorials:

- `python/server.py`
  - valid digital I/O component implementation
  - declares `api.io.digital.output.v1`
  - binds the procedures of the declared digital-I/O contract; the combined
    helper registers only what the manifest declares
  - a `component`: gateways may expose only Control endpoints, and this
    contract consists of procedures
- `python/client.py`
  - calls the canonical digital-I/O procedures
- `python/typed_server.py`
  - same component idea, with typed procedure payloads shown more explicitly
- `python/typed_client.py`
  - typed client for the typed server variant
- `python/invalid_server.py`
  - intentionally incomplete declaration
  - should fail validation during `initialize()`

## Run

Start the control backend first.

Server:

```bash
python3 tools/qa/fixtures/declare/device/python/server.py
```

Client:

```bash
python3 tools/qa/fixtures/declare/device/python/client.py --count 5
```

Validation failure demo:

```bash
python3 tools/qa/fixtures/declare/device/python/invalid_server.py
```

The invalid tutorial should fail contract validation before entering the operating state.
