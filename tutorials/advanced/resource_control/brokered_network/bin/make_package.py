#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from package_support import prepare_package, quote, sdk_python_dir, write_gateway_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a R2Kernel brokered UDP probe package.")
    parser.add_argument("--out", required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--bind-host", default="0.0.0.0")
    parser.add_argument("--bind-port", type=int, default=0)
    parser.add_argument("--target-host")
    parser.add_argument("--target-port", type=int, default=0)
    parser.add_argument("--result-file")
    parser.add_argument("--repo-root", help="Checkout or installation prefix containing sdk/python (default: the one containing this tutorial).")
    args = parser.parse_args()

    sdk_python = sdk_python_dir(__file__, args.repo_root)
    package_dir = prepare_package(args.out, Path(__file__).resolve().parent, "brokered_udp_probe.py")
    result_file = args.result_file or str(package_dir / "brokered_udp_probe.result.json")
    probe_args = [
        "--interface",
        args.interface,
        "--bind-host",
        args.bind_host,
        "--bind-port",
        str(args.bind_port),
        "--result-file",
        result_file,
    ]
    if args.target_host and args.target_port:
        probe_args.extend(["--target-host", args.target_host, "--target-port", str(args.target_port)])

    lines = [
        "schema_version: 1",
        "name: brokered-network-probe",
        "entrypoint:",
        "  command: ./bin/brokered_udp_probe.py",
        "  args:",
    ]
    lines.extend(f"    - {quote(value)}" for value in probe_args)
    lines.extend(
        [
            "  env:",
            f"    PYTHONPATH: {quote(str(sdk_python))}",
            "resources:",
            "  network:",
            "    interfaces:",
            f"      - name: {quote(args.interface)}",
            "        mode: shared",
            "        policy: brokered-probe",
            "  sandbox:",
            "    network: brokered",
        ]
    )
    (package_dir / "package.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_gateway_manifest(package_dir, "brokered-network-probe")
    print(package_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
