"""SocketCAN: what the bench asks the interface, and a real run on vcan.

The first part runs everywhere: ``ip``'s answers are canned, python-can's
enumeration and send are stubbed. The second needs a Linux box with a
vcan0 — CI's ``socketcan`` job makes one — and skips elsewhere, saying so.
No test here has seen a real adapter.
"""
from __future__ import annotations

import errno
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import can
import canopen
import pytest

import canopen_bench
from canopen_bench import data
from canopen_bench.bus import canopen_bus as cb
from canopen_bench.bus import socketcan
from canopen_bench.bus.canopen_bus import CanopenBus, _TraceListener
from canopen_bench.core import Bench
from canopen_bench.db import Db


def _can(state="ERROR-ACTIVE", bitrate=500000):
    info = {"state": state, **({"bittiming": {"bitrate": bitrate}} if bitrate else {})}
    return {"info_kind": "can", "info_data": info}


def _ip(monkeypatch, *, flags=("NOARP", "UP", "LOWER_UP"), linkinfo=None,
        returncode=0, stderr=""):
    """``ip -details -json link show`` answering with one link."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        out = json.dumps([{"ifname": cmd[-1], "flags": list(flags), "txqlen": 10,
                           "link_type": "can", "linkinfo": linkinfo or _can()}])
        return subprocess.CompletedProcess(cmd, returncode, out if not returncode else "", stderr)

    monkeypatch.setattr(socketcan.subprocess, "run", run)
    return calls


# -- what the interface is asked -------------------------------------------

def test_an_interface_up_at_the_chosen_bitrate_is_fine(monkeypatch):
    calls = _ip(monkeypatch)
    assert socketcan.link_problem("can0", 500) == ""
    assert calls == [["ip", "-details", "-json", "link", "show", "can0"]]


def test_another_bitrate_is_refused_with_the_line_that_fixes_it(monkeypatch):
    """python-can drops the bitrate it is handed; the OS's is the one on
    the wire. Opening anyway would hear nothing and say nothing."""
    _ip(monkeypatch, linkinfo=_can(bitrate=250000))
    problem = socketcan.link_problem("can0", 500)
    assert "runs at 250 kbit/s" in problem and "500" in problem
    assert ("sudo ip link set can0 down && sudo ip link set can0 up type can bitrate 500000"
            in problem)


def test_a_down_interface_says_how_to_bring_it_up(monkeypatch):
    _ip(monkeypatch, flags=("NOARP", "ECHO"), linkinfo=_can(state="STOPPED", bitrate=None))
    problem = socketcan.link_problem("can1", 125)
    assert "can1 is down" in problem
    assert "sudo ip link set can1 up type can bitrate 125000" in problem


def test_a_vcan_has_no_bitrate_to_check(monkeypatch):
    _ip(monkeypatch, linkinfo={"info_kind": "vcan"})
    assert socketcan.link_problem("vcan0", 1000) == ""


def test_a_down_vcan_is_brought_up_without_a_bitrate(monkeypatch):
    _ip(monkeypatch, flags=("NOARP",), linkinfo={"info_kind": "vcan"})
    problem = socketcan.link_problem("vcan0", 500)
    assert problem.endswith("sudo ip link set vcan0 up") and "bitrate" not in problem


def test_bus_off_is_named(monkeypatch):
    _ip(monkeypatch, linkinfo=_can(state="BUS-OFF"))
    assert "can0 is bus-off" in socketcan.link_problem("can0", 500)


def test_a_missing_interface_names_the_usual_reasons(monkeypatch):
    _ip(monkeypatch, returncode=1, stderr='Device "can0" does not exist.\n')
    problem = socketcan.link_problem("can0", 500)
    assert "no network interface can0" in problem and "--network host" in problem


def test_without_ip_nothing_is_judged(monkeypatch):
    """No iproute2 (a bare container), or one too old for -json: python-can
    opens the interface as it always did, rather than the bench refusing
    for a reason it made up."""
    def missing(cmd, **kw):
        raise FileNotFoundError("ip")

    monkeypatch.setattr(socketcan.subprocess, "run", missing)
    assert socketcan.link_problem("can0", 500) == ""
    _ip(monkeypatch, returncode=255, stderr='Option "-json" is unknown')
    assert socketcan.link_problem("can0", 500) == ""


# -- connect, channels, send, time ------------------------------------------

def _spy_connect(monkeypatch):
    seen: list[dict] = []

    def spy(self, *args, **kwargs):
        seen.append(kwargs)
        self.bus = object()

    monkeypatch.setattr(canopen.Network, "connect", spy)
    monkeypatch.setattr(cb, "_shutdown_network", lambda network: None)
    return seen


def test_the_card_opens_python_cans_socketcan_on_can0(monkeypatch):
    seen = _spy_connect(monkeypatch)
    asked = []
    monkeypatch.setattr(cb, "link_problem", lambda ch, kbit: asked.append((ch, kbit)) or "")
    bus = CanopenBus()
    bus.connect("socketcan", 250)
    bus.connect("socketcan", 500, "vcan0")
    assert seen == [{"interface": "socketcan", "bitrate": 250_000, "channel": "can0"},
                    {"interface": "socketcan", "bitrate": 500_000, "channel": "vcan0"}]
    assert asked == [("can0", 250), ("vcan0", 500)]


def test_a_link_problem_stops_the_connect_before_python_can(monkeypatch):
    seen = _spy_connect(monkeypatch)
    monkeypatch.setattr(cb, "link_problem", lambda ch, kbit: f"{ch} is down")
    bus = CanopenBus()
    with pytest.raises(ConnectionError, match="can0 is down"):
        bus.connect("socketcan", 500)
    assert seen == [] and bus.network is None and bus._trace is None


def test_other_adapters_are_not_asked(monkeypatch):
    _spy_connect(monkeypatch)
    monkeypatch.setattr(cb, "link_problem", lambda ch, kbit: pytest.fail("asked"))
    CanopenBus().connect("pcan", 500)


def test_detected_interfaces_are_offered_by_name(monkeypatch):
    """python-can lists the interfaces that are up and of link type can —
    vcan included. Each arrives as a choice that opens that interface."""
    rows = [{"interface": "socketcan", "channel": name} for name in ("can0", "can1", "vcan0")]
    monkeypatch.setattr(cb.can, "detect_available_configs",
                        lambda iface: rows if iface == "socketcan" else [])
    found = CanopenBus().channels("socketcan")
    assert [c["value"] for c in found] == ["can0", "can1", "vcan0"]
    assert found[2]["open"] == {"channel": "vcan0"} and "vcan0" in found[2]["label"]


def test_a_full_socketcan_queue_is_waited_out(monkeypatch):
    """can0 queues 10 frames by default; past that the kernel answers
    ENOBUFS and python-can raises CanOperationError. That is a full
    queue, not a lost adapter: retried, as for PCAN."""
    sent, refusals = [], [3]

    class Bus:
        def send(self, msg, timeout=None):
            if refusals[0]:
                refusals[0] -= 1
                raise can.CanOperationError("Failed to transmit: No buffer space available",
                                            errno.ENOBUFS)
            sent.append(msg)

    net = SimpleNamespace(bus=Bus(), send_lock=threading.Lock())
    cb._send_waiting(net, can.Message(arbitration_id=0x605, data=bytes(8)))
    assert len(sent) == 1 and refusals[0] == 0


def test_a_kernel_timestamp_is_already_wall_time():
    """SocketCAN stamps in epoch time off the same clock as time.time():
    no offset to estimate, nothing to re-anchor, the stamp stands."""
    bus = CanopenBus()
    bus._trace = _TraceListener()
    now = time.time()
    stamps = [now - 0.0042, now - 0.0031, now - 0.0007]
    for hw in stamps:
        bus._trace.queue.append(("RX", can.Message(arbitration_id=0x185, timestamp=hw), now))
    times = [f.time for f in bus.poll_frames()]
    assert times == [datetime.fromtimestamp(hw).strftime("%H:%M:%S.%f") for hw in stamps]
    assert bus._ts_offset is None


# -- which systems get the card ---------------------------------------------

def test_the_card_is_offered_on_linux_only(tmp_path):
    assert "socketcan" in [a["key"] for a in data.adapters_for("linux")]
    for other in ("win32", "darwin"):
        keys = [a["key"] for a in data.adapters_for(other)]
        assert "socketcan" not in keys and {"ixxat", "pcan", "vector", "demo"} <= set(keys)
    bench = Bench(Db(tmp_path / "test.db"))
    shown = "socketcan" in [a["key"] for a in bench.snapshot()["adapters"]]
    assert shown == sys.platform.startswith("linux")


# -- against a real vcan0 ----------------------------------------------------

VCAN = "vcan0"


def _no_link(name: str) -> str:
    """Why there is no ``name`` to test against — "" when there is."""
    if not sys.platform.startswith("linux"):
        return "SocketCAN is Linux only"
    if not Path(f"/sys/class/net/{name}").exists():
        return (f"no {name} interface — CI's socketcan job makes vcan0 (up) and vcan1 "
                "(down); locally: sudo modprobe vcan && sudo ip link add dev vcan0 type vcan "
                "&& sudo ip link set up vcan0")
    return ""


@pytest.fixture()
def vcan_slave():
    """A canopen LocalNode answering from the demo EDS, on its own socket."""
    if reason := _no_link(VCAN):
        pytest.skip(reason)
    net = canopen.Network()
    net.connect(interface="socketcan", channel=VCAN)
    eds = Path(canopen_bench.__file__).parent / "seed" / "DemoDevice.eds"
    net.create_node(canopen.LocalNode(5, str(eds)))
    yield net
    net.disconnect()


@pytest.fixture()
def vcan_master(vcan_slave):
    bus = CanopenBus()
    bus.connect("socketcan", 500, VCAN)  # the bitrate check runs for real: vcan has none
    yield bus
    bus.disconnect()


def test_scan_finds_the_node_on_vcan(vcan_master, monkeypatch):
    monkeypatch.setattr(cb, "_SCAN_SETTLE_S", 0.3)
    found = vcan_master.scan(1, 10)
    assert [d.node for d in found] == [5]
    assert found[0].identity == "0x4D2·0x1" and found[0].sn == "0x00000001"


def test_sdo_write_and_read_back_on_vcan(vcan_master):
    assert vcan_master.sdo_write(5, "0x2001", "0x00", "0x2A").ok
    read = vcan_master.sdo_read(5, "0x2001", "0x00")
    assert read.ok and read.value == "0x2A"


def test_vcan_frames_carry_kernel_time(vcan_master):
    assert vcan_master.sdo_read(5, "0x1018", "0x01").ok
    rx = [(msg, arrival) for way, msg, arrival in vcan_master._trace.queue if way == "RX"]
    assert rx, "the answer is in the trace"
    assert all(0 <= arrival - msg.timestamp < 1 for msg, arrival in rx), \
        "the kernel stamps in epoch time, just before we read the frame"
    vcan_master.poll_frames(64)
    assert vcan_master._ts_offset is None, "an epoch stamp needs no offset"


def test_a_down_vcan_refuses_to_connect():
    """CI adds vcan1 and leaves it down, which is the one link state a test
    can produce without root."""
    if reason := _no_link("vcan1"):
        pytest.skip(reason)
    with pytest.raises(ConnectionError, match="sudo ip link set vcan1 up"):
        CanopenBus().connect("socketcan", 500, "vcan1")
