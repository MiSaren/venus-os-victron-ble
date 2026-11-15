"""Minimal CBOR (RFC 8949) as used by the VE.Smart Bluetooth protocol.

Integers, byte and text strings, arrays (also of indefinite length), maps,
tags and simple values. VE.Smart messages are CBOR sequences: several items
back to back, the first one being the opcode.
"""

import struct

BREAK = object()  # end of an indefinite length item


class Truncated(ValueError):
    """The data ended inside an item."""


def _head(major, n):
    mt = major << 5
    if n < 24:
        return bytes([mt | n])
    for ai, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if n < 1 << (8 * size):
            return bytes([mt | ai]) + n.to_bytes(size, "big")
    raise ValueError("integer too large: %d" % n)


def encode(value):
    if isinstance(value, bool):
        return b"\xf5" if value else b"\xf4"
    if value is None:
        return b"\xf6"
    if isinstance(value, int):
        return _head(0, value) if value >= 0 else _head(1, -1 - value)
    if isinstance(value, (bytes, bytearray)):
        return _head(2, len(value)) + bytes(value)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _head(3, len(raw)) + raw
    if isinstance(value, (list, tuple)):
        return _head(4, len(value)) + b"".join(encode(v) for v in value)
    if isinstance(value, dict):
        return _head(5, len(value)) + b"".join(encode(k) + encode(v) for k, v in value.items())
    raise TypeError("cannot encode %s" % type(value).__name__)


def encode_seq(*items):
    return b"".join(encode(i) for i in items)


class Decoder:
    def __init__(self, data):
        self.data = bytes(data)
        self.pos = 0

    def empty(self):
        return self.pos >= len(self.data)

    def rest(self):
        return self.data[self.pos :]

    def _take(self, n):
        if self.pos + n > len(self.data):
            raise Truncated("CBOR data ends inside an item")
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def _argument(self, ai):
        if ai < 24:
            return ai
        if 24 <= ai <= 27:
            return int.from_bytes(self._take(1 << (ai - 24)), "big")
        if ai == 31:
            return None  # indefinite length
        raise ValueError("invalid CBOR additional information %d" % ai)

    def item(self):
        initial = self._take(1)[0]
        major, ai = initial >> 5, initial & 31
        if major == 7:
            if ai == 31:
                return BREAK
            if ai in (20, 21):
                return ai == 21
            if ai in (22, 23):
                return None
            if ai == 25:
                return struct.unpack(">e", self._take(2))[0]
            if ai == 26:
                return struct.unpack(">f", self._take(4))[0]
            if ai == 27:
                return struct.unpack(">d", self._take(8))[0]
            return ("simple", self._take(1)[0] if ai == 24 else ai)
        n = self._argument(ai)
        if major == 0:
            return n
        if major == 1:
            return -1 - n
        if major in (2, 3):
            if n is None:
                parts = []
                while True:
                    part = self.item()
                    if part is BREAK:
                        break
                    parts.append(part if isinstance(part, bytes) else str(part).encode("utf-8"))
                raw = b"".join(parts)
            else:
                raw = self._take(n)
            return raw if major == 2 else raw.decode("utf-8", "replace")
        if major == 4:
            out = []
            while n is None or len(out) < n:
                value = self.item()
                if value is BREAK:
                    if n is not None:
                        raise ValueError("unexpected CBOR break")
                    break
                out.append(value)
            return out
        if major == 5:
            out = {}
            while n is None or len(out) < n:
                key = self.item()
                if key is BREAK:
                    break
                out[key if not isinstance(key, list) else tuple(key)] = self.item()
            return out
        return ("tag", n, self.item())


def decode_seq(data):
    """All items of a CBOR sequence; raises Truncated or ValueError."""
    dec = Decoder(data)
    items = []
    while not dec.empty():
        items.append(dec.item())
    return items
