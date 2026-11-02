# Operation Fixtures

These fixtures demonstrate the native R2Kernel Operation model:

- `operation.start`, `operation.cancel`, `operation.query` over the control path
- operation feedback, result, and state over Event

Implemented languages:

- Python
- C++

## Python

Start the control backend first, then run:

```bash
python3 tools/qa/fixtures/transport/operation/python/server.py
python3 tools/qa/fixtures/transport/operation/python/client.py --goal 10 --delay-ms 100 --exit-on-complete
```

To test cancellation:

```bash
python3 tools/qa/fixtures/transport/operation/python/client.py \
  --goal 20 \
  --delay-ms 200 \
  --cancel-after-ms 1200 \
  --exit-on-complete
```

## C++

Build the fixtures (build options: `tutorials/support/README.md`):

```bash
# Ubuntu / Debian: sudo apt install -y build-essential cmake
python3 tutorials/support/build_support.py operation --fixture-root tools/qa/fixtures --mode release
```

Run the server and client:

```bash
tools/qa/fixtures/transport/operation/cpp/build/server
tools/qa/fixtures/transport/operation/cpp/build/client --goal 10 --delay-ms 100 --query-period-ms 200
```

The Python and C++ clients resolve their logical `operation` requirement through
`tools/qa/fixtures/transport/operation/r2kernel.deployment.yaml`.

To test cancellation:

```bash
tools/qa/fixtures/transport/operation/cpp/build/client \
  --goal 20 \
  --delay-ms 150 \
  --query-period-ms 200 \
  --cancel-after-ms 700
```
