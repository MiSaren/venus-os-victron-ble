"""Turns Victron instant readout advertisements into Venus OS D-Bus services.

Only devices for which an encryption key is configured are published, as
com.victronenergy.<role>.victron_<mac>. A service disappears again when the
device has not been heard for `timeout` seconds.
"""

import json
import logging
import os
import time

import dbus

from vedbus import VeDbusService

from . import PROCESS_NAME, VERSION, aes, hci, products, records
from . import settings as st

log = logging.getLogger("manager")

VICTRON_COMPANY_ID = 0x02E1
# AD type 0xFF + company id 0x02E1 (little endian) + product advertisement 0x10:
# a cheap test that skips the AD parsing for all other advertisements
_PREFILTER = b"\xff\xe1\x02\x10"

CONNECTION = "Bluetooth LE"
BLE_SENSORS_SERVICE = "com.victronenergy.ble"

MAX_UNCONFIGURED = 100  # Victron devices of others (marinas...) we keep track of
FORGET_AFTER = 3600  # drop unconfigured devices not heard for this long
STATE_SAVE_INTERVAL = 600  # write the yield counters to flash at most this often
RETRY_NAME_TAKEN = 60
RETRY_ERROR = 30
WRONG_KEY_LOG_INTERVAL = 600


class YieldCounter:
    """Synthetic /Yield/User (total solar yield, kWh) for VRM.

    The advertisement only carries the yield of today, while VRM computes the
    solar energy from the increase of /Yield/User. The counter adds every
    increase of the daily yield; a lower daily yield means a new day started
    (the charger reset its daily counter), then the new value is all new energy.
    VRM ignores counters that are zero, so None is published until the total
    is positive."""

    def __init__(self, total=0.0, last_daily=None):
        self.total = float(total or 0.0)
        self.last_daily = last_daily

    def update(self, daily):
        """Feed the yield of today; returns True when the total changed."""
        if daily is None:
            return False
        if self.last_daily is None:
            self.last_daily = daily  # first reading: baseline only
            return False
        delta = daily - self.last_daily if daily >= self.last_daily else daily
        self.last_daily = daily
        if delta <= 0:
            return False
        self.total = round(self.total + delta, 2)
        return True

    @property
    def value(self):
        return self.total if self.total > 0 else None


def _text_formatter(fmt):
    def gettext(path, value):
        try:
            return fmt % value
        except (TypeError, ValueError):
            return str(value)

    return gettext


class DeviceService:
    """One com.victronenergy.<role>.victron_<mac> service on its own D-Bus
    connection (every Venus service exports its paths at the object root)."""

    def __init__(self, dev, decoder, instance, custom_name, on_custom_name, values):
        self.role = decoder.role
        self.instance = instance
        self.name = "com.victronenergy.%s.%s" % (decoder.role, dev.dev_id)
        self._conn = dbus.bus.BusConnection(st.bus_type())
        # libdbus would otherwise _exit(1) the whole process when this
        # connection is closed again (device out of range, key removed...)
        self._conn.set_exit_on_disconnect(False)
        try:
            svc = VeDbusService(self.name, bus=self._conn, register=False)
            svc.add_path("/Mgmt/ProcessName", PROCESS_NAME)
            svc.add_path("/Mgmt/ProcessVersion", VERSION)
            svc.add_path("/Mgmt/Connection", CONNECTION)
            # Not a trusted link: systemcalc then does not use the battery
            # voltage for shared voltage sense (as for dbus-ble-sensors)
            svc.add_path("/Mgmt/InsecureConnection", 1)
            svc.add_path("/DeviceInstance", instance)
            svc.add_path("/ProductId", dev.product_id)
            svc.add_path("/ProductName", products.name(dev.product_id, decoder.generic_name))
            svc.add_path("/FirmwareVersion", None)
            svc.add_path("/HardwareVersion", None)
            svc.add_path("/Serial", None)
            svc.add_path("/Connected", 1)
            svc.add_path(
                "/CustomName",
                custom_name or "",
                writeable=True,
                onchangecallback=lambda path, value: on_custom_name(dev, value),
            )
            for path, fmt in sorted(decoder.paths.items()):
                svc.add_path(path, values.get(path), gettextcallback=_text_formatter(fmt))
            svc.register()
        except Exception:
            self._conn.close()
            raise
        self._svc = svc

    def update(self, values):
        with self._svc as s:
            for path, value in values.items():
                s[path] = value

    def set_custom_name(self, name):
        self._svc["/CustomName"] = name

    def close(self):
        self._svc.__del__()  # releases the bus name and unexports the paths
        self._conn.close()


class Device:
    def __init__(self, mac):
        self.mac = mac
        self.dev_id = st.device_id(mac)
        self.product_id = None
        self.record_type = None
        self.rssi = None
        self.last_seen = 0.0
        self.adverts = 0
        self.state = "seen"
        self.last_nonce = None
        self.last_payload = None
        self.last_update = 0.0  # last advertisement with valid, current data
        self.values = {}
        self.service = None
        self.retry_at = 0.0
        self.blocked = None  # why we must not publish it ourselves
        self.wrong_key_logged = -WRONG_KEY_LOG_INTERVAL
        self.yield_counter = None


class Manager:
    def __init__(self, bus, timeout=300, state_file=None, status_file=None):
        self.bus = bus
        self.settings = st.Settings(bus)
        self.timeout = timeout
        self.state_file = state_file
        self.status_file = status_file
        self.devices = {}  # mac -> Device
        self.config = {}  # mac -> {"key": bytes or None, "name": str}
        self.listener = None
        self.adverts = 0
        self.victron_adverts = 0
        self.started = time.monotonic()
        self.state = self._load_state()
        self.state_dirty = False
        self.last_state_save = time.monotonic()
        self._invalid_keys = set()

    # ---- configuration --------------------------------------------------

    def reload_config(self):
        """Read keys and names from localsettings; returns False on failure."""
        try:
            devs = self.settings.device_settings()
        except dbus.exceptions.DBusException as e:
            log.warning("cannot read settings: %s", e.get_dbus_message() or e)
            return False

        config = {}
        invalid = set()
        for mac, values in devs.items():
            raw = values.get("Key")
            key = aes.parse_key(raw)
            if raw and key is None:
                invalid.add(mac)
                if mac not in self._invalid_keys:
                    log.warning("%s: ignoring the key setting, it must be 32 hex digits", mac)
            config[mac] = {"key": key, "name": values.get("CustomName") or ""}
        self._invalid_keys = invalid

        for mac in sorted(set(config) | set(self.config)):
            old = self.config.get(mac, {})
            new = config.get(mac, {})
            if old.get("key") != new.get("key"):
                log.info("%s: key %s", mac, "set" if new.get("key") else "removed")
            dev = self.devices.get(mac)
            if dev is None:
                continue
            if old.get("key") != new.get("key"):
                dev.last_nonce = dev.last_payload = None
                if dev.service is not None:
                    self._remove_service(dev, "key changed" if new.get("key") else "key removed")
            elif dev.service is not None and old.get("name") != new.get("name"):
                dev.service.set_custom_name(new.get("name", ""))
        self.config = config
        return True

    def configured(self):
        return sorted(mac for mac, c in self.config.items() if c.get("key"))

    def check_ble_sensors(self):
        """Step aside for devices that dbus-ble-sensors publishes itself (should a
        future Venus OS release support these record types natively)."""
        try:
            if not self.bus.name_has_owner(BLE_SENSORS_SERVICE):
                devices = {}
            else:
                devices = st.unwrap(
                    self.bus.get_object(BLE_SENSORS_SERVICE, "/Devices", introspect=False).GetValue(
                        dbus_interface=st.BUSITEM, timeout=st.CALL_TIMEOUT
                    )
                )
        except dbus.exceptions.DBusException as e:
            log.debug("dbus-ble-sensors check failed: %s", e)
            return
        if not isinstance(devices, dict):
            devices = {}
        for dev in self.devices.values():
            native = devices.get(dev.dev_id + "/Enabled") == 1
            if native and not dev.blocked:
                dev.blocked = "handled by dbus-ble-sensors"
                log.info("%s: enabled in dbus-ble-sensors, which publishes it now", dev.mac)
                if dev.service is not None:
                    self._remove_service(dev, dev.blocked)
            elif not native and dev.blocked:
                dev.blocked = None

    # ---- advertisements -------------------------------------------------

    def on_advertisement(self, address, rssi, adv):
        self.adverts += 1
        if _PREFILTER not in adv:
            return
        data = hci.manufacturer_data(adv, VICTRON_COMPANY_ID)
        rec = records.parse_manufacturer_data(data) if data else None
        if rec is None:
            return
        self.victron_adverts += 1
        product_id, record_type, nonce, keycheck, payload = rec
        now = time.monotonic()

        dev = self.devices.get(address)
        if dev is None:
            if address not in self.config and self._unconfigured() >= MAX_UNCONFIGURED:
                return
            dev = self.devices[address] = Device(address)
        dev.product_id = product_id
        dev.record_type = record_type
        dev.rssi = rssi
        dev.last_seen = now
        dev.adverts += 1

        decoder = records.DECODERS.get(record_type)
        if decoder is None:
            dev.state = "not supported"
            return
        key = self.config.get(address, {}).get("key")
        if not key:
            dev.state = "no key"
            return
        if dev.blocked:
            dev.state = dev.blocked
            return
        if keycheck != key[0]:
            dev.state = "wrong key"
            if now - dev.wrong_key_logged >= WRONG_KEY_LOG_INTERVAL:
                dev.wrong_key_logged = now
                log.warning(
                    "%s: the key does not match the device (key check byte %02x, "
                    "device sends %02x); were the key or the Bluetooth PIN changed?",
                    address,
                    key[0],
                    keycheck,
                )
            if dev.service is not None:
                self._remove_service(dev, "wrong key")
            return
        if nonce == dev.last_nonce and payload == dev.last_payload and dev.service is not None:
            dev.last_update = now  # the same data again: still current
            return

        values = decoder.decode(aes.ctr(key, nonce, payload))
        if decoder.role == "solarcharger":
            values["/Yield/User"] = self._yield_user(dev, values.get("/History/Daily/0/Yield"))
        dev.last_nonce = nonce
        dev.last_payload = payload
        dev.last_update = now
        dev.values = values
        dev.state = "ok"
        self._publish(dev, decoder, values, now)

    def _unconfigured(self):
        return sum(1 for mac in self.devices if mac not in self.config)

    def _yield_user(self, dev, daily):
        saved = self.state.setdefault(dev.dev_id, {})
        if dev.yield_counter is None:
            dev.yield_counter = YieldCounter(saved.get("yield_total", 0.0), saved.get("yield_last_daily"))
        counter = dev.yield_counter
        counter.update(daily)
        if saved.get("yield_total") != counter.total or saved.get("yield_last_daily") != counter.last_daily:
            saved["yield_total"] = counter.total
            saved["yield_last_daily"] = counter.last_daily
            self.state_dirty = True
        return counter.value

    def _publish(self, dev, decoder, values, now):
        if dev.service is not None and dev.service.role != decoder.role:
            self._remove_service(dev, "record type changed")
        if dev.service is not None:
            dev.service.update(values)
            return
        if now < dev.retry_at:
            return
        try:
            instance = self.settings.device_instance(dev.mac, decoder.role)
            name = self.config.get(dev.mac, {}).get("name", "")
            dev.service = DeviceService(dev, decoder, instance, name, self._custom_name_changed, values)
        except dbus.exceptions.NameExistsException:
            dev.state = "name taken"
            dev.retry_at = now + RETRY_NAME_TAKEN
            log.warning(
                "%s: com.victronenergy.%s.%s already exists on D-Bus, retrying later",
                dev.mac,
                decoder.role,
                dev.dev_id,
            )
            return
        except Exception as e:
            dev.retry_at = now + RETRY_ERROR
            log.warning("%s: cannot create the D-Bus service: %s", dev.mac, e)
            return
        log.info(
            "%s: %s, published as %s (device instance %d)",
            dev.mac,
            products.name(dev.product_id, decoder.generic_name),
            dev.service.name,
            dev.service.instance,
        )

    def _custom_name_changed(self, dev, name):
        name = str(name)
        try:
            self.settings.set_device_setting(dev.mac, "CustomName", name)
        except Exception as e:
            log.warning("%s: cannot store the name: %s", dev.mac, e)
            return False
        self.config.setdefault(dev.mac, {"key": None})["name"] = name
        return True

    def _remove_service(self, dev, reason):
        log.info("%s: removing %s (%s)", dev.mac, dev.service.name, reason)
        try:
            dev.service.close()
        except Exception:
            log.exception("%s: closing the service failed", dev.mac)
        dev.service = None

    # ---- housekeeping ---------------------------------------------------

    def tick(self):
        now = time.monotonic()
        for mac, dev in list(self.devices.items()):
            if dev.service is not None and now - dev.last_update > self.timeout:
                self._remove_service(dev, "no data for %d s" % self.timeout)
                dev.state = "no data"
            if dev.service is None and mac not in self.config and now - dev.last_seen > FORGET_AFTER:
                del self.devices[mac]
        if self.state_dirty and now - self.last_state_save >= STATE_SAVE_INTERVAL:
            self.save_state()
        self.write_status()

    def shutdown(self):
        for dev in self.devices.values():
            if dev.service is not None:
                self._remove_service(dev, "shutting down")
        if self.state_dirty:
            self.save_state()
        self.write_status()

    def _load_state(self):
        if not self.state_file:
            return {}
        try:
            with open(self.state_file) as f:
                state = json.load(f)
            return state if isinstance(state, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("ignoring %s: %s", self.state_file, e)
            return {}

    def save_state(self):
        self.last_state_save = time.monotonic()
        if not self.state_file:
            self.state_dirty = False
            return
        try:
            _write_atomic(self.state_file, json.dumps(self.state, indent=1, sort_keys=True), sync=True)
            self.state_dirty = False
        except OSError as e:
            log.warning("cannot write %s: %s", self.state_file, e)

    def status(self):
        now = time.monotonic()
        devs = []
        for mac in sorted(set(self.devices) | set(self.configured())):
            dev = self.devices.get(mac)
            conf = self.config.get(mac, {})
            entry = {
                "mac": mac,
                "key": bool(conf.get("key")),
                "name": conf.get("name", ""),
            }
            if dev is None:
                entry["state"] = "not seen"
            else:
                decoder = records.DECODERS.get(dev.record_type)
                entry.update(
                    {
                        "product_id": dev.product_id,
                        "product": products.name(dev.product_id, decoder.generic_name if decoder else None),
                        "record_type": dev.record_type,
                        "type": records.record_type_name(dev.record_type),
                        "rssi": dev.rssi,
                        "seen_ago": round(now - dev.last_seen, 1),
                        "advertisements": dev.adverts,
                        "state": dev.state,
                        "service": dev.service.name if dev.service else None,
                        "instance": dev.service.instance if dev.service else None,
                        "values": dev.values if dev.service else {},
                    }
                )
            devs.append(entry)
        return {
            "version": VERSION,
            "uptime": round(now - self.started),
            "controllers": self.listener.controllers() if self.listener else [],
            "advertisements": self.adverts,
            "victron_advertisements": self.victron_adverts,
            "aes": aes.backend(),
            "devices": devs,
        }

    def write_status(self):
        if not self.status_file:
            return
        try:
            _write_atomic(self.status_file, json.dumps(self.status(), indent=1, sort_keys=True))
        except OSError as e:
            log.debug("cannot write %s: %s", self.status_file, e)


def _write_atomic(path, text, sync=False):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.write("\n")
        if sync:
            f.flush()
            os.fsync(f.fileno())
    os.replace(tmp, path)
