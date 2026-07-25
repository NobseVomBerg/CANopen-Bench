# bench-cpcusb

CPC-USB/ARM7 adapter support for
[canopen-bench](https://github.com/NobseVomBerg/CANopen-Bench): a
python-can backend plus the bench's adapter-card UI, in one package. It
registers two entry points — a `can.interface` driver for any
python-can user, and a `canopen_bench.plugins` adapter card for the
bench specifically.

## License

**GPL-2.0-only.** The driver (`bench_cpcusb/bus.py`) is a pyusb/WinUSB
port of the mainline Linux kernel driver
`drivers/net/can/usb/ems_usb.c` (GPL-2.0-only, © 2004–2009 EMS Dr.
Thomas Wuensche) — that inheritance is why this package is
GPL-licensed and lives outside canopen-bench's MIT core. See
`LICENSE`.

## Install

```bash
pip install -e .
```

Requires `canopen-bench>=1,<2`. On Windows, the device must be bound
to WinUSB before pyusb/libusb can open it — a one-time
[Zadig](https://zadig.akeo.ie/) step (select the CPC-USB/ARM7 device,
driver = WinUSB).

## What's in here

- `bench_cpcusb/bus.py` — `can.BusABC` implementation, driven over
  native USB bulk transfers (no vendor DLL, no COM-port framing)
- `bench_cpcusb/plugin.py` — the bench-side adapter card
  (`BenchPlugin.adapters()` / `adapter_backends()`)
- `bench_cpcusb/protocol.py` — CPC-USB wire protocol constants/framing

Both halves ship in one package deliberately: the adapter card is
meaningless without the driver it points at, and vice versa.

## Tests

```bash
pip install -e .[dev]
pytest tests/
```
