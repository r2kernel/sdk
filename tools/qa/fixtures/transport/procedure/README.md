# Procedure Fixtures

These fixtures demonstrate the native Procedure model:
- server side registers named procedures
- client side resolves a logical binding through `r2kernel.deployment.yaml`
- request/response is transported over the control path

Implemented languages:

- Python
- C++

## Run

Python:

```bash
python3 tools/qa/fixtures/transport/procedure/python/server.py
python3 tools/qa/fixtures/transport/procedure/python/client.py --binding procedure --arg 2 --count 5
```

The client keeps issuing periodic requests until you stop it. With `--count 5`,
it exits after five requests. Stop the server with `Ctrl-C` when you are done
observing the responses.

C++ (build options: `tutorials/support/README.md`):

```bash
# Ubuntu / Debian: sudo apt install -y build-essential cmake
python3 tutorials/support/build_support.py procedure --fixture-root tools/qa/fixtures --mode release
tools/qa/fixtures/transport/procedure/cpp/build/server
tools/qa/fixtures/transport/procedure/cpp/build/client --binding procedure --arg 2 --count 5
```

With `--count 5`, the C++ client exits after five requests. Omit `--count` or
pass `--count 0` to keep issuing periodic requests until `Ctrl-C`.

The Python and C++ clients resolve their logical `procedure` requirement through
`tools/qa/fixtures/transport/procedure/r2kernel.deployment.yaml`.
