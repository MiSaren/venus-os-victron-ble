# Design notes

## Goals and constraints

- Monitor Victron Bluetooth devices on Venus OS through their *Instant
  Readout* advertisements, read only.
- One opt-in exception: switch the night mode of Blue Smart chargers over a
  short GATT connection (see *Night mode control* below).
- Publish them as native Venus OS services, so that the GUI, VRM, MQTT and
  Node-RED work with them as with wired devices.
- Never disturb Venus OS' own `dbus-ble-sensors` (RuuviTags, tank sensors,
  Lynx Smart BMS NG, SolarSense).
- Survive firmware updates: no changes to the read-only root filesystem, no
  compiled code, no pip packages.

## Architecture

```
Bluetooth controller (hci0, hci1...)
  |  LE advertising reports of the scan that dbus-ble-sensors keeps running
  +--> dbus-ble-sensors       raw HCI socket: RuuviTags, tanks, Lynx BMS NG, SolarSense
  +--> dbus-victron-ble       raw HCI socket, receive only
        hci.Listener ........ socket per controller, parses HCI LE (extended) advertising reports
        Manager ............. Victron manufacturer data -> key check -> AES-128-CTR -> decoder
        records.* ........... bit field decoders, one per record type
        DeviceService ....... com.victronenergy.<role>.victron_<mac> (velib_python's vedbus)
        settings ............ com.victronenergy.settings /Settings/Devices/victron_<mac>/...
        status.json ......... /run/dbus-victron-ble/, read by the victron-ble tool
        state.json .......... /data/victron-ble/, the solar yield counters
```

The Linux kernel delivers every HCI event to each raw HCI socket bound to the
controller (`hci_send_to_sock`), each socket with its own filter. The listener
only sets a filter for LE meta events and never sends a command, so the scan
stays fully under `dbus-ble-sensors`' control. Python on Venus OS is built
without `AF_BLUETOOTH`, so the socket is created with ctypes; reading uses a
GLib IO watch.

## Advertisement format

Manufacturer specific data, company id 0x02E1, after the company id:

| Byte | Content |
|---|---|
| 0 | 0x10, product advertisement |
| 1 | (not used) |
| 2-3 | product id, little endian |
| 4 | record type |
| 5-6 | nonce / data counter, little endian |
| 7 | first byte of the encryption key, for a quick key check |
| 8- | record payload, AES-128-CTR encrypted |

The counter block is the nonce (as sent) followed by 14 zero bytes. All
current records fit in one AES block. Without Instant readout enabled, a
device only sends the first four bytes (no record type), which is how
`victron-ble scan` reports *instant readout off*.

Devices in a VE.Smart network also send non-connectable advertisements whose
manufacturer data starts with 0x02. These carry the network data, encrypted
with the network key, and are ignored.

## Record layouts

Start bits as in Victron's *Extra manufacturer data* document (2022-12-14),
counted from the start of the record. The encrypted payload starts at bit 32.
All fields are little endian bit fields. The decoders only check that each
field fits into the received payload, never the total length, because the
specification allows records to grow.

**Battery monitor (0x02)**

| Bit | Bits | Field | Unit | NA |
|---|---|---|---|---|
| 32 | 16 | time to go | 1 min | 0xFFFF |
| 48 | 16 | battery voltage, signed | 0.01 V | 0x7FFF |
| 64 | 16 | alarm reason | bits | - |
| 80 | 16 | aux: starter voltage (signed, 0.01 V), mid voltage (0.01 V) or temperature (0.01 K) | | 0x7FFF / 0xFFFF* |
| 96 | 2 | aux input: 0 starter, 1 mid, 2 temperature, 3 none | | 3 |
| 98 | 22 | battery current, signed | 0.001 A | 0x3FFFFF, 0x1FFFFF* |
| 120 | 20 | consumed Ah (negated) | 0.1 Ah | 0xFFFFF |
| 140 | 10 | state of charge | 0.1 % | 0x3FF |

\* The specification gives no NA value for the aux field; the usual VE.Reg
values are used. For the current it gives 0x3FFFFF; other decoders have seen
0x1FFFFF, which is no realistic reading either, so both are treated as NA.

**Solar charger (0x01)**

| Bit | Bits | Field | Unit | NA |
|---|---|---|---|---|
| 32 | 8 | device state | | 0xFF |
| 40 | 8 | charger error | | 0xFF |
| 48 | 16 | battery voltage, signed | 0.01 V | 0x7FFF |
| 64 | 16 | battery current, signed | 0.1 A | 0x7FFF |
| 80 | 16 | yield today | 0.01 kWh | 0xFFFF |
| 96 | 16 | PV power | 1 W | 0xFFFF |
| 112 | 9 | load output current | 0.1 A | 0x1FF |

**AC charger (0x08)** (marked "TBD" in the specification; real chargers send
exactly this)

| Bit | Bits | Field | Unit | NA |
|---|---|---|---|---|
| 32 | 8 | device state | | 0xFF |
| 40 | 8 | charger error | | 0xFF |
| 48 / 72 / 96 | 13 | output 1/2/3 voltage | 0.01 V | 0x1FFF |
| 61 / 85 / 109 | 11 | output 1/2/3 current | 0.1 A | 0x7FF |
| 120 | 7 | temperature, offset -40 | 1 °C | 0x7F |
| 127 | 9 | AC input current | 0.1 A | 0x1FF |

## D-Bus mapping

The paths follow what the Venus OS consumers read:

- **systemcalc:** battery `/Dc/0/*`, `/Soc`, `/TimeToGo` and
  `/ConsumedAmphours`; solar charger `/Dc/0/*` and `/Load/I`; charger
  `/Dc/0..2/*`; `/Connected`, `/ProductName` and `/Mgmt/Connection` for
  selecting services.
- **vrmlogger:** the same, plus `/Yield/Power`, `/History/Daily/0/Yield`,
  `/State`, `/ErrorCode`, the alarms and `/Yield/User` for the kWh counters.
- **gui-v2:** the device pages; paths that are not published are hidden there.

Derived values:

- `/Dc/0/Power` = V × I (systemcalc takes the battery power from it).
- `/Dc/0/MidVoltageDeviation` = 100 × (V − 2 × Vmid) / V, as in the BMV manual.
- `/TimeToGo` in seconds; invalid when the record says NA (charging).
- `/ConsumedAmphours` is negative, as for wired battery monitors.
- Alarms: 0 or 2 per alarm reason bit. The starter, temperature and midpoint
  alarms are only valid when the aux input is set to that mode.
- `/NrOfOutputs` = number of charger outputs with a valid voltage.
- `/Mgmt/InsecureConnection` = 1, so that systemcalc does not use the voltage
  for shared voltage sense.

**DC source for an AC charger (optional).** gui-v2 shows a
`com.victronenergy.charger` service only in its *Inverter / Charger* widget,
whose text is the system state (*Off* without an inverter/charger). DC sources
appear on the overview with their power instead; a `dcsource` with
`/Settings/MonitorMode` = -2 gets the *AC charger* box. With `DcSource` = 1 in
a charger's settings, the charger is therefore also published as
`com.victronenergy.dcsource.victron_<mac>`. It shows output 1 voltage, total
current, total power and temperature. The charger service stays, because only
it enters systemcalc's DC loads calculation. systemcalc does not count DC
sources there, so nothing is counted twice. The DC source has no
`/History/EnergyOut`, which vrmlogger would add to the alternator/DC source
energy. The cost is a second entry in the VRM device list. The DC source has its own VRM
instance from the settings group `victron_<mac>_dcsource` and lives and dies
with the charger service. The approach follows ChargerMeter (mebitek), which
mirrors a wired IP22 in the same way; no code was taken from it.

gui-v2 (v1.3) has a catch with DC sources that appear while it runs. It
fetches a new service's items with one `GetItems` call and creates them in
alphabetical order (veutil keeps them in a `QMap`). It treats a service as a
device as soon as it has a device instance and a name. A DC meter model then
reads `/Settings/MonitorMode` once, and that item is created after
`/DeviceInstance`, `/CustomName` and `/ProductName`. The source therefore
becomes a *Generic source* for good, with 0 A, because the widget's own
filter later sees the real type. The DC source service therefore starts without
`/ProductName` and `/CustomName`. It publishes them about 10 seconds later,
when gui-v2 has all items and `/Settings/MonitorMode` decides the type. The
device instance is valid from the start, so dbus-monitor based services
(systemcalc, vrmlogger) see a normal service.

**`/Yield/User`.** VRM computes solar energy from increases of the total yield
(vrmlogger's kWh deltas). It ignores a counter at zero and flags counters that
go backwards, so the synthetic counter starts invalid, only grows, and is kept
in `state.json`. A lower daily yield than before means the charger started a
new day; the new daily yield is then counted in full. The file is written at
most every 10 minutes and at shutdown. After a crash the counter can lose up to
10 minutes of increments, but it never goes backwards.

## Settings and identity

- Device id `victron_<mac>`, with the same prefix and settings group as
  `dbus-ble-sensors` uses for the Victron devices it supports, so that keys,
  names and VRM instances would carry over to a native implementation.
- The VRM instance comes from `ClassAndVrmInstance` (default `<role>:20`).
  localsettings moves a new device to the next free instance.
- Only devices with a valid key are published; others are listed in the status
  (at most 100, forgotten after an hour without advertisements).
- `DcSource` (0/1): also publish an AC charger as a DC source (see above).
- If `com.victronenergy.ble` lists the device as enabled
  (`/Devices/victron_<mac>/Enabled` = 1), `dbus-ble-sensors` publishes it itself
  and this service steps aside.

## Lifecycle

- A service is created on the first advertisement that decrypts with the
  configured key. Each service has its own private D-Bus connection, because
  Venus services export their paths at the object root. libdbus'
  *exit on disconnect* is switched off for these connections, otherwise
  closing one would `_exit(1)` the whole process.
- Repeated advertisements with the same nonce and payload only refresh the
  timeout. The data is unchanged by definition.
- No data for 300 s: the service is removed. It comes back with the next
  advertisement, also when the data did not change in the meantime.
- Wrong key (key check byte mismatch): the service is removed and a warning is
  logged every 10 minutes. A new key takes effect immediately.
- Settings are read at start (retried until localsettings is up), on every
  `PropertiesChanged` of a `Key`, `CustomName` or `DcSource` setting (within
  0.5 s), and every 60 seconds.
- A record type change (a SmartShunt switched to DC energy meter mode, for
  example) replaces the service.

## Scanning

`dbus-ble-sensors` sets up a passive scan with duplicate filtering off and
re-enables it every 10 s. By default the interval is 90 and the window is 15
(× 0.625 ms), so about one sixth of the time is spent listening. With
*Continuous scanning* both are 15, so it listens all the time.
`dbus-victron-ble` only enables scanning itself (LE Set Scan Enable) when
nothing at all has been received for 120 seconds. The night mode control is
the other exception: around its connections it resets the duplicate filter
and restores this scan (next section).

## Night mode control

Opt-in per charger: a charger with a PIN in `pins.json` (root only) gets a
`ChargerControl` and, once its state has been read, a
`com.victronenergy.switch.victron_<mac>` service with one toggle output.

```
Manager ─ ChargerControl ─ HelperRunner ─ python3 -m victronble.vesmart (one session per process)
   │        state, polling,   one session        BlueZ D-Bus: connect, pair, GATT, VE.Smart
   │        requests          at a time,         raw HCI: duplicate filter reset, scan restore
   └ SwitchService            90 s timeout       prints one JSON line
     /SwitchableOutput/night/...
```

The Bluetooth session runs in a helper process. It waits synchronously and
iterates its own GLib main loop. A stuck session can then not block the
listener and the services, and the runner kills it after 90 s.

**VE.Smart protocol.** This is VictronConnect's protocol. Venus OS contains
its server side in `/opt/victronenergy/vesmart-server`, for VictronConnect
connecting to the GX itself. It uses GATT service
306b0001-b081-4037-83dc-e59fcc3cdfd0 with three characteristics:

- 306b0002 is the control characteristic. A read returns flags, the maximum
  number of instances, free chunks, chunk size and ATT length. Written or
  notified flow control messages are F9 *n* (*n* more chunks may be sent),
  FA (chunk info), F8 (abort) and F7 (error).
- 306b0003 carries the last piece of a chunk.
- 306b0004 carries the other pieces.

A chunk is a CBOR sequence of messages:

| Message | Meaning | Answer |
|---|---|---|
| `1` | device list | `2 [instance, type, ...]` |
| `3 inst` | subscribe | `7 inst 3 result` |
| `5 inst [vregs]` | get | `8 inst vreg bytes` or `9 inst vreg error` |
| `6 inst [vreg, bytes]` | set | `8 ...` (echo) |

Values are VE.Direct registers as little endian bytes.

The Blue Smart IP22 12/30 (firmware 3.69) reported:

- its services: 1801, 306b0001-…cdfd0, 306b0001-…cdfd1 and 97580001
- ATT MTU 23; the control read returned `00 0100 01 50 1400` (one instance,
  one free chunk, chunks of 80 bytes, ATT length 20)
- the device list `[0, 0]`

The charger gives one chunk of credit at a time. About 250 registers per
second can be read, 20 per request.

**Finding the register.** All registers 0000-0FFF, 2000-2FFF and E000-EFFF
were read with night mode off, on (switched in VictronConnect) and off again;
122 exist. Apart from live values (voltages, currents, timers, the VE.Smart
network table) only two changed: 0xE001 0 → 2 → 0 and 0xEDF0 (maximum
current) 30.0 → 15.0 → 30.0 A. Writing 0xE001 alone switches night mode, and
the charger adjusts 0xEDF0 itself, as it must do when night mode ends after
8 h. 0x0200 (device mode, the on/off of MPPTs) does not exist on the IP22.
Only two SET frames can leave `vesmart.py`: `06 00 82 19 e001 41 02` and
`… 41 00`. `check_frame()` refuses any other.

**Venus OS specifics**, each found on the device:

- *Duplicate filter.* Kernel 5.10 connects through a background scan with
  the accept list and duplicate filtering on. The Raspberry Pi controller
  filters duplicates per address, and the charger alternates instant
  readout advertisements (ADV_NONCONN_IND) with connectable ones (ADV_IND).
  After the first non-connectable report the kernel saw no ADV_IND, so it
  never sent LE Create Connection. While connecting, the session restarts
  the scan without duplicate filtering, through a raw HCI socket.
- *Scan left behind.* After connecting, the kernel leaves its own scan
  parameters in the controller (accept list = the charger, 60 ms window).
  `dbus-ble-sensors` only re-enables scanning every 10 s, which left every
  other device silent. The session puts `dbus-ble-sensors`' parameters back
  (`hci.restore_ble_sensors_scan()`, the same commands as its
  `ble_scan_setup()`) right after connecting and again after disconnecting.
  It skips this when Bluetooth sensors are disabled.
- *No GATT client for ConnectDevice.* `/etc/bluetooth/ble.conf` sets
  `ReverseServiceDiscovery = false`. bluetoothd then starts its GATT client
  only for a connection requested with Device1.Connect(), not for
  Adapter1.ConnectDevice(). ConnectDevice is used only to create the device
  object when bluetoothd does not know the charger. That link is dropped at
  once.
- *Pairing order.* Device1.Connect() returns as soon as ATT is up. A Pair()
  that completes while the first GATT discovery still runs is never answered
  by bluetoothd 5.72: the browse for the pairing request fails with EBUSY
  and the request is dropped. The session therefore waits for
  ServicesResolved, then pairs.
- *Agent.* `vesmart-server` owns the default agent (DisplayOnly). Pairing
  uses an agent of the helper with KeyboardOnly capability, so the kernel
  asks for the passkey and the agent enters the PIN. The charger answers as
  DisplayOnly with legacy pairing. `forced_passkey`, Victron's kernel patch
  for the GX's own PIN, only applies when the GX displays the passkey.
- *Failed pairings count.* `vesmart-server` counts every authentication
  failure, including ours, and blocks pairing with the GX for a while after
  10 of them. Writes are therefore never retried blindly.
- *Pairings do not survive a reboot.* `/var/lib/bluetooth` is on tmpfs, so
  the first session after a reboot pairs again. The charger accepts this,
  tested by removing the pairing on the GX.

**Polling and state.** After the first read (5 s after the charger is
published) the control reads every 15 minutes; failures retry after 60 s,
doubling up to 15 minutes. A switch request is sent right away, or right
after the session that is running. The switch shows the requested state
while it is pending. A failed switch is not retried: the old state comes
back with Status 8 (fault) until the next successful read. A charger
without the register (value error 1) disables the control and gets no
switch. gui-v2 lists an output only once its `/State` is valid, so the
service is published after the first successful read.

## Boot and persistence

`/data/rcS.local` calls `boot.sh`, which writes
`/run/service/dbus-victron-ble/{run,log/run}` and links it into `/service`,
where svscan starts it. `/service` is a tmpfs, so this happens again at every
boot. The process runs under `softlimit` like the Venus Python services,
waits for D-Bus, localsettings and the Bluetooth controller if needed, and is
restarted by daemontools when it exits.

## Performance

On a Raspberry Pi 4 at 600 MHz an advertisement costs about 0.5 ms of CPU
time, mostly main loop and Python call overhead per HCI event. Decrypting and
decoding a record takes about 0.2 ms. This adds up to about 0.1 % of one core
with the default scanning and about 1.6 % with continuous scanning at around 30
advertisements per second. If ever needed, draining the socket on a timer
instead of waking up for every event would cut most of that.

## Testing

- `tests/test_records.py`: decoders against the specification and against
  real, encrypted advertisements with their keys (SmartShunt, BlueSolar MPPT,
  Blue Smart IP22, from the victron-ble project), plus round trips through
  `tests/advgen.py`.
- `tests/test_hci.py`, `tests/test_listener.py`: HCI event parsing, malformed
  packets, listener behaviour with a socket pair, scan watchdog.
- `tests/test_manager.py`: keys, wrong keys, timeouts, returns, renames, VRM
  instances, record type changes, the yield counter and its persistence,
  and the night mode switch lifecycle with a fake runner.
- `tests/test_vesmart.py`, `tests/test_control.py`, `tests/test_pins.py`:
  CBOR, VE.Smart frames and messages (bytes seen on the IP22), the SET guard,
  the control state machine, the helper runner with a fake helper process,
  and the PIN file.
- `tests/integration_test.py`: the real service on a private D-Bus, fed with
  synthetic encrypted advertisements, checked over D-Bus and with the
  command line tool. It runs against a localsettings stand-in or, on a
  development machine, against the real localsettings.
- On a device: passive listening alongside `dbus-ble-sensors` (no change to
  its sensors), a reboot test, and real devices.

## Limitations and ideas

- Read only, apart from the night mode switch: no other settings, no DVCC
  participation, no relay. Low current mode is probably 0xE001 = 1, which is
  not verified.
- The gui-v2 key field belongs to `dbus-ble-sensors`' device list, so keys
  are entered with `victron-ble key` (or written to the setting, for example
  over MQTT).
- More record types are straightforward to add as decoders: DC/DC converter
  (0x04, `dcdc`), inverter (0x03, `inverter`), DC energy meter (0x0D,
  `dcsource`/`dcload` by monitor mode), Orion XS (0x0F), SmartLithium (0x05),
  Smart BatteryProtect (0x09).
- VE.Smart networking packets cannot be decoded without the network key.
