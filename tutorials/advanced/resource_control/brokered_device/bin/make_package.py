#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from package_support import prepare_package, quote, sdk_python_dir  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a R2Kernel brokered device probe package.")
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--result-file")
    parser.add_argument("--repo-root", help="Checkout containing sdk/python (default: the checkout containing this tutorial).")
    parser.add_argument("--expect-direct-block", action="store_true")
    args = parser.parse_args()

    sdk_python = sdk_python_dir(__file__, args.repo_root)
    package_dir = prepare_package(args.out, Path(__file__).resolve().parent, "brokered_device_probe.py")
    result_file = args.result_file or str(package_dir / "brokered_device_probe.result.json")
    lines = [
        "schema_version: 1",
        "name: brokered-device-probe",
        "entrypoint:",
        "  command: ./bin/brokered_device_probe.py",
        "  args:",
        f"    - {quote(args.device)}",
        "    - --result-file",
        f"    - {quote(result_file)}",
    ]
    if args.expect_direct_block:
        lines.append("    - --expect-direct-block")
    lines.extend([
        "  env:",
        f"    PYTHONPATH: {quote(str(sdk_python))}",
        "resources:",
        "  devices:",
        "    paths:",
        f"      - {quote(args.device)}",
        "  sandbox:",
        "    devices: managed",
    ])
    (package_dir / "package.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(package_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
