"""EdsDemoBus device-side frames reach the trace.

Boot-ups and vendor-hook replies used to sit in a queue that only
``wait_frame`` read, so they never showed up in the trace at all. They now
leave through ``poll_frames`` like any other received frame: demo mode and
real hardware feed the same record, which is where a ``wait_for`` step
reads. No Bench/plugin machinery needed here — EdsDemoBus only needs a Db.
"""
from __future__ import annotations

from canopen_bench.bus.demo import EdsDemoBus
from canopen_bench.db import Db


def _bus(tmp_path) -> EdsDemoBus:
    bus = EdsDemoBus(Db(tmp_path / "wf.db"))
    bus.connect("demo", 500)
    return bus


def test_queue_raw_surfaces_as_a_received_frame(tmp_path):
    bus = _bus(tmp_path)
    bus.queue_raw(0x783, b"\x02\xAA")
    frames = bus.poll_frames(8)
    assert [(f.direction, f.cob_id, f.data) for f in frames] == [("RX", "0x783", "02 AA")]


def test_queue_raw_frame_is_handed_over_once(tmp_path):
    """The queue is drained, not re-read: a second poll must not repeat the
    frame, or one arrival would be counted as several."""
    bus = _bus(tmp_path)
    bus.queue_raw(0x700, b"\x01")
    assert len(bus.poll_frames(8)) == 1
    assert bus.poll_frames(8) == []


def test_send_raw_surfaces_as_a_sent_frame(tmp_path):
    """Our own send is mirrored into the trace as TX — visible to the
    operator, and distinguishable from a device's answer."""
    bus = _bus(tmp_path)
    bus.send_raw(0x780, b"\x01")
    frames = bus.poll_frames(8)
    assert [(f.direction, f.cob_id) for f in frames] == [("TX", "0x780")]
