"""Decoders for Victron "Instant Readout" BLE advertisement records.

Victron products with Bluetooth broadcast their main readings in the
manufacturer specific data (company 0x02E1) of their advertisements:

    byte 0      0x10, "product advertisement"
    byte 1      (not used here)
    byte 2-3    product id, little endian
    byte 4      record type
    byte 5-6    nonce / data counter, little endian
    byte 7      first byte of the encryption key (key check)
    byte 8-     record payload, AES-128-CTR encrypted

The payload layouts below follow Victron's "Extra manufacturer data"
specification (2022-12-14). The start bits in the tables are the ones from
that document: they count from the start of the record (record type, nonce,
key byte = 32 bits), so the encrypted payload starts at bit 32. All fields are
little endian bit fields.

Every decoder returns a dict of D-Bus path -> value for the matching Venus OS
service type; None means "not available" (an invalid D-Bus value).
"""

HEADER_BITS = 32

# Record types (byte 4)
SOLAR_CHARGER = 0x01
BATTERY_MONITOR = 0x02
INVERTER = 0x03
DCDC_CONVERTER = 0x04
SMART_LITHIUM = 0x05
INVERTER_RS = 0x06
GX_DEVICE = 0x07
AC_CHARGER = 0x08
SMART_BATTERY_PROTECT = 0x09
LYNX_SMART_BMS = 0x0A
MULTI_RS = 0x0B
VE_BUS = 0x0C
DC_ENERGY_METER = 0x0D
ORION_XS = 0x0F

RECORD_TYPE_NAMES = {
    0x00: "test record",
    SOLAR_CHARGER: "solar charger",
    BATTERY_MONITOR: "battery monitor",
    INVERTER: "inverter",
    DCDC_CONVERTER: "DC/DC converter",
    SMART_LITHIUM: "SmartLithium",
    INVERTER_RS: "Inverter RS",
    GX_DEVICE: "GX device",
    AC_CHARGER: "AC charger",
    SMART_BATTERY_PROTECT: "Smart BatteryProtect",
    LYNX_SMART_BMS: "(Lynx Smart) BMS",
    MULTI_RS: "Multi RS",
    VE_BUS: "VE.Bus",
    DC_ENERGY_METER: "DC energy meter",
    0x0E: "Smart BMS",
    ORION_XS: "Orion XS",
    0xFF: "extended record",
}


def record_type_name(record_type):
    return RECORD_TYPE_NAMES.get(record_type, "record type 0x%02X" % record_type)


class Fields:
    """Little endian bit fields of a decrypted record payload.

    u()/s() take the start bit as written in the specification (counted from
    the record header) and return None when the field is not (completely)
    present in the payload or holds one of the NA values. The specification
    asks for exactly this: records may get longer in later firmware, so only
    check that each field fits, never the total length."""

    __slots__ = ("_value", "_bits")

    def __init__(self, payload):
        self._value = int.from_bytes(payload, "little")
        self._bits = len(payload) * 8

    def u(self, start, bits, na=()):
        pos = start - HEADER_BITS
        if pos < 0 or pos + bits > self._bits:
            return None
        value = (self._value >> pos) & ((1 << bits) - 1)
        if value == na or (isinstance(na, tuple) and value in na):
            return None
        return value

    def s(self, start, bits, na=()):
        value = self.u(start, bits, na)
        if value is not None and value & (1 << (bits - 1)):
            value -= 1 << bits
        return value


def _scaled(value, divisor, digits):
    if value is None:
        return None
    return round(value / divisor, digits)


# Bits of VE_REG_ALARM_REASON (the same as the "AR" field of VE.Direct)
ALARM_LOW_VOLTAGE = 0x0001
ALARM_HIGH_VOLTAGE = 0x0002
ALARM_LOW_SOC = 0x0004
ALARM_LOW_STARTER_VOLTAGE = 0x0008
ALARM_HIGH_STARTER_VOLTAGE = 0x0010
ALARM_LOW_TEMPERATURE = 0x0020
ALARM_HIGH_TEMPERATURE = 0x0040
ALARM_MID_VOLTAGE = 0x0080

# Venus OS alarm levels
ALARM_OK = 0
ALARM_ALARM = 2

# VE_REG_BMV_AUX_INPUT
AUX_STARTER = 0
AUX_MIDPOINT = 1
AUX_TEMPERATURE = 2
AUX_NONE = 3


class Decoder:
    """Base class: one record type, mapped onto one Venus OS service type."""

    record_type = None
    role = None  # com.victronenergy.<role>
    generic_name = None  # product name when the product id is unknown
    paths = {}  # D-Bus path -> text format (for GetText)

    def decode(self, payload):
        raise NotImplementedError


class BatteryMonitor(Decoder):
    """SmartShunt, BMV-712 Smart... -> com.victronenergy.battery"""

    record_type = BATTERY_MONITOR
    role = "battery"
    generic_name = "Battery monitor"

    ALARMS = (
        (ALARM_LOW_VOLTAGE, "/Alarms/LowVoltage", None),
        (ALARM_HIGH_VOLTAGE, "/Alarms/HighVoltage", None),
        (ALARM_LOW_SOC, "/Alarms/LowSoc", None),
        (ALARM_LOW_STARTER_VOLTAGE, "/Alarms/LowStarterVoltage", AUX_STARTER),
        (ALARM_HIGH_STARTER_VOLTAGE, "/Alarms/HighStarterVoltage", AUX_STARTER),
        (ALARM_LOW_TEMPERATURE, "/Alarms/LowTemperature", AUX_TEMPERATURE),
        (ALARM_HIGH_TEMPERATURE, "/Alarms/HighTemperature", AUX_TEMPERATURE),
        (ALARM_MID_VOLTAGE, "/Alarms/MidVoltage", AUX_MIDPOINT),
    )

    paths = {
        "/Dc/0/Voltage": "%.2fV",
        "/Dc/0/Current": "%.2fA",
        "/Dc/0/Power": "%.0fW",
        "/Dc/0/Temperature": "%.1fC",
        "/Dc/0/MidVoltage": "%.2fV",
        "/Dc/0/MidVoltageDeviation": "%.1f%%",
        "/Dc/1/Voltage": "%.2fV",
        "/Soc": "%.1f%%",
        "/ConsumedAmphours": "%.1fAh",
        "/TimeToGo": "%.0fs",
    }
    paths.update((path, "%d") for _bit, path, _aux in ALARMS)

    def decode(self, payload):
        f = Fields(payload)
        ttg = f.u(32, 16, na=0xFFFF)  # 1 min
        voltage = f.s(48, 16, na=0x7FFF)  # 0.01 V
        alarm = f.u(64, 16)  # VE_REG_ALARM_REASON
        aux_mode = f.u(96, 2, na=AUX_NONE)  # VE_REG_BMV_AUX_INPUT
        # 0.001 A. The specification gives 0x3FFFFF as NA value, other
        # decoders have seen 0x1FFFFF; neither is a realistic reading.
        current = f.s(98, 22, na=(0x3FFFFF, 0x1FFFFF))
        consumed = f.u(120, 20, na=0xFFFFF)  # 0.1 Ah, negated
        soc = f.u(140, 10, na=0x3FF)  # 0.1 %

        v = _scaled(voltage, 100, 2)
        i = _scaled(current, 1000, 3)

        values = {
            "/Dc/0/Voltage": v,
            "/Dc/0/Current": i,
            "/Dc/0/Power": None if v is None or i is None else round(v * i, 1),
            "/Soc": _scaled(soc, 10, 1),
            "/ConsumedAmphours": None if consumed is None else (-consumed / 10.0 if consumed else 0.0),
            "/TimeToGo": None if ttg is None else ttg * 60,
            "/Dc/1/Voltage": None,
            "/Dc/0/MidVoltage": None,
            "/Dc/0/MidVoltageDeviation": None,
            "/Dc/0/Temperature": None,
        }

        # Aux input: one 16-bit field, meaning depends on the aux mode. The
        # specification lists no NA values here; use the usual VE.Reg ones.
        if aux_mode == AUX_STARTER:
            values["/Dc/1/Voltage"] = _scaled(f.s(80, 16, na=0x7FFF), 100, 2)
        elif aux_mode == AUX_MIDPOINT:
            mid = _scaled(f.u(80, 16, na=0xFFFF), 100, 2)
            values["/Dc/0/MidVoltage"] = mid
            if mid is not None and v:
                # BMV manual: deviation = 100 * (top half - bottom half) / battery voltage
                values["/Dc/0/MidVoltageDeviation"] = round(100.0 * (v - 2 * mid) / v, 1)
        elif aux_mode == AUX_TEMPERATURE:
            kelvin = f.u(80, 16, na=0xFFFF)  # 0.01 K
            if kelvin is not None:
                values["/Dc/0/Temperature"] = round(kelvin / 100.0 - 273.15, 2)

        for bit, path, needs_aux in self.ALARMS:
            if alarm is None or (needs_aux is not None and aux_mode != needs_aux):
                values[path] = None
            else:
                values[path] = ALARM_ALARM if alarm & bit else ALARM_OK

        return values


class SolarCharger(Decoder):
    """SmartSolar MPPT, BlueSolar MPPT + VE.Direct Bluetooth dongle...
    -> com.victronenergy.solarcharger"""

    record_type = SOLAR_CHARGER
    role = "solarcharger"
    generic_name = "Solar charger"

    paths = {
        "/State": "%d",
        "/ErrorCode": "%d",
        "/Dc/0/Voltage": "%.2fV",
        "/Dc/0/Current": "%.1fA",
        "/Yield/Power": "%.0fW",
        "/History/Daily/0/Yield": "%.2fkWh",
        "/Load/I": "%.1fA",
        # Total yield for VRM's energy counters; not in the record, the
        # manager derives it from the daily yield (see YieldCounter)
        "/Yield/User": "%.2fkWh",
    }

    def decode(self, payload):
        f = Fields(payload)
        return {
            "/State": f.u(32, 8, na=0xFF),  # VE_REG_DEVICE_STATE
            "/ErrorCode": f.u(40, 8, na=0xFF),  # VE_REG_CHR_ERROR_CODE
            "/Dc/0/Voltage": _scaled(f.s(48, 16, na=0x7FFF), 100, 2),
            # 0.1 A, negative when a load on the load output draws from the battery
            "/Dc/0/Current": _scaled(f.s(64, 16, na=0x7FFF), 10, 1),
            "/History/Daily/0/Yield": _scaled(f.u(80, 16, na=0xFFFF), 100, 2),  # 0.01 kWh
            "/Yield/Power": f.u(96, 16, na=0xFFFF),  # 1 W
            "/Load/I": _scaled(f.u(112, 9, na=0x1FF), 10, 1),
        }


class AcCharger(Decoder):
    """Blue Smart IP22/IP65/IP67 chargers, Phoenix Smart IP43 chargers...
    -> com.victronenergy.charger

    The specification marks this layout "TBD"; it matches what the chargers
    actually send (verified against real IP22 advertisements)."""

    record_type = AC_CHARGER
    role = "charger"
    generic_name = "AC charger"

    OUTPUTS = ((48, 61), (72, 85), (96, 109))  # start bits of voltage, current

    paths = {
        "/State": "%d",
        "/ErrorCode": "%d",
        "/NrOfOutputs": "%d",
        "/Dc/0/Temperature": "%.0fC",
        "/Ac/In/L1/I": "%.1fA",
    }
    for _n in range(len(OUTPUTS)):
        paths["/Dc/%d/Voltage" % _n] = "%.2fV"
        paths["/Dc/%d/Current" % _n] = "%.1fA"
    del _n

    def decode(self, payload):
        f = Fields(payload)
        values = {
            "/State": f.u(32, 8, na=0xFF),  # VE_REG_DEVICE_STATE
            "/ErrorCode": f.u(40, 8, na=0xFF),  # VE_REG_CHR_ERROR_CODE
        }
        outputs = 0
        for n, (vstart, istart) in enumerate(self.OUTPUTS):
            voltage = _scaled(f.u(vstart, 13, na=0x1FFF), 100, 2)  # 0.01 V
            values["/Dc/%d/Voltage" % n] = voltage
            values["/Dc/%d/Current" % n] = _scaled(f.u(istart, 11, na=0x7FF), 10, 1)  # 0.1 A
            if voltage is not None:
                outputs = n + 1
        temperature = f.u(120, 7, na=0x7F)  # 1 C, offset -40
        values["/Dc/0/Temperature"] = None if temperature is None else temperature - 40
        values["/Ac/In/L1/I"] = _scaled(f.u(127, 9, na=0x1FF), 10, 1)  # 0.1 A
        values["/NrOfOutputs"] = outputs or None
        return values


DECODERS = dict((d.record_type, d) for d in (BatteryMonitor(), SolarCharger(), AcCharger()))

# Optional second service for an AC charger: a DC source of the type "AC
# charger" (VE_REG_BMV_MONITOR_MODE -2, as a SmartShunt in DC energy meter mode
# would publish it). gui-v2 shows such a source with its power on the overview
# page; a com.victronenergy.charger service only gets the "Inverter / Charger"
# widget, which shows the system state.
MONITOR_MODE_AC_CHARGER = -2
DCSOURCE_PATHS = {
    "/Dc/0/Voltage": "%.2fV",
    "/Dc/0/Current": "%.1fA",
    "/Dc/0/Power": "%.0fW",
    "/Dc/0/Temperature": "%.0fC",
}


def charger_dc_totals(values):
    """An AC charger's decoded values -> DC source values: output 1 voltage,
    total current and power of all outputs. No energy counter on purpose."""
    power = current = None
    for n in range(len(AcCharger.OUTPUTS)):
        voltage, amps = values.get("/Dc/%d/Voltage" % n), values.get("/Dc/%d/Current" % n)
        if voltage is None or amps is None:
            continue
        power = (power or 0.0) + voltage * amps
        current = (current or 0.0) + amps
    return {
        "/Dc/0/Voltage": values.get("/Dc/0/Voltage"),
        "/Dc/0/Current": None if current is None else round(current, 1),
        "/Dc/0/Power": None if power is None else round(power, 1),
        "/Dc/0/Temperature": values.get("/Dc/0/Temperature"),
    }


def parse_manufacturer_data(data):
    """Split Victron manufacturer data (after the 0x02E1 company id).

    Returns (product_id, record_type, nonce bytes, key check byte, encrypted
    payload) or None when this is not an instant readout record."""
    if len(data) < 8 or data[0] != 0x10:
        return None
    product_id = data[2] | (data[3] << 8)
    record_type = data[4]
    return product_id, record_type, bytes(data[5:7]), data[7], bytes(data[8:])
