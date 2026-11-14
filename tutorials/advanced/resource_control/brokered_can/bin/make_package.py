#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from package_support import prepare_package, quote, sdk_python_dir, write_gateway_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a R2Kernel brokered CAN probe package.")
    parser.add_argument("--out", required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--result-file")
    parser.add_argument("--repo-root", help="Checkout or installation prefix containing sdk/python (default: the one containing this tutorial).")
    parser.add_argument("--receive-own-messages", action="store_true")
    parser.add_argument("--can-fd-frames", action="store_true")
    parser.add_argument("--blocking", action="store_true")
    args = parser.parse_args()

    sdk_python = sdk_python_dir(__file__, args.repo_root)
    package_dir = prepare_package(args.out, Path(__file__).resolve().parent, "brokered_can_probe.py")
    result_file = args.result_file or str(package_dir / "brokered_can_probe.result.json")
    probe_args = [
        "--interface",
        args.interface,
        "--result-file",
        result_file,
    ]
    if args.receive_own_messages:
        probe_args.append("--receive-own-messages")
    if args.can_fd_frames:
        probe_args.append("--can-fd-frames")
    if args.blocking:
        probe_args.append("--blocking")

    lines = [
        "schema_version: 1",
        "name: brokered-can-probe",
        "entrypoint:",
        "  command: ./bin/brokered_can_probe.py",
        "  args:",
    ]
    lines.extend(f"    - {quote(value)}" for value in probe_args)
    lines.extend(
        [
            "  env:",
            f"    PYTHONPATH: {quote(str(sdk_python))}",
            "resources:",
            "  devices:",
            "    can:",
            f"      - {quote(args.interface)}",
            "  sandbox:",
            "    network: brokered",
            "    can: brokered",
        ]
    )
    (package_dir / "package.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_gateway_manifest(package_dir, "brokered-can-probe")
    print(package_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
