#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
DAEMON_NAME="${SCRIPT_DIR##*/}"
SERVICE_LINK="/service/${DAEMON_NAME}"
RUN_FILE="${SCRIPT_DIR}/service/run"
MAIN_PY="${SCRIPT_DIR}/dbus-shelly-em.py"

# Stop service
if command -v svc >/dev/null 2>&1 && [[ -L "${SERVICE_LINK}" ]]; then
  svc -d "${SERVICE_LINK}" || true
else
  pkill -f -- "python3 .*${MAIN_PY}" || true
fi

# Remove supervise symlink
if [[ -L "${SERVICE_LINK}" || -d "${SERVICE_LINK}" ]]; then
  rm -f "${SERVICE_LINK}"
fi

# Disable run script to prevent future autostart
if [[ -f "${RUN_FILE}" ]]; then
  chmod a-x "${RUN_FILE}"
fi

echo "Uninstalled ${DAEMON_NAME}. (Code remains in ${SCRIPT_DIR}.)"
