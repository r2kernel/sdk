#!/usr/bin/env bash
set -euo pipefail

DEVICE="/dev/ttyAMA10"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Source root or installation prefix that contains tutorials/ and sdk/python
# (five levels above bin/).
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../../.." && pwd)"
PREFIX="${R2KERNEL_PREFIX:-}"
CONFIG_ARG=""
RESOURCED_ARG=""
LAUNCH_ARG=""
OUT_DIR="/tmp/r2kernel-brokered-tty"
SOCKET_PATH="/tmp/r2kernel-tty-resource.sock"
DEVICE_GROUP="r2kernel-device"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--device /dev/ttyAMA10] [--repo-root PATH] [--out DIR]
                         [--prefix PATH] [--config PATH]
                         [--resourced PATH] [--launch PATH]
                         [--socket PATH] [--device-group GROUP]

Run a reproducible R2Kernel brokered TTY device smoke test.

The script uses installed release binaries from --prefix, R2KERNEL_PREFIX,
/opt/r2kernel, or ~/.local/r2kernel. Without an installed prefix, pass
--config, --resourced and --launch explicitly. --repo-root names the source
root or installation prefix that contains sdk/python (default: the one
containing this tutorial).

The script temporarily protects the selected TTY node as root:DEVICE_GROUP
mode 0660, starts r2kernel-resourced as root, launches a managed package that
expects direct open() to fail, then verifies brokered open_device() succeeds.
The original device owner/group/mode is restored on exit.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --device)
      DEVICE="$2"
      shift 2
      ;;
    --repo-root)
      REPO_ROOT="$2"
      shift 2
      ;;
    --prefix)
      PREFIX="$2"
      shift 2
      ;;
    --config)
      CONFIG_ARG="$2"
      shift 2
      ;;
    --resourced)
      RESOURCED_ARG="$2"
      shift 2
      ;;
    --launch)
      LAUNCH_ARG="$2"
      shift 2
      ;;
    --out)
      OUT_DIR="$2"
      shift 2
      ;;
    --socket)
      SOCKET_PATH="$2"
      shift 2
      ;;
    --device-group)
      DEVICE_GROUP="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This smoke test is Linux-only." >&2
  exit 1
fi
if [[ ! -c "${DEVICE}" ]]; then
  echo "Device is not a character node: ${DEVICE}" >&2
  exit 1
fi
if id -nG | tr ' ' '\n' | grep -qx "${DEVICE_GROUP}"; then
  echo "Current user already belongs to ${DEVICE_GROUP}; direct-open blocking cannot be validated." >&2
  exit 1
fi

REPO_ROOT="$(cd "${REPO_ROOT}" && pwd)"
if [[ -z "${PREFIX}" && -x "/opt/r2kernel/bin/r2kernel-launch" ]]; then
  PREFIX="/opt/r2kernel"
fi
if [[ -z "${PREFIX}" && -x "${HOME}/.local/r2kernel/bin/r2kernel-launch" ]]; then
  PREFIX="${HOME}/.local/r2kernel"
fi
if [[ -n "${PREFIX}" ]]; then
  PREFIX="$(cd "${PREFIX}" && pwd)"
  CONFIG="${CONFIG_ARG:-${PREFIX}/runtime/config.yaml}"
  RESOURCED="${RESOURCED_ARG:-${PREFIX}/bin/r2kernel-resourced}"
  LAUNCH="${LAUNCH_ARG:-${PREFIX}/bin/r2kernel-launch}"
else
  if [[ -z "${CONFIG_ARG}" || -z "${RESOURCED_ARG}" || -z "${LAUNCH_ARG}" ]]; then
    echo "No installed R2Kernel prefix found; pass --prefix, or all of --config, --resourced and --launch." >&2
    exit 1
  fi
  CONFIG="${CONFIG_ARG}"
  RESOURCED="${RESOURCED_ARG}"
  LAUNCH="${LAUNCH_ARG}"
fi
LOG_PATH="${OUT_DIR}/r2kernel-resourced.log"
RESULT_PATH="${OUT_DIR}/brokered_device_probe.result.json"

if [[ ! -x "${RESOURCED}" ]]; then
  echo "r2kernel-resourced is not executable: ${RESOURCED}" >&2
  exit 1
fi
if [[ ! -x "${LAUNCH}" ]]; then
  echo "r2kernel-launch is not executable: ${LAUNCH}" >&2
  exit 1
fi
if [[ ! -f "${CONFIG}" ]]; then
  echo "Config file does not exist: ${CONFIG}" >&2
  exit 1
fi

ORIG_UID="$(stat -c '%u' "${DEVICE}")"
ORIG_GID="$(stat -c '%g' "${DEVICE}")"
ORIG_MODE="$(stat -c '%a' "${DEVICE}")"
RESOURCED_PID=""

cleanup() {
  if [[ -n "${RESOURCED_PID}" ]]; then
    sudo kill "${RESOURCED_PID}" >/dev/null 2>&1 || true
    sudo pkill -P "${RESOURCED_PID}" >/dev/null 2>&1 || true
    wait "${RESOURCED_PID}" >/dev/null 2>&1 || true
  fi
  sudo chown "${ORIG_UID}:${ORIG_GID}" "${DEVICE}" >/dev/null 2>&1 || true
  sudo chmod "${ORIG_MODE}" "${DEVICE}" >/dev/null 2>&1 || true
  sudo rm -f "${SOCKET_PATH}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

sudo groupadd --system -f "${DEVICE_GROUP}"
sudo chgrp "${DEVICE_GROUP}" "${DEVICE}"
sudo chmod 0660 "${DEVICE}"

rm -rf "${OUT_DIR}"
mkdir -p "${OUT_DIR}"
sudo rm -f "${SOCKET_PATH}"

sudo env \
  R2KERNEL_RESOURCE_SERVICE_SOCKET="${SOCKET_PATH}" \
  R2KERNEL_RESOURCE_SERVICE_SOCKET_MODE=0666 \
  "${RESOURCED}" --config "${CONFIG}" > "${LOG_PATH}" 2>&1 &
RESOURCED_PID=$!

for _ in $(seq 1 50); do
  if [[ -S "${SOCKET_PATH}" ]]; then
    break
  fi
  sleep 0.1
done
if [[ ! -S "${SOCKET_PATH}" ]]; then
  cat "${LOG_PATH}" >&2 || true
  echo "r2kernel-resourced did not create ${SOCKET_PATH}" >&2
  exit 1
fi

python3 "${SCRIPT_DIR}/make_package.py" \
  --out "${OUT_DIR}" \
  --device "${DEVICE}" \
  --repo-root "${REPO_ROOT}" \
  --expect-direct-block

R2KERNEL_RESOURCE_SERVICE_SOCKET="${SOCKET_PATH}" \
  "${LAUNCH}" run "${OUT_DIR}" \
  --config "${CONFIG}" \
  --detach \
  --json

for _ in $(seq 1 50); do
  if [[ -s "${RESULT_PATH}" ]]; then
    break
  fi
  sleep 0.1
done
if [[ ! -s "${RESULT_PATH}" ]]; then
  cat "${LOG_PATH}" >&2 || true
  echo "probe did not write ${RESULT_PATH}" >&2
  exit 1
fi

cat "${RESULT_PATH}"
python3 - "${RESULT_PATH}" <<'PY'
import json
import sys
path = sys.argv[1]
payload = json.load(open(path, "r", encoding="utf-8"))
if not payload.get("ok"):
    raise SystemExit(f"brokered TTY smoke failed: {json.dumps(payload, sort_keys=True)}")
PY
