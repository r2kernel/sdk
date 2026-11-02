#!/usr/bin/env python3
import argparse
import json
import socket
import time


def write_result(path, payload):
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
    else:
        print(json.dumps(payload, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description="Send R2Kernel NIC probe packets.")
    parser.add_argument("--target", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--bind-source")
    parser.add_argument("--bind-device")
    parser.add_argument("--label", default="r2kernel-nic-probe")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--interval-sec", type=float, default=0.1)
    parser.add_argument("--hold-sec", type=float, default=0.0)
    parser.add_argument("--result-file")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    device_error = None
    if args.bind_device:
        try:
            sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_BINDTODEVICE,
                args.bind_device.encode("utf-8") + b"\0",
            )
        except OSError as exc:
            device_error = {"errno": exc.errno, "error": str(exc)}
    if args.bind_source:
        sock.bind((args.bind_source, 0))

    sent = []
    for seq in range(args.count):
        payload = f"{args.label}:{seq}".encode("utf-8")
        sock.sendto(payload, (args.target, args.port))
        sent.append({"seq": seq, "payload": payload.decode("utf-8")})
        time.sleep(args.interval_sec)

    if args.hold_sec > 0:
        time.sleep(args.hold_sec)

    payload = {
        "ok": device_error is None,
        "target": args.target,
        "port": args.port,
        "bind_source": args.bind_source,
        "bind_device": args.bind_device,
        "device_bind_error": device_error,
        "socket_name": sock.getsockname(),
        "sent": sent,
    }
    write_result(args.result_file, payload)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
