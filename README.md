# victron-dbus-shelly-em
Integrate Shelly EM meters with [Victron Energy Venus OS](https://github.com/victronenergy/venus).

This service exposes one **D-Bus service per Shelly role** (grid or PV inverter) and supports **multiple devices** — including multiple channels on the **same** Shelly — running side by side.

---

## Highlights
- **Multi-device config**: define any number of Shellys via `[device:*]` sections.
- **Grid or PV** roles: publishes to `com.victronenergy.grid.*` or `com.victronenergy.pvinverter.*`.
- **Shelly EM channels**: pick `Channel = 0` or `1` (two CTs on a single Shelly EM).
- **Per-device log prefix**: every log line is tagged like `[device:grid:40@192.168.0.62]`.
- **Robust polling**: short jitter on start to avoid hammering one Shelly; single retry on read timeout.
- **One process per device**: avoids D-Bus root object (`/`) collisions.
- **Python 3 only**.

---

## Requirements
- Venus OS / GX device with root access.
- `python3` available on the target.
- Shelly EM reachable over the LAN (HTTP `/status`).

> Tested against Shelly EM firmware with `/status` JSON. This service currently reads a **single channel** per `[device:*]` (you can add multiple device sections pointing to the same host with different `Channel`).

---

## Installation

Replace `<gx-ip>` with your Venus OS device IP and adjust the local folder name if needed. Copy the project directory to your GX device, e.g. under `/data/victron-dbus-shelly-em`, and run the installer.

```sh
# on your PC
git clone https://github.com/bmesuere/victron-dbus-shelly-em/victron-dbus-shelly-em.git
cd victron-dbus-shelly-em

# create the target dir on the GX
ssh root@<gx-ip> 'mkdir -p /data/victron-dbus-shelly-em'

# explicitly copy only the needed files (no .git)
scp -r \
  dbus-shelly-em.py \
  config.ini \
  install.sh \
  restart.sh \
  uninstall.sh \
  readme.md \
  root@<gx-ip>:/data/victron-dbus-shelly-em/
```

Then, on the GX device:

```sh
cd /data/victron-dbus-shelly-em
chmod +x install.sh restart.sh uninstall.sh
./install.sh
```

What `install.sh` does:
- Writes/updates `service/run` with an **absolute path** and `python3` exec.
- Links `/service/<folder-name>` → `service/` so daemontools supervises it.
- Ensures persistence by appending `install.sh` to `/data/rc.local`.

To restart after config changes:
```sh
/data/victron-dbus-shelly-em/restart.sh
```

To remove the service (code remains on disk):
```sh
/data/victron-dbus-shelly-em/uninstall.sh
```

---

## Configuration
Create or edit `/data/victron-dbus-shelly-em/config.ini` using the **new format only**.

### Minimal example (grid + PV; two channels on one Shelly)
```ini
[global]
LogLevel = INFO            ; DEBUG, INFO, WARNING, ERROR, CRITICAL (names or numbers)
SignOfLifeLog = 1          ; minutes between info dumps to the log (0 disables)

[device:grid]
Host = 192.168.0.62
Username =
Password =
Channel = 0                ; 0 or 1
Role = grid                ; grid | pvinverter
DeviceInstance = 40        ; must be unique across all devices
CustomName = Shelly Grid
Position = 1               ; 0=AC, 1=AC-Out 1, 2=AC-Out 2

[device:pv]
Host = 192.168.0.62
Username =
Password =
Channel = 1
Role = pvinverter
DeviceInstance = 41
CustomName = Shelly PV
Position = 0
```

### Field reference
| Section | Key             | Meaning |
|--------:|-----------------|---------|
| `[global]` | `LogLevel`      | Logging level by name or number. |
| `[global]` | `SignOfLifeLog` | Every N minutes, log current values and last update time. `0` disables. |
| `[device:*]` | `Host`        | IP/hostname of the Shelly. |
| `[device:*]` | `Username`/`Password` | HTTP basic auth if configured on the Shelly (leave blank if not). |
| `[device:*]` | `Channel`     | Channel index on EM (0/1). For 3EM, configure one section per desired Lx channel. |
| `[device:*]` | `Role`        | `grid` or `pvinverter` (controls D-Bus namespace). |
| `[device:*]` | `DeviceInstance` | Unique integer per device; used in the D-Bus service name. |
| `[device:*]` | `CustomName`  | Shown in D-Bus. |
| `[device:*]` | `Position`    | Victron “position” value (usually only meaningful for PV inverters). |

> Inline comments `; ...` and `# ...` are supported in values. Numeric fields must remain numeric after stripping comments.

---

## How it works (short version)
- On start, the launcher reads `config.ini`, validates unique `DeviceInstance` values, and spawns **one process per `[device:*]`**.
- Each process:
  - Creates one D-Bus service: `com.victronenergy.<role>.http_<DeviceInstance>`.
  - Polls `http://<Host>/status` on a 500 ms interval (staggered start), with a **single retry** on read timeout.
  - Publishes `/Ac/Power`, `/Ac/Voltage`, `/Ac/Current`, and energy counters; current is derived via `I = sqrt(P² + Q²) / V`.
  - Logs with a per-device prefix.

---

## Troubleshooting
- **“Can't register the object-path handler for '/'…”**
  You’re trying to run multiple services in one process. This repo **spawns one process per device** to avoid that. Use the included `install.sh` and don’t wrap it in another supervisor that runs multiple instances in a single process.

- **Read timeouts** when two sections point to the same `Host`:
  Normal if they fire together; mitigated by the staggered start and one quick retry. If your Wi‑Fi is weak, increase interval (code constant `REFRESH_INTERVAL_MS`).

- **Duplicate DeviceInstance**:
  The service exits with an explicit error. Use unique integers per section.

- **No logs**:
  Check `/data/victron-dbus-shelly-em/current.log` and console. `LogLevel` can be set to `DEBUG` for verboser output.

- **Auth issues**:
  If the Shelly has HTTP auth enabled, set `Username` and `Password`. Leave them blank otherwise.

---

## Development notes
- Code is Python 3 only and uses the Venus OS `vedbus` library.
- D-Bus service names follow `com.victronenergy.<role>.http_<DeviceInstance:02d>`.
- The code accepts inline comments in INI values (e.g. `DeviceInstance = 40 ; unique`).

---

## Credits
Originally inspired by community projects around Venus OS smart meter integrations. This version is a ground-up refactor for multi-device configs, clean logging, and reliability on Shelly EM/3EM hardware.
