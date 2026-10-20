"""LSS fastscan (CiA 305): the master in canopen_bench/lss.py against the
demo bus's LSS slaves, the `lss_fastscan` step, and how the trace reads it."""
from __future__ import annotations

import threading
import time

import can
import pytest
from conftest import connect_and_scan, write_seed_eds_files
from test_executor import run_selected

from canopen_bench import lss as lsslib
from canopen_bench import testcases as tclib
from canopen_bench.bus.canopen_bus import _decode_cob, _TraceListener
from canopen_bench.core import Bench, _annotate_lss
from canopen_bench.db import Db


@pytest.fixture()
def bench(tmp_path):
    bench = Bench(Db(tmp_path / "lss.db"))
    write_seed_eds_files(bench)
    connect_and_scan(bench)  # three demo devices on nodes 1..3
    return bench


@pytest.fixture()
def bus(bench):
    """The demo devices without their node-IDs, waiting as LSS slaves."""
    bus = bench._demo_bus
    for node in bus.device_nodes():
        bus.unconfigure(node)
    assert bus.device_nodes() == [] and bus.unconfigured() == 3
    return bus


#: the demo answers at once, so a question needs no 10 ms window here
FAST = 0.002


def _sent(bus, cs: int) -> list[bytes]:
    return [bytes.fromhex(f.data) for f in bus._sdo_frames
            if f.direction == "TX" and f.cob_id == "0x7E5" and f.data.startswith(f"{cs:02X}")]


def test_the_lowest_lss_address_gets_the_first_node_id(bus):
    addresses = sorted(slave["address"] for slave in bus._lss)
    found = lsslib.assign(bus, 5, 20, {}, FAST)
    assert [(dev.node, dev.address) for dev in found] == list(zip((5, 6, 7), addresses))
    assert bus.device_nodes() == [5, 6, 7] and bus.unconfigured() == 0
    bootups = [f.cob_id for f in bus._sdo_frames if f.direction == "RX" and f.data == "00"]
    assert bootups == ["0x705", "0x706", "0x707"]


def test_every_device_goes_back_to_waiting_before_the_next_is_scanned(bus):
    """A device left in configuration would take the next node-ID as well."""
    lsslib.assign(bus, 1, 20, {}, FAST)
    order = [bytes.fromhex(f.data)[0] for f in bus._sdo_frames
             if f.direction == "TX" and f.cob_id == "0x7E5" and not f.data.startswith("51")]
    assert order == [0x04] + [0x11, 0x17, 0x04] * 3


def test_a_known_part_is_confirmed_with_one_question(bus):
    vendor = bus._lss[0]["address"][0]
    lsslib.assign(bus, 1, 20, {0: vendor}, FAST)
    scans = _sent(bus, lsslib.FASTSCAN)
    assert scans[1] == lsslib.frame(0x51, *vendor.to_bytes(4, "little"), 0, 0, 1)
    # three devices, 3 x 33 questions saved, and the final start nobody answers
    assert len(scans) == 3 * (1 + 1 + 3 * 33) + 1


def test_a_device_with_another_product_stops_the_scan(bus):
    products = sorted({slave["address"][1] for slave in bus._lss})
    assert len(products) == 2, "the seeded registry has two device types"
    with pytest.raises(lsslib.LssError, match="none with product") as err:
        lsslib.assign(bus, 1, 20, {1: products[0]}, FAST)
    assert [dev.node for dev in err.value.assigned] == [1, 2]
    assert bus.unconfigured() == 1 and not bus._lss[0]["config"]


def test_more_devices_than_node_ids_fail_and_leave_the_rest_waiting(bus):
    with pytest.raises(lsslib.LssError, match=r"more unconfigured devices than node-IDs 1\.\.2"):
        lsslib.assign(bus, 1, 2, {}, FAST)
    assert bus.device_nodes() == [1, 2] and bus.unconfigured() == 1
    assert not bus._lss[0]["config"], "the device found last is back in waiting"


def test_a_refused_node_id_names_the_error(bus):
    with pytest.raises(lsslib.LssError, match=r"configure node-ID 200: refused, error 1 \(node-ID out of range\)"):
        lsslib.assign(bus, 200, 210, {}, FAST)


def test_a_device_without_boot_up_fails(bus, monkeypatch):
    monkeypatch.setattr(bus, "_activate", lambda slave, node: None)
    with pytest.raises(lsslib.LssError, match="node 1: no boot-up within 0.05 s"):
        lsslib.assign(bus, 1, 20, {}, FAST, bootup=0.05)


def test_nobody_unconfigured_is_nothing_to_do(bench):
    assert lsslib.assign(bench._demo_bus, 1, 20, {}, FAST) == []


def test_a_stop_ends_between_two_devices(bus):
    checks = iter([False, True])
    found = lsslib.assign(bus, 1, 20, {}, FAST, stop=lambda: next(checks))
    assert [dev.node for dev in found] == [1] and bus.unconfigured() == 2


def test_a_late_answer_does_not_count_for_the_next_question(bench, monkeypatch):
    """Every slave answers a fastscan question on its own, a millisecond or
    so after the others. Seen live with three devices: the master asked on
    after the first answer, and the late ones answered questions nobody had
    a 0 for. The two of one product here answer 2 ms apart."""
    bus = bench._demo_bus
    for node in (1, 2):
        bus.unconfigure(node)
    vendor, product = bus._lss[0]["address"][:2]
    expected = sorted(slave["address"] for slave in bus._lss)
    original = bus._lss_request

    # not a threading.Timer: it waits on an Event, which on Windows wakes on the
    # ~15.6 ms system tick, past the 10 ms window, so the test itself made the
    # late answer it is about. time.sleep is high-resolution there (Python 3.11+)
    def answer_after(delay, deliver, answer):
        time.sleep(delay)
        deliver(*answer)

    def staggered(data):
        answers = []
        bus.queue_raw = lambda cob, payload: answers.append((cob, payload))
        try:
            original(data)
        finally:
            del bus.queue_raw
        for i, answer in enumerate(answers):
            # bound now, not when the thread wakes: by then the next
            # question may have the collecting lambda in its place
            threading.Thread(target=answer_after, args=(0.002 * i, bus.queue_raw, answer)).start()

    monkeypatch.setattr(bus, "_lss_request", staggered)
    found = lsslib.assign(bus, 1, 20, {0: vendor, 1: product})
    assert [dev.address for dev in found] == expected


# -- the step ------------------------------------------------------------------

@pytest.mark.parametrize("val, error", [
    ({"vendor": "0x4D2", "first": 1, "last": 32, "into": "R3"}, None),
    ({}, None),
    ({"vendor": "0x4D2", "colour": 1}, "unknown field(s) ['colour']"),
    ({"into": "R16"}, "into must be a register"),
    ({"timeout": 0}, "timeout must be a duration"),
    ({"bootup": "1s"}, "bootup must be a duration"),
    ({"first": [1]}, "invalid first"),
    ("0x4D2", "needs a mapping"),
])
def test_the_step_is_checked_when_the_file_loads(val, error):
    got = tclib._check_step({"lss_fastscan": val})
    assert (got is None) if error is None else (error in got), got


FASTSCAN_TC = """\
id: "0301"
name: "lss_fastscan hands out node-IDs"
steps:
  - lss_fastscan: {first: 4, last: 9, timeout: 0.002, into: R1}
  - jump_eq: {a: R1, b: 3, to: ok}
  - fail: "wrong count"
  - label: ok
"""


def test_the_step_counts_and_logs_what_it_assigned(bench, bus, tmp_path):
    folder = tmp_path / "tcs"
    folder.mkdir()
    (folder / "TC0301_fastscan.yaml").write_text(FASTSCAN_TC)
    bench.dispatch("set_path", {"which": "tc", "value": str(folder)})
    bench.dispatch("dev_toggle", {"node": 1})
    run_selected(bench, {"0301"})
    assert bench.results == {"0301": "PASS"}
    assert bus.device_nodes() == [4, 5, 6]
    assert [ln["msg"].split(":")[0] for ln in bench.logs if ln["msg"].startswith("LSS")] \
        == ["LSS  node 4", "LSS  node 5", "LSS  node 6"]


# -- reading it back -------------------------------------------------------------

def test_lss_frames_are_not_node_100_and_101():
    assert (_decode_cob(0x7E5), _decode_cob(0x7E4)) == ("LSS req", "LSS resp")


@pytest.mark.parametrize("cob, data, obj, val", [
    ("0x7E5", "04 00 00 00 00 00 00 00", "switch state global", "waiting"),
    ("0x7E5", "51 00 00 00 00 80 00 00", "fastscan", "start"),
    ("0x7E5", "51 D2 04 00 00 00 00 01", "fastscan", "vendor = 0x000004D2"),
    ("0x7E5", "51 00 00 26 00 13 03 03", "fastscan", "serial 0x00260000 from bit 19"),
    ("0x7E4", "4F 00 00 00 00 00 00 00", "fastscan answer", ""),
    ("0x7E5", "11 03 00 00 00 00 00 00", "configure node-ID", "node-ID 3"),
    ("0x7E4", "11 FF 01 00 00 00 00 00", "configure node-ID", "error 0xFF, device error 0x01"),
    ("0x7E4", "17 00 00 00 00 00 00 00", "store configuration", "ok"),
])
def test_the_trace_names_the_lss_service(cob, data, obj, val):
    row = {"cob": cob, "data": data, "obj": "", "val": ""}
    _annotate_lss(row)
    assert (row["obj"], row["val"]) == (obj, val)


def test_a_real_bus_hands_received_frames_to_listeners():
    got = []
    listener = _TraceListener(deliver=lambda cob, data: got.append((cob, data)))
    listener.on_message_received(can.Message(arbitration_id=0x7E4, data=b"\x4f" + bytes(7)))
    listener.on_message_received(can.Message(arbitration_id=0x7E4, is_error_frame=True))
    assert got == [(0x7E4, b"\x4f" + bytes(7))] and len(listener.queue) == 2
