#!/usr/bin/env python
# vim: ts=2 sw=2 et

import platform
import logging
import sys
import os
import time
import requests  # HTTP GET
import configparser  # INI config
from gi.repository import GLib as gobject

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

# Victron libs (velib_python)
# Use absolute path;
VIC_TRON_PATH = "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python"
if VIC_TRON_PATH not in sys.path:
    sys.path.insert(1, VIC_TRON_PATH)
from vedbus import VeDbusService


class DbusShellyEmService:
    def __init__(
        self, paths, productname="Shelly EM", connection="Shelly EM HTTP JSON service"
    ):
        self.config = self._getConfig()
        deviceinstance = int(self.config["DEFAULT"]["DeviceInstance"])
        customname = self.config["DEFAULT"]["CustomName"]
        role = self.config["DEFAULT"]["Role"]

        allowed_roles = ["pvinverter", "grid"]
        if role in allowed_roles:
            servicename = "com.victronenergy." + role
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
        self.shelly_base = "http://%s" % host
        username = self.config["ONPREMISE"].get("Username", "").strip()
        password = self.config["ONPREMISE"].get("Password", "")
        self.auth = (username, password) if username else None

        # Read selected channel (0 or 1) for Shelly EM
        self.channel_idx = self._getSelectedChannel()

        self._dbusservice = VeDbusService(
            "{}.http_{:02d}".format(servicename, deviceinstance)
        )
        self._paths = paths

        logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path(
            "/Mgmt/ProcessVersion",
            "Unknown version, and running on Python " + platform.python_version(),
        )
        self._dbusservice.add_path("/Mgmt/Connection", connection)

        # Create the mandatory objects
        self._dbusservice.add_path("/DeviceInstance", deviceinstance)
        self._dbusservice.add_path("/ProductId", productid)
        self._dbusservice.add_path(
            "/DeviceType", DEVICE_TYPE_ET340
        )  # found on https://www.sascha-curth.de/projekte/005_Color_Control_GX.html#experiment - should be an ET340 Energy Meter
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
        gobject.timeout_add(500, self._update)  # pause 500ms before the next request

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
            raise ValueError("Invalid JSON from Shelly at %s: %s" % (URL, e))

        if not isinstance(meter_data, dict):
            raise ValueError("Unexpected JSON structure from Shelly at %s" % (URL))

        return meter_data

    def _signOfLife(self):
        logging.info("--- Start: sign of life ---")
        logging.info("Last _update() call: %s" % (self._lastUpdate))
        logging.info("Last '/Ac/Power': %s" % (self._dbusservice["/Ac/Power"]))
        logging.info("--- End: sign of life ---")
        return True

    def _update(self):
        try:
            # fetch data from Shelly EM
            meter_data = self._getShellyData()

            # Select configured EM channel (0 or 1) and use it as L1
            ch = self.channel_idx
            em_list = meter_data.get("emeters", [])
            if not isinstance(em_list, list) or len(em_list) <= ch:
                raise ValueError("Shelly status has no emeters[%d]" % ch)
            em = em_list[ch]

            # send data to DBus
            self._dbusservice["/Ac/Power"] = float(em.get("power", 0) or 0)
            self._dbusservice["/Ac/Voltage"] = float(em.get("voltage", 0) or 0)
            self._dbusservice["/Ac/Current"] = float(em.get("current", 0) or 0)

            self._dbusservice["/Ac/L1/Voltage"] = float(em.get("voltage", 0) or 0)
            self._dbusservice["/Ac/L1/Current"] = float(em.get("current", 0) or 0)
            self._dbusservice["/Ac/L1/Power"] = float(em.get("power", 0) or 0)

            self._dbusservice["/Ac/L1/Energy/Forward"] = (
                float(em.get("total", 0) or 0) / 1000.0
            )
            self._dbusservice["/Ac/L1/Energy/Reverse"] = (
                float(em.get("total_returned", 0) or 0) / 1000.0
            )

            # Old version
            # self._dbusservice['/Ac/Energy/Forward'] = self._dbusservice['/Ac/L1/Energy/Forward'] + self._dbusservice['/Ac/L2/Energy/Forward'] + self._dbusservice['/Ac/L3/Energy/Forward']
            # self._dbusservice['/Ac/Energy/Reverse'] = self._dbusservice['/Ac/L1/Energy/Reverse'] + self._dbusservice['/Ac/L2/Energy/Reverse'] + self._dbusservice['/Ac/L3/Energy/Reverse']

            # New Version - from xris99
            # Calc = 60min * 60 sec / 0.500 (refresh interval of 500ms) * 1000
            if self._dbusservice["/Ac/Power"] > 0:
                self._dbusservice["/Ac/Energy/Forward"] = self._dbusservice[
                    "/Ac/Energy/Forward"
                ] + (self._dbusservice["/Ac/Power"] / (60 * 60 / 0.5 * 1000))
            if self._dbusservice["/Ac/Power"] < 0:
                self._dbusservice["/Ac/Energy/Reverse"] = self._dbusservice[
                    "/Ac/Energy/Reverse"
                ] + (self._dbusservice["/Ac/Power"] * -1 / (60 * 60 / 0.5 * 1000))

            # logging
            logging.debug(
                "House Consumption (/Ac/Power): %s" % (self._dbusservice["/Ac/Power"])
            )
            logging.debug(
                "House Forward (/Ac/Energy/Forward): %s"
                % (self._dbusservice["/Ac/Energy/Forward"])
            )
            logging.debug(
                "House Reverse (/Ac/Energy/Reverse): %s"
                % (self._dbusservice["/Ac/Energy/Reverse"])
            )
            logging.debug("---")

            # increment UpdateIndex - to show that new data is available an wrap
            self._dbusservice["/UpdateIndex"] = (
                self._dbusservice["/UpdateIndex"] + 1
            ) % 256

            # update lastupdate vars
            self._lastUpdate = time.time()
        except (
            ValueError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            ConnectionError,
        ) as e:
            logging.critical(
                "Error getting data from Shelly at %s - check network or device status. Setting power values to 0. Details: %s",
                self.shelly_base,
                e,
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
            logging.critical("Error at %s", "_update", exc_info=e)

        # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
        return True

    def _handlechangedvalue(self, path, value):
        logging.debug("someone else updated %s to %s" % (path, value))
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
                "/Ac/L1/Energy/Forward": {"initial": 0, "textformat": _kwh},
                "/Ac/L1/Energy/Reverse": {"initial": 0, "textformat": _kwh},
            }
        )

        logging.info(
            "Connected to dbus, and switching over to gobject.MainLoop() (= event based)"
        )
        mainloop = gobject.MainLoop()
        mainloop.run()
    except (
        ValueError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    ) as e:
        logging.critical("Error in main type %s", str(e))
    except Exception as e:
        logging.critical("Error at %s", "main", exc_info=e)


if __name__ == "__main__":
    main()
