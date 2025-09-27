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
SERVICE_DIR_ABS="$(readlink -f "${SCRIPT_DIR}/service")"

if [ -L "${SERVICE_LINK}" ]; then
  # if it's a symlink, ensure it points to the right place
  CURRENT_TARGET="$(readlink -f "${SERVICE_LINK}")"
  if [ "${CURRENT_TARGET}" != "${SERVICE_DIR_ABS}" ]; then
    echo "Relinking ${SERVICE_LINK} -> ${SERVICE_DIR_ABS}"
    ln -snf "${SERVICE_DIR_ABS}" "${SERVICE_LINK}"
  else
    echo "Service link ${SERVICE_LINK} already points to ${SERVICE_DIR_ABS}."
  fi
elif [ -e "${SERVICE_LINK}" ]; then
  # exists but not a symlink — refuse to proceed
  echo "ERROR: ${SERVICE_LINK} exists and is not a symlink. Remove or rename it, then re-run install." >&2
  exit 1
else
  ln -s "${SERVICE_DIR_ABS}" "${SERVICE_LINK}"
  echo "Created service link ${SERVICE_LINK} -> ${SERVICE_DIR_ABS}"
fi


# ensure persistence across firmware updates
RC_LOCAL="/data/rc.local"
if [[ ! -f "${RC_LOCAL}" ]]; then
  printf "#!/bin/bash\n\n" > "${RC_LOCAL}"
  chmod 755 "${RC_LOCAL}"
fi
grep -qxF "${SCRIPT_DIR}/install.sh" "${RC_LOCAL}" || echo "${SCRIPT_DIR}/install.sh" >> "${RC_LOCAL}"

echo "Installed ${DAEMON_NAME}. supervise should (re)start it automatically."
