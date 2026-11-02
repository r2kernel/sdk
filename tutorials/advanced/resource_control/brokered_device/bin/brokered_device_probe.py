#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path

from r2kernel.core.resource import ResourceBrokerClient


def direct_open_result(path: str, read: bool, write: bool) -> dict:
    if read and write:
        flags = os.O_RDWR
    elif write:
        flags = os.O_WRONLY
    else:
        flags = os.O_RDONLY
    flags |= os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        return {
            "ok": False,
            "errno": exc.errno,
            "blocked": exc.errno in (errno.EACCES, errno.EPERM),
            "error": str(exc),
        }
    else:
        os.close(fd)
        return {"ok": True, "blocked": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe R2Kernel brokered device FD access.")
    parser.add_argument("device")
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--read", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--expect-direct-block", action="store_true")
    args = parser.parse_args()

    read = bool(args.read or not args.write)
    write = bool(args.write)
    result = {
        "device": args.device,
        "read": read,
        "write": write,
        "direct_open": direct_open_result(args.device, read, write),
        "brokered_device": {"ok": False},
    }
    try:
        opened = ResourceBrokerClient().open_device(args.device, read=read, write=write)
        try:
            stat = os.fstat(opened.fd)
            result["brokered_device"] = {
                "ok": True,
                "metadata": opened.metadata,
                "mode": stat.st_mode,
                "rdev": stat.st_rdev,
            }
        finally:
            opened.close()
    except Exception as exc:
        result["brokered_device"] = {"ok": False, "error": repr(exc)}

    result["ok"] = bool(result["brokered_device"].get("ok"))
    if args.expect_direct_block:
        result["ok"] = result["ok"] and bool(result["direct_open"].get("blocked"))
    path = Path(args.result_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
