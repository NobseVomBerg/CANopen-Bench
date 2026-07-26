# SPDX-License-Identifier: GPL-2.0-only
"""CPC-USB/ARM7 wire protocol — pure encode/decode, no USB I/O.

What this module holds is interface information: USB identifiers, endpoint
numbers, message and command type values, byte layouts, and SJA1000
register semantics. Those values are fixed by the device firmware — there
is no freedom in choosing them, and getting one wrong means the adapter
does not answer. They were read off the mainline Linux driver
``drivers/net/can/usb/ems_usb.c`` (Copyright (C) 2004-2009 EMS Dr. Thomas
Wuensche), the only public verified description of this protocol.

``PROTOCOL.md`` in this package documents the whole protocol and is the
reference this module implements. Keeping the module free of USB calls
means it can be unit-tested byte-for-byte without the adapter attached.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import can

VENDOR_ID = 0x12D6
PRODUCT_ID_ARM7 = 0x0444

BULK_EP_OUT = 0x02
BULK_EP_IN = 0x82
INTR_EP_IN = 0x81

RX_BUFFER_SIZE = 64
CPC_HEADER_SIZE = 4  # outer framing on the bulk pipe: [count, 0, 0, 0]
CPC_MSG_HEADER_LEN = 11  # type(1) + length(1) + msgid(1) + ts_sec(4) + ts_nsec(4)
CPC_CAN_MSG_MIN_SIZE = 5  # envelope length of an RTR frame (id-only, no data)
CPC_OVR_HW = 0x80  # high bit of the RX message-count byte: hw overrun happened

# -- messages device -> host --------------------------------------------------
MSG_CAN_FRAME = 1
MSG_RTR_FRAME = 8
MSG_CAN_PARAMS = 12
MSG_CAN_STATE = 14
MSG_EXT_CAN_FRAME = 16
MSG_EXT_RTR_FRAME = 17
MSG_CONTROL = 19
MSG_CONFIRM = 20
MSG_OVERRUN = 21
MSG_CAN_FRAME_ERROR = 23
MSG_ERR_COUNTER = 25

# -- commands host -> device ---------------------------------------------------
CMD_CAN_FRAME = 1
CMD_CONTROL = 3
CMD_CAN_PARAMS = 6
CMD_RTR_FRAME = 13
CMD_EXT_CAN_FRAME = 15
CMD_EXT_RTR_FRAME = 16

CC_TYPE_SJA1000 = 2

# Control-value subjects and actions for the CONTR_* control command
CONTR_CAN_MESSAGE = 0x04
CONTR_CAN_STATE = 0x0C
CONTR_BUS_ERROR = 0x1C
CONTR_CONT_ON = 1

SJA1000_MOD_NORMAL = 0x00
SJA1000_MOD_RESET = 0x01
SJA1000_DEFAULT_OUTPUT_CONTROL = 0xDA

SJA1000_SR_BUS_OFF = 0x80
SJA1000_SR_ERROR_STATUS = 0x40
SJA1000_ECC_MASK = 0xC0
SJA1000_ECC_DIR = 0x20

EMS_USB_ARM7_CLOCK = 8_000_000  # SJA1000 register clock as seen by the device


def bitrate_to_btr(bitrate: int, sample_point: float = 87.5) -> tuple[int, int]:
    """Bitrate (bit/s) -> SJA1000 BTR0/BTR1 register pair.

    A quantum search: walk the prescaler range, keep the combinations whose
    effective bitrate is within 1/256 of the request, prefer the smallest
    prescaler and then the sample point closest to the one asked for. This
    is the textbook way to solve for CAN bit timing; every implementation
    that does it ends up with the same shape.

    What is specific here is the search range. This adapter's limits are
    tseg1 1..16, tseg2 1..8, sjw 1..4, brp **1..64** — python-can's own
    search caps brp at 32 (the ISO 11898 minimum requirement), which cannot
    reach the low end of the adapter's range: 10 kbit/s at an 8 MHz clock
    needs a prescaler above 32. Candidates are handed to ``can.BitTiming``
    so python-can still validates the final combination. See PROTOCOL.md.
    """
    if sample_point < 50.0:
        raise ValueError(f"sample_point (={sample_point}) must not be below 50%.")

    candidates: list[can.BitTiming] = []
    for brp in range(1, 65):  # ems_usb_bittiming_const: brp_min=1, brp_max=64
        nbt = int(EMS_USB_ARM7_CLOCK / (bitrate * brp))
        if nbt < 8:
            break

        effective_bitrate = EMS_USB_ARM7_CLOCK / (nbt * brp)
        if abs(effective_bitrate - bitrate) > bitrate / 256:
            continue

        tseg1 = round(sample_point / 100 * nbt) - 1
        tseg1 = min(tseg1, nbt - 2)  # leave at least 1 TQ for tseg2
        tseg2 = nbt - tseg1 - 1
        sjw = min(tseg2, 4)

        if not (1 <= tseg1 <= 16 and 1 <= tseg2 <= 8 and 1 <= sjw <= 4):
            continue
        candidates.append(
            can.BitTiming(f_clock=EMS_USB_ARM7_CLOCK, brp=brp, tseg1=tseg1, tseg2=tseg2, sjw=sjw)
        )

    if not candidates:
        raise ValueError(f"No suitable CPC-USB/ARM7 bit timings found for {bitrate} bit/s.")

    candidates.sort(key=lambda bt: (bt.brp, abs(bt.sample_point - sample_point)))
    bt = candidates[0]

    btr0 = ((bt.brp - 1) & 0x3F) | (((bt.sjw - 1) & 0x3) << 6)
    btr1 = ((bt.tseg1 - 1) & 0xF) | (((bt.tseg2 - 1) & 0x7) << 4)
    if bt.nof_samples == 3:
        btr1 |= 0x80
    return btr0, btr1


def _msg_header(type_: int, length: int) -> bytes:
    return struct.pack("<BBBII", type_, length, 0, 0, 0)


def encode_can_frame(
    arbitration_id: int, data: bytes, dlc: int, *, is_extended: bool, is_remote: bool
) -> bytes:
    """Build the fixed 28-byte bulk-OUT transfer for one CAN frame.

    Layout: CPC_HEADER_SIZE(4, zero) + ems_cpc_msg header(11) + cpc_can_msg(13).
    ``dlc`` is passed separately from ``data`` since a remote frame carries a
    requested length but no data bytes.
    """
    if is_remote:
        cmd_type = CMD_EXT_RTR_FRAME if is_extended else CMD_RTR_FRAME
        envelope_len = CPC_CAN_MSG_MIN_SIZE
        data = b""
    else:
        cmd_type = CMD_EXT_CAN_FRAME if is_extended else CMD_CAN_FRAME
        envelope_len = CPC_CAN_MSG_MIN_SIZE + dlc

    can_msg = struct.pack("<IB8s", arbitration_id, dlc, data.ljust(8, b"\x00")[:8])
    return b"\x00" * CPC_HEADER_SIZE + _msg_header(cmd_type, envelope_len) + can_msg


def encode_control_cmd(value: int) -> bytes:
    """27-byte control command (subject|action in the first body byte).

    The firmware wants ``length = CPC_MSG_HEADER_LEN + 1`` for this one
    command — an envelope size, where every other command carries a body
    size. That inconsistency is the device's, and it has to be reproduced
    or the command is rejected. The remaining bytes are zero-filled here;
    the firmware reads only the first one. See PROTOCOL.md, "Control
    command".
    """
    length = CPC_MSG_HEADER_LEN + 1
    payload = bytes([value]) + b"\x00" * (length - 1)
    return b"\x00" * CPC_HEADER_SIZE + _msg_header(CMD_CONTROL, length) + payload


def encode_can_params(*, mode: int, btr0: int = 0, btr1: int = 0) -> bytes:
    """28-byte CAN-parameters command (controller init / mode / bit-timing).

    The hardware acceptance filter is left wide open (code 0, mask 0xFF..) —
    filtering happens in software, where it can change without a controller
    reset.
    """
    sja1000 = struct.pack(
        "<BBBBBBBBBBBB",
        mode,
        0x00, 0x00, 0x00, 0x00,  # acc_code0..3
        0xFF, 0xFF, 0xFF, 0xFF,  # acc_mask0..3 (filter open)
        btr0, btr1,
        SJA1000_DEFAULT_OUTPUT_CONTROL,
    )
    payload = bytes([CC_TYPE_SJA1000]) + sja1000
    return b"\x00" * CPC_HEADER_SIZE + _msg_header(CMD_CAN_PARAMS, len(payload)) + payload


@dataclass
class RxCanFrame:
    arbitration_id: int
    data: bytes
    dlc: int
    is_extended: bool
    is_remote: bool


@dataclass
class RxCanState:
    bus_off: bool
    error_warning: bool


@dataclass
class RxBusError:
    ecc: int
    rx_err: int
    tx_err: int


@dataclass
class RxOverrun:
    hardware: bool


RxMessage = RxCanFrame | RxCanState | RxBusError | RxOverrun


def decode_bulk_packet(buf: bytes) -> list[RxMessage]:
    """Parse one bulk-IN transfer into zero or more decoded messages.

    Transfer layout: byte 0 is a message count whose top bit flags that the
    device's hardware FIFO already overran, then that many back-to-back
    message structures. See PROTOCOL.md, "Transfer framing".

    Unlike the rest of this module, this loop is a direct port of
    ``ems_usb_read_bulk_callback`` in ``drivers/net/can/usb/ems_usb.c``
    (GPL-2.0-only, Copyright (C) 2004-2009 EMS Dr. Thomas Wuensche): it
    keeps that function's control flow, its bounds check and its variable
    names. Much of that is dictated by the wire format, but not the
    expression — treat this function as GPL-2.0-only derived code, and
    rewrite it against PROTOCOL.md before relicensing the package.
    """
    if len(buf) <= CPC_HEADER_SIZE:
        return []

    out: list[RxMessage] = []
    msg_count = buf[0] & ~CPC_OVR_HW
    hw_overrun = bool(buf[0] & CPC_OVR_HW)
    if hw_overrun:
        out.append(RxOverrun(hardware=True))

    start = CPC_HEADER_SIZE
    while msg_count:
        if start + CPC_MSG_HEADER_LEN > len(buf):
            break
        msg_type, length, _msgid, _ts_sec, _ts_nsec = struct.unpack_from(
            "<BBBII", buf, start
        )
        union_off = start + CPC_MSG_HEADER_LEN

        if msg_type in (MSG_CAN_FRAME, MSG_EXT_CAN_FRAME, MSG_RTR_FRAME, MSG_EXT_RTR_FRAME):
            arb_id, dlc = struct.unpack_from("<IB", buf, union_off)
            is_remote = msg_type in (MSG_RTR_FRAME, MSG_EXT_RTR_FRAME)
            data = b"" if is_remote else bytes(buf[union_off + 5: union_off + 5 + dlc])
            out.append(RxCanFrame(
                arbitration_id=arb_id, data=data, dlc=dlc,
                is_extended=msg_type in (MSG_EXT_CAN_FRAME, MSG_EXT_RTR_FRAME),
                is_remote=is_remote,
            ))
        elif msg_type == MSG_CAN_STATE:
            state = buf[union_off]
            out.append(RxCanState(
                bus_off=bool(state & SJA1000_SR_BUS_OFF),
                error_warning=bool(state & SJA1000_SR_ERROR_STATUS),
            ))
        elif msg_type == MSG_CAN_FRAME_ERROR:
            _ecode, _cc_type, ecc, rxerr, txerr = struct.unpack_from("<BBBBB", buf, union_off)
            out.append(RxBusError(ecc=ecc, rx_err=rxerr, tx_err=txerr))
        elif msg_type == MSG_OVERRUN:
            out.append(RxOverrun(hardware=False))

        start += CPC_MSG_HEADER_LEN + length
        msg_count -= 1
        if start > len(buf):
            break

    return out
