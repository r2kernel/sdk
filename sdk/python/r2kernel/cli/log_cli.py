"""CLI tool to view r2kernel library logs (not device logs)."""

from __future__ import annotations

import argparse
from collections import deque

from r2kernel.helper.library_logger import get_library_log_path


def _tail(path, lines: int):
    if not path.exists():
        print(f"Log file not found at {path}. Run a r2kernel tool to generate logs first.")
        return
    with path.open("r") as f:
        for line in deque(f, maxlen=lines):
            print(line.rstrip("\n"))


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Show r2kernel library logs.")
    parser.add_argument("--lines", "-n", type=int, default=50, help="Number of lines from the end to display.")
    parser.add_argument("--path", action="store_true", help="Only print the log file path.")
    args = parser.parse_args(argv)

    log_path = get_library_log_path()
    if args.path:
        print(log_path)
        return

    _tail(log_path, max(args.lines, 1))


if __name__ == "__main__":
    main()
