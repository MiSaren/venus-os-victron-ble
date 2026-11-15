"""Night mode switch for Blue Smart AC chargers (opt-in: needs the charger's PIN).

For an AC charger with a PIN in pins.json, a com.victronenergy.switch service
with one toggle output, "Night mode", is published next to the charger
service; gui-v2 shows it in its switch pane, and Node-RED / VRM can use it.

Night mode can also change without us (VictronConnect, the MODE button, and
it ends by itself after 8 h), so the charger is read every 15 minutes and
after each switch. All Bluetooth work runs in a helper process
(python3 -m victronble.vesmart), one session at a time: a session takes a few
seconds, during which VictronConnect cannot connect to the charger.
"""

import json
import logging
import os
import signal
import time

import dbus

from vedbus import VeDbusService

from . import PROCESS_NAME, VERSION
from . import settings as st

log = logging.getLogger("control")

OUTPUT = "night"
OUTPUT_PATH = "/SwitchableOutput/" + OUTPUT
OUTPUT_NAME = "Night mode"
TYPE_TOGGLE = 1
FUNCTION_MANUAL = 2
STATUS_OFF = 0x00
STATUS_FAULT = 0x08
STATUS_ON = 0x09
DEVICE_STATE_CONNECTED = 0x100

POLL_INTERVAL = 900
FIRST_READ_DELAY = 5
RETRY_MIN = 60
RETRY_MAX = 900
HELPER_TIMEOUT = 90
KILL_GRACE = 10

# Output settings the user can change in gui-v2, kept in localsettings:
# switch path -> (device setting name, default)
OUTPUT_SETTINGS = {
    OUTPUT_PATH + "/Settings/CustomName": ("NightModeCustomName", ""),
    OUTPUT_PATH + "/Settings/Group": ("NightModeGroup", ""),
    OUTPUT_PATH + "/Settings/ShowUIControl": ("NightModeShowUI", 1),
}


class HelperRunner:
    """Runs helper sessions one at a time: there is one radio, and a charger
    takes one connection. submit(mac, op, callback) queues a session;
    callback(result) is called from the main loop when it ended."""

    def __init__(self, glib, command, pins_file, cwd=None, timeout=HELPER_TIMEOUT):
        self.glib = glib
        self.command = list(command)
        self.pins_file = pins_file
        self.cwd = cwd
        self.timeout = timeout
        self.queue = []  # [mac, op, callback]
        self.current = None

    def submit(self, mac, op, callback):
        for job in self.queue:
            if job[0] == mac:  # one queued session per charger; a switch beats a read
                if op != "read" or job[1] == "read":
                    job[1], job[2] = op, callback
                return
        self.queue.append([mac, op, callback])
        self._next()

    def busy(self):
        return self.current is not None

    def _next(self):
        if self.current is not None or not self.queue:
            return
        mac, op, callback = self.queue.pop(0)
        argv = self.command + ["--mac", mac, "--op", op, "--pins-file", self.pins_file]
        options = {"working_directory": self.cwd} if self.cwd else {}
        try:
            pid, _stdin, stdout, _stderr = self.glib.spawn_async(
                argv,
                flags=self.glib.SpawnFlags.DO_NOT_REAP_CHILD,
                standard_output=True,
                **options,
            )
        except Exception as e:
            log.warning("%s: cannot start the helper: %s", mac, e)
            self.glib.idle_add(self._finish_failed, callback, "cannot start the helper: %s" % e)
            return
        self.current = {
            "pid": pid,
            "mac": mac,
            "op": op,
            "callback": callback,
            "stdout": stdout,
            "started": time.monotonic(),
            "killed": False,
        }
        self.current["timer"] = self.glib.timeout_add_seconds(self.timeout, self._timed_out)
        self.glib.child_watch_add(self.glib.PRIORITY_DEFAULT, pid, self._exited, self.current)

    def _finish_failed(self, callback, error):
        callback({"ok": False, "error": error})
        self._next()
        return False

    def _timed_out(self):
        job = self.current
        if job is None:
            return False
        sig = signal.SIGKILL if job["killed"] else signal.SIGTERM
        log.warning(
            "%s: helper session took too long, %s", job["mac"], "killing" if job["killed"] else "stopping"
        )
        try:
            os.kill(job["pid"], sig)
        except OSError:
            pass
        job["killed"] = True
        job["timer"] = self.glib.timeout_add_seconds(KILL_GRACE, self._timed_out)
        return False

    def _exited(self, pid, status, job):
        self.glib.source_remove(job["timer"])
        output = b""
        try:
            while True:
                chunk = os.read(job["stdout"], 4096)
                if not chunk:
                    break
                output += chunk
        except OSError:
            pass
        os.close(job["stdout"])
        self.glib.spawn_close_pid(pid)
        result = None
        for line in reversed(output.decode("utf-8", "replace").splitlines()):
            try:
                result = json.loads(line)
                break
            except ValueError:
                continue
        if not isinstance(result, dict):
            result = {"ok": False, "error": "helper ended without a result (status %s)" % status}
        self.current = None
        try:
            job["callback"](result)
        except Exception:
            log.exception("handling the result of %s %s failed", job["mac"], job["op"])
        self._next()

    def stop(self):
        self.queue = []
        if self.current is not None:
            try:
                os.kill(self.current["pid"], signal.SIGTERM)
            except OSError:
                pass


class ChargerControl:
    """Night mode state of one charger. Pure logic: Bluetooth sessions go
    through the runner, on_change(control) reports every result."""

    def __init__(self, mac, runner, now, poll_interval=POLL_INTERVAL, on_change=None):
        self.mac = mac
        self.runner = runner
        self.poll_interval = poll_interval
        self.on_change = on_change or (lambda control: None)
        self.night = None  # last state read from the charger
        self.mode = None
        self.max_current = None
        self.error = None
        self.unsupported = False
        self.last_ok = None  # monotonic
        self.last_done = None  # wall clock, for 'victron-ble night'
        self.next_read = now + FIRST_READ_DELAY
        self.retry = RETRY_MIN
        self.requested = None  # switch state asked for and not done yet
        self.running = None  # op of the session in progress
        self.closed = False

    @property
    def state(self):
        """What the switch shows: the requested state while it is pending."""
        return self.requested if self.requested is not None else self.night

    @property
    def status(self):
        if self.error:
            return STATUS_FAULT
        return STATUS_ON if self.state else STATUS_OFF

    def tick(self, now):
        if self.closed or self.unsupported or self.running:
            return
        if self.requested is not None and self.requested != self.night:
            self._submit("night-on" if self.requested else "night-off")
        elif now >= self.next_read:
            self._submit("read")

    def request(self, on, now):
        self.requested = bool(on)
        self.tick(now)

    def close(self):
        self.closed = True

    def _submit(self, op):
        self.running = op
        self.runner.submit(self.mac, op, self._done)

    def _done(self, result):
        now = time.monotonic()
        op, self.running = self.running, None
        self.last_done = time.time()
        if self.closed:
            return
        if result.get("ok"):
            self.night = bool(result.get("night"))
            self.mode = result.get("mode")
            self.max_current = result.get("max_current")
            self.error = None
            self.last_ok = now
            self.retry = RETRY_MIN
            self.next_read = now + self.poll_interval
            if op != "read" and self.requested == self.night:
                self.requested = None
            log.info("%s: night mode %s (%s)", self.mac, "on" if self.night else "off", op)
        elif result.get("unsupported"):
            self.unsupported = True
            self.error = result.get("error") or "no night mode"
            log.warning("%s: %s; night mode control disabled", self.mac, self.error)
        else:
            self.error = result.get("error") or "failed"
            self.next_read = now + self.retry
            self.retry = min(self.retry * 2, RETRY_MAX)
            if op != "read":
                self.requested = None  # no blind retries of a switch: the GUI shows the fault
            log.warning("%s: %s failed: %s", self.mac, op, self.error)
        self.on_change(self)
        self.tick(now)

    def info(self, now):
        return {
            "night": self.night,
            "mode": self.mode,
            "max_current": self.max_current,
            "requested": self.requested,
            "busy": self.running is not None,
            "error": self.error,
            "unsupported": self.unsupported,
            "last_ok_ago": None if self.last_ok is None else round(now - self.last_ok),
            "next_read_in": None if self.running else max(0, round(self.next_read - now)),
            "last_done": self.last_done,
        }


class SwitchService:
    """com.victronenergy.switch.<id>: one toggle output for the night mode."""

    def __init__(
        self,
        name,
        instance,
        product_id,
        product_name,
        custom_name,
        output_settings,
        on_state,
        on_output_setting,
        on_custom_name,
        state,
        status,
    ):
        self.name = name
        self.instance = instance
        self._conn = dbus.bus.BusConnection(st.bus_type())
        self._conn.set_exit_on_disconnect(False)
        try:
            svc = VeDbusService(name, bus=self._conn, register=False)
            svc.add_path("/Mgmt/ProcessName", PROCESS_NAME)
            svc.add_path("/Mgmt/ProcessVersion", VERSION)
            svc.add_path("/Mgmt/Connection", "Bluetooth LE")
            svc.add_path("/DeviceInstance", instance)
            svc.add_path("/ProductId", product_id)
            svc.add_path("/ProductName", product_name)
            svc.add_path("/FirmwareVersion", None)
            svc.add_path("/HardwareVersion", None)
            svc.add_path("/Serial", None)
            svc.add_path("/Connected", 1)
            svc.add_path("/State", DEVICE_STATE_CONNECTED)
            svc.add_path(
                "/CustomName",
                custom_name or "",
                writeable=True,
                onchangecallback=lambda path, value: on_custom_name(self, value),
            )
            svc.add_path(OUTPUT_PATH + "/Name", OUTPUT_NAME)
            svc.add_path(
                OUTPUT_PATH + "/State",
                None if state is None else int(bool(state)),
                writeable=True,
                onchangecallback=lambda path, value: on_state(value),
            )
            svc.add_path(OUTPUT_PATH + "/Status", status)
            for path in sorted(OUTPUT_SETTINGS):
                svc.add_path(
                    path,
                    output_settings.get(path, OUTPUT_SETTINGS[path][1]),
                    writeable=True,
                    onchangecallback=on_output_setting,
                )
            svc.add_path(
                OUTPUT_PATH + "/Settings/Type",
                TYPE_TOGGLE,
                writeable=True,
                onchangecallback=lambda path, value: value == TYPE_TOGGLE,
            )
            svc.add_path(OUTPUT_PATH + "/Settings/ValidTypes", 1 << TYPE_TOGGLE)
            svc.add_path(OUTPUT_PATH + "/Settings/Function", FUNCTION_MANUAL)
            svc.add_path(OUTPUT_PATH + "/Settings/ValidFunctions", 1 << FUNCTION_MANUAL)
            svc.register()
        except Exception:
            self._conn.close()
            raise
        self._svc = svc

    def set_state(self, state, status):
        with self._svc as s:
            s[OUTPUT_PATH + "/State"] = None if state is None else int(bool(state))
            s[OUTPUT_PATH + "/Status"] = status

    def set_custom_name(self, name):
        self._svc["/CustomName"] = name

    def close(self):
        self._svc.__del__()
        self._conn.close()
