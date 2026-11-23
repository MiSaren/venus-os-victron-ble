"""VE.Smart: VictronConnect's Bluetooth (GATT) protocol, for the night mode
of Blue Smart AC chargers.

Protocol (Venus OS has the server side of it in /opt/victronenergy/
vesmart-server; community projects use the client side): GATT service
306b0001-b081-4037-83dc-e59fcc3cdfd0 with three characteristics:

  306b0002  control. Read: flags, max instances (u16), free chunks, chunk
            size, ATT length (u16). Written / notified: F9 n = n more chunks
            may be sent (flow control), FA n size = chunk info, F8 = abort,
            F7 = error.
  306b0003  the last (or only) piece of a CBOR chunk
  306b0004  the other pieces of a chunk longer than the ATT length

A chunk is a CBOR sequence of messages, each starting with its opcode:

  1                         device list   -> 2 [instance, type, ...]
  3 instance                subscribe     -> 7 instance 3 result
  5 instance [vreg, ...]    get           -> 8 instance vreg bytes
                                             | 9 instance vreg error
  6 instance [vreg, bytes]  set           -> 8 instance vreg bytes (echo)

Values are VE.Direct registers as little endian bytes. The characteristics
need an encrypted link: the charger pairs with passkey entry, its Bluetooth
PIN being the passkey.

Night mode, found on a Blue Smart IP22 12/30 (firmware 3.69) by reading all
registers with night mode off, on and off again: register 0xE001 is the
charge current mode, 0 = normal, 2 = night. The charger then lowers the
maximum current (0xEDF0) to 50 % by itself, and ends night mode after 8 h.
This module writes nothing but 0xE001 = 0 or 2.

Venus OS specifics:
 - bluetoothd runs with ReverseServiceDiscovery = false: its GATT client only
   starts for a connection asked for with Device1.Connect(), not for
   Adapter1.ConnectDevice(). A device object that does not exist yet (after
   a reboot) is created with ConnectDevice, which is dropped at once; the
   pairing happens on the Device1.Connect() link.
 - vesmart-server owns the default agent (DisplayOnly). Pairing goes through
   an own KeyboardOnly agent of this process instead.
 - The kernel (5.10) connects through a background scan with duplicate
   filtering, and the Raspberry Pi controller filters duplicates per
   address: the charger alternates instant readout (non-connectable) and
   connectable advertisements, so the kernel never saw a connectable one.
   While connecting, the duplicate filter is reset.
 - The kernel then leaves its own scan parameters in the controller (accept
   list = the charger only), and dbus-ble-sensors only re-enables the scan:
   its parameters are put back after connecting and after disconnecting.

Run as a helper process, one session per call (see main()):

    python3 -m victronble.vesmart --mac MAC --op read|night-on|night-off --pins-file FILE

It prints one JSON line with the result.
"""

import argparse
import json
import logging
import signal
import sys
import time

from . import cbor, hci, pins
from . import settings as st

log = logging.getLogger("vesmart")

UUID_CONTROL = "306b0002-b081-4037-83dc-e59fcc3cdfd0"
UUID_LAST = "306b0003-b081-4037-83dc-e59fcc3cdfd0"
UUID_DATA = "306b0004-b081-4037-83dc-e59fcc3cdfd0"

OP_DEVICE_LIST = 1
OP_DEVICE_LIST_REPLY = 2
OP_SUBSCRIBE = 3
OP_GET = 5
OP_SET = 6
OP_RESPONSE = 7
OP_VALUE = 8
OP_VALUE_ERROR = 9

CTRL_ERROR = 0xF7
CTRL_ABORT = 0xF8
CTRL_READY = 0xF9
CTRL_CHUNK_INFO = 0xFA

VREG_DEVICE_STATE = 0x0201
VREG_CHARGE_CURRENT_MODE = 0xE001
VREG_MAX_CURRENT = 0xEDF0  # 0.1 A
MODE_NORMAL = 0
MODE_NIGHT = 2
VALUE_ERROR_UNKNOWN = 1  # vreg not supported by the device

# The only SET frames this module can send.
NIGHT_FRAMES = {
    True: bytes.fromhex("06008219e0014102"),
    False: bytes.fromhex("06008219e0014100"),
}


class SessionError(Exception):
    pass


# ---- protocol (pure functions) ---------------------------------------------


def device_list_frame():
    return cbor.encode_seq(OP_DEVICE_LIST)


def subscribe_frame(instance):
    return cbor.encode_seq(OP_SUBSCRIBE, instance)


def get_frame(instance, vregs):
    return cbor.encode_seq(OP_GET, instance, list(vregs))


def night_frame(on):
    frame = cbor.encode_seq(OP_SET, 0, [VREG_CHARGE_CURRENT_MODE, bytes([MODE_NIGHT if on else MODE_NORMAL])])
    if frame != NIGHT_FRAMES[bool(on)]:  # belt and braces: never send anything else
        raise SessionError("unexpected night mode frame %s" % frame.hex())
    return frame


def check_frame(frame):
    """Every frame passes here before it is sent: a SET must be one of the
    two night mode frames."""
    if frame[:1] == bytes([OP_SET]) and frame not in NIGHT_FRAMES.values():
        raise SessionError("refusing to send %s" % frame.hex())
    return frame


def split_chunk(chunk, att_len):
    """[(uuid, piece)]: the leading pieces go to 306b0004, the last to 306b0003."""
    pieces = [chunk[i : i + att_len] for i in range(0, len(chunk), att_len)] or [b""]
    return [(UUID_DATA, p) for p in pieces[:-1]] + [(UUID_LAST, pieces[-1])]


def parse_control_info(raw):
    raw = bytes(raw)
    info = {"raw": raw.hex()}
    if len(raw) >= 5:
        info.update(
            flags=raw[0],
            max_instances=raw[1] | raw[2] << 8,
            max_free_chunks=raw[3],
            max_chunk_size=raw[4],
        )
    if len(raw) >= 7:
        info["att_length"] = raw[5] | raw[6] << 8
    return info


def parse_messages(chunk):
    """Messages of a received chunk as tuples:
    ("devices", [instance, type, ...]), ("response", instance, cmd, result),
    ("value", instance, vreg, bytes), ("error", instance, vreg, error),
    ("unknown", opcode, rest of the chunk). Raises cbor.Truncated / ValueError."""
    dec = cbor.Decoder(chunk)
    out = []
    while not dec.empty():
        op = dec.item()
        if op == OP_DEVICE_LIST_REPLY:
            out.append(("devices", dec.item()))
        elif op == OP_RESPONSE:
            out.append(("response", dec.item(), dec.item(), dec.item()))
        elif op == OP_VALUE:
            instance, vreg, value = dec.item(), dec.item(), dec.item()
            out.append(("value", instance, vreg, value if isinstance(value, bytes) else None))
        elif op == OP_VALUE_ERROR:
            out.append(("error", dec.item(), dec.item(), dec.item()))
        else:
            out.append(("unknown", op, dec.rest()))
            break
    return out


def le_uint(value):
    return int.from_bytes(value, "little") if value else None


def describe(values):
    """Interpret the registers this module reads."""
    mode = le_uint(values.get(VREG_CHARGE_CURRENT_MODE))
    max_current = le_uint(values.get(VREG_MAX_CURRENT))
    return {
        "mode": mode,
        "night": None if mode is None else mode == MODE_NIGHT,
        "max_current": None if max_current is None else max_current / 10.0,
        "state": le_uint(values.get(VREG_DEVICE_STATE)),
    }


# ---- BlueZ session (helper process only: dbus and GLib are imported there) --

BLUEZ = "org.bluez"
ADAPTER_IF = "org.bluez.Adapter1"
DEVICE_IF = "org.bluez.Device1"
CHAR_IF = "org.bluez.GattCharacteristic1"
AGENT_MGR_IF = "org.bluez.AgentManager1"
PROPS_IF = "org.freedesktop.DBus.Properties"
OM_IF = "org.freedesktop.DBus.ObjectManager"
AGENT_PATH = "/victronble/vesmart/agent"
SCAN_KICK_INTERVAL = 1.5


def _agent_class(dbus):
    import dbus.service

    class Agent(dbus.service.Object):
        """KeyboardOnly agent: enters the charger's PIN, rejects everything else."""

        def __init__(self, bus, pin):
            dbus.service.Object.__init__(self, bus, AGENT_PATH)
            self.pin = pin
            self.requests = 0

        @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
        def Release(self):
            pass

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
        def RequestPasskey(self, device):
            self.requests += 1
            if self.pin is None:
                log.warning("pairing asks for the PIN, but none is configured")
                raise dbus.DBusException("org.bluez.Error.Rejected", "no PIN")
            return dbus.UInt32(int(self.pin))

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
        def RequestPinCode(self, device):
            if self.pin is None:
                raise dbus.DBusException("org.bluez.Error.Rejected", "no PIN")
            return self.pin

        @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
        def DisplayPasskey(self, device, passkey, entered):
            pass

        @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
        def DisplayPinCode(self, device, pincode):
            pass

        @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
        def RequestConfirmation(self, device, passkey):
            raise dbus.DBusException("org.bluez.Error.Rejected", "passkey entry expected")

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
        def RequestAuthorization(self, device):
            raise dbus.DBusException("org.bluez.Error.Rejected", "not expected")

        @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
        def AuthorizeService(self, device, uuid):
            raise dbus.DBusException("org.bluez.Error.Rejected", "not expected")

        @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
        def Cancel(self):
            pass

    return Agent


class Session:
    """One connection to one charger. Synchronous: it iterates the GLib main
    loop while waiting, so it belongs in its own (helper) process."""

    aborted = False  # set by SIGTERM

    def __init__(self, bus, mac, pin, adapter="hci0", addr_type="random", deadline=60.0, scan_restore=None):
        import dbus
        from gi.repository import GLib

        self.dbus = dbus
        self.GLib = GLib
        self.bus = bus
        self.mac = st.normalize_mac(mac)
        self.pin = pin
        self.adapter = adapter
        self.adapter_path = "/org/bluez/" + adapter
        self.dev_id = int(adapter[3:]) if adapter.startswith("hci") and adapter[3:].isdigit() else 0
        self.addr_type = addr_type
        self.dev_path = "%s/dev_%s" % (self.adapter_path, self.mac.replace(":", "_"))
        self.deadline = time.monotonic() + deadline
        self.scan_restore = scan_restore  # callable, or None to leave the scan alone
        self.om = dbus.Interface(bus.get_object(BLUEZ, "/"), OM_IF)
        self.connected = False
        self.chars = {}
        self.path_uuid = {}
        self.credits = 0
        self.rx = bytearray()
        self.att_len = 20
        self.control_info = None
        self.devices = None
        self.values = {}
        self.errors = {}
        self.responses = {}
        self.scan_restored = False
        self.agent = None
        self._wake = GLib.timeout_add(25, lambda: True)
        self._signal = bus.add_signal_receiver(
            self._properties_changed,
            signal_name="PropertiesChanged",
            dbus_interface=PROPS_IF,
            bus_name=BLUEZ,
            path_keyword="path",
        )

    # -- waiting

    def wait(self, cond, timeout):
        ctx = self.GLib.MainContext.default()
        until = min(time.monotonic() + timeout, self.deadline)
        while not cond():
            if Session.aborted:
                raise SessionError("aborted")
            if time.monotonic() >= until:
                if until >= self.deadline:
                    raise SessionError("session took too long")
                return False
            ctx.iteration(True)
        return True

    def call(self, method, *args, timeout=30, tick=None):
        box = {}
        method(
            *args,
            reply_handler=lambda *r: box.setdefault("ok", r),
            error_handler=lambda e: box.setdefault("err", e),
            timeout=timeout,
        )
        next_tick = [time.monotonic() + (tick[0] if tick else 0)]

        def done():
            if box:
                return True
            if tick and time.monotonic() >= next_tick[0]:
                next_tick[0] = time.monotonic() + tick[0]
                tick[1]()
            return False

        if not self.wait(done, timeout + 2):
            raise SessionError("no reply from bluetoothd")
        if "err" in box:
            raise box["err"]
        return box["ok"]

    # -- signals

    def _properties_changed(self, interface, changed, invalidated, path=None):
        if interface == DEVICE_IF and path == self.dev_path and "Connected" in changed:
            self.connected = bool(changed["Connected"])
        elif interface == CHAR_IF and "Value" in changed and path in self.path_uuid:
            self._on_value(self.path_uuid[path], bytes(changed["Value"]))

    def _on_value(self, uuid, data):
        if uuid == UUID_CONTROL:
            if data[:1] == bytes([CTRL_READY]) and len(data) >= 2:
                self.credits += data[1]
            elif data[:1] == bytes([CTRL_ERROR]):
                log.warning("charger reports error %s", data.hex())
            elif data[:1] == bytes([CTRL_ABORT]):
                log.warning("charger aborted the transfer")
        elif uuid == UUID_DATA:
            self.rx += data
        elif uuid == UUID_LAST:
            chunk, self.rx = bytes(self.rx) + data, bytearray()
            try:
                messages = parse_messages(chunk)
            except ValueError as e:
                log.warning("cannot decode %s: %s", chunk.hex(), e)
                messages = []
            for msg in messages:
                if msg[0] == "devices":
                    self.devices = msg[1]
                elif msg[0] == "response":
                    self.responses[(msg[1], msg[2])] = msg[3]
                elif msg[0] == "value":
                    self.values[(msg[1], msg[2])] = msg[3]
                elif msg[0] == "error":
                    self.errors[(msg[1], msg[2])] = msg[3]
            self._write_async(UUID_CONTROL, bytes([CTRL_READY, 1]))  # room for one more chunk

    # -- GATT writes

    def _write_options(self, uuid):
        flags = self.chars[uuid]["flags"]
        return {"type": "command" if "write-without-response" in flags else "request"}

    def _write(self, uuid, data):
        char = self.chars[uuid]["iface"]
        self.call(
            char.WriteValue, self.dbus.Array(data, signature="y"), self._write_options(uuid), timeout=10
        )

    def _write_async(self, uuid, data):
        self.chars[uuid]["iface"].WriteValue(
            self.dbus.Array(data, signature="y"),
            self._write_options(uuid),
            reply_handler=lambda *a: None,
            error_handler=lambda e: log.debug("write failed: %s", e),
            timeout=10,
        )

    def send(self, frame):
        check_frame(frame)
        if not self.wait(lambda: self.credits > 0 or not self.connected, 5):
            raise SessionError("the charger does not accept data")
        if not self.connected:
            raise SessionError("disconnected")
        self.credits -= 1
        for uuid, piece in split_chunk(frame, self.att_len):
            self._write(uuid, piece)

    # -- scan

    def _scan_tick(self):
        """While connecting: reset the duplicate filter. Once linked: restore
        dbus-ble-sensors' scan."""
        if self.connected:
            self._restore_scan()
        else:
            hci.reset_duplicate_filter(self.dev_id)

    def _restore_scan(self):
        if not self.scan_restored and self.scan_restore is not None:
            self.scan_restored = True
            self.scan_restore(self.dev_id)

    # -- steps

    def _device_props(self):
        objs = self.om.GetManagedObjects()
        return objs.get(self.dbus.ObjectPath(self.dev_path), {}).get(DEVICE_IF)

    def open(self):
        """Connect (pairing if needed), discover, handshake, subscribe."""
        Agent = _agent_class(self.dbus)
        self.agent = Agent(self.bus, self.pin)
        manager = self.dbus.Interface(self.bus.get_object(BLUEZ, "/org/bluez"), AGENT_MGR_IF)
        manager.RegisterAgent(AGENT_PATH, "KeyboardOnly")
        props = self._device_props()
        if props is None:
            self._create_device()
            props = self._device_props() or {}
        self.connected = bool(props.get("Connected"))
        device = self.dbus.Interface(self.bus.get_object(BLUEZ, self.dev_path), DEVICE_IF)
        if not self.connected:
            for attempt in range(3):
                try:
                    self.call(device.Connect, timeout=30, tick=(SCAN_KICK_INTERVAL, self._scan_tick))
                    break
                except self.dbus.DBusException as e:
                    if attempt == 2 or "abort-by-local" not in str(e):
                        raise
                    log.info("connection attempt aborted, retrying")
                    self._restore_scan()
                    self.scan_restored = False
                    time.sleep(1)
        self.connected = True
        self._restore_scan()
        dev_props = self.dbus.Interface(self.bus.get_object(BLUEZ, self.dev_path), PROPS_IF)
        # Pair only once the services are resolved: Device1.Connect returns as
        # soon as ATT is up, and bluetoothd (5.72) never answers a Pair() that
        # completes while its first GATT discovery still runs (the browse for
        # the pairing request fails with EBUSY and the request is dropped).
        self._resolve(dev_props)
        if not bool(dev_props.Get(DEVICE_IF, "Paired")):
            self._pair(device)
        self._handshake()

    def _create_device(self):
        """No device object (after a reboot): ConnectDevice creates one. Its
        link has no GATT client (ReverseServiceDiscovery = false), and pairing
        over it would wait for a service discovery that never comes, so it is
        dropped again; open() then connects with Device1.Connect and pairs."""
        adapter = self.dbus.Interface(self.bus.get_object(BLUEZ, self.adapter_path), ADAPTER_IF)
        log.info("%s: new to bluetoothd, creating the device", self.mac)
        path = self.call(
            adapter.ConnectDevice,
            self.dbus.Dictionary({"Address": self.mac, "AddressType": self.addr_type}, signature="sv"),
            timeout=30,
            tick=(SCAN_KICK_INTERVAL, self._scan_tick),
        )[0]
        if str(path) != self.dev_path:
            raise SessionError("unexpected device path %s" % path)
        self.connected = True
        self._restore_scan()
        device = self.dbus.Interface(self.bus.get_object(BLUEZ, self.dev_path), DEVICE_IF)
        try:
            device.Disconnect(timeout=10)
        except self.dbus.DBusException as e:
            log.debug("disconnect: %s", e)
        self.wait(lambda: not self.connected, 5)
        self.scan_restored = False
        time.sleep(1)

    def _pair(self, device):
        if self.pin is None:
            raise SessionError("not paired and no PIN configured")
        log.info("%s: pairing", self.mac)
        try:
            self.call(device.Pair, timeout=60)
        except self.dbus.DBusException as e:
            if e.get_dbus_name() != "org.bluez.Error.AlreadyExists":
                raise
        log.info("%s: paired", self.mac)

    def _resolve(self, dev_props):
        if not bool(dev_props.Get(DEVICE_IF, "ServicesResolved")):
            resolved = [False]

            def changed(interface, props, invalidated, path=None):
                if interface == DEVICE_IF and path == self.dev_path and props.get("ServicesResolved"):
                    resolved[0] = True

            match = self.bus.add_signal_receiver(
                changed, "PropertiesChanged", PROPS_IF, BLUEZ, path_keyword="path"
            )
            try:
                if not bool(dev_props.Get(DEVICE_IF, "ServicesResolved")):
                    if not self.wait(lambda: resolved[0], 30):
                        raise SessionError("GATT services not resolved")
            finally:
                match.remove()
        for path, ifaces in self.om.GetManagedObjects().items():
            if not str(path).startswith(self.dev_path + "/") or CHAR_IF not in ifaces:
                continue
            char = ifaces[CHAR_IF]
            uuid = str(char["UUID"]).lower()
            if uuid in (UUID_CONTROL, UUID_LAST, UUID_DATA):
                self.chars[uuid] = {
                    "iface": self.dbus.Interface(self.bus.get_object(BLUEZ, path), CHAR_IF),
                    "flags": [str(f) for f in char.get("Flags", [])],
                    "mtu": int(char.get("MTU", 23)),
                }
                self.path_uuid[str(path)] = uuid
        if len(self.chars) != 3:
            raise SessionError("no VE.Smart service on this device")

    def _handshake(self):
        for uuid in (UUID_CONTROL, UUID_LAST, UUID_DATA):
            self.call(self.chars[uuid]["iface"].StartNotify, timeout=10)
        raw = self.call(
            self.chars[UUID_CONTROL]["iface"].ReadValue, self.dbus.Dictionary({}, signature="sv")
        )[0]
        self.control_info = parse_control_info(bytes(raw))
        self.att_len = max(
            20, min(self.control_info.get("att_length") or 20, self.chars[UUID_LAST]["mtu"] - 3)
        )
        self._write(UUID_CONTROL, bytes([CTRL_CHUNK_INFO, 0x80, 0xFF]))  # we take 128 chunks of any size
        self.wait(lambda: self.credits > 0, 3)
        self._write(UUID_CONTROL, bytes([CTRL_READY, 0x80]))
        self.send(device_list_frame())
        if not self.wait(lambda: self.devices is not None, 5):
            raise SessionError("no device list")
        self.send(subscribe_frame(0))
        if not self.wait(lambda: (0, OP_SUBSCRIBE) in self.responses, 5) or self.responses[(0, OP_SUBSCRIBE)]:
            raise SessionError("subscribe failed: %s" % self.responses.get((0, OP_SUBSCRIBE)))

    def get(self, vregs, timeout=5):
        for v in vregs:
            self.values.pop((0, v), None)
            self.errors.pop((0, v), None)
        self.send(get_frame(0, vregs))
        if not self.wait(lambda: all((0, v) in self.values or (0, v) in self.errors for v in vregs), timeout):
            raise SessionError("no answer for registers %s" % ", ".join("%04X" % v for v in vregs))
        return {v: self.values.get((0, v)) for v in vregs}, {
            v: self.errors[(0, v)] for v in vregs if (0, v) in self.errors
        }

    def set_night(self, on):
        key = (0, VREG_CHARGE_CURRENT_MODE)
        self.values.pop(key, None)
        self.errors.pop(key, None)
        self.send(night_frame(on))
        if not self.wait(lambda: key in self.values or key in self.errors, 5):
            raise SessionError("no answer to the write")
        if key in self.errors:
            raise SessionError("the charger refused the write (error %s)" % self.errors[key])
        return le_uint(self.values[key])

    def close(self):
        try:
            for uuid in list(self.chars):
                try:
                    self.chars[uuid]["iface"].StopNotify(timeout=5)
                except self.dbus.DBusException:
                    pass
            if self.connected:
                device = self.dbus.Interface(self.bus.get_object(BLUEZ, self.dev_path), DEVICE_IF)
                try:
                    device.Disconnect(timeout=10)
                except self.dbus.DBusException as e:
                    log.debug("disconnect: %s", e)
        finally:
            if self.agent is not None:
                try:
                    manager = self.dbus.Interface(self.bus.get_object(BLUEZ, "/org/bluez"), AGENT_MGR_IF)
                    manager.UnregisterAgent(AGENT_PATH)
                except self.dbus.DBusException:
                    pass
            self._signal.remove()
            self.GLib.source_remove(self._wake)
            if self.scan_restore is not None:
                time.sleep(0.5)
                self.scan_restored = False
                self._restore_scan()


WATCHED = (VREG_CHARGE_CURRENT_MODE, VREG_MAX_CURRENT, VREG_DEVICE_STATE)


def explain(error):
    """A short, useful text for a failed session."""
    name = error.get_dbus_name() if hasattr(error, "get_dbus_name") else None
    text = (error.get_dbus_message() if hasattr(error, "get_dbus_message") else None) or str(error)
    if name == "org.bluez.Error.AuthenticationFailed" or "Authentication" in text:
        return "pairing failed: wrong PIN?"
    if name in ("org.freedesktop.DBus.Error.NoReply",) or "abort-by-local" in text or "page-timeout" in text:
        return "charger not reachable (busy with VictronConnect, or out of range?)"
    return text


def run(bus, mac, op, pin, adapter="hci0", deadline=60.0, scan_restore=None):
    """One session; returns the result dict (never raises for device errors)."""
    started = time.monotonic()
    result = {"mac": mac, "op": op, "ok": False}
    session = None
    try:
        session = Session(bus, mac, pin, adapter=adapter, deadline=deadline, scan_restore=scan_restore)
        session.open()
        values, errors = session.get(WATCHED)
        if errors.get(VREG_CHARGE_CURRENT_MODE) == VALUE_ERROR_UNKNOWN:
            result.update(unsupported=True, error="this charger has no night mode register")
            return result
        if errors:
            raise SessionError("register errors %s" % {"%04X" % k: v for k, v in errors.items()})
        if op in ("night-on", "night-off"):
            echo = session.set_night(op == "night-on")
            result["echo"] = echo
            session.wait(lambda: False, 1.5)  # the charger adjusts its current limit
            values, errors = session.get(WATCHED)
        result.update(describe(values))
        if op == "night-on" and not result["night"] or op == "night-off" and result["night"]:
            raise SessionError("the charger did not switch (mode %s)" % result["mode"])
        result["ok"] = True
    except Exception as e:  # dbus errors, timeouts, protocol errors
        result["error"] = explain(e)
        log.warning("%s %s failed: %s", mac, op, result["error"])
    finally:
        if session is not None:
            try:
                session.close()
            except Exception as e:
                log.warning("closing the session: %s", e)
        result["seconds"] = round(time.monotonic() - started, 2)
    return result


def ble_sensors_scan_restorer(bus):
    """A function that puts dbus-ble-sensors' scan back, or None when
    dbus-ble-sensors is not scanning (not running / Bluetooth disabled)."""
    import dbus

    try:
        if not bus.name_has_owner("com.victronenergy.ble"):
            return None
        settings = st.Settings(bus)
        try:
            if settings.get("/Settings/BleSensors/Bluetooth/Enabled") == 0:
                return None
        except dbus.exceptions.DBusException:
            pass
        try:
            continuous = settings.get("/Settings/BleSensors/ContinuousScan") == 1
        except dbus.exceptions.DBusException:
            continuous = False
    except dbus.exceptions.DBusException as e:
        log.warning("cannot check dbus-ble-sensors: %s", e)
        return None

    def restore(dev_id):
        params, enable = hci.restore_ble_sensors_scan(dev_id, continuous)
        log.debug("scan restored (continuous %s): status %s/%s", continuous, params, enable)

    return restore


def main(argv=None):
    parser = argparse.ArgumentParser(prog="vesmart", description="one VE.Smart session with a charger")
    parser.add_argument("--mac", required=True)
    parser.add_argument("--op", required=True, choices=("read", "night-on", "night-off"))
    parser.add_argument("--pins-file", required=True)
    parser.add_argument("--adapter", default="hci0")
    parser.add_argument("--deadline", type=float, default=60.0)
    parser.add_argument("-d", "--debug", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        format="vesmart %(levelname)s: %(message)s",
        level=logging.DEBUG if args.debug else logging.INFO,
    )
    mac = st.normalize_mac(args.mac)
    if mac is None:
        parser.error("bad MAC address")

    def terminate(signum, frame):
        Session.aborted = True

    signal.signal(signal.SIGTERM, terminate)

    import dbus
    import dbus.mainloop.glib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    result = run(
        bus,
        mac,
        args.op,
        pins.load(args.pins_file).get(mac),
        adapter=args.adapter,
        deadline=args.deadline,
        scan_restore=ble_sensors_scan_restorer(bus),
    )
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
