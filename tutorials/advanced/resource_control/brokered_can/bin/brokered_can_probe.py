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


def direct_can_socket_result() -> dict:
    if not hasattr(socket, "AF_CAN") or not hasattr(socket, "CAN_RAW"):
        return {
            "ok": False,
            "blocked": False,
            "unsupported": True,
            "error": "Python socket module does not expose AF_CAN/CAN_RAW",
        }
    try:
        sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
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


def json_sockname(sockname):
    if isinstance(sockname, tuple):
        return list(sockname)
    return sockname


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe R2Kernel brokered SocketCAN access.")
    parser.add_argument("--interface", required=True)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--receive-own-messages", action="store_true")
    parser.add_argument("--can-fd-frames", action="store_true")
    parser.add_argument("--blocking", action="store_true")
    parser.add_argument("--hold-sec", type=float, default=0.1)
    args = parser.parse_args()

    result = {
        "interface": args.interface,
        "direct_can": direct_can_socket_result(),
        "brokered_can": {"ok": False},
    }
    if ResourceBrokerClient is None:
        result["brokered_can"] = {"ok": False, "error": repr(IMPORT_ERROR)}
    else:
        try:
            opened = ResourceBrokerClient().open_can_socket(
                interface=args.interface,
                receive_own_messages=args.receive_own_messages,
                can_fd_frames=args.can_fd_frames,
                nonblocking=not args.blocking,
            )
            with opened.socket as sock:
                result["brokered_can"] = {
                    "ok": True,
                    "sockname": json_sockname(sock.getsockname()),
                    "metadata": opened.metadata,
                }
                if args.hold_sec > 0:
                    time.sleep(args.hold_sec)
        except Exception as exc:
            result["brokered_can"] = {"ok": False, "error": repr(exc)}

    result["ok"] = result["direct_can"].get("blocked") and result["brokered_can"].get("ok")
    path = Path(args.result_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
