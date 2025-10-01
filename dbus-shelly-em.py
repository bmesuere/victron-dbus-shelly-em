#!/usr/bin/env python

import math
import platform
import logging
import sys
import os
import time
from datetime import datetime
import requests  # HTTP GET
import configparser  # INI config
from gi.repository import GLib as gobject
from multiprocessing import Process

# Victron libs (velib_python)
# Use absolute path;
VIC_TRON_PATH = "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python"
if VIC_TRON_PATH not in sys.path:
    sys.path.insert(1, VIC_TRON_PATH)
from vedbus import VeDbusService

# Paths & constants
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")
LOG_FILE = os.path.join(BASE_DIR, "current.log")

# Victron / product constants
PRODUCT_ID_PVINVERTER = 0xA144
PRODUCT_ID_GRID = 45069
DEVICE_TYPE_ET340 = 345

# Networking
REQUEST_TIMEOUT_SECONDS = 1
REFRESH_INTERVAL_MS = 500


class DeviceAdapter(logging.LoggerAdapter):
    """Prefixes all log messages with a compact device tag.

    Example: "[grid:40@192.168.0.62]" or "[pv:41@192.168.0.63]".
    """

    def process(self, msg, kwargs):
        prefix = self.extra.get("prefix", "device")
        return f"[{prefix}] {msg}", kwargs


class DbusShellyEmService:
    def __init__(
        self,
        device_cfg,
        global_cfg,
        paths,
        dev_name: str,
        productname="Shelly EM",
        connection="Shelly EM HTTP JSON service",
    ):
        self.global_cfg = global_cfg
        self.device_cfg = device_cfg

        di_str = self.device_cfg.get("DeviceInstance", "").strip()
        if not di_str or not di_str.isdigit():
            logging.critical(
                f"Invalid or missing DeviceInstance for section '{dev_name}' — please set a unique integer"
            )
            sys.exit(1)
        deviceinstance = int(di_str)
        customname = self.device_cfg.get("CustomName", "Shelly EM")
        role = self.device_cfg.get("Role", "grid").strip().lower()

        allowed_roles = ["pvinverter", "grid"]
        if role in allowed_roles:
            servicename = f"com.victronenergy.{role}"
        else:
            logging.critical(
                f"Configured Role '{role}' is not in the allowed list {allowed_roles}"
            )
            sys.exit(1)

        productid = PRODUCT_ID_PVINVERTER if role == "pvinverter" else PRODUCT_ID_GRID

        # Reuse one HTTP session for all requests
        self.session = requests.Session()
        # timeouts are a (connect, read) tuple; see _getShellyData
        self._request_timeout = REQUEST_TIMEOUT_SECONDS

        # Shelly connection settings derived once
        host = self.device_cfg.get("Host", "").strip()
        # Device-scoped logger with a readable prefix (dev name, role, instance, host)
        tag = f"{dev_name}:{role}:{deviceinstance}@{host or '-'}"
        self.log = DeviceAdapter(logging.getLogger(__name__), {"prefix": tag})
        if not host:
            self.log.critical("[device:*] section requires Host")
            sys.exit(1)
        self.shelly_base = f"http://{host}"
        username = self.device_cfg.get("Username", "").strip()
        password = self.device_cfg.get("Password", "")
        self.auth = (username, password) if username else None

        # Read selected channel (0 or 1) for Shelly EM
        self.channel_idx = self._getSelectedChannel()

        self._dbusservice = VeDbusService(f"{servicename}.http_{deviceinstance:02d}")
        self._paths = paths

        self.log.debug(f"{servicename} /DeviceInstance = {deviceinstance}")

        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path(
            "/Mgmt/ProcessVersion",
            f"Unknown version, and running on Python {platform.python_version()}",
        )
        self._dbusservice.add_path("/Mgmt/Connection", connection)

        # Create the mandatory objects
        self._dbusservice.add_path("/DeviceInstance", deviceinstance)
        self._dbusservice.add_path("/ProductId", productid)
        self._dbusservice.add_path("/DeviceType", DEVICE_TYPE_ET340)
        self._dbusservice.add_path("/ProductName", productname)
        self._dbusservice.add_path("/CustomName", customname)
        self._dbusservice.add_path("/Latency", None)
        self._dbusservice.add_path("/FirmwareVersion", 0.3)
        self._dbusservice.add_path("/HardwareVersion", 0)
        self._dbusservice.add_path("/Connected", 1)
        self._dbusservice.add_path("/Role", role)
        self._dbusservice.add_path("/Position", self._getShellyPosition())
        self._dbusservice.add_path("/Serial", self._getShellySerial())
        self._dbusservice.add_path("/UpdateIndex", 0)

        # add path values to dbus
        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path,
                settings["initial"],
                gettextcallback=settings["textformat"],
                writeable=True,
                onchangecallback=self._handlechangedvalue,
            )

        self._lastUpdate = 0
        self._periodic_id = None
        self._signoflife_id = None

        # Set up the main loop with a staggered start to avoid hammering the same Shelly when multiple devices share a host
        jitter_ms = (
            (deviceinstance * 53 + self.channel_idx * 17) % REFRESH_INTERVAL_MS
        ) or 50
        gobject.timeout_add(jitter_ms, self._start_periodic)

        # schedule sign-of-life only if > 0 minutes
        sol_minutes = self._getSignOfLifeInterval()
        if sol_minutes > 0 and self._signoflife_id is None:
            try:
                from gi.repository import GLib

                self._signoflife_id = GLib.timeout_add_seconds(
                    sol_minutes * 60, self._signOfLife
                )
            except Exception:
                # fallback to millisecond API if seconds API is unavailable
                self._signoflife_id = gobject.timeout_add(
                    sol_minutes * 60 * 1000, self._signOfLife
                )
            self.log.info(
                f"Sign-of-life every {sol_minutes} minute(s) (timer id {self._signoflife_id})"
            )
        elif sol_minutes <= 0:
            self.log.info("Sign-of-life disabled (SignOfLifeLog <= 0)")

    # ----------------------
    # Config helpers
    # ----------------------
    def _getSignOfLifeInterval(self):
        value = self.global_cfg.get("SignOfLifeLog", "0").strip()
        return int(value or 0)

    def _getShellyPosition(self):
        value = self.device_cfg.get("Position", "0").strip()
        return int(value or 0)

    def _getSelectedChannel(self):
        try:
            value = self.device_cfg.get("Channel", "0").strip()
            channel = int(value or 0)
        except Exception:
            channel = 0
        if channel not in (0, 1):
            self.log.warning(f"Invalid Channel '{value}' in config; defaulting to 0")
            channel = 0
        return channel

    # ----------------------
    # Shelly & DBus helpers
    # ----------------------
    def _calc_current(self, power: float, reactive: float, voltage: float) -> float:
        """Compute RMS current from active power (W), reactive power (var) and voltage (V).
        Uses S = sqrt(P^2 + Q^2) to avoid division by pf≈0; I = S / V.
        Returns 0.0 if voltage ≤ 0 or inputs are not finite.
        """
        try:
            p = float(power or 0.0)
            q = float(reactive or 0.0)
            v = float(voltage or 0.0)
        except Exception:
            return 0.0
        if not math.isfinite(p) or not math.isfinite(q) or not math.isfinite(v):
            return 0.0
        if v <= 0.0:
            return 0.0
        S = math.hypot(p, q)  # sqrt(p*p + q*q)
        return S / v

    def _getShellyData(self):
        URL = self.shelly_base + "/status"

        def _do_get():
            return self.session.get(
                url=URL,
                timeout=self._request_timeout,
                auth=self.auth,
                headers={"Accept": "application/json"},
            )

        try:
            r = _do_get()
        except requests.exceptions.ReadTimeout:
            # one quick retry on read timeout
            r = _do_get()

        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            self.log.critical(
                f"HTTP error from Shelly at {self.shelly_base}/status: {e}", exc_info=e
            )
            raise

        try:
            meter_data = r.json()
        except ValueError as e:
            raise ValueError(f"Invalid JSON from Shelly at {URL}: {e}")

        if not isinstance(meter_data, dict):
            raise ValueError(f"Unexpected JSON structure from Shelly at {URL}")

        return meter_data

    def _getShellySerial(self):
        meter_data = self._getShellyData()
        if not meter_data.get("mac"):
            raise ValueError("Response does not contain 'mac' attribute")
        return meter_data["mac"]

    def _start_periodic(self):
        if self._periodic_id is None:
            self._periodic_id = gobject.timeout_add(REFRESH_INTERVAL_MS, self._update)
            self.log.info(
                f"Registered periodic update every {REFRESH_INTERVAL_MS} ms (timer id {self._periodic_id})"
            )
        else:
            self.log.warning(
                f"Periodic timer already registered (id {self._periodic_id}); skipping duplicate"
            )
        return False

    def _signOfLife(self):
        # Pretty-print last update timestamp with local time and age
        if self._lastUpdate:
            dt = datetime.fromtimestamp(self._lastUpdate)
            age = time.time() - self._lastUpdate
            self.log.info("--- Start: sign of life ---")
            self.log.info(
                f"Last _update() call: {dt:%Y-%m-%d %H:%M:%S} ({int(age)}s ago)"
            )
        else:
            self.log.info("--- Start: sign of life ---")
            self.log.info("Last _update() call: never")
        self.log.info(f"Last '/Ac/Power': {self._dbusservice['/Ac/Power']}")
        self.log.info(f"Last '/Ac/Voltage': {self._dbusservice['/Ac/Voltage']}")
        self.log.info(f"Last '/Ac/Current': {self._dbusservice['/Ac/Current']}")
        self.log.info(
            f"Last '/Ac/Energy/Forward': {self._dbusservice['/Ac/Energy/Forward']}"
        )
        self.log.info(
            f"Last '/Ac/Energy/Reverse': {self._dbusservice['/Ac/Energy/Reverse']}"
        )
        self.log.info("--- End: sign of life ---")
        return True

    def _update(self):
        try:
            meter_data = self._getShellyData()
            ch = self.channel_idx
            em_list = meter_data.get("emeters", [])
            if not isinstance(em_list, list) or len(em_list) <= ch:
                raise ValueError(f"Shelly status has no emeters[{ch}]")
            em = em_list[ch]

            # Bail out if Shelly marks this sample invalid
            if not bool(em.get("is_valid", True)):
                self.log.warning(
                    f"Shelly channel {ch} reports is_valid=false; skipping update"
                )
                return True

            p = float(em.get("power", 0) or 0)
            v = float(em.get("voltage", 0) or 0)
            q = float(em.get("reactive", 0) or 0)

            # Shelly doesn't report current, so we calculate it
            i = self._calc_current(p, q, v)

            total_kwh = float(em.get("total", 0) or 0) / 1000.0
            total_returned_kwh = float(em.get("total_returned", 0) or 0) / 1000.0

            # Send data to DBus
            self._dbusservice["/Ac/Power"] = p
            self._dbusservice["/Ac/Voltage"] = v
            self._dbusservice["/Ac/Current"] = i

            self._dbusservice["/Ac/L1/Power"] = p
            self._dbusservice["/Ac/L1/Voltage"] = v
            self._dbusservice["/Ac/L1/Current"] = i

            self._dbusservice["/Ac/Energy/Forward"] = total_kwh
            self._dbusservice["/Ac/Energy/Reverse"] = total_returned_kwh

            # Increment UpdateIndex - to show that new data is available, wraps at 256
            self._dbusservice["/UpdateIndex"] = (
                self._dbusservice["/UpdateIndex"] + 1
            ) % 256

            self.log.debug(f"Consumption (/Ac/Power): {p}")
            self.log.debug(f"Voltage (/Ac/Voltage): {v}")
            self.log.debug(f"Current (/Ac/Current): {i}")
            self.log.debug(f"Forward (/Ac/Energy/Forward): {total_kwh}")
            self.log.debug(f"Reverse (/Ac/Energy/Reverse): {total_returned_kwh}")
            self.log.debug("---")

            self._lastUpdate = time.time()
        except (
            ValueError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            ConnectionError,
        ) as e:
            self.log.critical(
                f"Error getting data from Shelly at {self.shelly_base} - check network or device status. "
                f"Setting power values to 0. Details: {e}",
                exc_info=e,
            )
            self._dbusservice["/Ac/L1/Power"] = 0
            self._dbusservice["/Ac/Voltage"] = 0
            self._dbusservice["/Ac/Current"] = 0
            self._dbusservice["/Ac/L1/Voltage"] = 0
            self._dbusservice["/Ac/L1/Current"] = 0
            self._dbusservice["/Ac/Power"] = 0
            self._dbusservice["/UpdateIndex"] = (
                self._dbusservice["/UpdateIndex"] + 1
            ) % 256
        except Exception as e:
            self.log.critical("Unhandled exception in _update", exc_info=e)
        # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
        return True

    def _handlechangedvalue(self, path, value):
        self.log.debug(f"someone else updated {path} to {value}")
        return True  # accept the change


def load_config(path):
    cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cp.read(path)
    if not cp.has_section("global"):
        logging.critical("Missing [global] section in config")
        sys.exit(1)
    device_sections = [s for s in cp.sections() if s.lower().startswith("device:")]
    if not device_sections:
        logging.critical("At least one [device:*] section is required")
        sys.exit(1)
    devices = [(name, cp[name]) for name in device_sections]
    return cp["global"], devices


def getLogLevel():
    cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cp.read(CONFIG_PATH)
    level_str = (
        cp["global"].get("LogLevel", "INFO") if cp.has_section("global") else "INFO"
    )
    if isinstance(level_str, int):
        return level_str
    try:
        return int(level_str)
    except (TypeError, ValueError):
        pass
    name = str(level_str).strip().upper()
    level = None
    if hasattr(logging, "getLevelNamesMapping"):
        level = logging.getLevelNamesMapping().get(name)
    if level is None:
        level = getattr(logging, name, None)
    if isinstance(level, int):
        return level
    return logging.INFO


def run_device(name, device_cfg, global_cfg):
    """Spawned in a separate process to avoid D-Bus root object path ('/') conflicts.
    Each process creates its own VeDbusService and GLib main loop.
    """
    logging.basicConfig(
        format="%(asctime)s,%(msecs)d %(processName)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=getLogLevel(),
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(),
        ],
        force=True,
    )

    from dbus.mainloop.glib import DBusGMainLoop

    DBusGMainLoop(set_as_default=True)

    _kwh = lambda p, v: (str(round(v, 2)) + " kWh")
    _a = lambda p, v: (str(round(v, 1)) + " A")
    _w = lambda p, v: (str(round(v, 1)) + " W")
    _v = lambda p, v: (str(round(v, 1)) + " V")

    role = device_cfg.get("Role", "grid").strip().lower()
    logging.info(
        f"Starting device '{name}' (role={role}, instance={device_cfg.get('DeviceInstance')}, host={device_cfg.get('Host')})"
    )

    svc = DbusShellyEmService(
        device_cfg=device_cfg,
        global_cfg=global_cfg,
        paths={
            "/Ac/Energy/Forward": {"initial": 0, "textformat": _kwh},
            "/Ac/Energy/Reverse": {"initial": 0, "textformat": _kwh},
            "/Ac/Power": {"initial": 0, "textformat": _w},
            "/Ac/Current": {"initial": 0, "textformat": _a},
            "/Ac/Voltage": {"initial": 0, "textformat": _v},
            "/Ac/L1/Voltage": {"initial": 0, "textformat": _v},
            "/Ac/L1/Current": {"initial": 0, "textformat": _a},
            "/Ac/L1/Power": {"initial": 0, "textformat": _w},
        },
        dev_name=name,
    )

    logging.info("Connected to dbus; entering gobject.MainLoop()")
    mainloop = gobject.MainLoop()
    mainloop.run()


def main():
    logging.basicConfig(
        format="%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=getLogLevel(),
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(),
        ],
    )

    try:
        logging.info("Start")

        global_cfg, devices = load_config(CONFIG_PATH)

        # Validate unique DeviceInstance values
        seen_instances = set()
        for name, d in devices:
            inst_str = d.get("DeviceInstance", "").strip()
            if not inst_str or not inst_str.isdigit():
                logging.critical(
                    f"Missing or invalid DeviceInstance in section '{name}' — please set a unique integer."
                )
                sys.exit(1)
            inst = int(inst_str)
            if inst in seen_instances:
                logging.critical(
                    "Duplicate DeviceInstance %d across sections; ensure uniqueness.",
                    inst,
                )
                sys.exit(1)
            seen_instances.add(inst)

        # Spawn one process per device to avoid D-Bus root object ('/') conflicts
        procs = []
        for name, d in devices:
            p = Process(target=run_device, args=(name, d, global_cfg), daemon=True)
            p.start()
            procs.append(p)
            logging.info(f"Spawned process {p.name} (pid={p.pid}) for device '{name}'")

        for p in procs:
            p.join()

    except (
        ValueError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    ) as e:
        logging.critical(f"Error in main: {e}")
    except Exception as e:
        logging.critical("Unhandled exception in main", exc_info=e)


if __name__ == "__main__":
    main()
