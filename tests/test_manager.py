"""Manager logic without D-Bus: settings and services are replaced by fakes."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "ext", "velib_python"))

import advgen  # noqa: E402
from victronble import aes, manager, records  # noqa: E402

KEY = aes.parse_key("00112233445566778899aabbccddeeff")
OTHER_KEY = aes.parse_key("ff112233445566778899aabbccddeeff")
SHUNT = "C0:FF:EE:00:00:01"
MPPT = "C0:FF:EE:00:00:02"
IP22 = "C0:FF:EE:00:00:03"


class FakeSettings:
    def __init__(self):
        self.devices = {}  # mac -> {name: value}
        self.instances = {}

    def device_settings(self):
        return {mac: dict(v) for mac, v in self.devices.items()}

    def set_device_setting(self, mac, name, value):
        self.devices.setdefault(mac, {})[name] = value

    def device_instance(self, mac, role, default=20):
        taken = {i for (r, i) in self.instances.values() if r == role}
        if mac not in self.instances or self.instances[mac][0] != role:
            instance = default
            while instance in taken:
                instance += 1
            self.instances[mac] = (role, instance)
        return self.instances[mac][1]


class FakeService:
    created = []

    def __init__(self, dev, decoder, instance, custom_name, on_custom_name, values):
        self.role = decoder.role
        self.instance = instance
        self.name = "com.victronenergy.%s.%s" % (decoder.role, dev.dev_id)
        self.values = dict(values)
        self.custom_name = custom_name
        self.on_custom_name = lambda name: on_custom_name(dev, name)
        self.updates = 0
        self.closed = False
        FakeService.created.append(self)

    def update(self, values):
        self.values.update(values)
        self.updates += 1

    def set_custom_name(self, name):
        self.custom_name = name

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def adv(mac_unused, product_id, record_type, nonce, payload, key=KEY):
    return advgen.advertisement(advgen.manufacturer_data(product_id, record_type, nonce, key, payload))


class ManagerTest(unittest.TestCase):
    def setUp(self):
        FakeService.created = []
        self.clock = Clock()
        patches = [
            mock.patch.object(manager, "DeviceService", FakeService),
            mock.patch.object(manager.time, "monotonic", self.clock),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = os.path.join(self.tmp.name, "state.json")
        self.status_file = os.path.join(self.tmp.name, "status.json")
        self.m = self.make_manager()

    def make_manager(self):
        m = manager.Manager(bus=None, timeout=300, state_file=self.state_file, status_file=self.status_file)
        m.settings = self.settings = getattr(self, "settings", FakeSettings())
        return m

    def shunt(self, nonce, voltage=13.2, current=-1.5, soc=80.0, key=KEY):
        return adv(
            SHUNT,
            0xA389,
            records.BATTERY_MONITOR,
            nonce,
            advgen.battery_monitor(voltage=voltage, current=current, soc=soc, consumed_ah=-10),
            key,
        )

    def mppt(self, nonce, daily, pv=100):
        return adv(
            MPPT,
            0xA053,
            records.SOLAR_CHARGER,
            nonce,
            advgen.solar_charger(state=3, voltage=13.4, current=5.0, yield_today_kwh=daily, pv_w=pv),
        )

    def test_unconfigured_device_is_only_listed(self):
        self.m.reload_config()
        self.m.on_advertisement(SHUNT, -70, self.shunt(1))
        self.assertEqual(FakeService.created, [])
        self.assertEqual(self.m.devices[SHUNT].state, "no key")
        status = self.m.status()
        json.dumps(status)
        self.assertEqual(status["devices"][0]["product"], "SmartShunt 500A/50mV")

    def test_publish_update_and_dedup(self):
        self.settings.set_device_setting(SHUNT, "Key", KEY.hex())
        self.settings.set_device_setting(SHUNT, "CustomName", "House bank")
        self.m.reload_config()
        self.m.on_advertisement(SHUNT, -70, self.shunt(1))
        (svc,) = FakeService.created
        self.assertEqual(svc.name, "com.victronenergy.battery.victron_c0ffee000001")
        self.assertEqual((svc.instance, svc.custom_name), (20, "House bank"))
        self.assertEqual(svc.values["/Dc/0/Voltage"], 13.2)
        self.assertEqual(svc.values["/Soc"], 80.0)
        # same nonce and data: no update, but the data counts as current
        self.clock.now += 100
        self.m.on_advertisement(SHUNT, -70, self.shunt(1))
        self.assertEqual(svc.updates, 0)
        self.assertEqual(self.m.devices[SHUNT].last_update, self.clock.now)
        self.m.on_advertisement(SHUNT, -70, self.shunt(2, soc=79.9))
        self.assertEqual((svc.updates, svc.values["/Soc"]), (1, 79.9))

    def test_timeout_and_return(self):
        self.settings.set_device_setting(SHUNT, "Key", KEY.hex())
        self.m.reload_config()
        self.m.on_advertisement(SHUNT, -70, self.shunt(1))
        svc = FakeService.created[0]
        self.clock.now += 299
        self.m.tick()
        self.assertFalse(svc.closed)
        self.clock.now += 2
        self.m.tick()
        self.assertTrue(svc.closed)
        self.assertEqual(self.m.devices[SHUNT].state, "no data")
        self.m.on_advertisement(SHUNT, -70, self.shunt(3))
        self.assertEqual(len(FakeService.created), 2)
        self.assertEqual(FakeService.created[1].instance, 20)  # same instance again

    def test_return_with_unchanged_data(self):
        """Back in range after a timeout while the data did not change (same
        nonce, e.g. a charger in storage): the service must come back."""
        self.settings.set_device_setting(SHUNT, "Key", KEY.hex())
        self.m.reload_config()
        self.m.on_advertisement(SHUNT, -70, self.shunt(1))
        self.clock.now += 301
        self.m.tick()
        self.assertTrue(FakeService.created[0].closed)
        self.m.on_advertisement(SHUNT, -70, self.shunt(1))
        self.assertEqual(len(FakeService.created), 2)
        self.assertFalse(FakeService.created[1].closed)

    def test_wrong_key(self):
        self.settings.set_device_setting(SHUNT, "Key", OTHER_KEY.hex())
        self.m.reload_config()
        with self.assertLogs("manager", "WARNING"):
            self.m.on_advertisement(SHUNT, -70, self.shunt(1))
        self.assertEqual(self.m.devices[SHUNT].state, "wrong key")
        self.assertEqual(FakeService.created, [])
        # fixing the key in the settings
        self.settings.set_device_setting(SHUNT, "Key", KEY.hex())
        self.m.reload_config()
        self.m.on_advertisement(SHUNT, -70, self.shunt(1))
        self.assertEqual(len(FakeService.created), 1)

    def test_key_change_and_removal(self):
        self.settings.set_device_setting(SHUNT, "Key", KEY.hex())
        self.m.reload_config()
        self.m.on_advertisement(SHUNT, -70, self.shunt(1))
        svc = FakeService.created[0]
        self.settings.set_device_setting(SHUNT, "Key", "")
        self.m.reload_config()
        self.assertTrue(svc.closed)
        self.m.on_advertisement(SHUNT, -70, self.shunt(2))
        self.assertEqual(len(FakeService.created), 1)
        self.assertEqual(self.m.devices[SHUNT].state, "no key")

    def test_invalid_key_setting(self):
        self.settings.set_device_setting(SHUNT, "Key", "1234")
        with self.assertLogs("manager", "WARNING"):
            self.m.reload_config()
        self.assertIsNone(self.m.config[SHUNT]["key"])

    def test_custom_name(self):
        self.settings.set_device_setting(SHUNT, "Key", KEY.hex())
        self.m.reload_config()
        self.m.on_advertisement(SHUNT, -70, self.shunt(1))
        svc = FakeService.created[0]
        self.assertTrue(svc.on_custom_name("Starter"))  # written in the GUI
        self.assertEqual(self.settings.devices[SHUNT]["CustomName"], "Starter")
        self.settings.set_device_setting(SHUNT, "CustomName", "Engine")  # changed elsewhere
        self.m.reload_config()
        self.assertEqual(svc.custom_name, "Engine")

    def test_instances_per_class(self):
        for mac in (SHUNT, "C0:FF:EE:00:00:09"):
            self.settings.set_device_setting(mac, "Key", KEY.hex())
        self.settings.set_device_setting(IP22, "Key", KEY.hex())
        self.m.reload_config()
        self.m.on_advertisement(SHUNT, -70, self.shunt(1))
        self.m.on_advertisement("C0:FF:EE:00:00:09", -70, self.shunt(1))
        self.m.on_advertisement(
            IP22,
            -70,
            adv(
                IP22,
                0xA330,
                records.AC_CHARGER,
                7,
                advgen.ac_charger(state=4, outputs=[(14.2, 12.3)], temperature=25),
            ),
        )
        self.assertEqual(
            [(s.role, s.instance) for s in FakeService.created],
            [("battery", 20), ("battery", 21), ("charger", 20)],
        )
        self.assertEqual(FakeService.created[2].values["/Dc/0/Current"], 12.3)

    def test_record_type_change(self):
        """A SmartShunt switched to another mode becomes another service."""
        self.settings.set_device_setting(SHUNT, "Key", KEY.hex())
        self.m.reload_config()
        self.m.on_advertisement(SHUNT, -70, self.shunt(1))
        self.m.on_advertisement(
            SHUNT,
            -70,
            adv(SHUNT, 0xA389, records.SOLAR_CHARGER, 2, advgen.solar_charger(state=3, yield_today_kwh=0.1)),
        )
        first, second = FakeService.created
        self.assertTrue(first.closed)
        self.assertEqual(second.role, "solarcharger")

    def test_yield_counter_and_persistence(self):
        self.settings.set_device_setting(MPPT, "Key", KEY.hex())
        self.m.reload_config()
        totals = []
        nonce = 0
        for daily in (0.50, 0.50, 0.75, 1.20, 0.0, 0.10, 0.35):  # new day after 1.20
            nonce += 1
            self.m.on_advertisement(MPPT, -60, self.mppt(nonce, daily))
            totals.append(FakeService.created[0].values["/Yield/User"])
        self.assertEqual(totals, [None, None, 0.25, 0.7, 0.7, 0.8, 1.05])
        self.m.shutdown()
        with open(self.state_file) as f:
            saved = json.load(f)["victron_c0ffee000002"]
        self.assertEqual(saved, {"yield_total": 1.05, "yield_last_daily": 0.35})
        # restart: continues where it was, including the yield made meanwhile
        FakeService.created = []
        m2 = self.make_manager()
        m2.reload_config()
        m2.on_advertisement(MPPT, -60, self.mppt(nonce + 1, 0.55))
        self.assertEqual(FakeService.created[0].values["/Yield/User"], 1.25)

    def test_state_saved_rarely(self):
        self.settings.set_device_setting(MPPT, "Key", KEY.hex())
        self.m.reload_config()
        self.m.on_advertisement(MPPT, -60, self.mppt(1, 0.1))
        self.m.on_advertisement(MPPT, -60, self.mppt(2, 0.2))
        self.m.tick()
        self.assertFalse(os.path.exists(self.state_file))
        self.clock.now += manager.STATE_SAVE_INTERVAL
        self.m.on_advertisement(MPPT, -60, self.mppt(3, 0.3))
        self.m.tick()
        self.assertTrue(os.path.exists(self.state_file))

    def test_unconfigured_limit_and_forget(self):
        for n in range(manager.MAX_UNCONFIGURED + 20):
            self.m.on_advertisement("AA:00:00:00:%02X:%02X" % (n // 256, n % 256), -90, self.shunt(1))
        self.assertEqual(len(self.m.devices), manager.MAX_UNCONFIGURED)
        self.clock.now += manager.FORGET_AFTER + 1
        self.m.tick()
        self.assertEqual(len(self.m.devices), 0)

    def test_other_advertisements_ignored(self):
        self.m.on_advertisement(SHUNT, -70, bytes([2, 1, 6, 5, 0xFF, 0x99, 0x04, 5, 1]))  # ruuvi-like
        self.m.on_advertisement(SHUNT, -70, advgen.advertisement(bytes([0x10, 0x02])))  # too short
        self.assertEqual(self.m.devices, {})
        self.assertEqual(self.m.adverts, 2)

    def test_status_file(self):
        self.settings.set_device_setting(SHUNT, "Key", KEY.hex())
        self.settings.set_device_setting(MPPT, "Key", KEY.hex())
        self.m.reload_config()
        self.m.on_advertisement(SHUNT, -70, self.shunt(1))
        self.m.tick()
        with open(self.status_file) as f:
            status = json.load(f)
        states = {d["mac"]: d["state"] for d in status["devices"]}
        self.assertEqual(states, {SHUNT: "ok", MPPT: "not seen"})
        shunt = [d for d in status["devices"] if d["mac"] == SHUNT][0]
        self.assertEqual(shunt["values"]["/Soc"], 80.0)
        self.assertEqual(shunt["service"], "com.victronenergy.battery.victron_c0ffee000001")


class YieldCounterTest(unittest.TestCase):
    def test_counter(self):
        c = manager.YieldCounter()
        self.assertFalse(c.update(None))
        self.assertFalse(c.update(2.0))  # baseline
        self.assertIsNone(c.value)
        self.assertTrue(c.update(2.5))
        self.assertFalse(c.update(2.5))
        self.assertFalse(c.update(0.0))  # new day
        self.assertTrue(c.update(0.01))
        self.assertEqual(c.value, 0.51)


if __name__ == "__main__":
    unittest.main()
