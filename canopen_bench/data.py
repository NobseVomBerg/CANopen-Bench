"""Static catalogs: neutral demo/seed data.

Real device data comes from the bus (scan/SDO) and from parsed EDS files;
what remains here are UI seeds and the catalogs for features whose real
implementation is still pending (test runner, SWDL, static objects
fallback). Everything device-family- or vendor-specific (EDS registry
seeds, vendor adapter cards, vendor firmware) lives in extension packages
— see canopen_bench/plugin.py and docs/extending.md.
"""

# One-time seed for the (real, sqlite-backed) EDS registry — see Db.eds_add.
# The neutral core seeds nothing; device-family rows come from plugins
# (BenchPlugin.seed_eds) or from the user uploading EDS files.
SEED_EDS_FILES: list[dict] = []

# Test catalog: id, name, tools, avg time
TESTS = [
    ("0000", "Test the Tests", "—", "0.4 s"),
    ("0001", "PSU ON with 57V on Channel 2", "PSU", "0.8 s"),
    ("1000", "Read Eeprom values via CANopen objects", "—", "3.2 s"),
    ("4433", "Serial number readout", "—", "1.1 s"),
    ("4455", "Process parameter local control", "—", "2.6 s"),
    ("4526", "Error handling basics", "PSU", "4.0 s"),
    ("4559", "LED test", "—", "1.9 s"),
    ("4598", "Notification: enter/leave setup menu", "—", "2.2 s"),
    ("4602", "Aux power off handling, under-voltage", "PSU", "8.4 s"),
    ("4613", "Menu — go through parameters menu", "—", "6.1 s"),
    ("4622", "Obj 2040h product identification", "—", "0.7 s"),
    ("4671", "RPDO1 transfer", "—", "1.3 s"),
    ("4672", "RPDO2 transfer", "—", "1.4 s"),
    ("4709", "Switching between operating states", "PSU", "12.2 s"),
    ("4790", "Error handling — HW errors not clickable-away", "—", "5.5 s"),
    ("4828", "Test automation, math functions", "—", "0.3 s"),
]
# Result of the previous run (seed data)
LAST_RESULTS = {
    "0000": "PASS", "0001": "PASS", "1000": "PASS", "4433": "PASS", "4455": "FAIL",
    "4526": "PASS", "4559": "PASS", "4602": "PASS", "4613": "PASS", "4622": "PASS",
    "4671": "PASS", "4709": "FAIL", "4790": "PASS", "4828": "PASS",
}
# Tests that deterministically fail in the simulated test runner (_run_step)
FAILING_TESTS = {"4455", "4709"}
DEFAULT_TEST_SEL = ["0001", "1000", "4433", "4602", "4613", "4622"]

SEED_REPORTS = [
    {"name": "run_0714_1132.html", "score": "28/28", "ok": True},
    {"name": "run_0713_1748.html", "score": "26/28", "ok": False},
    {"name": "run_0713_0910.html", "score": "28/28", "ok": True},
]

# Firmware library: version, file, tag, meta. Neutral demo entries only —
# demo DUTs report fw 1.0.0-demo, so 1.1.0 gives the simulated SWDL an
# upgrade story; real vendor firmware catalogs come from plugins
# (BenchPlugin.firmware) and are listed first.
FIRMWARE = [
    {"ver": "1.1.0", "file": "demo_device_v1.1.0.bin", "tag": "latest", "meta": "180 KB · 2026-07-01"},
    {"ver": "1.0.0", "file": "demo_device_v1.0.0.bin", "tag": "released", "meta": "178 KB · 2026-05-12"},
]

# CiA-301 EMCY error codes: exact codes plus 0xXX00/0xX000 class entries
# that serve as fallbacks for unlisted codes (lookup: exact, then
# code & 0xFF00, then code & 0xF000 — see Bench._emcy_text). Vendor- or
# profile-specific codes are merged over this table via
# BenchPlugin.emcy_codes; on conflict the plugin text wins.
EMCY_CODES = {
    0x0000: "Error reset / no error",
    0x1000: "Generic error",
    0x2000: "Current — generic",
    0x2100: "Current, device input side",
    0x2200: "Current inside the device",
    0x2300: "Current, device output side",
    0x3000: "Voltage — generic",
    0x3100: "Mains voltage",
    0x3200: "Voltage inside the device",
    0x3300: "Output voltage",
    0x4000: "Temperature — generic",
    0x4100: "Ambient temperature",
    0x4200: "Device temperature",
    0x5000: "Device hardware",
    0x6000: "Device software — generic",
    0x6100: "Internal software",
    0x6200: "User software",
    0x6300: "Data set",
    0x7000: "Additional modules",
    0x8000: "Monitoring — generic",
    0x8100: "Communication — generic",
    0x8110: "CAN overrun (objects lost)",
    0x8120: "CAN in error passive mode",
    0x8130: "Life guard / heartbeat error",
    0x8140: "Recovered from bus off",
    0x8150: "CAN-ID collision",
    0x8200: "Protocol error — generic",
    0x8210: "PDO not processed (length error)",
    0x8220: "PDO length exceeded",
    0x8230: "DAM MPDO not processed (destination not available)",
    0x8240: "Unexpected SYNC data length",
    0x8250: "RPDO timeout",
    0x9000: "External error",
    0xF000: "Additional functions — generic",
    0xFF00: "Device specific — generic",
}

# Error-register bits (object 0x1001), bit 0 first.
ERROR_REGISTER_BITS = ("generic", "current", "voltage", "temperature",
                       "communication", "device profile", "reserved", "manufacturer")

# CiA-301 SDO abort codes. An abort is the device (or the bench) saying
# exactly why a transfer failed, and the number is the whole of that
# message — shown as a bare 0x05040000 it is one more thing to look up in
# a standard nobody has open, and the two aborts that mean "the device did
# not answer" and "that object does not exist" read identically.
ABORT_CODES = {
    0x05030000: "Toggle bit not alternated",
    0x05040000: "SDO protocol timed out — no answer",
    0x05040001: "Command specifier not valid or unknown",
    0x05040002: "Invalid block size",
    0x05040003: "Invalid sequence number",
    0x05040004: "CRC error",
    0x05040005: "Out of memory",
    0x06010000: "Unsupported access to an object",
    0x06010001: "Read access to a write-only object",
    0x06010002: "Write access to a read-only object",
    0x06020000: "Object does not exist in the dictionary",
    0x06040041: "Object cannot be mapped to a PDO",
    0x06040042: "Mapping would exceed the PDO length",
    0x06040043: "General parameter incompatibility",
    0x06040047: "General internal incompatibility in the device",
    0x06060000: "Access failed due to a hardware error",
    0x06070010: "Data type does not match, length does not match",
    0x06070012: "Data type does not match, length too high",
    0x06070013: "Data type does not match, length too low",
    0x06090011: "Sub-index does not exist",
    0x06090030: "Value range of parameter exceeded",
    0x06090031: "Value of parameter written too high",
    0x06090032: "Value of parameter written too low",
    0x06090036: "Maximum value is less than minimum value",
    0x060A0023: "Resource not available: SDO connection",
    0x08000000: "General error",
    0x08000020: "Data cannot be transferred or stored",
    0x08000021: "Data cannot be transferred — local control",
    0x08000022: "Data cannot be transferred — device state",
    0x08000023: "No object dictionary present or generation failed",
    0x08000024: "No data available",
}

# Built-in adapter cards: generic COTS hardware plus demo mode. Vendor- or
# machine-specific adapters are contributed by plugins (BenchPlugin.adapters
# + adapter_backends) and are listed before these.
ADAPTERS = [
    {"key": "ixxat", "label": "IXXAT USB-to-CAN", "sub": "HMS · V2 compact",
     "conn": "IXXAT connected", "foot": "IXXAT USB-to-CAN", "iface": "IXXAT",
     "driver": "driver: VCI4 · 4.1.2", "full": "IXXAT USB-to-CAN"},
    # No PCAN hardware here — the backend is python-can's, wired up but never
    # run against a device. Don't advertise capabilities we can't stand behind
    # (the adapter family does CAN-FD; this tool does not — see _CANDUMP_RE).
    {"key": "pcan", "label": "PCAN-USB", "sub": "PEAK-System · untested",
     "conn": "PCAN connected", "foot": "PCAN-USB", "iface": "PCAN",
     "driver": "driver: PCANBasic via python-can", "full": "PCAN-USB"},
    # Confirmed on a VN1630: plain CAN needs only Vector's free XL driver,
    # not a CANoe/CANalyzer licence — which is why the card is here at all,
    # since plenty of that hardware sits in drawers for want of one.
    {"key": "vector", "label": "Vector VN1600", "sub": "Vector · VN1630 tested",
     "conn": "Vector connected", "foot": "Vector VN1600", "iface": "VECTOR",
     "driver": "driver: XL driver via python-can", "full": "Vector VN1610/VN1630"},
    {"key": "demo", "label": "Demo mode", "sub": "virtual DUTs from EDS",
     "conn": "Demo connected", "foot": "Demo mode", "iface": "DEMO",
     # keep this as short as the other driver lines — the adapter cards are
     # equal-width columns and clip what doesn't fit (see SetupPage in app.js)
     "driver": "no hardware · no driver", "full": "Demo mode"},
]
