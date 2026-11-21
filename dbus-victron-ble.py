#!/usr/bin/env python3
"""dbus-victron-ble: Victron instant readout devices on the Venus OS D-Bus.

Reads the Bluetooth LE advertisements of SmartShunt / BMV battery monitors,
SmartSolar solar chargers and Blue Smart / Phoenix Smart AC chargers and
publishes them as regular com.victronenergy services (GUI, VRM, MQTT,
Node-RED), without a cable and without pairing. The data is read only.

The advertisements are encrypted; a device is published once its key is set:
    victron-ble key <MAC> <KEY>     (key from VictronConnect: Product info >
                                     Instant readout via Bluetooth > Show)

Optionally the night mode of a Blue Smart charger can be switched (a switch in
the gui-v2 switch pane); that needs the charger's Bluetooth PIN:
    victron-ble control <MAC> on
"""

import argparse
import logging
import os
import shlex
import signal
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(1, os.path.join(HERE, "ext", "velib_python"))
sys.path.insert(1, HERE)

import dbus  # noqa: E402
import dbus.mainloop.glib  # noqa: E402
from gi.repository import GLib  # noqa: E402

from victronble import VERSION, aes, control  # noqa: E402
from victronble import settings as st  # noqa: E402
from victronble.hci import Listener  # noqa: E402
from victronble.manager import Manager  # noqa: E402

log = logging.getLogger("main")

STATUS_FILE = "/run/dbus-victron-ble/status.json"


class Replay:
    """Feeds advertisements from a file instead of (or in addition to) the
    radio, for testing. Lines: <delay seconds> <address> <rssi> <AD hex>"""

    def __init__(self, path, callback, loop=False):
        self.items = []
        with open(path) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    delay, address, rssi, data = line.split()
                    self.items.append((float(delay), address.upper(), int(rssi), bytes.fromhex(data)))
        self.callback = callback
        self.loop = loop
        self.index = 0

    def start(self):
        if self.items:
            self._schedule()

    def _schedule(self):
        GLib.timeout_add(max(0, int(self.items[self.index][0] * 1000)), self._fire)

    def _fire(self):
        _delay, address, rssi, data = self.items[self.index]
        self.callback(address, rssi, data)
        self.index += 1
        if self.index >= len(self.items):
            if not self.loop:
                log.info("replay finished")
                return False
            self.index = 0
        self._schedule()
        return False


def connect_bus(attempts=30):
    """The bus, waiting a while for it: early at boot it may not be up yet."""
    for attempt in range(attempts):
        try:
            return dbus.SessionBus() if st.bus_type() == dbus.bus.BUS_SESSION else dbus.SystemBus()
        except dbus.exceptions.DBusException as e:
            if attempt == attempts - 1:
                raise
            log.info("waiting for D-Bus: %s", e.get_dbus_message() or e)
            time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description="Victron instant readout (Bluetooth) devices on D-Bus")
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="remove a device after this many seconds without data (default 300)",
    )
    parser.add_argument(
        "--state-file",
        default=os.path.join(HERE, "state.json"),
        help="persistent counters (default: state.json next to this script)",
    )
    parser.add_argument(
        "--status-file",
        default=STATUS_FILE,
        help="status for 'victron-ble status' (default %s)" % STATUS_FILE,
    )
    parser.add_argument(
        "--scan-watchdog",
        type=int,
        default=120,
        help="enable scanning when nothing was received for this many seconds (0 = never, default 120)",
    )
    parser.add_argument(
        "--pins-file",
        default=os.path.join(HERE, "pins.json"),
        help="Bluetooth PINs of controlled chargers (default: pins.json next to this script)",
    )
    parser.add_argument(
        "--control-poll",
        type=int,
        default=control.POLL_INTERVAL,
        help="read the night mode of controlled chargers this often, seconds (default %d)"
        % control.POLL_INTERVAL,
    )
    parser.add_argument("--no-control", action="store_true", help="never connect to a device (no night mode)")
    parser.add_argument("--control-helper", help=argparse.SUPPRESS)  # tests: replaces the vesmart helper
    parser.add_argument("--replay", metavar="FILE", help="feed advertisements from FILE (testing)")
    parser.add_argument("--replay-loop", action="store_true", help="repeat the replay file forever")
    parser.add_argument("--dcsource-name-delay", type=float, default=10, help=argparse.SUPPRESS)  # tests
    parser.add_argument("--no-hci", action="store_true", help="do not listen to Bluetooth")
    parser.add_argument("-d", "--debug", action="store_true")
    parser.add_argument("-V", "--version", action="version", version=VERSION)
    args = parser.parse_args()

    logging.basicConfig(
        format="%(levelname)s %(name)s: %(message)s", level=logging.DEBUG if args.debug else logging.INFO
    )
    log.info("dbus-victron-ble %s, AES via %s", VERSION, aes.backend())

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = connect_bus()

    runner = None
    if not args.no_control:
        helper = (
            shlex.split(args.control_helper)
            if args.control_helper
            else [sys.executable, "-u", "-m", "victronble.vesmart"]
        )
        runner = control.HelperRunner(GLib, helper, args.pins_file, cwd=HERE)

    manager = Manager(
        bus,
        timeout=args.timeout,
        state_file=args.state_file,
        status_file=args.status_file,
        mirror_name_delay=args.dcsource_name_delay,
        runner=runner,
        pins_file=args.pins_file,
        control_poll=args.control_poll,
    )

    def apply_timezone():
        """Local time = the Venus time zone setting (the system clock runs in UTC);
        the days of the solar history are calendar days in local time."""
        try:
            tz = manager.settings.get("/Settings/System/TimeZone")
        except dbus.exceptions.DBusException:
            return
        if tz and os.environ.get("TZ") != tz and os.path.exists(os.path.join("/usr/share/zoneinfo", tz)):
            os.environ["TZ"] = tz
            time.tzset()
            log.info("time zone %s", tz)

    def load_config():
        if not manager.reload_config():
            return True  # localsettings not up yet (boot): try again
        apply_timezone()
        configured = manager.configured()
        log.info(
            "%d device(s) with a key%s",
            len(configured),
            (": " + ", ".join(configured)) if configured else "; see 'victron-ble key'",
        )
        return False

    if load_config():
        GLib.timeout_add_seconds(5, load_config)
    manager.check_ble_sensors()

    reload_pending = []

    def reload_settings():
        reload_pending.clear()
        manager.reload_config()
        return False

    def on_setting_changed(changes, path=None):
        if not path or not path.startswith("/Settings/Devices/" + st.DEVICE_PREFIX):
            return
        if path.rsplit("/", 1)[-1] in ("Key", "CustomName", "DcSource") and not reload_pending:
            reload_pending.append(GLib.timeout_add(500, reload_settings))

    bus.add_signal_receiver(
        on_setting_changed,
        signal_name="PropertiesChanged",
        dbus_interface=st.BUSITEM,
        bus_name=st.SETTINGS_SERVICE,
        path_keyword="path",
    )

    listener = None
    if not args.no_hci:
        listener = Listener(manager.on_advertisement, GLib, scan_watchdog=args.scan_watchdog)
        manager.listener = listener
        listener.refresh()
        if not listener.controllers():
            log.warning("no Bluetooth controller yet, waiting for one")

    if args.replay:
        Replay(args.replay, manager.on_advertisement, loop=args.replay_loop).start()

    def tick():
        manager.tick()
        if listener is not None and not listener.controllers():
            listener.refresh()  # at boot the controller may come up after us
        return True

    def every_minute():
        manager.reload_config()
        apply_timezone()
        manager.check_ble_sensors()
        if listener is not None:
            listener.refresh()
        return True

    GLib.timeout_add_seconds(5, tick)
    GLib.timeout_add_seconds(60, every_minute)
    manager.write_status()

    mainloop = GLib.MainLoop()

    def stop():
        log.info("stopping")
        mainloop.quit()
        return False

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, stop)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, stop)
    mainloop.run()

    manager.shutdown()
    if listener is not None:
        listener.close()


if __name__ == "__main__":
    main()
