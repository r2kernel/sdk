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
    parser = argparse.ArgumentParser(description="Receive R2Kernel NIC probe packets.")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--timeout-sec", type=float, default=10.0)
    parser.add_argument("--result-file")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(args.timeout_sec)
    sock.bind((args.bind, args.port))
    received = []
    deadline = time.monotonic() + args.timeout_sec
    while len(received) < args.count and time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            break
        received.append(
            {
                "from": addr[0],
                "port": addr[1],
                "payload": data.decode("utf-8", errors="replace"),
            }
        )
    payload = {
        "ok": len(received) >= args.count,
        "bind": args.bind,
        "port": args.port,
        "expected_count": args.count,
        "received_count": len(received),
        "received": received,
    }
    write_result(args.result_file, payload)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
