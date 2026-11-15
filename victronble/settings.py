"""Venus OS localsettings (com.victronenergy.settings) access.

Per device settings live where dbus-ble-sensors keeps the settings of the
Victron devices it supports itself:

    /Settings/Devices/victron_<mac>/Key                   32 hex digits
    /Settings/Devices/victron_<mac>/CustomName            name shown in GUI / VRM
    /Settings/Devices/victron_<mac>/ClassAndVrmInstance   e.g. battery:20

so that keys, names and VRM instances stay the same should a future Venus OS
release support these record types natively.
"""

import logging
import os
import string

import dbus

log = logging.getLogger("settings")

SETTINGS_SERVICE = "com.victronenergy.settings"
BUSITEM = "com.victronenergy.BusItem"
SETTINGS_IFACE = "com.victronenergy.Settings"
DEVICE_PREFIX = "victron_"
DEFAULT_INSTANCE = 20  # same default as dbus-ble-sensors
CALL_TIMEOUT = 10


def bus_type():
    """Session bus when one is set up (tests), the system bus otherwise; the
    same rule as velib_python and localsettings use."""
    return dbus.bus.BUS_SESSION if "DBUS_SESSION_BUS_ADDRESS" in os.environ else dbus.bus.BUS_SYSTEM


def normalize_mac(text):
    """'aa:bb:cc:dd:ee:ff', 'AABBCCDDEEFF', 'aa-bb-...' -> 'AA:BB:CC:DD:EE:FF' or None."""
    if text is None:
        return None
    hexdigits = str(text).strip().replace(":", "").replace("-", "").replace(" ", "")
    if len(hexdigits) != 12 or any(c not in string.hexdigits for c in hexdigits):
        return None
    hexdigits = hexdigits.upper()
    return ":".join(hexdigits[i : i + 2] for i in range(0, 12, 2))


def device_id(mac):
    return DEVICE_PREFIX + mac.replace(":", "").lower()


def device_path(mac, name):
    return "/Settings/Devices/%s/%s" % (device_id(mac), name)


def unwrap(value):
    if isinstance(value, dbus.Array):
        return None if len(value) == 0 else [unwrap(v) for v in value]
    if isinstance(
        value,
        (dbus.Int16, dbus.Int32, dbus.Int64, dbus.UInt16, dbus.UInt32, dbus.UInt64, dbus.Byte, dbus.Boolean),
    ):
        return int(value)
    if isinstance(value, dbus.Double):
        return float(value)
    if isinstance(value, (dbus.String, dbus.ObjectPath)):
        return str(value)
    if isinstance(value, dbus.Dictionary):
        return dict((unwrap(k), unwrap(v)) for k, v in value.items())
    return value


class Settings:
    def __init__(self, bus):
        self.bus = bus

    def _obj(self, path):
        return self.bus.get_object(SETTINGS_SERVICE, path, introspect=False)

    def device_settings(self):
        """{mac: {'Key': ..., 'CustomName': ..., ...}} of all victron_* devices."""
        try:
            values = self._obj("/Settings/Devices").GetValue(dbus_interface=BUSITEM, timeout=CALL_TIMEOUT)
        except dbus.exceptions.DBusException as e:
            if e.get_dbus_name() in (
                "org.freedesktop.DBus.Error.UnknownObject",
                "org.freedesktop.DBus.Error.UnknownMethod",
            ):
                return {}
            raise
        result = {}
        for rel, value in unwrap(values).items():
            devid, _, name = rel.partition("/")
            if not devid.startswith(DEVICE_PREFIX) or not name or "/" in name:
                continue
            mac = normalize_mac(devid[len(DEVICE_PREFIX) :])
            if mac is not None:
                result.setdefault(mac, {})[name] = value
        return result

    def add(self, path, default):
        """Create a setting (no-op when it exists); returns its current value."""
        if isinstance(default, int):
            default = dbus.Int32(default)
        elif isinstance(default, float):
            default = dbus.Double(default)
        else:
            default = dbus.String(default)
        result = self.bus.call_blocking(
            SETTINGS_SERVICE,
            "/",
            SETTINGS_IFACE,
            "AddSettings",
            "aa{sv}",
            [[{"path": dbus.String(path), "default": default}]],
            timeout=CALL_TIMEOUT,
        )
        r = result[0]
        if int(r.get("error", -1)) != 0:
            raise RuntimeError("AddSettings %s failed with error %s" % (path, int(r.get("error", -1))))
        return unwrap(r.get("value"))

    def get(self, path):
        return unwrap(self._obj(path).GetValue(dbus_interface=BUSITEM, timeout=CALL_TIMEOUT))

    def set(self, path, value):
        if not isinstance(value, (int, float)):
            value = dbus.String(value)
        return int(self._obj(path).SetValue(value, dbus_interface=BUSITEM, timeout=CALL_TIMEOUT))

    def set_device_setting(self, mac, name, value):
        """Create (if needed) and set /Settings/Devices/victron_<mac>/<name>."""
        path = device_path(mac, name)
        self.add(path, "" if isinstance(value, str) else type(value)())
        if self.set(path, value) != 0:
            raise RuntimeError("SetValue %s rejected" % path)

    def device_instance(self, mac, role, default=DEFAULT_INSTANCE):
        """VRM device instance via ClassAndVrmInstance; localsettings hands out
        a free instance when the default one is taken."""
        path = device_path(mac, "ClassAndVrmInstance")
        value = self.add(path, "%s:%d" % (role, default))
        cls, _, instance = str(value).partition(":")
        if cls != role:
            # The device used to send another record type: re-classify it
            self.set(path, "%s:%d" % (role, default))
            cls, _, instance = str(self.get(path)).partition(":")
        return int(instance)
