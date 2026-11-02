#!/usr/bin/env python3
import argparse
import grp
import json
import os
import stat
import time


def group_names():
    names = []
    for gid in os.getgroups():
        try:
            names.append(grp.getgrgid(gid).gr_name)
        except KeyError:
            names.append(str(gid))
    return names


def path_metadata(path):
    st = os.stat(path)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return {
        "mode": oct(stat.S_IMODE(st.st_mode)),
        "uid": st.st_uid,
        "gid": st.st_gid,
        "group": group,
        "rdev": st.st_rdev,
    }


def write_result(path, payload):
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
    else:
        print(json.dumps(payload, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description="Open a claimed R2Kernel device node.")
    parser.add_argument("device")
    parser.add_argument("--result-file")
    parser.add_argument("--hold-sec", type=float, default=0.0)
    args = parser.parse_args()

    payload = {
        "device": args.device,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "groups": group_names(),
    }
    try:
        payload["stat"] = path_metadata(args.device)
    except OSError as exc:
        payload.update({"ok": False, "errno": exc.errno, "error": str(exc)})
        write_result(args.result_file, payload)
        return 2

    fd = None
    try:
        fd = os.open(args.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        payload["ok"] = True
        if args.hold_sec > 0:
            time.sleep(args.hold_sec)
        return 0
    except OSError as exc:
        payload.update({"ok": False, "errno": exc.errno, "error": str(exc)})
        return 1
    finally:
        if fd is not None:
            os.close(fd)
        write_result(args.result_file, payload)


if __name__ == "__main__":
    raise SystemExit(main())
