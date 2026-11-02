#!/usr/bin/env bash
set -euo pipefail

duration="${1:-3}"
echo "r2kernel resource-control demo started"
echo "pid=$$"
if command -v taskset >/dev/null 2>&1; then
  taskset -pc "$$" || true
fi
sleep "$duration"
echo "r2kernel resource-control demo finished"
