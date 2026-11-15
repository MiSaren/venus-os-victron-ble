#!/usr/bin/env python3
"""Minimal stand-in for Venus' localsettings, for tests on a private bus.

Implements what dbus-victron-ble and victron-ble use: AddSettings on '/',
GetValue/SetValue/GetText on settings, GetValue on groups (dict of relative
paths), PropertiesChanged signals and unique ClassAndVrmInstance values.

The real localsettings must not be started a second time on a Venus device:
at startup it checks /data/conf/vncpassword.txt and may reset the password.
"""

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

BUSITEM = "com.victronenergy.BusItem"
SETTINGS = "com.victronenergy.Settings"


class Settings(dbus.service.FallbackObject):
    def __init__(self, bus):
        dbus.service.FallbackObject.__init__(self, bus, "/")
        self.values = {}  # absolute path -> value

    def _children(self, path):
        prefix = path.rstrip("/") + "/"
        return {p[len(prefix) :]: v for p, v in self.values.items() if p.startswith(prefix)}

    def _unique_instance(self, path, value):
        if not path.endswith("/ClassAndVrmInstance"):
            return value
        cls, _, instance = str(value).partition(":")
        instance = int(instance)
        taken = {v for p, v in self.values.items() if p.endswith("/ClassAndVrmInstance") and p != path}
        while "%s:%d" % (cls, instance) in taken:
            instance += 1
        return dbus.String("%s:%d" % (cls, instance))

    def _set(self, path, value):
        value = self._unique_instance(path, value)
        if self.values.get(path) != value:
            self.values[path] = value
            # emitted at '/' + rel_path: drop the leading slash
            self.PropertiesChanged({"Value": value, "Text": str(value)}, rel_path=path[1:])
        return value

    @dbus.service.method(SETTINGS, in_signature="aa{sv}", out_signature="aa{sv}", rel_path_keyword="rel")
    def AddSettings(self, definitions, rel=None):
        result = []
        for d in definitions:
            path = str(d["path"])
            if path not in self.values:
                self._set(path, d["default"])
            result.append({"path": path, "error": dbus.Int32(0), "value": self.values[path]})
        return result

    @dbus.service.method(BUSITEM, out_signature="v", rel_path_keyword="rel")
    def GetValue(self, rel=None):
        if rel in self.values:
            return self.values[rel]
        children = self._children(rel)
        if children:
            return dbus.Dictionary(children, signature="sv")
        raise dbus.exceptions.DBusException("no such path", name="org.freedesktop.DBus.Error.UnknownObject")

    @dbus.service.method(BUSITEM, out_signature="v", rel_path_keyword="rel")
    def GetText(self, rel=None):
        return str(self.GetValue(rel))

    @dbus.service.method(BUSITEM, in_signature="v", out_signature="i", rel_path_keyword="rel")
    def SetValue(self, value, rel=None):
        if rel not in self.values:
            return -1
        self._set(rel, value)
        return 0

    @dbus.service.signal(BUSITEM, signature="a{sv}", rel_path_keyword="rel_path")
    def PropertiesChanged(self, changes, rel_path=None):
        pass


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    Settings(bus)
    name = dbus.service.BusName("com.victronenergy.settings", bus)  # noqa: F841 (keeps the name)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
