"""Builds encrypted Victron instant readout advertisements, for tests and demos.

The encoders are the inverse of victronble.records: raw field values are
packed at the start bits of the specification, unused bits are set to one
(as the specification asks of the devices), then the payload is encrypted
with AES-CTR exactly like a device does.
"""

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from victronble import aes, records  # noqa: E402

VICTRON = 0x02E1


def pack(fields, nbytes):
    """fields: (spec start bit, bits, raw value) -> little endian payload."""
    value = (1 << (nbytes * 8)) - 1
    for start, bits, raw in fields:
        pos = start - records.HEADER_BITS
        mask = ((1 << bits) - 1) << pos
        value = (value & ~mask) | ((raw & ((1 << bits) - 1)) << pos)
    return value.to_bytes(nbytes, "little")


def _raw(value, scale, na):
    return na if value is None else int(round(value * scale))


def battery_monitor(
    voltage=None,
    current=None,
    soc=None,
    consumed_ah=None,
    ttg_min=None,
    alarm=0,
    aux_mode=records.AUX_NONE,
    aux_raw=0xFFFF,
):
    return pack(
        [
            (32, 16, 0xFFFF if ttg_min is None else ttg_min),
            (48, 16, _raw(voltage, 100, 0x7FFF)),
            (64, 16, alarm),
            (80, 16, aux_raw),
            (96, 2, aux_mode),
            (98, 22, _raw(current, 1000, 0x3FFFFF)),
            (120, 20, 0xFFFFF if consumed_ah is None else int(round(-consumed_ah * 10))),
            (140, 10, _raw(soc, 10, 0x3FF)),
        ],
        15,
    )


def solar_charger(
    state=None, error=0, voltage=None, current=None, yield_today_kwh=None, pv_w=None, load_a=None
):
    return pack(
        [
            (32, 8, 0xFF if state is None else state),
            (40, 8, 0xFF if error is None else error),
            (48, 16, _raw(voltage, 100, 0x7FFF)),
            (64, 16, _raw(current, 10, 0x7FFF)),
            (80, 16, _raw(yield_today_kwh, 100, 0xFFFF)),
            (96, 16, 0xFFFF if pv_w is None else pv_w),
            (112, 9, _raw(load_a, 10, 0x1FF)),
        ],
        12,
    )


def ac_charger(state=None, error=0, outputs=((None, None),), temperature=None, ac_current=None):
    outputs = list(outputs) + [(None, None)] * (3 - len(outputs))
    fields = [(32, 8, 0xFF if state is None else state), (40, 8, 0xFF if error is None else error)]
    for (vstart, istart), (v, i) in zip(records.AcCharger.OUTPUTS, outputs):
        fields.append((vstart, 13, _raw(v, 100, 0x1FFF)))
        fields.append((istart, 11, _raw(i, 10, 0x7FF)))
    fields.append((120, 7, 0x7F if temperature is None else temperature + 40))
    fields.append((127, 9, _raw(ac_current, 10, 0x1FF)))
    return pack(fields, 13)


def manufacturer_data(product_id, record_type, nonce, key, payload):
    """Victron manufacturer data (after the company id), encrypted."""
    nonce_bytes = struct.pack("<H", nonce)
    return (
        bytes([0x10, 0x02])
        + struct.pack("<H", product_id)
        + bytes([record_type])
        + nonce_bytes
        + bytes([key[0]])
        + aes.ctr(key, nonce_bytes, payload)
    )


def advertisement(mfg_data, name=None):
    """Complete advertising data: flags, manufacturer data, optional name."""
    adv = bytes([2, 0x01, 0x06])
    m = struct.pack("<H", VICTRON) + mfg_data
    adv += bytes([len(m) + 1, 0xFF]) + m
    if name:
        n = name.encode()
        adv += bytes([len(n) + 1, 0x09]) + n
    return adv


def hci_event(address, rssi, adv, extended=False):
    """HCI LE (extended) advertising report event carrying one report."""
    addr = bytes(int(x, 16) for x in reversed(address.split(":")))
    if extended:
        report = (
            struct.pack("<H", 0x0010)
            + bytes([0])
            + addr
            + bytes([1, 0, 0xFF, 0x7F, rssi & 0xFF])
            + struct.pack("<H", 0)
            + bytes([0])
            + bytes(6)
            + bytes([len(adv)])
            + adv
        )
        params = bytes([0x0D, 1]) + report
    else:
        report = bytes([0x03, 0x00]) + addr + bytes([len(adv)]) + adv + bytes([rssi & 0xFF])
        params = bytes([0x02, 1]) + report
    return bytes([0x04, 0x3E, len(params)]) + params
