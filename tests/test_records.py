"""Decoder tests: specification layouts, real advertisements, round trips.

Real advertisements and keys come from the test suite of
https://github.com/keshavdv/victron-ble (public domain).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import advgen  # noqa: E402
from victronble import aes, records  # noqa: E402

BM = records.DECODERS[records.BATTERY_MONITOR]
SC = records.DECODERS[records.SOLAR_CHARGER]
AC = records.DECODERS[records.AC_CHARGER]


def decode_advert(hexdata, key):
    """Manufacturer data (after the company id) + key -> (product id, record type, values)."""
    product_id, record_type, nonce, keycheck, payload = records.parse_manufacturer_data(
        bytes.fromhex(hexdata)
    )
    key = aes.parse_key(key)
    assert keycheck == key[0], "key check byte mismatch"
    return product_id, record_type, records.DECODERS[record_type].decode(aes.ctr(key, nonce, payload))


class FieldsTest(unittest.TestCase):
    def test_bits_and_bounds(self):
        f = records.Fields(bytes([0x34, 0x12, 0xFF]))
        self.assertEqual(f.u(32, 16), 0x1234)
        self.assertEqual(f.u(36, 8), 0x23)
        self.assertEqual(f.s(48, 8), -1)
        self.assertIsNone(f.u(48, 8, na=0xFF))
        self.assertIsNone(f.u(44, 16))  # does not fit: payload is only 24 bits
        self.assertIsNone(f.u(0, 8))  # header bits are not part of the payload
        self.assertEqual(f.u(48, 8, na=(0x00, 0x01)), 0xFF)


class BatteryMonitorTest(unittest.TestCase):
    def test_real_advertisement(self):
        pid, rt, v = decode_advert(
            "100289a302b040af925d09a4d89aa0128bdef48c6298a9", "aff4d0995b7d1e176c0c33ecb9e70dcd"
        )
        self.assertEqual((pid, rt), (0xA389, records.BATTERY_MONITOR))  # SmartShunt 500A/50mV
        self.assertEqual(v["/Dc/0/Voltage"], 12.53)
        self.assertEqual(v["/Dc/0/Current"], 0.0)
        self.assertEqual(v["/Dc/0/Power"], 0.0)
        self.assertEqual(v["/Soc"], 50.0)
        self.assertEqual(v["/ConsumedAmphours"], -50.0)
        self.assertIsNone(v["/TimeToGo"])
        self.assertIsNone(v["/Dc/1/Voltage"])
        self.assertIsNone(v["/Dc/0/Temperature"])
        self.assertEqual(v["/Alarms/LowVoltage"], 0)
        self.assertIsNone(v["/Alarms/LowStarterVoltage"])  # aux input not used

    def test_decrypted_payload(self):
        v = BM.decode(bytes.fromhex("ffffe50400000000030000f40140df03"))
        self.assertEqual((v["/Dc/0/Voltage"], v["/Soc"], v["/ConsumedAmphours"]), (12.53, 50.0, -50.0))

    def test_aux_inputs(self):
        v = BM.decode(bytes.fromhex("ffffe6040000feff010000000080fe0c"))
        self.assertEqual(v["/Dc/0/MidVoltage"], 655.34)
        self.assertIsNotNone(v["/Alarms/MidVoltage"])
        v = BM.decode(bytes.fromhex("ffffe6040000feff000000000080feac"))
        self.assertEqual(v["/Dc/1/Voltage"], -0.02)
        self.assertEqual(v["/Alarms/LowStarterVoltage"], 0)
        # 0xFFFF Kelvin is "not available" (victron-ble would report 382.2 C)
        v = BM.decode(bytes.fromhex("ffffe6040000ffff020000000080fede"))
        self.assertIsNone(v["/Dc/0/Temperature"])
        self.assertEqual(v["/Alarms/HighTemperature"], 0)

    def test_round_trip(self):
        payload = advgen.battery_monitor(
            voltage=13.27,
            current=-12.345,
            soc=87.6,
            consumed_ah=-23.4,
            ttg_min=754,
            alarm=records.ALARM_LOW_SOC | records.ALARM_HIGH_TEMPERATURE,
            aux_mode=records.AUX_TEMPERATURE,
            aux_raw=round((21.5 + 273.15) * 100),
        )
        v = BM.decode(payload)
        self.assertEqual(v["/Dc/0/Voltage"], 13.27)
        self.assertEqual(v["/Dc/0/Current"], -12.345)
        self.assertEqual(v["/Dc/0/Power"], round(13.27 * -12.345, 1))
        self.assertEqual(v["/Soc"], 87.6)
        self.assertEqual(v["/ConsumedAmphours"], -23.4)
        self.assertEqual(v["/TimeToGo"], 754 * 60)
        self.assertAlmostEqual(v["/Dc/0/Temperature"], 21.5, places=2)
        self.assertEqual(v["/Alarms/LowSoc"], 2)
        self.assertEqual(v["/Alarms/HighTemperature"], 2)
        self.assertEqual(v["/Alarms/LowVoltage"], 0)
        self.assertIsNone(v["/Alarms/MidVoltage"])

    def test_midpoint_deviation(self):
        v = BM.decode(
            advgen.battery_monitor(
                voltage=25.0, current=1, soc=90, consumed_ah=0, aux_mode=records.AUX_MIDPOINT, aux_raw=1200
            )
        )
        self.assertEqual(v["/Dc/0/MidVoltage"], 12.0)
        self.assertEqual(v["/Dc/0/MidVoltageDeviation"], 4.0)  # top 13.0 V, bottom 12.0 V

    def test_not_available(self):
        v = BM.decode(advgen.battery_monitor())
        for path in (
            "/Dc/0/Voltage",
            "/Dc/0/Current",
            "/Dc/0/Power",
            "/Soc",
            "/ConsumedAmphours",
            "/TimeToGo",
        ):
            self.assertIsNone(v[path], path)
        # both NA values seen for the 22-bit current
        for raw in (0x3FFFFF, 0x1FFFFF):
            payload = advgen.pack([(48, 16, 1300), (96, 2, 3), (98, 22, raw)], 15)
            self.assertIsNone(BM.decode(payload)["/Dc/0/Current"])
        self.assertEqual(BM.decode(advgen.battery_monitor(consumed_ah=0))["/ConsumedAmphours"], 0.0)

    def test_truncated(self):
        v = BM.decode(advgen.battery_monitor(voltage=12.5, current=2, soc=50)[:6])
        self.assertEqual(v["/Dc/0/Voltage"], 12.5)
        self.assertIsNone(v["/Dc/0/Current"])
        self.assertIsNone(v["/Soc"])


class SolarChargerTest(unittest.TestCase):
    def test_real_advertisement(self):
        pid, rt, v = decode_advert(
            "100242a0016207adceb37b605d7e0ee21b24df5c", "adeccb947395801a4dd45a2eaa44bf17"
        )
        self.assertEqual((pid, rt), (0xA042, records.SOLAR_CHARGER))  # BlueSolar MPPT 75/15
        self.assertEqual(v["/State"], 4)  # absorption
        self.assertEqual(v["/ErrorCode"], 0)
        self.assertEqual(v["/Dc/0/Voltage"], 13.88)
        self.assertEqual(v["/Dc/0/Current"], 1.4)
        self.assertEqual(v["/History/Daily/0/Yield"], 0.03)
        self.assertEqual(v["/Yield/Power"], 19)
        self.assertEqual(v["/Load/I"], 0.0)

    def test_decrypted_payloads(self):
        v = SC.decode(bytes.fromhex("0300f80402000200030000fe8c9a5572"))
        self.assertEqual(v["/State"], 3)
        v = SC.decode(bytes.fromhex("0300fb09650032000901ffff31bc45ad"))
        self.assertEqual(
            (v["/Dc/0/Current"], v["/Dc/0/Voltage"], v["/Yield/Power"], v["/History/Daily/0/Yield"]),
            (10.1, 25.55, 265, 0.5),
        )
        self.assertIsNone(v["/Load/I"])  # no load output

    def test_round_trip(self):
        v = SC.decode(
            advgen.solar_charger(
                state=5, error=0, voltage=13.5, current=-0.4, yield_today_kwh=1.23, pv_w=0, load_a=2.5
            )
        )
        self.assertEqual(
            (v["/State"], v["/Dc/0/Current"], v["/History/Daily/0/Yield"], v["/Load/I"]), (5, -0.4, 1.23, 2.5)
        )
        self.assertNotIn("/Yield/User", v)  # added by the manager


class AcChargerTest(unittest.TestCase):
    def test_real_advertisement(self):
        pid, rt, v = decode_advert(
            "100030a308f926c1b5170a0d2280335bf12d5ed083", "c129cf8f75c3fe5a1655b481e205fb7d"
        )
        self.assertEqual((pid, rt), (0xA330, records.AC_CHARGER))  # Blue Smart IP22 12/30 (1)
        self.assertEqual(v["/State"], 6)  # storage
        self.assertEqual(v["/ErrorCode"], 0)
        self.assertEqual(v["/Dc/0/Voltage"], 13.49)
        self.assertEqual(v["/Dc/0/Current"], 0.4)
        self.assertIsNone(v["/Dc/1/Voltage"])
        self.assertIsNone(v["/Dc/1/Current"])
        self.assertEqual(v["/Dc/0/Temperature"], 21)
        self.assertIsNone(v["/Ac/In/L1/I"])
        self.assertEqual(v["/NrOfOutputs"], 1)

    def test_decrypted_payload(self):
        v = AC.decode(bytes.fromhex("060046a500ffffffffffffbdffeb3d1f"))
        self.assertEqual(
            (v["/State"], v["/Dc/0/Voltage"], v["/Dc/0/Current"], v["/Dc/0/Temperature"]), (6, 13.5, 0.5, 21)
        )

    def test_three_outputs(self):
        v = AC.decode(
            advgen.ac_charger(
                state=3, outputs=[(14.4, 10.0), (14.4, 5.5), (13.2, 0.0)], temperature=-5, ac_current=4.2
            )
        )
        self.assertEqual(v["/NrOfOutputs"], 3)
        self.assertEqual((v["/Dc/1/Voltage"], v["/Dc/1/Current"], v["/Dc/2/Voltage"]), (14.4, 5.5, 13.2))
        self.assertEqual(v["/Dc/0/Temperature"], -5)
        self.assertEqual(v["/Ac/In/L1/I"], 4.2)


class AdvertisementTest(unittest.TestCase):
    def test_header(self):
        self.assertIsNone(records.parse_manufacturer_data(bytes.fromhex("1002")))
        self.assertIsNone(records.parse_manufacturer_data(bytes.fromhex("110289a302b040af925d")))  # not 0x10
        product_id, record_type, nonce, keycheck, payload = records.parse_manufacturer_data(
            bytes.fromhex("100289a302bb01af129087600b9b97bc2c32867c8238da")
        )
        self.assertEqual(
            (product_id, record_type, nonce, keycheck, len(payload)),
            (0xA389, 2, bytes([0xBB, 0x01]), 0xAF, 15),
        )

    def test_wrong_key_detected(self):
        *_rest, keycheck, _payload = records.parse_manufacturer_data(
            bytes.fromhex("100289a302bb01af129087600b9b97bc2c32867c8238da")
        )
        self.assertNotEqual(keycheck, aes.parse_key("ff" * 16)[0])

    def test_encrypt_matches_device(self):
        """advgen encrypts exactly like a SmartShunt: re-encrypting the decrypted
        payload of a real advertisement gives the same bytes."""
        real = bytes.fromhex("100289a302b040af925d09a4d89aa0128bdef48c6298a9")
        key = aes.parse_key("aff4d0995b7d1e176c0c33ecb9e70dcd")
        _pid, _rt, nonce, _kc, payload = records.parse_manufacturer_data(real)
        plain = aes.ctr(key, nonce, payload)
        self.assertEqual(
            advgen.manufacturer_data(0xA389, 2, int.from_bytes(nonce, "little"), key, plain)[2:], real[2:]
        )

    def test_parse_key(self):
        self.assertEqual(aes.parse_key(" AF:F4 d0-99" + "00" * 12), bytes.fromhex("aff4d099" + "00" * 12))
        self.assertIsNone(aes.parse_key("abc"))
        self.assertIsNone(aes.parse_key("zz" * 16))
        self.assertIsNone(aes.parse_key(None))


if __name__ == "__main__":
    unittest.main()
