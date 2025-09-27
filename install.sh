#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
DAEMON_NAME="${SCRIPT_DIR##*/}"                    # service name == directory name
SERVICE_LINK="/service/${DAEMON_NAME}"
RUN_FILE="${SCRIPT_DIR}/service/run"
MAIN_PY="${SCRIPT_DIR}/dbus-shelly-em.py"          # main entrypoint

# sanity checks
if [[ ! -f "${MAIN_PY}" ]]; then
  echo "ERROR: ${MAIN_PY} not found. Expected main script 'dbus-shelly-em.py' in ${SCRIPT_DIR}." >&2
  exit 1
fi

# ensure helper scripts are executable
chmod 755 "${SCRIPT_DIR}/restart.sh" "${SCRIPT_DIR}/uninstall.sh"

# create/update run script with absolute path to main
mkdir -p "${SCRIPT_DIR}/service"
cat > "${RUN_FILE}" <<'SH'
#!/bin/sh
# daemontools run script: exec our Python program; stdout/stderr go to supervise
exec 2>&1
# NOTE: The install script rewrites the next line with the absolute path to the main script.
# PLACEHOLDER_MAIN=
SH
# append absolute exec line (python3)
echo "exec python3 \"${MAIN_PY}\"" >> "${RUN_FILE}"
chmod 755 "${RUN_FILE}"

# symlink service directory for supervise
if [[ -L "${SERVICE_LINK}" || -d "${SERVICE_LINK}" ]]; then
  echo "Service link ${SERVICE_LINK} already exists; leaving it in place."
else
  ln -s "${SCRIPT_DIR}/service" "${SERVICE_LINK}"
fi

# ensure persistence across firmware updates
RC_LOCAL="/data/rc.local"
if [[ ! -f "${RC_LOCAL}" ]]; then
  printf "#!/bin/bash\n\n" > "${RC_LOCAL}"
  chmod 755 "${RC_LOCAL}"
fi
grep -qxF "${SCRIPT_DIR}/install.sh" "${RC_LOCAL}" || echo "${SCRIPT_DIR}/install.sh" >> "${RC_LOCAL}"

echo "Installed ${DAEMON_NAME}. supervise should (re)start it automatically."
