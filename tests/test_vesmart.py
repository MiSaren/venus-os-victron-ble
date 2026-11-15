"""VE.Smart protocol pieces without Bluetooth: CBOR, frames, chunks, messages.

Byte sequences are from a Blue Smart IP22 12/30 (firmware 3.69) and from the
VictronConnect traffic quoted in community notes."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from victronble import cbor, vesmart  # noqa: E402


class CborTest(unittest.TestCase):
    def test_integers(self):
        for value, hexed in (
            (0, "00"),
            (23, "17"),
            (24, "1818"),
            (255, "18ff"),
            (256, "190100"),
            (0xE001, "19e001"),
            (65536, "1a00010000"),
            (-1, "20"),
            (-25, "3818"),
        ):
            self.assertEqual(cbor.encode(value).hex(), hexed, value)
            self.assertEqual(cbor.decode_seq(bytes.fromhex(hexed)), [value])

    def test_strings_and_containers(self):
        value = [b"\x02", "Night mode", [1, 2], {1: b""}, True, False, None]
        self.assertEqual(cbor.decode_seq(cbor.encode(value)), [value])

    def test_indefinite_array(self):
        self.assertEqual(cbor.decode_seq(bytes.fromhex("029f0000ff")), [2, [0, 0]])

    def test_victronconnect_init(self):
        # keep-alive 10 s on instance 0, subscribe instances 1 and 3 (SmartSolar)
        self.assertEqual(
            cbor.decode_seq(bytes.fromhex("060082189342102703010303")),
            [6, 0, [0x93, b"\x10\x27"], 3, 1, 3, 3],
        )

    def test_truncated(self):
        for hexed in ("19e0", "4302", "82 01".replace(" ", ""), "9f00"):
            with self.assertRaises(ValueError, msg=hexed):
                cbor.decode_seq(bytes.fromhex(hexed))


class FrameTest(unittest.TestCase):
    def test_get(self):
        self.assertEqual(vesmart.get_frame(0, [0xEDF1]).hex(), "05008119edf1")
        self.assertEqual(vesmart.get_frame(0, [0xE001, 0xEDF0, 0x0201]).hex(), "05008319e00119edf0190201")

    def test_subscribe_and_device_list(self):
        self.assertEqual(vesmart.subscribe_frame(0), b"\x03\x00")
        self.assertEqual(vesmart.device_list_frame(), b"\x01")

    def test_night_frames(self):
        self.assertEqual(vesmart.night_frame(True).hex(), "06008219e0014102")
        self.assertEqual(vesmart.night_frame(False).hex(), "06008219e0014100")

    def test_only_night_mode_sets_pass(self):
        for frame in (vesmart.night_frame(True), vesmart.night_frame(False), vesmart.get_frame(0, [0x0200])):
            self.assertEqual(vesmart.check_frame(frame), frame)
        for hexed in ("06008219e0014101", "06008219edf14102", "0600821902004104", "06018219e0014102"):
            with self.assertRaises(vesmart.SessionError, msg=hexed):
                vesmart.check_frame(bytes.fromhex(hexed))

    def test_split_chunk(self):
        chunk = bytes(range(45))
        parts = vesmart.split_chunk(chunk, 20)
        self.assertEqual([u for u, _ in parts], [vesmart.UUID_DATA, vesmart.UUID_DATA, vesmart.UUID_LAST])
        self.assertEqual(b"".join(p for _, p in parts), chunk)
        self.assertEqual(vesmart.split_chunk(b"\x01", 20), [(vesmart.UUID_LAST, b"\x01")])
        self.assertEqual(vesmart.split_chunk(bytes(20), 20), [(vesmart.UUID_LAST, bytes(20))])

    def test_control_info(self):
        info = vesmart.parse_control_info(bytes.fromhex("00010001501400"))
        self.assertEqual(
            (info["flags"], info["max_instances"], info["max_free_chunks"], info["max_chunk_size"]),
            (0, 1, 1, 80),
        )
        self.assertEqual(info["att_length"], 20)
        self.assertNotIn("att_length", vesmart.parse_control_info(bytes.fromhex("0008000150")))


class MessageTest(unittest.TestCase):
    def test_parse(self):
        chunk = (
            cbor.encode_seq(2, [0, 0])
            + bytes.fromhex("07000300")  # subscribe ok
            + bytes.fromhex("080019e0014102")  # 0xE001 = 2
            + bytes.fromhex("080019edf0429600")  # 0xEDF0 = 150
            + bytes.fromhex("090019020001")  # 0x0200: error 1 (unknown)
        )
        self.assertEqual(
            vesmart.parse_messages(chunk),
            [
                ("devices", [0, 0]),
                ("response", 0, 3, 0),
                ("value", 0, 0xE001, b"\x02"),
                ("value", 0, 0xEDF0, b"\x96\x00"),
                ("error", 0, 0x0200, 1),
            ],
        )

    def test_unknown_opcode_stops(self):
        self.assertEqual(vesmart.parse_messages(bytes.fromhex("0d00ff")), [("unknown", 13, b"\x00\xff")])

    def test_unknown_instance_response(self):
        # what an IP65 answered to a SET for instance 3: "unknown instance"
        self.assertEqual(vesmart.parse_messages(bytes.fromhex("07030602")), [("response", 3, 6, 2)])

    def test_describe(self):
        values = {0xE001: b"\x02", 0xEDF0: b"\x96\x00", 0x0201: b"\x05"}
        self.assertEqual(
            vesmart.describe(values), {"mode": 2, "night": True, "max_current": 15.0, "state": 5}
        )
        values = {0xE001: b"\x00", 0xEDF0: b"\x2c\x01", 0x0201: b"\x03"}
        self.assertEqual(
            vesmart.describe(values), {"mode": 0, "night": False, "max_current": 30.0, "state": 3}
        )
        self.assertEqual(vesmart.describe({})["night"], None)

    def test_explain(self):
        class Err(Exception):
            def __init__(self, name, text):
                self.name, self.text = name, text

            def get_dbus_name(self):
                return self.name

            def get_dbus_message(self):
                return self.text

        self.assertIn(
            "PIN", vesmart.explain(Err("org.bluez.Error.AuthenticationFailed", "Authentication Failed"))
        )
        self.assertIn(
            "not reachable", vesmart.explain(Err("org.bluez.Error.Failed", "le-connection-abort-by-local"))
        )
        self.assertEqual(vesmart.explain(vesmart.SessionError("no device list")), "no device list")


if __name__ == "__main__":
    unittest.main()
