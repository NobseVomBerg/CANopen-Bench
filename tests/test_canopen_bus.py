"""CanopenBus tests — a real canopen.Network master talking to a real
canopen.LocalNode slave, both on python-can's in-process ``virtual`` bus.

No hardware and no shortcuts: this exercises the actual SDO/NMT wire
protocol end to end, just with a loopback transport instead of a CAN
adapter.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import can
import canopen
import pytest
from canopen.objectdictionary import (
    UNSIGNED8,
    UNSIGNED32,
    VISIBLE_STRING,
    ObjectDictionary,
    ODRecord,
    ODVariable,
)

from canopen_bench.bus import canopen_bus as cb
from canopen_bench.bus.canopen_bus import CanopenBus, _TraceListener

SLAVE_NODE_ID = 5


def _build_slave_od() -> ObjectDictionary:
    od = ObjectDictionary()

    identity = ODRecord("Identity", 0x1018)
    sub0 = ODVariable("highest sub-index supported", 0x1018, 0)
    sub0.data_type = UNSIGNED8
    sub0.access_type = "ro"
    identity.add_member(sub0)
    for i, name in enumerate(
        ["Vendor-ID", "Product code", "Revision number", "Serial number"], start=1
    ):
        v = ODVariable(name, 0x1018, i)
        v.data_type = UNSIGNED32
        v.access_type = "ro"
        identity.add_member(v)
    od.add_object(identity)

    device_name = ODVariable("Device name", 0x1008, 0)
    device_name.data_type = VISIBLE_STRING
    device_name.access_type = "ro"
    od.add_object(device_name)

    sw_version = ODVariable("Software version", 0x100A, 0)
    sw_version.data_type = VISIBLE_STRING
    sw_version.access_type = "ro"
    od.add_object(sw_version)

    counter = ODVariable("Test counter", 0x2000, 0)
    counter.data_type = UNSIGNED32
    counter.access_type = "rw"
    od.add_object(counter)

    return od


@pytest.fixture()
def channel(request):
    return f"test-{request.node.name}"


@pytest.fixture()
def slave(channel):
    net = canopen.Network()
    net.connect(interface="virtual", channel=channel)
    node = canopen.LocalNode(SLAVE_NODE_ID, _build_slave_od())
    net.create_node(node)
    node.sdo[0x1018][1].raw = 0xABCD
    node.sdo[0x1018][2].raw = 0x1234
    node.sdo[0x1018][4].raw = 0xDEADBEEF
    node.sdo[0x1008].raw = "TestDevice"
    node.sdo[0x100A].raw = "1.2.3"
    node.sdo[0x2000].raw = 42
    yield net, node
    net.disconnect()


@pytest.fixture()
def master(channel, slave):
    bus = CanopenBus()
    net = canopen.Network()
    bus._install_listeners(net)  # mirrors ALL listener wiring of connect(): trace + error listener
    net.connect(interface="virtual", channel=channel)
    bus.network = net
    bus.adapter = "cpc"
    bus.bitrate = 500
    yield bus
    bus.disconnect()


def test_sdo_read_scalar(master):
    res = master.sdo_read(SLAVE_NODE_ID, "0x2000", "00")
    assert res.ok
    assert res.value == "0x0000002A"  # 42


def test_sdo_write_then_read_round_trip(master):
    written = master.sdo_write(SLAVE_NODE_ID, "0x2000", "00", "0x00000063")  # 99
    assert written.ok
    res = master.sdo_read(SLAVE_NODE_ID, "0x2000", "00")
    assert res.ok
    assert res.value == "0x00000063"


def test_sdo_read_identity_serial(master):
    res = master.sdo_read(SLAVE_NODE_ID, "0x1018", "04")
    assert res.ok
    assert res.value == "0xDEADBEEF"


def test_sdo_read_missing_object_returns_abort(master):
    res = master.sdo_read(SLAVE_NODE_ID, "0x3000", "00")
    assert not res.ok
    assert res.abort.startswith("0x0602")  # Object does not exist


def test_sdo_read_unknown_node_times_out(master):
    res = master.sdo_read(99, "0x1018", "01")
    assert not res.ok


def test_nmt_start_reaches_slave(master, slave):
    _, node = slave
    master.nmt("start", node=SLAVE_NODE_ID)
    time.sleep(0.2)
    assert node.nmt.state == "OPERATIONAL"


def test_nmt_broadcast_reaches_slave(master, slave):
    _, node = slave
    master.nmt("preop", node=None)
    time.sleep(0.2)
    assert node.nmt.state == "PRE-OPERATIONAL"


def test_scan_finds_slave_and_reads_identity(master, monkeypatch):
    monkeypatch.setattr(cb, "_SCAN_SETTLE_S", 0.1)
    found = master.scan(node_from=1, node_to=20)
    assert [d.node for d in found] == [SLAVE_NODE_ID]
    dev = found[0]
    assert dev.name == "TestDevice"
    assert dev.fw == "1.2.3"
    assert dev.sn == "0xDEADBEEF"
    assert dev.identity == "0xABCD·0x1234"  # canonical minimal-width format


def test_scan_respects_node_range(master, monkeypatch):
    monkeypatch.setattr(cb, "_SCAN_SETTLE_S", 0.1)
    found = master.scan(node_from=SLAVE_NODE_ID + 1, node_to=20)
    assert found == []


def test_poll_frames_observes_sdo_traffic(master):
    # RX comes from the Notifier listener; our own TX (which no adapter
    # echoes back) is mirrored into the trace by the send hook.
    master.sdo_read(SLAVE_NODE_ID, "0x2000", "00")
    frames = master.poll_frames(max_frames=16)
    by_dir = {(f.direction, f.cob_id) for f in frames}
    assert ("TX", f"0x{0x600 + SLAVE_NODE_ID:03X}") in by_dir  # our request
    assert ("RX", f"0x{0x580 + SLAVE_NODE_ID:03X}") in by_dir  # slave's response
    assert all(f.time for f in frames)  # bus timestamps, not poll time


def test_a_frame_read_late_does_not_shift_every_frame_after_it(master):
    """The adapter's clock is mapped onto wall time by the smallest gap
    seen between a hardware stamp and our reading it, not by the first one.

    The first frame is the worst one to ask: the adapter's FIFO can already
    hold traffic when we connect, so it may have been stamped long before
    our thread reached it. Anchoring there put every later RX that far
    behind the TX it answered — a trace in which every request appears to
    go out before its answer comes back, which reads as a tool that does
    not wait for one.
    """
    now = time.time()
    master._trace = _TraceListener()
    master._ts_offset = None
    hw = 50644.0

    # waiting in the adapter since before we connected: stamped 130 ms
    # before our thread got to it
    master._trace.queue.append(
        ("RX", can.Message(arbitration_id=0x701, data=[0], timestamp=hw), now))
    # our own request 10 ms later, stamped by us as it goes out
    master._trace.queue.append(
        ("TX", can.Message(arbitration_id=0x601, data=[0], timestamp=now + 0.010),
         now + 0.010))
    # its answer 1 ms after that, read with no backlog in the way
    master._trace.queue.append(
        ("RX", can.Message(arbitration_id=0x581, data=[0], timestamp=hw + 0.141),
         now + 0.011))

    frames = master.poll_frames(max_frames=16)
    fmt = "%H:%M:%S.%f"
    request = datetime.strptime(frames[1].time, fmt)
    answer = datetime.strptime(frames[2].time, fmt)
    assert answer > request, \
        f"answer at {frames[2].time} before request at {frames[1].time}"
    assert (answer - request).total_seconds() == pytest.approx(0.001, abs=0.0005)


def test_poll_frames_maps_relative_driver_clock_to_wall_time(master):
    now = time.time()
    master._trace = _TraceListener()

    # First relative frame anchors the offset: renders exactly at arrival.
    first_relative = can.Message(arbitration_id=0x580 + SLAVE_NODE_ID, data=[0], timestamp=50644.609)
    master._trace.queue.append(("RX", first_relative, now))

    # Second relative frame, 150us later on the hardware clock but with 3ms
    # of Notifier scheduling jitter on arrival — the mapped time must track
    # the hardware delta, not the jittery arrival delta.
    second_relative = can.Message(
        arbitration_id=0x580 + SLAVE_NODE_ID, data=[0], timestamp=50644.609150
    )
    master._trace.queue.append(("RX", second_relative, now + 0.003))

    # Epoch-based stamp, close to arrival: kept as-is.
    epoch_msg = can.Message(arbitration_id=0x600 + SLAVE_NODE_ID, data=[0], timestamp=now - 0.0005)
    master._trace.queue.append(("TX", epoch_msg, now))

    # No usable timestamp: falls back to arrival.
    zero_msg = can.Message(arbitration_id=0x700 + SLAVE_NODE_ID, data=[0], timestamp=0)
    master._trace.queue.append(("RX", zero_msg, now))

    frames = master.poll_frames(max_frames=16)
    assert len(frames) == 4

    fmt = "%H:%M:%S.%f"
    arrival_str = datetime.fromtimestamp(now).strftime(fmt)
    epoch_str = datetime.fromtimestamp(now - 0.0005).strftime(fmt)

    assert frames[0].time == arrival_str  # first relative frame anchors at arrival

    t0 = datetime.strptime(frames[0].time, fmt)
    t1 = datetime.strptime(frames[1].time, fmt)
    delta_us = (t1 - t0).total_seconds() * 1_000_000
    assert delta_us == pytest.approx(150, abs=1)  # hardware-precise delta, arrival jitter absent

    assert frames[2].time == epoch_str  # plausible epoch stamp trusted
    assert frames[3].time == arrival_str  # zero stamp discarded


def test_no_connection_is_inert_not_raising():
    bus = CanopenBus()
    assert bus.scan() == []
    assert bus.poll_frames() == []
    res = bus.sdo_read(1, "0x1018", "01")
    assert not res.ok
    bus.nmt("start")  # must not raise
    bus.disconnect()  # must not raise when never connected


def test_rx_error_auto_disconnects_and_reports(master):
    lost = threading.Event()
    reasons: list[str] = []

    def on_lost(reason: str) -> None:
        reasons.append(reason)
        lost.set()

    master.on_lost = on_lost

    def broken_recv(timeout=None):
        raise can.CanOperationError("recv: port disconnected")

    master.network.bus.recv = broken_recv

    assert lost.wait(5), "on_lost callback did not fire within 5s"
    assert "port disconnected" in reasons[0]
    assert master.network is None
    master.disconnect()  # must not raise


def test_sdo_send_error_returns_connection_lost(master):
    lost = threading.Event()
    master.on_lost = lambda reason: lost.set()

    def broken_send(msg, timeout=None):
        raise can.CanOperationError("send: port gone")

    master.network.bus.send = broken_send

    res = master.sdo_read(SLAVE_NODE_ID, "0x2000", "00")
    assert not res.ok
    assert res.abort == "connection lost"
    assert master.network is None
    assert lost.wait(5), "on_lost callback did not fire within 5s"


def test_nmt_send_error_does_not_raise_and_tears_down(master):
    def broken_send(msg, timeout=None):
        raise can.CanOperationError("send: port gone")

    master.network.bus.send = broken_send

    master.nmt("start", node=SLAVE_NODE_ID)  # must not raise
    assert master.network is None


def test_disconnect_survives_stored_notifier_exception(master):
    master.network.notifier.exception = can.CanOperationError("stale rx error")
    master.disconnect()  # must not raise
    assert master.network is None


# -- adapter backend mapping ------------------------------------------------
#
# Everything below is wiring, not protocol: which python-can backend a card
# resolves to, and what reaches it. For adapters without hardware here that
# is the whole of what can honestly be tested — it proves the arguments are
# handed over, not that a device answers.

def _connect_kwargs(monkeypatch, adapter: str, bitrate: int = 500) -> dict:
    """Call `connect` with canopen's own connect stubbed out, and report the
    keyword arguments python-can would have been handed."""
    seen: dict = {}

    def spy(self, *args, **kwargs):
        seen.update(kwargs)
        self.bus = object()  # enough for the teardown path

    monkeypatch.setattr(canopen.Network, "connect", spy)
    monkeypatch.setattr(cb, "_shutdown_network", lambda network: None)
    bus = CanopenBus()
    bus.connect(adapter, bitrate)
    return seen


def test_vector_card_resolves_to_the_python_can_vector_backend(monkeypatch):
    kwargs = _connect_kwargs(monkeypatch, "vector", 250)
    assert kwargs["interface"] == "vector"
    assert kwargs["bitrate"] == 250_000
    assert kwargs["channel"] == 0


def test_vector_addresses_the_hardware_channel_directly(monkeypatch):
    """python-can defaults to app_name="CANalyzer", which resolves the
    channel through an application entry in Vector's Hardware Config. A
    bench that only has the free XL driver has no such entry, so the
    channel must be addressed by its global index instead."""
    assert _connect_kwargs(monkeypatch, "vector")["app_name"] is None


def test_adapters_without_extra_arguments_pass_none(monkeypatch):
    kwargs = _connect_kwargs(monkeypatch, "ixxat")
    assert kwargs == {"interface": "ixxat", "bitrate": 500_000, "channel": 0}


def test_plugin_backend_pairs_still_work(monkeypatch):
    """`adapter_backends` has always returned (interface, channel) pairs.
    The third element is optional; a plugin written before it existed must
    not need touching."""
    seen: dict = {}
    monkeypatch.setattr(canopen.Network, "connect",
                        lambda self, *a, **kw: (seen.update(kw), setattr(self, "bus", object())))
    monkeypatch.setattr(cb, "_shutdown_network", lambda network: None)
    bus = CanopenBus(extra_backends={"legacy": ("virtual", None)})
    bus.connect("legacy", 500)
    assert seen == {"interface": "virtual", "bitrate": 500_000}


def test_plugin_backend_may_carry_extra_arguments(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(canopen.Network, "connect",
                        lambda self, *a, **kw: (seen.update(kw), setattr(self, "bus", object())))
    monkeypatch.setattr(cb, "_shutdown_network", lambda network: None)
    bus = CanopenBus(extra_backends={"fancy": ("virtual", 2, {"receive_own_messages": True})})
    bus.connect("fancy", 500)
    assert seen["receive_own_messages"] is True
    assert seen["channel"] == 2


def test_poll_frames_offset_follows_a_drifting_driver_clock(master, monkeypatch):
    """The adapter's crystal is not the PC's. A hundred ppm apart is
    ordinary, and it is a third of a second an hour — all in one
    direction. An offset taken as the smallest gap ever seen can only
    follow that downwards, so RX frames slid steadily earlier until a
    response was stamped before the request that caused it. The window
    lets the estimate rise again.
    """
    monkeypatch.setattr(cb, "_TS_WINDOW_S", 1.0)
    master._trace = _TraceListener()
    hw, host = 50644.0, time.time()          # relative driver clock, wall clock

    def rx(hw_at: float, arrival_at: float) -> None:
        master._trace.queue.append(
            ("RX", can.Message(arbitration_id=0x581, data=[0], timestamp=hw_at), arrival_at))

    rx(hw, host)                              # anchors: offset == true_offset
    # a minute of the driver clock running 200 ppm slow: the gap grows by
    # 12 ms, and every frame is stamped correctly only if the offset follows
    for i in range(1, 7):
        t = 10.0 * i
        rx(hw + t - t * 200e-6, host + t)
    frames = master.poll_frames(max_frames=16)

    fmt = "%H:%M:%S.%f"
    late = datetime.strptime(frames[-1].time, fmt)
    want = datetime.strptime(datetime.fromtimestamp(host + 60.0).strftime(fmt), fmt)
    drift_ms = abs((late - want).total_seconds()) * 1000
    assert drift_ms < 1.0, f"stamp drifted {drift_ms:.1f} ms behind the frame's arrival"


def test_poll_frames_offset_still_drops_at_once_on_a_better_sample(master, monkeypatch):
    """Downwards it must not wait for a window: a smaller gap is a closer
    reading of the offset itself, and everything above it was queueing."""
    monkeypatch.setattr(cb, "_TS_WINDOW_S", 60.0)
    master._trace = _TraceListener()
    hw, host = 50644.0, time.time()
    master._trace.queue.append(
        ("RX", can.Message(arbitration_id=0x581, data=[0], timestamp=hw), host + 0.100))
    master._trace.queue.append(   # same hardware instant, read 100 ms sooner
        ("RX", can.Message(arbitration_id=0x581, data=[0], timestamp=hw + 1.0), host + 1.0))
    frames = master.poll_frames(max_frames=8)

    fmt = "%H:%M:%S.%f"
    assert frames[1].time == datetime.fromtimestamp(host + 1.0).strftime(fmt)


def test_poll_frames_reanchors_when_the_clock_steps(master):
    """An adapter reset or a stepped PC clock is not drift — the whole
    mapping is wrong and waiting out a window would stamp every frame in
    it against the old anchor."""
    master._trace = _TraceListener()
    hw, host = 50644.0, time.time()
    master._trace.queue.append(
        ("RX", can.Message(arbitration_id=0x581, data=[0], timestamp=hw), host))
    master._trace.queue.append(     # driver clock restarted near zero
        ("RX", can.Message(arbitration_id=0x581, data=[0], timestamp=12.5), host + 0.5))
    frames = master.poll_frames(max_frames=8)

    fmt = "%H:%M:%S.%f"
    assert frames[1].time == datetime.fromtimestamp(host + 0.5).strftime(fmt)
