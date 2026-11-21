#!/usr/bin/env python3
"""End-to-end test on a private D-Bus: real Venus localsettings + dbus-victron-ble
fed with synthetic, encrypted advertisements (--replay). Nothing touches the
system bus, so it is safe on a live Venus OS device.

    tests/integration_test.py              (with tests/fake_settings.py)
    tests/integration_test.py --localsettings path/to/localsettings.py

Never point --localsettings at a Venus device's own localsettings while on that
device: a second instance checks /data/conf/vncpassword.txt at startup and may
reset the password. Use the fake there (the default); test against the real
localsettings on a development machine.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import advgen  # noqa: E402
from victronble import aes, records  # noqa: E402

SHUNT, MPPT, IP22 = "C0:FF:EE:00:00:01", "C0:FF:EE:00:00:02", "C0:FF:EE:00:00:03"
STRANGER, WRONG = "C0:FF:EE:00:00:04", "C0:FF:EE:00:00:05"
KEYS = {
    SHUNT: aes.parse_key("0102030405060708090a0b0c0d0e0f10"),
    MPPT: aes.parse_key("1112131415161718191a1b1c1d1e1f20"),
    IP22: aes.parse_key("2122232425262728292a2b2c2d2e2f30"),
    STRANGER: aes.parse_key("3132333435363738393a3b3c3d3e3f40"),
    WRONG: aes.parse_key("4142434445464748494a4b4c4d4e4f50"),
}
REPLAY_SECONDS = 30
TIMEOUT = 4

BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:dir=%s</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""

failures = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        failures.append(what)


def wait_for(fn, timeout=15.0, interval=0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            result = fn()
            if result:
                return result
        except Exception:
            pass
        time.sleep(interval)
    return None


def write_replay(path):
    """Adverts every 0.25 s for REPLAY_SECONDS; values change every second."""
    lines = []
    steps = int(REPLAY_SECONDS / 0.25)
    for n in range(steps):
        second = n // 4
        soc = 80.0 - 0.1 * second
        shunt = advgen.battery_monitor(
            voltage=13.2,
            current=-4.25,
            soc=soc,
            consumed_ah=-20.5,
            ttg_min=600,
            aux_mode=records.AUX_STARTER,
            aux_raw=1262,
        )
        mppt = advgen.solar_charger(
            state=3, voltage=13.4, current=7.5, yield_today_kwh=0.40 + 0.01 * second, pv_w=104, load_a=None
        )
        ip22 = advgen.ac_charger(state=5, outputs=[(13.8, 1.2)], temperature=24, ac_current=None)
        adverts = [
            (SHUNT, 0xA389, records.BATTERY_MONITOR, shunt, KEYS[SHUNT]),
            (MPPT, 0xA053, records.SOLAR_CHARGER, mppt, KEYS[MPPT]),
            (IP22, 0xA330, records.AC_CHARGER, ip22, KEYS[IP22]),
            (STRANGER, 0xA38A, records.BATTERY_MONITOR, shunt, KEYS[STRANGER]),
            (WRONG, 0xA389, records.BATTERY_MONITOR, shunt, KEYS[WRONG]),
        ]
        for i, (mac, pid, rt, payload, key) in enumerate(adverts):
            mfg = advgen.manufacturer_data(pid, rt, 100 + second, key, payload)
            lines.append(
                "%.3f %s -%d %s" % (0.25 if i == 0 else 0, mac, 60 + i, advgen.advertisement(mfg).hex())
            )
    with open(path, "w") as f:
        f.write("# synthetic adverts\n" + "\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--localsettings", help="real localsettings.py (development machine only!)")
    parser.add_argument("--keep", action="store_true", help="keep the temp directory")
    args = parser.parse_args()

    tmp = tempfile.mkdtemp(prefix="victron-ble-test-")
    procs = []
    try:
        conf = os.path.join(tmp, "bus.conf")
        with open(conf, "w") as f:
            f.write(BUS_CONFIG % tmp)
        bus_proc = subprocess.Popen(
            ["dbus-daemon", "--config-file=" + conf, "--print-address", "--nofork"],
            stdout=subprocess.PIPE,
            text=True,
        )
        procs.append(bus_proc)
        address = bus_proc.stdout.readline().strip()
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = address
        print("private bus: %s" % address)

        os.makedirs(os.path.join(tmp, "conf"))
        if args.localsettings:
            cmd = [sys.executable, args.localsettings, "--path=" + os.path.join(tmp, "conf"), "--no-delay"]
        else:
            cmd = [sys.executable, os.path.join(HERE, "fake_settings.py")]
        print("settings: %s" % (args.localsettings or "fake_settings.py"))
        procs.append(
            subprocess.Popen(
                cmd, stdout=open(os.path.join(tmp, "localsettings.log"), "w"), stderr=subprocess.STDOUT
            )
        )

        import dbus

        bus = dbus.SessionBus()

        def get(service, path):
            return bus.get_object(service, path, introspect=False).GetValue(
                dbus_interface="com.victronenergy.BusItem"
            )

        def text(service, path):
            return bus.get_object(service, path, introspect=False).GetText(
                dbus_interface="com.victronenergy.BusItem"
            )

        def setv(service, path, value):
            return bus.get_object(service, path, introspect=False).SetValue(
                value, dbus_interface="com.victronenergy.BusItem"
            )

        check(wait_for(lambda: bus.name_has_owner("com.victronenergy.settings")), "localsettings is up")

        cli = [sys.executable, os.path.join(ROOT, "victron-ble")]
        status_file = os.path.join(tmp, "status.json")
        for mac in (SHUNT, IP22, WRONG, STRANGER):
            key = KEYS[mac] if mac != WRONG else KEYS[SHUNT]  # WRONG gets someone else's key
            if mac == STRANGER:
                continue  # no key for this one
            r = subprocess.run(cli + ["key", mac, key.hex()], capture_output=True, text=True)
            check(r.returncode == 0, "victron-ble key %s" % mac)

        pins_file = os.path.join(tmp, "pins.json")
        fake_state = os.path.join(tmp, "fake-charger.json")
        r = subprocess.run(
            cli + ["--pins-file", pins_file, "control", IP22, "on"],
            input="123456\n",
            capture_output=True,
            text=True,
        )
        check(r.returncode == 0, "victron-ble control %s on (PIN from stdin)" % IP22)
        check(os.stat(pins_file).st_mode & 0o777 == 0o600, "pins.json readable by root only")

        replay = os.path.join(tmp, "replay.txt")
        write_replay(replay)
        daemon_log = open(os.path.join(tmp, "daemon.log"), "w")
        daemon = subprocess.Popen(
            [
                sys.executable,
                os.path.join(ROOT, "dbus-victron-ble.py"),
                "--no-hci",
                "--replay",
                replay,
                "--timeout",
                str(TIMEOUT),
                "--state-file",
                os.path.join(tmp, "state.json"),
                "--status-file",
                status_file,
                "--dcsource-name-delay",
                "1",
                "--pins-file",
                pins_file,
                "--control-helper",
                "%s %s --state-file %s" % (sys.executable, os.path.join(HERE, "fake_vesmart.py"), fake_state),
                "--control-poll",
                "3600",
            ],
            stdout=daemon_log,
            stderr=subprocess.STDOUT,
        )
        procs.append(daemon)

        battery = "com.victronenergy.battery.victron_c0ffee000001"
        charger = "com.victronenergy.charger.victron_c0ffee000003"
        solar = "com.victronenergy.solarcharger.victron_c0ffee000002"

        print("battery monitor:")
        check(wait_for(lambda: bus.name_has_owner(battery)), "service %s" % battery)
        check(get(battery, "/Dc/0/Voltage") == 13.2, "voltage 13.2")
        check(get(battery, "/Dc/0/Current") == -4.25, "current -4.25")
        check(abs(get(battery, "/Dc/0/Power") - (-56.1)) < 0.01, "power -56.1")
        check(get(battery, "/Dc/1/Voltage") == 12.62, "starter voltage 12.62")
        check(get(battery, "/TimeToGo") == 36000, "time to go 36000 s")
        check(get(battery, "/ConsumedAmphours") == -20.5, "consumed -20.5 Ah")
        check(get(battery, "/Alarms/LowStarterVoltage") == 0, "starter alarm ok")
        check(len(get(battery, "/Dc/0/Temperature")) == 0, "temperature invalid (aux = starter)")
        check(get(battery, "/ProductName") == "SmartShunt 500A/50mV", "product name")
        check(get(battery, "/ProductId") == 0xA389, "product id")
        check(get(battery, "/DeviceInstance") == 20, "device instance 20")
        check(get(battery, "/Connected") == 1, "connected")
        check(get(battery, "/Mgmt/Connection") == "Bluetooth LE", "connection text")
        check(get(battery, "/Mgmt/InsecureConnection") == 1, "insecure connection flag")
        check(text(battery, "/Dc/0/Voltage") == "13.20V", "GetText 13.20V")
        tree = get(battery, "/")
        check(isinstance(tree, dict) and "Soc" in tree, "root GetValue returns the tree")
        soc1 = get(battery, "/Soc")
        check(wait_for(lambda: get(battery, "/Soc") < soc1, timeout=5), "soc updates (%.1f -> lower)" % soc1)
        check(
            get("com.victronenergy.settings", "/Settings/Devices/victron_c0ffee000001/ClassAndVrmInstance")
            == "battery:20",
            "ClassAndVrmInstance battery:20",
        )
        check(setv(battery, "/CustomName", "House bank") == 0, "rename via D-Bus accepted")
        check(
            wait_for(
                lambda: get("com.victronenergy.settings", "/Settings/Devices/victron_c0ffee000001/CustomName")
                == "House bank"
            ),
            "name stored in localsettings",
        )
        subprocess.run(cli + ["name", SHUNT, "Domestic"], capture_output=True)
        check(wait_for(lambda: get(battery, "/CustomName") == "Domestic", timeout=5), "rename via settings")

        print("AC charger:")
        check(wait_for(lambda: bus.name_has_owner(charger)), "service %s" % charger)
        check(get(charger, "/Dc/0/Voltage") == 13.8 and get(charger, "/Dc/0/Current") == 1.2, "output 1")
        check(get(charger, "/State") == 5 and get(charger, "/NrOfOutputs") == 1, "state float, 1 output")
        check(get(charger, "/Dc/0/Temperature") == 24, "temperature 24")
        check(get(charger, "/DeviceInstance") == 20, "charger device instance 20")
        check(get(charger, "/ProductName") == "Blue Smart IP22 Charger 12/30 (1)", "charger product name")

        print("night mode switch:")
        switch = "com.victronenergy.switch.victron_c0ffee000003"
        night = "/SwitchableOutput/night"
        check(
            wait_for(lambda: bus.name_has_owner(switch), timeout=20),
            "service %s after the first read" % switch,
        )
        check(get(switch, night + "/State") == 0 and get(switch, night + "/Status") == 0, "night mode off")
        check(get(switch, night + "/Name") == "Night mode", "output name 'Night mode'")
        check(
            get(switch, night + "/Settings/Type") == 1 and get(switch, night + "/Settings/ValidTypes") == 2,
            "a toggle, nothing else",
        )
        check(
            get(switch, "/State") == 256 and get(switch, "/DeviceInstance") == 20, "device state, instance 20"
        )
        check(get(switch, "/ProductName") == "Blue Smart IP22 Charger 12/30 (1)", "switch product name")
        check(setv(switch, night + "/State", 1) == 0, "switch on (written as gui-v2 does)")
        check(
            wait_for(lambda: get(switch, night + "/Status") == 9, timeout=10), "status on after the session"
        )
        check(json.load(open(fake_state))["mode"] == 2, "the charger is in night mode")
        r = subprocess.run(
            cli + ["--status-file", status_file, "night", IP22, "off"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        check(r.returncode == 0 and "night mode is off" in r.stdout, "victron-ble night %s off" % IP22)
        check(get(switch, night + "/State") == 0 and json.load(open(fake_state))["mode"] == 0, "switched off")
        check(setv(switch, night + "/State", 5) != 0, "an invalid state is refused")
        check(setv(switch, night + "/Settings/CustomName", "Yotila") == 0, "output renamed")
        check(
            get("com.victronenergy.settings", "/Settings/Devices/victron_c0ffee000003/NightModeCustomName")
            == "Yotila",
            "output name stored in the settings",
        )
        check(
            json.load(open(fake_state))["sessions"] == ["read", "night-on", "night-off"],
            "Bluetooth sessions: read, night-on, night-off",
        )
        r = subprocess.run(
            cli + ["--status-file", status_file, "night", IP22], capture_output=True, text=True
        )
        check(r.returncode == 0 and "off (max 30.0 A)" in r.stdout, "victron-ble night %s (status)" % IP22)

        print("AC charger as DC source (enabled while running):")
        dcsource = "com.victronenergy.dcsource.victron_c0ffee000003"
        check(not bus.name_has_owner(dcsource), "no DC source by default")
        r = subprocess.run(cli + ["dcsource", IP22, "on"], capture_output=True, text=True)
        check(r.returncode == 0, "victron-ble dcsource %s on" % IP22)
        check(wait_for(lambda: bus.name_has_owner(dcsource), timeout=5), "service %s appears" % dcsource)
        check(get(dcsource, "/Settings/MonitorMode") == -2, "monitor mode -2 (AC charger)")
        check(abs(get(dcsource, "/Dc/0/Power") - 16.6) < 0.01, "DC source power 16.6 W (13.8 V x 1.2 A)")
        check(
            get(dcsource, "/Dc/0/Current") == 1.2 and get(dcsource, "/Dc/0/Voltage") == 13.8, "DC source V, I"
        )
        check(
            wait_for(lambda: get(dcsource, "/ProductName") == "Blue Smart IP22 Charger 12/30 (1)", timeout=8),
            "DC source product name (published a moment later)",
        )
        check(
            "History/EnergyOut" not in get(dcsource, "/"), "no energy counter (VRM must not count it twice)"
        )
        check(
            get(
                "com.victronenergy.settings",
                "/Settings/Devices/victron_c0ffee000003_dcsource/ClassAndVrmInstance",
            )
            == "dcsource:20",
            "own VRM instance dcsource:20",
        )
        check(bus.name_has_owner(charger), "charger service still there")

        print("solar charger (key added while running):")
        time.sleep(1.5)
        check(not bus.name_has_owner(solar), "not published without a key")
        r = subprocess.run(cli + ["key", MPPT, KEYS[MPPT].hex()], capture_output=True, text=True)
        check(r.returncode == 0, "victron-ble key %s" % MPPT)
        check(wait_for(lambda: bus.name_has_owner(solar), timeout=5), "service %s appears" % solar)
        check(get(solar, "/Yield/Power") == 104 and get(solar, "/State") == 3, "PV power 104 W, bulk")
        check(len(get(solar, "/Load/I")) == 0, "no load output")
        check(wait_for(lambda: get(solar, "/Yield/User") > 0, timeout=5), "Yield/User counter starts")
        y1 = get(solar, "/Yield/User")
        check(wait_for(lambda: get(solar, "/Yield/User") > y1, timeout=5), "Yield/User grows")
        check(get(solar, "/History/Overall/DaysAvailable") == 1, "solar history: 1 day available")
        check(get(solar, "/History/Daily/0/MaxPower") == 104, "solar history: max power of today")
        check(len(get(solar, "/History/Daily/1/Yield")) == 0, "solar history: no yesterday yet")
        check(text(solar, "/History/Daily/0/MaxBatteryVoltage").endswith("V"), "solar history: formatted text")

        print("others:")
        status = wait_for(lambda: json.load(open(status_file)), timeout=8)
        states = {d["mac"]: d["state"] for d in status["devices"]}
        check(states.get(STRANGER) == "no key", "device without key only listed")
        check(states.get(WRONG) == "wrong key", "wrong key detected")
        check(
            not bus.name_has_owner("com.victronenergy.battery.victron_c0ffee000004"), "stranger not published"
        )
        r = subprocess.run(cli + ["--status-file", status_file, "status"], capture_output=True, text=True)
        check(r.returncode == 0 and "SmartShunt 500A/50mV" in r.stdout, "victron-ble status")
        print("\n" + r.stdout)

        print("timeout:")
        check(
            wait_for(lambda: not bus.name_has_owner(battery), timeout=REPLAY_SECONDS + TIMEOUT + 10),
            "battery service removed after the data stops",
        )
        check(wait_for(lambda: not bus.name_has_owner(charger), timeout=10), "charger service removed")
        check(wait_for(lambda: not bus.name_has_owner(dcsource), timeout=5), "DC source removed with it")
        check(
            wait_for(lambda: not bus.name_has_owner(switch), timeout=5), "night mode switch removed with it"
        )

        daemon.send_signal(signal.SIGTERM)
        rc = daemon.wait(timeout=10)
        check(rc == 0, "clean exit on SIGTERM (exit code %s)" % rc)
        state = json.load(open(os.path.join(tmp, "state.json")))
        check(state.get("victron_c0ffee000002", {}).get("yield_total", 0) > 0, "yield counter saved")
    finally:
        for p in reversed(procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        print("\nlogs in %s" % tmp if args.keep or failures else "")
        if failures:
            print(open(os.path.join(tmp, "daemon.log")).read()[-4000:])
        elif not args.keep:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    print("%d failure(s)" % len(failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
