#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import json
import socket
import time
from pathlib import Path

try:
    from r2kernel.core.resource import ResourceBrokerClient
except Exception as exc:  # pragma: no cover - tutorial diagnostic path
    ResourceBrokerClient = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


def direct_udp_socket_result() -> dict:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as exc:
        return {
            "ok": False,
            "errno": exc.errno,
            "blocked": exc.errno == errno.EPERM,
            "error": str(exc),
        }
    else:
        sock.close()
        return {"ok": True, "blocked": False}


def direct_tcp_socket_result() -> dict:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        return {
            "ok": False,
            "errno": exc.errno,
            "blocked": exc.errno == errno.EPERM,
            "error": str(exc),
        }
    else:
        sock.close()
        return {"ok": True, "blocked": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe R2Kernel brokered UDP socket access.")
    parser.add_argument("--interface", required=True)
    parser.add_argument("--bind-host", default="0.0.0.0")
    parser.add_argument("--bind-port", type=int, default=0)
    parser.add_argument("--target-host")
    parser.add_argument("--target-port", type=int, default=0)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--hold-sec", type=float, default=0.1)
    args = parser.parse_args()

    result = {
        "interface": args.interface,
        "bind_host": args.bind_host,
        "bind_port": args.bind_port,
        "target_host": args.target_host,
        "target_port": args.target_port,
        "direct_udp": direct_udp_socket_result(),
        "direct_tcp": direct_tcp_socket_result(),
        "brokered_udp": {"ok": False},
        "brokered_tcp": {"ok": False},
    }
    if ResourceBrokerClient is None:
        result["brokered_udp"] = {"ok": False, "error": repr(IMPORT_ERROR)}
    else:
        try:
            opened = ResourceBrokerClient().open_udp_socket(
                interface=args.interface,
                bind_host=args.bind_host,
                bind_port=args.bind_port,
            )
            with opened.socket as sock:
                local = sock.getsockname()
                sent = 0
                if args.target_host and args.target_port:
                    payload = f"r2kernel-brokered-{time.time_ns()}".encode("ascii")
                    sent = sock.sendto(payload, (args.target_host, args.target_port))
                result["brokered_udp"] = {
                    "ok": True,
                    "local": list(local),
                    "sent": sent,
                    "metadata": opened.metadata,
                }
                if args.hold_sec > 0:
                    time.sleep(args.hold_sec)
            opened_tcp = ResourceBrokerClient().open_tcp_socket(
                interface=args.interface,
                bind_host=args.bind_host,
                bind_port=0,
            )
            with opened_tcp.socket as sock:
                result["brokered_tcp"] = {
                    "ok": True,
                    "local": list(sock.getsockname()),
                    "metadata": opened_tcp.metadata,
                }
        except Exception as exc:
            result["broker_error"] = repr(exc)

    result["ok"] = (
        result["direct_udp"].get("blocked")
        and result["direct_tcp"].get("blocked")
        and result["brokered_udp"].get("ok")
        and result["brokered_tcp"].get("ok")
    )
    path = Path(args.result_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
