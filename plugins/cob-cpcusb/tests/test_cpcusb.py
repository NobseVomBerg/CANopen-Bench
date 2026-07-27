"""CPC-USB/ARM7 backend tests — pure protocol codec + a mocked USB device.

None of this needs the adapter attached: protocol.py has no USB calls at
all, and CpcUsbBus is exercised against a stand-in for pyusb's Device.
"""
from __future__ import annotations

import struct

import can
import pytest
import usb.core
from cob_cpcusb import CpcUsbBus
from cob_cpcusb import protocol as p

# -- protocol.py: pure encode/decode --------------------------------------------

def test_bitrate_to_btr_round_trips_to_requested_bitrate():
    for bitrate in (1_000_000, 500_000, 250_000, 125_000, 100_000, 50_000, 20_000, 10_000):
        btr0, btr1 = p.bitrate_to_btr(bitrate)
        assert 0 <= btr0 <= 0xFF
        assert 0 <= btr1 <= 0xFF
        bt = can.BitTiming.from_registers(f_clock=p.EMS_USB_ARM7_CLOCK, btr0=btr0, btr1=btr1)
        assert abs(bt.bitrate - bitrate) <= bitrate / 256


def test_encode_can_frame_data_layout():
    frame = p.encode_can_frame(0x123, b"\x01\x02\x03", 3, is_extended=False, is_remote=False)
    assert len(frame) == p.CPC_HEADER_SIZE + p.CPC_MSG_HEADER_LEN + 13
    assert frame[:4] == b"\x00\x00\x00\x00"
    msg_type, envelope_len, msgid = struct.unpack_from("<BBB", frame, 4)
    assert msg_type == p.CMD_CAN_FRAME
    assert envelope_len == p.CPC_CAN_MSG_MIN_SIZE + 3
    assert msgid == 0
    arb_id, dlc = struct.unpack_from("<IB", frame, 15)
    assert arb_id == 0x123
    assert dlc == 3
    assert frame[20:23] == b"\x01\x02\x03"
    assert frame[23:28] == b"\x00" * 5  # zero-padded, not left uninitialized


def test_encode_can_frame_extended_and_rtr_pick_correct_command_type():
    ext = p.encode_can_frame(0x1FFFFFFF, b"", 0, is_extended=True, is_remote=False)
    assert ext[4] == p.CMD_EXT_CAN_FRAME
    rtr = p.encode_can_frame(0x42, b"", 4, is_extended=False, is_remote=True)
    assert rtr[4] == p.CMD_RTR_FRAME
    assert rtr[5] == p.CPC_CAN_MSG_MIN_SIZE  # no data bytes counted for RTR
    ext_rtr = p.encode_can_frame(0x42, b"", 4, is_extended=True, is_remote=True)
    assert ext_rtr[4] == p.CMD_EXT_RTR_FRAME


def test_encode_control_cmd_length_quirk_and_zero_fill():
    cmd = p.encode_control_cmd(0x05)
    assert len(cmd) == p.CPC_HEADER_SIZE + p.CPC_MSG_HEADER_LEN + 12
    assert cmd[4] == p.CMD_CONTROL
    assert cmd[5] == p.CPC_MSG_HEADER_LEN + 1  # envelope-size quirk the firmware requires
    assert cmd[15] == 0x05
    assert cmd[16:] == b"\x00" * 11


def test_encode_can_params_layout():
    buf = p.encode_can_params(mode=p.SJA1000_MOD_NORMAL, btr0=0x00, btr1=0x1C)
    assert len(buf) == p.CPC_HEADER_SIZE + p.CPC_MSG_HEADER_LEN + 13
    assert buf[4] == p.CMD_CAN_PARAMS
    assert buf[5] == 13
    assert buf[15] == p.CC_TYPE_SJA1000
    assert buf[16] == p.SJA1000_MOD_NORMAL
    assert buf[17:21] == b"\x00\x00\x00\x00"  # acc_code0..3
    assert buf[21:25] == b"\xff\xff\xff\xff"  # acc_mask0..3 (filter open)
    assert buf[25] == 0x00  # btr0
    assert buf[26] == 0x1C  # btr1
    assert buf[27] == p.SJA1000_DEFAULT_OUTPUT_CONTROL


def test_decode_bulk_packet_round_trips_a_data_frame():
    tx = p.encode_can_frame(0x321, b"\xaa\xbb", 2, is_extended=False, is_remote=False)
    rx_buf = bytes([1, 0, 0, 0]) + tx[p.CPC_HEADER_SIZE:]
    [msg] = p.decode_bulk_packet(rx_buf)
    assert isinstance(msg, p.RxCanFrame)
    assert msg.arbitration_id == 0x321
    assert msg.data == b"\xaa\xbb"
    assert msg.dlc == 2
    assert not msg.is_extended
    assert not msg.is_remote


def _tight_rx_can_msg(arb_id: int, data: bytes, *, msg_type: int = p.MSG_CAN_FRAME) -> bytes:
    """Build one RX record the way the device packs a *multi*-record bulk
    transfer: an 11-byte header plus only ``envelope_len`` body bytes, not
    padded out to the fixed 13-byte CAN slot. That is what the record walk
    advances over — distinct from encode_can_frame(), which always emits a
    fixed-size *single*-record USB transfer for TX.
    """
    envelope_len = p.CPC_CAN_MSG_MIN_SIZE + len(data)
    header = struct.pack("<BBBII", msg_type, envelope_len, 0, 0, 0)
    return header + struct.pack("<IB", arb_id, len(data)) + data


def test_decode_bulk_packet_handles_multiple_messages_in_one_transfer():
    m1 = _tight_rx_can_msg(0x1, b"\x01")
    m2 = _tight_rx_can_msg(0x2, b"\x02\x03")
    rx_buf = bytes([2, 0, 0, 0]) + m1 + m2
    msgs = p.decode_bulk_packet(rx_buf)
    assert [m.arbitration_id for m in msgs] == [0x1, 0x2]
    assert [m.data for m in msgs] == [b"\x01", b"\x02\x03"]


def test_decode_bulk_packet_hardware_overrun_flag():
    rx_buf = bytes([p.CPC_OVR_HW, 0, 0, 0, 0])  # len > CPC_HEADER_SIZE, no messages queued
    [msg] = p.decode_bulk_packet(rx_buf)
    assert isinstance(msg, p.RxOverrun)
    assert msg.hardware is True


def test_decode_bulk_packet_can_state():
    hdr = struct.pack("<BBBII", p.MSG_CAN_STATE, 1, 0, 0, 0)
    rx_buf = bytes([1, 0, 0, 0]) + hdr + bytes([p.SJA1000_SR_BUS_OFF])
    [msg] = p.decode_bulk_packet(rx_buf)
    assert isinstance(msg, p.RxCanState)
    assert msg.bus_off is True
    assert msg.error_warning is False


def test_decode_bulk_packet_empty_or_too_short_buffer():
    assert p.decode_bulk_packet(b"") == []
    assert p.decode_bulk_packet(b"\x00\x00\x00\x00") == []
    assert p.decode_bulk_packet(b"\x01\x00\x00") == []


def test_decode_bulk_packet_truncated_message_stops_without_raising():
    rx_buf = bytes([1, 0, 0, 0]) + b"\x01\x02"  # count says 1 message, but header is cut off
    assert p.decode_bulk_packet(rx_buf) == []


# -- bus.py: CpcUsbBus against a fake pyusb device ------------------------------

class FakeUsbDevice:
    """Stand-in for usb.core.Device: records writes, replays queued reads."""

    def __init__(self):
        self.writes: list[bytes] = []
        self.reads: list[bytes] = []
        self._configured = False

    def set_configuration(self):
        self._configured = True

    def write(self, endpoint, data, timeout=None):
        assert endpoint == p.BULK_EP_OUT
        self.writes.append(bytes(data))
        return len(data)

    def read(self, endpoint, length, timeout=None):
        assert endpoint == p.BULK_EP_IN
        if not self.reads:
            raise usb.core.USBTimeoutError("no data", -7, "timeout")
        return self.reads.pop(0)


@pytest.fixture()
def fake_dev(monkeypatch):
    dev = FakeUsbDevice()
    monkeypatch.setattr(usb.core, "find", lambda **kw: dev)
    monkeypatch.setattr(usb.util, "dispose_resources", lambda d: None)
    return dev


def test_cpcusb_bus_init_sequence(fake_dev):
    bus = CpcUsbBus(channel="cpcusb", bitrate=500_000)
    try:
        assert fake_dev._configured
        # reset, reset+btr, 3x control enable, normal+btr = 6 writes
        assert len(fake_dev.writes) == 6
        assert fake_dev.writes[0][4] == p.CMD_CAN_PARAMS
        assert fake_dev.writes[0][16] == p.SJA1000_MOD_RESET
        assert fake_dev.writes[-1][16] == p.SJA1000_MOD_NORMAL
        control_types = [w[4] for w in fake_dev.writes[2:5]]
        assert control_types == [p.CMD_CONTROL] * 3
    finally:
        bus.shutdown()


def test_cpcusb_bus_send(fake_dev):
    bus = CpcUsbBus(channel="cpcusb", bitrate=500_000)
    try:
        n_init = len(fake_dev.writes)
        msg = can.Message(arbitration_id=0x77, data=b"\x01\x02", is_extended_id=False)
        bus.send(msg)
        assert len(fake_dev.writes) == n_init + 1
        sent = fake_dev.writes[-1]
        assert sent[4] == p.CMD_CAN_FRAME
        arb_id, dlc = struct.unpack_from("<IB", sent, 15)
        assert arb_id == 0x77
        assert dlc == 2
    finally:
        bus.shutdown()


def test_cpcusb_bus_recv(fake_dev):
    bus = CpcUsbBus(channel="cpcusb", bitrate=500_000)
    try:
        tx = p.encode_can_frame(0x55, b"\xde\xad", 2, is_extended=False, is_remote=False)
        fake_dev.reads.append(bytes([1, 0, 0, 0]) + tx[p.CPC_HEADER_SIZE:])
        msg = bus.recv(timeout=0.1)
        assert msg is not None
        assert msg.arbitration_id == 0x55
        assert bytes(msg.data) == b"\xde\xad"
    finally:
        bus.shutdown()


def test_cpcusb_bus_recv_timeout_returns_none(fake_dev):
    bus = CpcUsbBus(channel="cpcusb", bitrate=500_000)
    try:
        assert bus.recv(timeout=0.05) is None
    finally:
        bus.shutdown()


def test_cpcusb_bus_raises_when_device_not_found(monkeypatch):
    monkeypatch.setattr(usb.core, "find", lambda **kw: None)
    with pytest.raises(can.exceptions.CanInitializationError):
        CpcUsbBus(channel="cpcusb")


def test_cpcusb_plugin_is_registered():
    import importlib.metadata as metadata

    eps = metadata.entry_points(group="can.interface")
    names = {ep.name for ep in eps}
    assert "cpcusb" in names
