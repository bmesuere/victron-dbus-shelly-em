#!/usr/bin/env python
# vim: ts=2 sw=2 et

import math
import platform
import logging
import sys
import os
import time
import requests  # HTTP GET
import configparser  # INI config
from gi.repository import GLib as gobject

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
REQUEST_TIMEOUT_SECONDS = 5
REFRESH_INTERVAL_MS = 500


class DbusShellyEmService:
    def __init__(
        self, paths, productname="Shelly EM", connection="Shelly EM HTTP JSON service"
    ):
        self.config = self._getConfig()
        deviceinstance = int(self.config["DEFAULT"]["DeviceInstance"])  # required
        customname = self.config["DEFAULT"].get("CustomName", "Shelly EM")
        role = self.config["DEFAULT"].get("Role", "grid")

        allowed_roles = ["pvinverter", "grid"]
        if role in allowed_roles:
            servicename = f"com.victronenergy.{role}"
        else:
            logging.error(
                "Configured Role '%s' is not in the allowed list %s",
                role,
                allowed_roles,
            )
            sys.exit(1)

        if role == "pvinverter":
            productid = PRODUCT_ID_PVINVERTER
        else:
            productid = PRODUCT_ID_GRID

        # Reuse one HTTP session for all requests
        self.session = requests.Session()
        self._request_timeout = REQUEST_TIMEOUT_SECONDS

        # Shelly connection settings derived once
        host = self.config["ONPREMISE"]["Host"].strip()
        self.shelly_base = f"http://{host}"
        username = self.config["ONPREMISE"].get("Username", "").strip()
        password = self.config["ONPREMISE"].get("Password", "")
        self.auth = (username, password) if username else None

        # Read selected channel (0 or 1) for Shelly EM
        self.channel_idx = self._getSelectedChannel()

        self._dbusservice = VeDbusService(f"{servicename}.http_{deviceinstance:02d}")
        self._paths = paths

        logging.debug(f"{servicename} /DeviceInstance = {deviceinstance}")

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
        self._dbusservice.add_path(
            "/Position", self._getShellyPosition()
        )  # normally only needed for pvinverter
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

        # last update
        self._lastUpdate = 0

        # add _update function 'timer'
        gobject.timeout_add(REFRESH_INTERVAL_MS, self._update)

        # add _signOfLife 'timer' to get feedback in log every 5minutes
        gobject.timeout_add(self._getSignOfLifeInterval() * 60 * 1000, self._signOfLife)

    def _getShellySerial(self):
        meter_data = self._getShellyData()  # request/parse Shelly status

        if not meter_data["mac"]:
            raise ValueError("Response does not contain 'mac' attribute")

        serial = meter_data["mac"]
        return serial

    def _getConfig(self):
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH)
        return config

    def _getSignOfLifeInterval(self):
        value = self.config["DEFAULT"].get("SignOfLifeLog", "0").strip()
        return int(value or 0)

    def _getShellyPosition(self):
        value = self.config["DEFAULT"].get("Position", "0").strip()
        return int(value or 0)

    def _getSelectedChannel(self):
        try:
            value = self.config["ONPREMISE"].get("Channel", "0").strip()
            channel = int(value or 0)
        except Exception:
            channel = 0
        if channel not in (0, 1):
            logging.warning("Invalid Channel '%s' in config; defaulting to 0", value)
            channel = 0
        return channel

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

        r = self.session.get(
            url=URL,
            timeout=self._request_timeout,
            auth=self.auth,
            headers={"Accept": "application/json"},
        )
        # Raise for HTTP errors (4xx/5xx)
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            logging.critical(
                "HTTP error from Shelly at %s/status: %s",
                self.shelly_base,
                e,
                exc_info=e,
            )
            raise

        try:
            meter_data = r.json()
        except ValueError as e:
            raise ValueError(f"Invalid JSON from Shelly at {URL}: {e}")

        if not isinstance(meter_data, dict):
            raise ValueError(f"Unexpected JSON structure from Shelly at {URL}")

        return meter_data

    def _signOfLife(self):
        logging.info("--- Start: sign of life ---")
        logging.info(f"Last _update() call: {self._lastUpdate}")
        logging.info(f"Last '/Ac/Power': {self._dbusservice['/Ac/Power']}")
        logging.info(f"Last '/Ac/Voltage': {self._dbusservice['/Ac/Voltage']}")
        logging.info(f"Last '/Ac/Current': {self._dbusservice['/Ac/Current']}")
        logging.info(
            f"Last '/Ac/Energy/Forward': {self._dbusservice['/Ac/Energy/Forward']}"
        )
        logging.info(
            f"Last '/Ac/Energy/Reverse': {self._dbusservice['/Ac/Energy/Reverse']}"
        )
        logging.info("--- End: sign of life ---")
        return True

    def _update(self):
        try:
            # Fetch data from Shelly EM
            meter_data = self._getShellyData()

            # Select configured EM channel (0 or 1) and use it as L1
            ch = self.channel_idx
            em_list = meter_data.get("emeters", [])
            if not isinstance(em_list, list) or len(em_list) <= ch:
                raise ValueError(f"Shelly status has no emeters[{ch}]")
            em = em_list[ch]

            # Bail out if Shelly marks this sample invalid
            if not bool(em.get("is_valid", True)):
                logging.warning(
                    "Shelly channel %d reports is_valid=false; skipping update", ch
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

            logging.debug(f"Consumption (/Ac/Power): {p}")
            logging.debug(f"Voltage (/Ac/Voltage): {v}")
            logging.debug(f"Current (/Ac/Current): {i}")
            logging.debug(f"Forward (/Ac/Energy/Forward): {total_kwh}")
            logging.debug(f"Reverse (/Ac/Energy/Reverse): {total_returned_kwh}")
            logging.debug("---")

            self._lastUpdate = time.time()
        except (
            ValueError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            ConnectionError,
        ) as e:
            logging.critical(
                f"Error getting data from Shelly at {self.shelly_base} - check network or device status. Setting power values to 0. Details: {e}",
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
            logging.critical("Unhandled exception in _update", exc_info=e)

        # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
        return True

    def _handlechangedvalue(self, path, value):
        logging.debug(f"someone else updated {path} to {value}")
        return True  # accept the change


def getLogLevel():
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    logLevelString = config["DEFAULT"]["LogLevel"]

    if logLevelString:
        level = logging.getLevelName(logLevelString)
    else:
        level = logging.INFO

    return level


def main():
    # configure logging
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

        from dbus.mainloop.glib import DBusGMainLoop

        # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
        DBusGMainLoop(set_as_default=True)

        # formatting
        _kwh = lambda p, v: (str(round(v, 2)) + " kWh")
        _a = lambda p, v: (str(round(v, 1)) + " A")
        _w = lambda p, v: (str(round(v, 1)) + " W")
        _v = lambda p, v: (str(round(v, 1)) + " V")

        # start our main-service
        pvac_output = DbusShellyEmService(
            paths={
                "/Ac/Energy/Forward": {
                    "initial": 0,
                    "textformat": _kwh,
                },  # energy bought from the grid
                "/Ac/Energy/Reverse": {
                    "initial": 0,
                    "textformat": _kwh,
                },  # energy sold to the grid
                "/Ac/Power": {"initial": 0, "textformat": _w},
                "/Ac/Current": {"initial": 0, "textformat": _a},
                "/Ac/Voltage": {"initial": 0, "textformat": _v},
                "/Ac/L1/Voltage": {"initial": 0, "textformat": _v},
                "/Ac/L1/Current": {"initial": 0, "textformat": _a},
                "/Ac/L1/Power": {"initial": 0, "textformat": _w},
            }
        )

        logging.info("Connected to dbus; entering gobject.MainLoop() (event-based)")
        mainloop = gobject.MainLoop()
        mainloop.run()
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
