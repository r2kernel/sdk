#!/usr/bin/env python3
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from package_support import quote, write_gateway_manifest  # noqa: E402

SCRIPTS = ["device_open_probe.py", "udp_sender.py"]


def copy_scripts(package_dir, scripts_dir):
    bin_dir = package_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in SCRIPTS:
        src = scripts_dir / name
        dst = bin_dir / name
        shutil.copy2(src, dst)
        dst.chmod(dst.stat().st_mode | 0o111)


def write_manifest(package_dir, name, command, args, resources, *, network_mode="brokered"):
    lines = [
        "schema_version: 1",
        f"name: {quote(name)}",
        "entrypoint:",
        f"  command: {quote(command)}",
        "  args:",
    ]
    for arg in args:
        lines.append(f"    - {quote(arg)}")
    lines.extend(["resources:"])
    lines.extend(resources)
    lines.extend(["  sandbox:", f"    network: {network_mode}"])
    (package_dir / "package.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_gateway_manifest(package_dir, name)


def make_device_package(out_dir, scripts_dir, device, result_file):
    package_dir = out_dir / "device_open"
    copy_scripts(package_dir, scripts_dir)
    write_manifest(
        package_dir,
        "physical-device-open-probe",
        "./bin/device_open_probe.py",
        [device, "--result-file", result_file, "--hold-sec", "0.5"],
        ["  devices:", "    paths:", f"      - {quote(device)}"],
    )


def network_resources(iface, mode, policy, udp_port):
    return [
        "  network:",
        "    interfaces:",
        f"      - name: {quote(iface)}",
        f"        mode: {quote(mode)}",
        f"        policy: {quote(policy)}",
        "        udp_ports:",
        f"          - {udp_port}",
    ]


def make_sender_package(
    out_dir,
    scripts_dir,
    name,
    claim_iface,
    mode,
    bind_source,
    target,
    port,
    label,
    result_file,
):
    package_dir = out_dir / name
    copy_scripts(package_dir, scripts_dir)
    args = [
        "--target",
        target,
        "--port",
        str(port),
        "--bind-source",
        bind_source,
        "--label",
        label,
        "--count",
        "3",
        "--interval-sec",
        "0.05",
        "--result-file",
        result_file,
    ]
    write_manifest(
        package_dir,
        name.replace("_", "-"),
        "./bin/udp_sender.py",
        args,
        network_resources(claim_iface, mode, name, port),
        network_mode="advisory",
    )


def main():
    parser = argparse.ArgumentParser(description="Create R2Kernel physical I/O validation packages.")
    parser.add_argument("--out", required=True)
    parser.add_argument("--device")
    parser.add_argument("--native-iface")
    parser.add_argument("--native-source")
    parser.add_argument("--usb-iface")
    parser.add_argument("--usb-source")
    parser.add_argument("--target")
    parser.add_argument("--port", type=int, default=19080)
    parser.add_argument("--mode", default="exclusive", choices=["exclusive", "shared", "advisory"])
    args = parser.parse_args()

    out_dir = Path(args.out)
    scripts_dir = Path(__file__).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.device:
        make_device_package(
            out_dir,
            scripts_dir,
            args.device,
            str(out_dir / "device_open.result.json"),
        )

    network_ready = all([args.native_iface, args.native_source, args.usb_iface, args.usb_source, args.target])
    if network_ready:
        make_sender_package(
            out_dir,
            scripts_dir,
            "nic_claim_native_send_native",
            args.native_iface,
            args.mode,
            args.native_source,
            args.target,
            args.port,
            "claim-native-send-native",
            str(out_dir / "native_send_native.result.json"),
        )
        make_sender_package(
            out_dir,
            scripts_dir,
            "nic_claim_usb_send_usb",
            args.usb_iface,
            args.mode,
            args.usb_source,
            args.target,
            args.port,
            "claim-usb-send-usb",
            str(out_dir / "usb_send_usb.result.json"),
        )
        make_sender_package(
            out_dir,
            scripts_dir,
            "nic_claim_usb_send_native",
            args.usb_iface,
            args.mode,
            args.native_source,
            args.target,
            args.port,
            "claim-usb-send-native",
            str(out_dir / "usb_claim_native_send.result.json"),
        )
        make_sender_package(
            out_dir,
            scripts_dir,
            "nic_claim_native_send_usb",
            args.native_iface,
            args.mode,
            args.usb_source,
            args.target,
            args.port,
            "claim-native-send-usb",
            str(out_dir / "native_claim_usb_send.result.json"),
        )

    print(out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
