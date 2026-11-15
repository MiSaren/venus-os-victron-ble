"""HCI event and advertising data parsing."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import advgen  # noqa: E402
from victronble import hci  # noqa: E402

MFG = bytes.fromhex("100289a302b040af925d09a4d89aa0128bdef48c6298a9")
ADDRESS = "C0:FF:EE:12:34:56"


class EventTest(unittest.TestCase):
    def test_legacy_report(self):
        adv = advgen.advertisement(MFG, name="SmartShunt")
        reports = list(hci.parse_event(advgen.hci_event(ADDRESS, -71, adv)))
        self.assertEqual(reports, [(ADDRESS, -71, adv)])

    def test_extended_report(self):
        adv = advgen.advertisement(MFG)
        reports = list(hci.parse_event(advgen.hci_event(ADDRESS, -90, adv, extended=True)))
        self.assertEqual(reports, [(ADDRESS, -90, adv)])

    def test_two_reports(self):
        adv1 = advgen.advertisement(MFG)
        adv2 = bytes([2, 1, 6])
        r1 = advgen.hci_event("00:11:22:33:44:55", -50, adv1)[5:]
        r2 = advgen.hci_event("66:77:88:99:AA:BB", -60, adv2)[5:]
        params = bytes([0x02, 2]) + r1 + r2
        pkt = bytes([0x04, 0x3E, len(params)]) + params
        self.assertEqual(
            [r[:2] for r in hci.parse_event(pkt)], [("00:11:22:33:44:55", -50), ("66:77:88:99:AA:BB", -60)]
        )

    def test_malformed(self):
        pkt = advgen.hci_event(ADDRESS, -71, advgen.advertisement(MFG))
        for n in range(len(pkt)):
            list(hci.parse_event(pkt[:n]))  # must not raise
        self.assertEqual(list(hci.parse_event(b"")), [])
        bad_len = bytearray(pkt)
        bad_len[13] = 200  # data length beyond the event
        self.assertEqual(list(hci.parse_event(bytes(bad_len))), [])
        self.assertEqual(list(hci.parse_event(bytes([0x04, 0x0E, 4, 1, 0x0C, 0x20, 0]))), [])  # cmd complete


class AdvertisingDataTest(unittest.TestCase):
    def test_finds_victron(self):
        adv = bytes([2, 1, 6, 5, 0xFF, 0x4C, 0x00, 1, 2]) + advgen.advertisement(MFG)[3:]
        self.assertEqual(hci.manufacturer_data(adv, 0x02E1), MFG)
        self.assertEqual(hci.manufacturer_data(adv, 0x004C), bytes([1, 2]))
        self.assertIsNone(hci.manufacturer_data(adv, 0x0499))

    def test_bad_structures(self):
        self.assertIsNone(hci.manufacturer_data(bytes([0, 0xFF, 0xE1, 0x02]), 0x02E1))  # zero length
        self.assertIsNone(hci.manufacturer_data(bytes([9, 0xFF, 0xE1, 0x02, 0x10]), 0x02E1))  # overrun
        self.assertIsNone(hci.manufacturer_data(bytes([2, 0xFF, 0xE1]), 0x02E1))  # too short for an id
        self.assertIsNone(hci.manufacturer_data(b"", 0x02E1))

    def test_address(self):
        self.assertEqual(hci.format_address(bytes([0x56, 0x34, 0x12, 0xEE, 0xFF, 0xC0])), ADDRESS)


if __name__ == "__main__":
    unittest.main()
