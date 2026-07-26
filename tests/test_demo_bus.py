"""EdsDemoBus.wait_frame — races multiple (COB-ID, data-prefix) pairs in a
single wait so a step can't miss an out-of-band signal (e.g. addressing's
sender-agnostic Addr-End) while blocked on its primary COB. No Bench/plugin
machinery needed here — EdsDemoBus only needs a Db to exist.
"""
from __future__ import annotations

import time

from canopen_bench.bus.demo import EdsDemoBus
from canopen_bench.db import Db


def _bus(tmp_path) -> EdsDemoBus:
    return EdsDemoBus(Db(tmp_path / "wf.db"))


def test_wait_frame_races_and_returns_index_of_second_pair_when_it_arrives(tmp_path):
    bus = _bus(tmp_path)
    bus.queue_raw(0x783, b"\x02")  # only the second raced pair matches
    idx = bus.wait_frame([(0x700, b"\x01"), (0x783, b"\x02")], timeout=1.0)
    assert idx == 1


def test_wait_frame_same_cob_different_prefix_picks_matching_prefix(tmp_path):
    bus = _bus(tmp_path)
    bus.queue_raw(0x783, b"\x02\xAA")  # matches pair 1's prefix, not pair 0's
    idx = bus.wait_frame([(0x783, b"\x01"), (0x783, b"\x02")], timeout=1.0)
    assert idx == 1


def test_wait_frame_no_match_times_out_and_returns_none(tmp_path):
    bus = _bus(tmp_path)
    start = time.monotonic()
    idx = bus.wait_frame([(0x700, b"\x01"), (0x783, b"\x02")], timeout=0.3)
    elapsed = time.monotonic() - start
    assert idx is None
    assert elapsed < 0.6  # roughly the requested timeout, not much more


def test_wait_frame_single_pair_list_still_works(tmp_path):
    bus = _bus(tmp_path)
    bus.queue_raw(0x700, b"\x01")
    idx = bus.wait_frame([(0x700, b"\x01")], timeout=1.0)
    assert idx == 0
