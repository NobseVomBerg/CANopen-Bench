# bench-cpcusb

CPC-USB/ARM7 adapter support for [canopen-bench](../../): a python-can
backend plus the bench's adapter-card UI, in one package. It registers
two entry points — a `can.interface` driver for any python-can user, and
a `canopen_bench.plugins` adapter card for the bench specifically.

**This is a separate distribution.** It shares the canopen-bench
repository but not its package: own `pyproject.toml`, own version, own
tests, installed on its own. `pip install canopen-bench` does not bring
it — install it only if you have this adapter. It is here as the worked
reference for what a plugin package looks like.

## Protocol

The adapter's USB protocol — identifiers, endpoints, transfer framing,
message types, SJA1000 register semantics, bit timing and the required
initialization order — is documented in [`PROTOCOL.md`](PROTOCOL.md).
That interface data was read off the mainline Linux driver
`drivers/net/can/usb/ems_usb.c` (© 2004–2009 EMS Dr. Thomas Wuensche),
which is its only public verified description. The implementation here
is a pyusb/libusb userspace driver against that protocol; none of the
kernel driver's own machinery (URB handling, sk_buff/netdev integration,
the Linux CAN error-frame state machine, TX queue and echo handling) has
a counterpart in it.

## License

**MIT**, see `LICENSE` — same as the canopen-bench core.

This package was GPL-2.0-only until `decode_bulk_packet` was
reimplemented against `PROTOCOL.md`; that one function had followed the
kernel driver's control flow rather than just its protocol facts. The
reasoning, and what remains in common, is written up in
[`PROTOCOL.md`](PROTOCOL.md#provenance-and-licensing).

## Install

```bash
pip install -e ./plugins/bench-cpcusb     # from the repository root
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
