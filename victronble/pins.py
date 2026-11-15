"""Bluetooth PINs of the chargers that may be controlled (night mode).

Kept in a file that only root can read (pins.json next to the service), not in
localsettings: settings are readable by anyone on the LAN through MQTT, and
the PIN gives control over the charger. A device is controlled when it has a
PIN here.
"""

import json
import os

from . import settings as st


def valid_pin(text):
    """A Victron Bluetooth PIN: 6 digits. Returns it as a string, or None."""
    text = str(text or "").strip()
    return text if len(text) == 6 and text.isdigit() else None


def load(path):
    """{mac: pin}; an absent or broken file gives {}."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    pins = {}
    for mac, pin in data.items():
        mac, pin = st.normalize_mac(mac), valid_pin(pin)
        if mac and pin:
            pins[mac] = pin
    return pins


def save(path, pins):
    """Write atomically with mode 0600 (set before any PIN is written)."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            fd = None
            json.dump(dict(sorted(pins.items())), f, indent=1)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    finally:
        if fd is not None:
            os.close(fd)
    os.replace(tmp, path)


def macs(path):
    """The devices with a PIN, without keeping the PINs around."""
    return set(load(path))
