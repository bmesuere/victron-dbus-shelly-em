#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
DAEMON_NAME="${SCRIPT_DIR##*/}"
SERVICE_LINK="/service/${DAEMON_NAME}"
MAIN_PY="${SCRIPT_DIR}/dbus-shelly-em.py"

# Prefer daemontools if available
if command -v svc >/dev/null 2>&1 && [[ -L "${SERVICE_LINK}" ]]; then
  svc -t "${SERVICE_LINK}" || true
else
  # Fallback: kill any python3 processes running this specific main script
  pkill -f -- "python3 .*${MAIN_PY}" || true
fi

echo "Restart signal sent for ${DAEMON_NAME}."
