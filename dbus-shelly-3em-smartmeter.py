#!/usr/bin/env python
# vim: ts=2 sw=2 et

# Standard library
import platform
import logging
import sys
import os
import time
import requests  # HTTP GET
import configparser  # INI config
from gi.repository import GLib as gobject

# Victron libs (velib_python)
VIC_TRON_PATH = "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python"
if VIC_TRON_PATH not in sys.path:
    sys.path.insert(1, VIC_TRON_PATH)
from vedbus import VeDbusService

# Config & HTTP constants
CONFIG_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "config.ini")
DEFAULT_TIMEOUT = 5
SESSION = requests.Session()


class DbusShelly3emService:
    def __init__(
        self, paths, productname="Shelly 3EM", connection="Shelly 3EM HTTP JSON service"
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
                "Configured Role '%s' is not allowed. Allowed: %s", role, allowed_roles
            )
            exit(1)

        if role == "pvinverter":
            productid = 0xA144
        else:
            productid = 45069

        self._dbusservice = VeDbusService(
            "{}.http_{:02d}".format(servicename, deviceinstance)
        )
        self._paths = paths

        logging.debug("%s /DeviceInstance = %d", servicename, deviceinstance)

        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path(
            "/Mgmt/ProcessVersion",
            "Unknown version, running on Python " + platform.python_version(),
        )
        self._dbusservice.add_path("/Mgmt/Connection", connection)

        # Create the mandatory objects
        self._dbusservice.add_path("/DeviceInstance", deviceinstance)
        self._dbusservice.add_path("/ProductId", productid)
        self._dbusservice.add_path(
            "/DeviceType", 345
        )  # Found on https://www.sascha-curth.de/projekte/005_Color_Control_GX.html#experiment - should be an ET340 Energy Meter
        self._dbusservice.add_path("/ProductName", productname)
        self._dbusservice.add_path("/CustomName", customname)
        self._dbusservice.add_path("/Latency", None)
        self._dbusservice.add_path("/FirmwareVersion", 0.3)
        self._dbusservice.add_path("/HardwareVersion", 0)
        self._dbusservice.add_path("/Connected", 1)
        self._dbusservice.add_path("/Role", role)
        self._dbusservice.add_path(
            "/Position", self._getShellyPosition()
        )  # Normally only needed for pvinverter
        self._dbusservice.add_path("/Serial", self._getShellySerial())
        self._dbusservice.add_path("/UpdateIndex", 0)

        # Add path values to dbus
        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path,
                settings["initial"],
                gettextcallback=settings["textformat"],
                writeable=True,
                onchangecallback=self._handlechangedvalue,
            )

        # Last update
        self._lastUpdate = 0

        # Add _update function 'timer'
        gobject.timeout_add(500, self._update)  # pause 500ms before the next request

        # Add _signOfLife 'timer' to get feedback in log every 5minutes
        gobject.timeout_add(self._getSignOfLifeInterval() * 60 * 1000, self._signOfLife)

    def _getShellySerial(self):
        meter_data = self._getShellyData()

        if not meter_data["mac"]:
            raise ValueError("Response does not contain 'mac' attribute")

        serial = meter_data["mac"]
        return serial

    def _getConfig(self):
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH)
        # Basic validation — keep behavior the same but fail fast on obviously broken configs
        if "DEFAULT" not in config:
            raise ValueError("Missing [DEFAULT] section in %s" % CONFIG_PATH)
        access_type = config["DEFAULT"].get("AccessType", "").strip()
        if access_type == "OnPremise":
            if "ONPREMISE" not in config or not config["ONPREMISE"].get("Host"):
                raise ValueError(
                    "AccessType OnPremise requires [ONPREMISE] with Host in %s"
                    % CONFIG_PATH
                )
        return config

    def _getSignOfLifeInterval(self):
        value = self.config["DEFAULT"].get("SignOfLifeLog", "0").strip()
        return int(value or 0)

    def _getShellyPosition(self):
        value = self.config["DEFAULT"].get("Position", "0").strip()
        return int(value or 0)

    def _getShellyStatusUrl(self):
        accessType = self.config["DEFAULT"]["AccessType"]
        if accessType == "OnPremise":
            username = self.config["ONPREMISE"].get("Username", "")
            password = self.config["ONPREMISE"].get("Password", "")
            host = self.config["ONPREMISE"]["Host"]
            if username or password:
                URL = "http://%s:%s@%s/status" % (username, password, host)
                URL = URL.replace(":@", "")
            else:
                URL = "http://%s/status" % host
        else:
            raise ValueError(
                "AccessType %s is not supported"
                % (self.config["DEFAULT"]["AccessType"])
            )
        return URL

    def _getShellyData(self):
        URL = self._getShellyStatusUrl()
        meter_r = SESSION.get(url=URL, timeout=DEFAULT_TIMEOUT)

        meter_r.raise_for_status()
        meter_data = meter_r.json()

        if not meter_data:
            raise ValueError("Converting response to JSON failed")

        return meter_data

    def _signOfLife(self):
        logging.info("--- Start: sign of life ---")
        logging.info("Last _update() call: %s", self._lastUpdate)
        logging.info("Last '/Ac/Power': %s", self._dbusservice["/Ac/Power"])
        logging.info("--- End: sign of life ---")
        return True

    def _update(self):
        try:
            # Get data from Shelly 3EM
            meter_data = self._getShellyData()

            try:
                remapL1 = int(self.config["ONPREMISE"].get("L1Position", "1"))
            except (KeyError, ValueError):
                remapL1 = 1

            # Clamp to valid range (1..3) for 3-phase meters
            if remapL1 not in (1, 2, 3):
                remapL1 = 1

            if remapL1 > 1 and len(meter_data.get("emeters", [])) >= 3:
                old_l1 = meter_data["emeters"][0]
                meter_data["emeters"][0] = meter_data["emeters"][remapL1 - 1]
                meter_data["emeters"][remapL1 - 1] = old_l1

            # Send data to DBus
            self._dbusservice["/Ac/Power"] = meter_data["total_power"]
            self._dbusservice["/Ac/L1/Voltage"] = meter_data["emeters"][0]["voltage"]
            self._dbusservice["/Ac/L2/Voltage"] = meter_data["emeters"][1]["voltage"]
            self._dbusservice["/Ac/L3/Voltage"] = meter_data["emeters"][2]["voltage"]
            self._dbusservice["/Ac/L1/Current"] = meter_data["emeters"][0]["current"]
            self._dbusservice["/Ac/L2/Current"] = meter_data["emeters"][1]["current"]
            self._dbusservice["/Ac/L3/Current"] = meter_data["emeters"][2]["current"]
            self._dbusservice["/Ac/L1/Power"] = meter_data["emeters"][0]["power"]
            self._dbusservice["/Ac/L2/Power"] = meter_data["emeters"][1]["power"]
            self._dbusservice["/Ac/L3/Power"] = meter_data["emeters"][2]["power"]
            self._dbusservice["/Ac/L1/Energy/Forward"] = (
                meter_data["emeters"][0]["total"] / 1000
            )
            self._dbusservice["/Ac/L2/Energy/Forward"] = (
                meter_data["emeters"][1]["total"] / 1000
            )
            self._dbusservice["/Ac/L3/Energy/Forward"] = (
                meter_data["emeters"][2]["total"] / 1000
            )
            self._dbusservice["/Ac/L1/Energy/Reverse"] = (
                meter_data["emeters"][0]["total_returned"] / 1000
            )
            self._dbusservice["/Ac/L2/Energy/Reverse"] = (
                meter_data["emeters"][1]["total_returned"] / 1000
            )
            self._dbusservice["/Ac/L3/Energy/Reverse"] = (
                meter_data["emeters"][2]["total_returned"] / 1000
            )
            # Aggregate total energy from phase counters
            self._dbusservice["/Ac/Energy/Forward"] = (
                self._dbusservice["/Ac/L1/Energy/Forward"]
                + self._dbusservice["/Ac/L2/Energy/Forward"]
                + self._dbusservice["/Ac/L3/Energy/Forward"]
            )
            self._dbusservice["/Ac/Energy/Reverse"] = (
                self._dbusservice["/Ac/L1/Energy/Reverse"]
                + self._dbusservice["/Ac/L2/Energy/Reverse"]
                + self._dbusservice["/Ac/L3/Energy/Reverse"]
            )

            # Logging
            logging.debug(
                "House Consumption (/Ac/Power): %s", self._dbusservice["/Ac/Power"]
            )
            logging.debug(
                "House Forward (/Ac/Energy/Forward): %s",
                self._dbusservice["/Ac/Energy/Forward"],
            )
            logging.debug(
                "House Reverse (/Ac/Energy/Reverse): %s",
                self._dbusservice["/Ac/Energy/Reverse"],
            )
            logging.debug("---")

            # Increment UpdateIndex - to show that new data is available an wrap
            self._dbusservice["/UpdateIndex"] = (
                self._dbusservice["/UpdateIndex"] + 1
            ) % 256

            # Update lastupdate vars
            self._lastUpdate = time.time()
        except (
            ValueError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            ConnectionError,
        ) as e:
            logging.critical(
                "Error getting data from Shelly - check network or Shelly status. Setting power values to 0. Details: %s",
                e,
                exc_info=e,
            )
            self._dbusservice["/Ac/L1/Power"] = 0
            self._dbusservice["/Ac/L2/Power"] = 0
            self._dbusservice["/Ac/L3/Power"] = 0
            self._dbusservice["/Ac/Power"] = 0
            self._dbusservice["/UpdateIndex"] = (
                self._dbusservice["/UpdateIndex"] + 1
            ) % 256
        except Exception as e:
            logging.critical("Error at %s", "_update", exc_info=e)

        # Return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
        return True

    def _handlechangedvalue(self, path, value):
        logging.debug("someone else updated %s to %s", path, value)
        return True  # Accept the change


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
    # Configure logging
    logging.basicConfig(
        format="%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=getLogLevel(),
        handlers=[
            logging.FileHandler(
                "%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))
            ),
            logging.StreamHandler(),
        ],
    )

    try:
        logging.info("Start")
        logging.info("Using config file: %s", CONFIG_PATH)

        from dbus.mainloop.glib import DBusGMainLoop

        # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
        DBusGMainLoop(set_as_default=True)

        # Formatting
        _kwh = lambda p, v: (str(round(v, 2)) + " kWh")
        _a = lambda p, v: (str(round(v, 1)) + " A")
        _w = lambda p, v: (str(round(v, 1)) + " W")
        _v = lambda p, v: (str(round(v, 1)) + " V")

        # Start our main-service
        pvac_output = DbusShelly3emService(
            paths={
                "/Ac/Energy/Forward": {
                    "initial": 0,
                    "textformat": _kwh,
                },  # Energy bought from the grid
                "/Ac/Energy/Reverse": {
                    "initial": 0,
                    "textformat": _kwh,
                },  # Energy sold to the grid
                "/Ac/Power": {"initial": 0, "textformat": _w},
                "/Ac/Current": {"initial": 0, "textformat": _a},
                "/Ac/Voltage": {"initial": 0, "textformat": _v},
                "/Ac/L1/Voltage": {"initial": 0, "textformat": _v},
                "/Ac/L2/Voltage": {"initial": 0, "textformat": _v},
                "/Ac/L3/Voltage": {"initial": 0, "textformat": _v},
                "/Ac/L1/Current": {"initial": 0, "textformat": _a},
                "/Ac/L2/Current": {"initial": 0, "textformat": _a},
                "/Ac/L3/Current": {"initial": 0, "textformat": _a},
                "/Ac/L1/Power": {"initial": 0, "textformat": _w},
                "/Ac/L2/Power": {"initial": 0, "textformat": _w},
                "/Ac/L3/Power": {"initial": 0, "textformat": _w},
                "/Ac/L1/Energy/Forward": {"initial": 0, "textformat": _kwh},
                "/Ac/L2/Energy/Forward": {"initial": 0, "textformat": _kwh},
                "/Ac/L3/Energy/Forward": {"initial": 0, "textformat": _kwh},
                "/Ac/L1/Energy/Reverse": {"initial": 0, "textformat": _kwh},
                "/Ac/L2/Energy/Reverse": {"initial": 0, "textformat": _kwh},
                "/Ac/L3/Energy/Reverse": {"initial": 0, "textformat": _kwh},
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
