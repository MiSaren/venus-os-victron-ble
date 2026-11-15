"""Passive Bluetooth LE advertisement listener (raw HCI sockets).

Venus OS' dbus-ble-sensors keeps the controller scanning (passive scan, no
duplicate filtering, re-enabled every 10 s). The kernel hands a copy of every
HCI event to each raw HCI socket bound to the controller, so this listener
receives the same LE advertising reports without sending any HCI command: it
does not change the scan, and dbus-ble-sensors (RuuviTags, tank sensors...)
keeps working exactly as before.

Only if no advertisement at all arrives for a long time (for example because
"Bluetooth sensors" are disabled and nobody scans) it asks the controller to
scan with an LE Set Scan Enable command.

The night mode control (vesmart.py) also sends scan commands, only around its
short GATT connections: the kernel changes the scan while it connects, and
command() / restore_ble_sensors_scan() put dbus-ble-sensors' scan back.

Python on Venus OS is built without AF_BLUETOOTH support in the socket module,
so the socket is created through libc with ctypes.
"""

import ctypes
import errno
import logging
import os
import select
import struct
import time

log = logging.getLogger("hci")

AF_BLUETOOTH = 31
BTPROTO_HCI = 1
SOCK_RAW = 3
SOCK_NONBLOCK = 0o4000
SOCK_CLOEXEC = 0o2000000
SOL_HCI = 0
HCI_FILTER = 2
HCI_CHANNEL_RAW = 0

HCI_COMMAND_PKT = 0x01
HCI_EVENT_PKT = 0x04
EVT_LE_META_EVENT = 0x3E
LE_ADVERTISING_REPORT = 0x02
LE_EXTENDED_ADVERTISING_REPORT = 0x0D

# LE Set Scan Enable (OGF 0x08, OCF 0x000C): enable, no duplicate filtering
LE_SET_SCAN_ENABLE = struct.pack("<BHBBB", HCI_COMMAND_PKT, 0x200C, 2, 1, 0)

SYSFS_BT = "/sys/class/bluetooth"

_libc = ctypes.CDLL(None, use_errno=True)
_libc.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
_libc.bind.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
_libc.setsockopt.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]


def _oserror(what):
    err = ctypes.get_errno()
    return OSError(err, "%s: %s" % (what, os.strerror(err)))


def open_raw_socket(dev_id):
    """Raw HCI socket on hciN that only receives LE meta events."""
    fd = _libc.socket(AF_BLUETOOTH, SOCK_RAW | SOCK_NONBLOCK | SOCK_CLOEXEC, BTPROTO_HCI)
    if fd < 0:
        raise _oserror("socket")
    try:
        addr = struct.pack("<HHH", AF_BLUETOOTH, dev_id, HCI_CHANNEL_RAW)  # struct sockaddr_hci
        if _libc.bind(fd, addr, len(addr)) < 0:
            raise _oserror("bind hci%d" % dev_id)
        # struct hci_filter: type mask, event mask[2], opcode (padded to 16 bytes)
        flt = struct.pack("<IIIH2x", 1 << HCI_EVENT_PKT, 0, 1 << (EVT_LE_META_EVENT - 32), 0)
        if _libc.setsockopt(fd, SOL_HCI, HCI_FILTER, flt, len(flt)) < 0:
            raise _oserror("setsockopt HCI_FILTER")
    except Exception:
        os.close(fd)
        raise
    return fd


def _rssi(byte):
    return byte - 256 if byte > 127 else byte


def format_address(addr):
    """6 bytes, least significant first (as in HCI) -> 'AA:BB:CC:DD:EE:FF'."""
    return ":".join("%02X" % b for b in reversed(addr))


def parse_event(pkt):
    """Yield (address, rssi, advertising data) for each report in an HCI LE
    (extended) advertising report event. Malformed packets yield nothing."""
    if len(pkt) < 5 or pkt[0] != HCI_EVENT_PKT or pkt[1] != EVT_LE_META_EVENT:
        return
    end = 3 + pkt[2]
    if len(pkt) < end:
        return
    sub = pkt[3]
    num = pkt[4]
    pos = 5
    if sub == LE_ADVERTISING_REPORT:
        # event type, address type, address[6], data length, data, rssi
        for _ in range(num):
            if pos + 9 > end:
                return
            dlen = pkt[pos + 8]
            if pos + 10 + dlen > end:
                return
            yield (
                format_address(pkt[pos + 2 : pos + 8]),
                _rssi(pkt[pos + 9 + dlen]),
                bytes(pkt[pos + 9 : pos + 9 + dlen]),
            )
            pos += 10 + dlen
    elif sub == LE_EXTENDED_ADVERTISING_REPORT:
        # event type[2], address type, address[6], primary phy, secondary phy,
        # sid, tx power, rssi, periodic interval[2], direct address type,
        # direct address[6], data length, data
        for _ in range(num):
            if pos + 24 > end:
                return
            dlen = pkt[pos + 23]
            if pos + 24 + dlen > end:
                return
            yield (
                format_address(pkt[pos + 3 : pos + 9]),
                _rssi(pkt[pos + 13]),
                bytes(pkt[pos + 24 : pos + 24 + dlen]),
            )
            pos += 24 + dlen


def manufacturer_data(adv, company_id):
    """Payload (after the company id) of the first manufacturer specific data
    AD structure of the given company, or None."""
    pos = 0
    n = len(adv)
    while pos + 1 < n:
        length = adv[pos]
        if length == 0 or pos + 1 + length > n:
            return None
        if adv[pos + 1] == 0xFF and length >= 3 and adv[pos + 2] | (adv[pos + 3] << 8) == company_id:
            return bytes(adv[pos + 4 : pos + 1 + length])
        pos += 1 + length
    return None


def list_controllers():
    ids = []
    try:
        names = os.listdir(SYSFS_BT)
    except OSError:
        return ids
    for name in names:
        if name.startswith("hci") and name[3:].isdigit():
            ids.append(int(name[3:]))
    return sorted(ids)


# ---- HCI commands (used around GATT connections, see vesmart.py) ----------

EVT_CMD_COMPLETE = 0x0E
EVT_CMD_STATUS = 0x0F
OGF_LE = 0x08
OCF_LE_SET_SCAN_PARAMETERS = 0x000B
OCF_LE_SET_SCAN_ENABLE = 0x000C

# The scan set up by dbus-ble-sensors (src/ble-scan.c, ble_scan_setup()):
# passive, window 15 (9.375 ms), interval = window with ContinuousScan,
# else 90; own address random, public if the controller refuses that.
BLE_SENSORS_SCAN_WINDOW = 15
BLE_SENSORS_SCAN_INTERVAL = 90
LE_PUBLIC_ADDRESS = 0
LE_RANDOM_ADDRESS = 1


def command_packet(ogf, ocf, params=b""):
    return struct.pack("<BHB", HCI_COMMAND_PKT, (ogf << 10) | ocf, len(params)) + bytes(params)


def command_status(pkt, opcode):
    """Status byte of a Command Complete / Command Status event for opcode, else None."""
    if len(pkt) < 7 or pkt[0] != HCI_EVENT_PKT:
        return None
    if pkt[1] == EVT_CMD_COMPLETE and struct.unpack_from("<H", pkt, 4)[0] == opcode:
        return pkt[6]
    if pkt[1] == EVT_CMD_STATUS and struct.unpack_from("<H", pkt, 5)[0] == opcode:
        return pkt[3]
    return None


def command(dev_id, ogf, ocf, params=b"", timeout=1.0):
    """Send an HCI command to hciN; returns the status byte of the reply
    (0 = success) or None when none arrived in time. Needs root."""
    opcode = (ogf << 10) | ocf
    fd = _libc.socket(AF_BLUETOOTH, SOCK_RAW | SOCK_NONBLOCK | SOCK_CLOEXEC, BTPROTO_HCI)
    if fd < 0:
        raise _oserror("socket")
    try:
        addr = struct.pack("<HHH", AF_BLUETOOTH, dev_id, HCI_CHANNEL_RAW)
        if _libc.bind(fd, addr, len(addr)) < 0:
            raise _oserror("bind hci%d" % dev_id)
        events = (1 << EVT_CMD_COMPLETE) | (1 << EVT_CMD_STATUS)
        flt = struct.pack("<IIIH2x", 1 << HCI_EVENT_PKT, events, 0, opcode)
        if _libc.setsockopt(fd, SOL_HCI, HCI_FILTER, flt, len(flt)) < 0:
            raise _oserror("setsockopt HCI_FILTER")
        os.write(fd, command_packet(ogf, ocf, params))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                continue
            try:
                pkt = os.read(fd, 260)
            except BlockingIOError:
                continue
            status = command_status(pkt, opcode)
            if status is not None:
                return status
    finally:
        os.close(fd)


def le_set_scan_enable(dev_id, enable, filter_duplicates):
    return command(
        dev_id, OGF_LE, OCF_LE_SET_SCAN_ENABLE, bytes([int(bool(enable)), int(bool(filter_duplicates))])
    )


def le_set_scan_parameters(dev_id, active, interval, window, own_address_type, filter_policy=0):
    params = struct.pack("<BHHBB", int(bool(active)), interval, window, own_address_type, filter_policy)
    return command(dev_id, OGF_LE, OCF_LE_SET_SCAN_PARAMETERS, params)


def reset_duplicate_filter(dev_id):
    """Scan off and on again without duplicate filtering; the scan parameters
    stay as they are. Returns the status of the enable command."""
    le_set_scan_enable(dev_id, False, False)
    return le_set_scan_enable(dev_id, True, False)


def restore_ble_sensors_scan(dev_id, continuous):
    """Set up the scan exactly as dbus-ble-sensors does when it starts.
    Returns (parameters status, enable status)."""
    interval = BLE_SENSORS_SCAN_WINDOW if continuous else BLE_SENSORS_SCAN_INTERVAL
    params_status = None
    for own_address_type in (LE_RANDOM_ADDRESS, LE_PUBLIC_ADDRESS):
        le_set_scan_enable(dev_id, False, True)
        params_status = le_set_scan_parameters(
            dev_id, False, interval, BLE_SENSORS_SCAN_WINDOW, own_address_type
        )
        if params_status == 0:
            break
    return params_status, le_set_scan_enable(dev_id, True, False)


class Listener:
    """Listens on all Bluetooth controllers, calls callback(address, rssi, adv)."""

    def __init__(self, callback, glib, scan_watchdog=120):
        self._callback = callback
        self._glib = glib
        self._sockets = {}  # dev_id -> (fd, watch source id)
        self._scan_watchdog = scan_watchdog
        self.reports = 0
        self.last_report = time.monotonic()
        self._last_scan_kick = 0.0
        self._errors = {}  # dev_id -> last error, logged once

    def _warn(self, dev_id, error):
        if self._errors.get(dev_id) != str(error):
            self._errors[dev_id] = str(error)
            log.warning("hci%d: %s", dev_id, error)

    def refresh(self):
        """Open new controllers, forget removed ones. Call periodically."""
        present = list_controllers()
        for dev_id in list(self._sockets):
            if dev_id not in present:
                self._close(dev_id, "controller removed")
        for dev_id in present:
            if dev_id not in self._sockets:
                self._open(dev_id)
        self._check_scanning()

    def controllers(self):
        return ["hci%d" % d for d in sorted(self._sockets)]

    def close(self):
        for dev_id in list(self._sockets):
            self._close(dev_id, None)

    def _open(self, dev_id):
        try:
            fd = open_raw_socket(dev_id)
        except OSError as e:
            self._warn(dev_id, e)
            return
        glib = self._glib
        source = glib.io_add_watch(
            fd,
            glib.PRIORITY_DEFAULT,
            glib.IO_IN | glib.IO_ERR | glib.IO_HUP | glib.IO_NVAL,
            self._readable,
            dev_id,
        )
        self._sockets[dev_id] = (fd, source)
        self._errors.pop(dev_id, None)
        log.info("listening for advertisements on hci%d", dev_id)

    def _close(self, dev_id, reason):
        fd, source = self._sockets.pop(dev_id)
        self._glib.source_remove(source)
        os.close(fd)
        if reason:
            log.warning("hci%d closed: %s", dev_id, reason)

    def _readable(self, fd, condition, dev_id):
        if dev_id not in self._sockets:
            return False
        for _ in range(256):  # bounded, so that a flood cannot starve the main loop
            try:
                pkt = os.read(fd, 1024)
            except BlockingIOError:
                break
            except OSError as e:
                # ENETDOWN / ENODEV when the controller goes down; refresh() reopens
                self._sockets.pop(dev_id)
                os.close(fd)
                self._warn(dev_id, e)
                return False
            if not pkt:
                break
            for address, rssi, adv in parse_event(pkt):
                self.reports += 1
                self.last_report = time.monotonic()
                try:
                    self._callback(address, rssi, adv)
                except Exception:
                    log.exception("advertisement handler failed")
        else:
            return True
        if condition & (self._glib.IO_ERR | self._glib.IO_HUP | self._glib.IO_NVAL):
            self._sockets.pop(dev_id, None)
            os.close(fd)
            self._warn(dev_id, "socket error, will reopen")
            return False
        return True

    def _check_scanning(self):
        """Kick the scan when nothing has been received for a long time."""
        if not self._scan_watchdog or not self._sockets:
            return
        now = time.monotonic()
        if now - self.last_report < self._scan_watchdog or now - self._last_scan_kick < self._scan_watchdog:
            return
        self._last_scan_kick = now
        for dev_id, (fd, _source) in self._sockets.items():
            try:
                os.write(fd, LE_SET_SCAN_ENABLE)
                log.info("hci%d: no advertisements for %d s, enabled scanning", dev_id, self._scan_watchdog)
            except OSError as e:
                if e.errno not in (errno.ENETDOWN, errno.EBUSY):
                    log.warning("hci%d: scan enable: %s", dev_id, e)
