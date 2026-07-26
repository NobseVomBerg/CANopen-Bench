# CPC-USB/ARM7 wire protocol

Reference for the USB protocol implemented in `bench_cpcusb/protocol.py`
(encode/decode, no I/O) and driven in `bench_cpcusb/bus.py` (pyusb bulk
transfers, `can.BusABC`). Everything here is interface information —
endpoint numbers, message identifiers, byte layouts, register semantics
and the required call order. None of it is invented by this project; it
is what the device firmware dictates, cross-checked against the mainline
Linux driver `drivers/net/can/usb/ems_usb.c` (Copyright (C) 2004-2009 EMS
Dr. Thomas Wuensche), which is the only public verified description of
this protocol.

The adapter carries an SJA1000-compatible CAN controller behind an ARM7
USB bridge, so most configuration is really SJA1000 register writes
wrapped in a USB envelope; the register semantics below are the
controller's, not the bridge's.

## USB identity and endpoints

| | Value |
|---|---|
| Vendor ID | `0x12D6` |
| Product ID (ARM7) | `0x0444` |
| Bulk OUT (host → device) | endpoint `0x02` |
| Bulk IN (device → host) | endpoint `0x82` |
| Interrupt IN | endpoint `0x81` (unused here) |
| Bulk IN read size | 64 bytes |

On Windows the device must be bound to WinUSB (e.g. with Zadig) before
libusb/pyusb can claim it — the vendor driver holds the interface
otherwise. Same prerequisite python-can's gs_usb backend has.

## Transfer framing

Every bulk transfer, both directions, starts with a 4-byte outer header
followed by one or more message structures back to back:

```
[outer header: 4 bytes][message][message]…
```

Byte 0 of the outer header is a **message count** on the IN direction;
bytes 1-3 are padding. On the OUT direction the whole outer header is
zero-filled and exactly one message follows.

Bit 7 (`0x80`) of the count byte is an **overrun flag**: the device is
telling you its hardware FIFO already dropped frames. Mask it off before
using the value as a count — `count = buf[0] & 0x7F`.

### Message header

11 bytes, little-endian, in front of every message body:

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | message / command type |
| 1 | 1 | length of the body that follows |
| 2 | 1 | message id |
| 3 | 4 | timestamp, seconds |
| 7 | 4 | timestamp, nanoseconds |

The device's timestamps are ignored by this driver — python-can stamps
frames on arrival instead, so the bench's own clock stays the single
time base across adapters.

Advancing to the next message in a multi-message transfer is
`offset += 11 + length`.

## Device → host messages

| Type | Meaning |
|---|---|
| 1 | CAN frame, standard id |
| 8 | RTR frame, standard id |
| 12 | CAN parameters |
| 14 | CAN controller state |
| 16 | CAN frame, extended id |
| 17 | RTR frame, extended id |
| 19 | control |
| 20 | confirmation |
| 21 | overrun |
| 23 | CAN bus error |
| 25 | error counters |

## Host → device commands

| Type | Meaning |
|---|---|
| 1 | send CAN frame, standard id |
| 3 | control (enable/disable notifications) |
| 6 | set CAN parameters (controller init) |
| 13 | send RTR frame, standard id |
| 15 | send CAN frame, extended id |
| 16 | send RTR frame, extended id |

Note the type numbers are **not** symmetric between the two directions —
e.g. 16 means "extended CAN frame" from the device but "send extended RTR
frame" to it. Decode tables must be direction-specific.

## CAN frame body

13 bytes:

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | arbitration id (little-endian) |
| 4 | 1 | DLC |
| 5 | 8 | data, zero-padded |

The declared body length differs from the struct size: a data frame
declares `5 + dlc`, an RTR frame declares `5` (id and DLC only, no data —
a remote frame requests a length but carries no bytes). The transfer
itself is still padded out to the full 28 bytes.

## Control command

Enables the asynchronous notifications the driver wants. The body value
is `subject | action`:

| Subject | Value |
|---|---|
| CAN message | `0x04` |
| CAN state | `0x0C` |
| bus error | `0x1C` |

with action `1` = turn on. Three separate commands are sent, one per
subject.

**Quirk worth knowing:** for this one command the reference
implementation sets the header's length field to `11 + 1` — an envelope
size, where every other command puts a body size there. This driver
reproduces that value because the firmware expects it, and zero-fills the
remaining body. The reference leaves those bytes uninitialized (they are
stack memory in C); zero-filling is a strict superset of the observable
behaviour, since the firmware only reads the first byte.

## CAN parameters command

13-byte body: one byte controller type (`2` = SJA1000) followed by the
12-byte SJA1000 register block:

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | mode |
| 1 | 4 | acceptance code 0-3 |
| 5 | 4 | acceptance mask 0-3 |
| 9 | 1 | BTR0 |
| 10 | 1 | BTR1 |
| 11 | 1 | output control |

Mode `0x01` = reset, `0x00` = normal. Acceptance code is left at zero and
the mask at `0xFF FF FF FF`, i.e. the hardware filter accepts everything —
filtering happens in software, where the bench can change it without a
controller reset. Output control is `0xDA` (push-pull, normal output
mode), the value the reference uses for this hardware.

## Bit timing

The controller clock is **8 MHz** on this device. BTR0/BTR1 are the
standard SJA1000 pair:

```
BTR0 = (BRP - 1) | ((SJW - 1) << 6)
BTR1 = (TSEG1 - 1) | ((TSEG2 - 1) << 4) | (three_samples << 7)
```

Hardware limits for this adapter: `TSEG1` 1-16, `TSEG2` 1-8, `SJW` 1-4,
`BRP` **1-64**.

The BRP range is the part that matters in practice. python-can's own
timing search caps BRP at 32 (the ISO 11898 minimum requirement), which
cannot reach the low end of this adapter's documented range — 10 kbit/s
at an 8 MHz clock needs BRP above 32. `bitrate_to_btr` therefore runs a
quantum search over the full 1-64 range, keeps candidates within 1/256
relative bitrate error, and prefers the smallest BRP, then the sample
point closest to the requested one (default 87.5%). The resulting
parameters are handed to `can.BitTiming` for validation, so python-can
still has the final say on whether a combination is sane.

## Initialization sequence

Order is dictated by the controller — bit timing only latches while in
reset:

1. mode = reset
2. mode = reset, with BTR0/BTR1 set
3. control: CAN message notifications on
4. control: CAN state notifications on
5. control: bus error notifications on
6. mode = normal, with BTR0/BTR1 set

Shutdown puts the controller back into reset before releasing the USB
device, so a re-open starts from a defined state rather than inheriting
whatever the previous session left behind.

## Receive path

Read 64 bytes from the bulk IN endpoint, decode into zero or more
messages, queue them, and hand out CAN frames one at a time. State,
bus-error and overrun notifications are logged at debug level and
otherwise skipped — python-can's `recv()` contract wants a
`can.Message` or nothing, and this bench surfaces bus health through its
own status line rather than through synthesized error frames.

A read timeout is not an error: it means no traffic arrived within the
window, which is the normal state of an idle bus.
