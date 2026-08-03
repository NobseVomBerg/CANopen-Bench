import asyncio
import base64
import io
import json
import os
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from canopen.objectdictionary.eds import import_eds
from conftest import SEED_EDS, connect_and_scan, write_seed_eds_files

import canopen_bench.core as core_mod
import canopen_bench.testcases as tclib
from canopen_bench.bus.canopen_bus import CanopenBus, _decode_cob
from canopen_bench.bus.interface import NO_SERIAL
from canopen_bench.core import Bench, normalize_identity, trace_class, trace_node
from canopen_bench.db import Db
from canopen_bench.eds_od import pdo_mapping
from canopen_bench.plugin import BenchPlugin

DEMO_DEVICE_EDS = core_mod.SEED_EDS


def drive_verify(bench: Bench, timeout: float = 3.0) -> None:
    """Dispatch mc_verify and wait for its async scan+compare to finish."""
    orig = core_mod.SCAN_DELAY_S
    core_mod.SCAN_DELAY_S = 0.02
    try:
        async def go():
            bench.dispatch("mc_verify", {})
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while bench.mc["busy"] and loop.time() < deadline:
                await asyncio.sleep(0.02)
        asyncio.run(go())
    finally:
        core_mod.SCAN_DELAY_S = orig
    assert not bench.mc["busy"], "verify did not finish in time"

MINIMAL_EDS = """\
[FileInfo]
FileName=test_device.eds
FileVersion=1
FileRevision=1
EDSVersion=4.0

[DeviceInfo]
VendorName=Test Vendor
VendorNumber=45
ProductName=TEST_DEV
ProductNumber=100
RevisionNumber=1
NrOfRXPDO=0
NrOfTXPDO=0
"""


@pytest.fixture()
def bench(tmp_path):
    b = Bench(Db(tmp_path / "test.db"))  # default adapter: demo
    write_seed_eds_files(b)  # demo DUTs need the seed EDS entries as real files
    return b


def run_to_completion(b: Bench, max_steps: int = 500) -> None:
    for _ in range(max_steps):
        if not b.running:
            return
        b._run_step()


@pytest.fixture()
def connected_bench(bench):
    connect_and_scan(bench)
    return bench


def test_snapshot_shape(bench):
    snap = bench.snapshot()
    for key in ("connected", "devices", "logs", "eds", "mc", "objects",
                "favorites", "raw", "tests", "swdl", "trace"):
        assert key in snap
    assert snap["connected"] is False
    assert snap["devices"] == []  # nothing until connect + scan


def test_starts_disconnected_then_connect_and_scan_populates(bench):
    assert not bench.connected
    connect_and_scan(bench)
    assert bench.connected
    # demo bus: 2 DUTs for the first enabled profile, 1 for each further one
    assert len(bench.devices) == 3
    assert {d["node"] for d in bench.devices} == {1, 2, 3}
    assert all(not d["sel"] for d in bench.devices)


def test_disconnect_clears_devices(connected_bench):
    connected_bench.dispatch("connect_toggle", {})
    assert not connected_bench.connected
    assert connected_bench.devices == []


def test_bus_lost_flips_state_and_logs(connected_bench):
    connected_bench._on_bus_lost("function canControlGetStatus failed")
    assert connected_bench.connected is False
    assert connected_bench.devices == []
    last = connected_bench.logs[-1]
    assert "connection lost" in last["msg"]
    assert last["type"] == "emcy0"

    log_count = len(connected_bench.logs)
    connected_bench._on_bus_lost("function canControlGetStatus failed")
    assert len(connected_bench.logs) == log_count  # no duplicate log entry


def test_bus_lost_when_never_connected_is_silent(bench):
    bench._on_bus_lost("x")
    assert bench.connected is False
    assert not any("connection lost" in log["msg"] for log in bench.logs)


def test_bus_lost_callback_registered_on_both_buses(bench):
    assert bench._hw_bus.on_lost == bench._on_bus_lost
    assert bench._demo_bus.on_lost == bench._on_bus_lost


def test_connect_failure_leaves_tool_disconnected(tmp_path):
    class FailingBus(CanopenBus):
        def connect(self, adapter: str, bitrate: int) -> None:
            raise RuntimeError("VCI device not found")

    bench = Bench(Db(tmp_path / "test.db"), bus=FailingBus())
    bench.dispatch("set_adapter", {"adapter": "ixxat"})  # hardware path, not demo
    bench.dispatch("connect_toggle", {})  # must not raise
    assert bench.connected is False
    assert any("connect failed" in ln["msg"] and ln["type"] == "emcy0" for ln in bench.logs)


def test_normalize_identity_canonicalizes_all_historic_widths():
    assert normalize_identity("0x000004D2·0x00001150") == "0x4D2·0x1150"  # hardware scan
    assert normalize_identity("0x04D2·0x1150") == "0x4D2·0x1150"  # old EDS upload
    assert normalize_identity("0x4D2·0x1150") == "0x4D2·0x1150"  # already canonical
    assert normalize_identity("?") == "?"  # incomplete identity passes through


def test_scan_matches_registry_entries_stored_in_legacy_format(bench):
    # entry stored in the old fixed-width format; the demo bus reports its
    # identity verbatim, so both comparison sides run through normalization
    bench.db.eds_remove("dut_alpha_v2.eds")
    bench.db.eds_add("legacy_alpha.eds", "DUT_ALPHA", "0x000004D2·0x00001150", "LGA", True)
    bench.db.eds_write_file("legacy_alpha.eds", SEED_EDS)
    connect_and_scan(bench)
    legacy = [d for d in bench.devices if d["eds"] == "legacy_alpha.eds"]
    assert legacy, bench.devices


def test_bus_state_defaults_to_unknown(bench):
    assert bench._demo_bus.bus_state() == ""


def test_favorites_toggle_read_and_persist(bench):
    connect_and_scan(bench)
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("fav_toggle", {"idx": "0x2050", "sub": "00"})
    assert bench.favorites == [{"idx": "0x2050", "sub": "00", "label": "Variant id"}]
    bench.dispatch("fav_read_all", {})
    assert bench.obj_vals["0x2050:00"] == "0x00"  # real SDO read via the demo bus
    again = Bench(Db(bench.db.path))  # favorites survive a restart
    assert again.favorites[0]["idx"] == "0x2050"
    bench.dispatch("fav_toggle", {"idx": "0x2050", "sub": "00"})  # toggle off
    assert bench.favorites == []


def test_favorites_migrates_legacy_fav_sets(tmp_path):
    """Old workspaces stored named favorite sets ("fav_sets" + active
    "fav_set"); those carry over into the single auto-saved list on init."""
    db = Db(tmp_path / "test.db")
    db.set("fav_sets", {"Default": [{"idx": "0x2050", "sub": "00", "label": "Variant id"}],
                        "Debug": [{"idx": "0x2040", "sub": "01", "label": "Product code"}]})
    db.set("fav_set", "Debug")
    b = Bench(db)
    assert b.favorites == [{"idx": "0x2040", "sub": "01", "label": "Product code"}]


def _trace_row(cob: str, data: str) -> dict:
    return {"time": "", "dir": "RX", "cob": cob, "len": "8", "data": data,
            "dec": "", "flag": "", "obj": "", "val": ""}


def test_trace_annotation_names_object_and_decodes_decimal(connected_bench):
    bench = connected_bench  # node 1 carries dut_alpha_v2.eds (SEED_EDS)
    row = _trace_row("0x581", "43 50 20 00 2A 00 00 00")  # expedited upload resp
    bench._annotate_sdo(row)
    assert row["obj"] == "0x2050:00 Variant id"
    assert row["val"] == "42"
    req = _trace_row("0x601", "40 40 20 01 00 00 00 00")  # upload request, record member
    bench._annotate_sdo(req)
    assert req["obj"] == "0x2040:01 Product code"
    assert req["val"] == ""  # a read request carries no value


def test_trace_annotation_abort_and_unknown_node(connected_bench):
    bench = connected_bench
    row = _trace_row("0x581", "80 00 30 00 00 00 02 06")
    bench._annotate_sdo(row)
    assert row["val"] == "abort 0x06020000"
    other = _trace_row("0x62A", "40 00 10 00 00 00 00 00")  # node 42: not scanned
    bench._annotate_sdo(other)
    assert other["obj"] == "0x1000:00"  # index:sub still shown, no EDS name
    hb = _trace_row("0x701", "05")  # heartbeat: not an SDO frame
    bench._annotate_sdo(hb)
    assert hb["obj"] == "" and hb["val"] == ""


def _emcy_row(cob: str, data: str) -> dict:
    row = _trace_row(cob, data)
    dec = _decode_cob(int(cob, 16))
    row["dec"] = dec
    row["cls"] = trace_class(dec)
    row["node"] = trace_node(cob)
    return row


def test_decode_cob_sync_and_emcy():
    assert _decode_cob(0x080) == "SYNC"
    assert _decode_cob(0x083) == "EMCY node 03"


def test_emcy_text_resolution_order(bench):
    assert bench._emcy_text(0x8130) == "Life guard / heartbeat error"  # exact
    assert bench._emcy_text(0x8223) == "Protocol error — generic"  # 0xXX00 class
    assert bench._emcy_text(0x1234) == "Generic error"  # 0xX000 group
    assert bench._emcy_text(0xA123) == "Unknown error code"  # no match at all


def test_annotate_emcy_full_path_logs_and_annotates(bench):
    row = _emcy_row("0x083", "30 81 11 00 00 00 00 00")
    before = bench.emcy_new
    bench._annotate_emcy(row)
    assert row["obj"] == "0x8130 Life guard / heartbeat error"
    assert row["val"] == "reg 0x11 generic+communication"
    last = bench.logs[-1]
    assert last["type"] == "emcy"
    assert "EMCY node 03" in last["msg"]
    assert bench.emcy_new == before + 1


def test_annotate_emcy_error_reset_is_info_not_alarm(bench):
    row = _emcy_row("0x083", "00 00 00 00 00 00 00 00")
    before = bench.emcy_new
    bench._annotate_emcy(row)
    assert row["obj"] == "0x0000 Error reset / no error"
    last = bench.logs[-1]
    assert last["type"] == "info"
    assert bench.emcy_new == before  # no badge for a clearing reset


def test_annotate_emcy_ignores_non_emcy_and_short_frames(bench):
    log_count = len(bench.logs)
    before = bench.emcy_new

    hb = _emcy_row("0x701", "05")  # heartbeat, not EMCY
    bench._annotate_emcy(hb)
    assert hb["obj"] == "" and hb["val"] == ""

    short = _emcy_row("0x083", "30 81")  # < 3 data bytes
    bench._annotate_emcy(short)
    assert short["obj"] == "" and short["val"] == ""

    assert len(bench.logs) == log_count  # neither frame produced a log entry
    assert bench.emcy_new == before


# -- PDO payload decoding via the EDS default mapping -----------------------

def test_pdo_mapping_reads_1a00_defaults_from_demo_device_eds():
    od = import_eds(str(DEMO_DEVICE_EDS), None)
    assert pdo_mapping(od, 0x1A00) == [(0x606C, 0, 32), (0x2002, 0, 16)]
    assert pdo_mapping(od, 0x1A01) == []  # object not present


def test_pdo_mapping_empty_for_od_without_the_object():
    od = import_eds(io.StringIO(MINIMAL_EDS), None)
    assert pdo_mapping(od, 0x1A00) == []


@pytest.fixture()
def pdo_bench(bench):
    ok, msg = bench.add_eds_file("DemoDevice.eds", DEMO_DEVICE_EDS.read_text())
    assert ok, msg
    bench.devices = [{"node": 3, "eds": "DemoDevice.eds", "sel": False, "name": "DemoDevice",
                      "nmt": "Operational", "fw": "1", "sn": "260003"}]
    return bench


def test_annotate_pdo_multi_signal_decode_with_sign_extension(pdo_bench):
    row = _emcy_row("0x183", "DC 05 00 00 FB FF")  # node 3, TxPDO1
    pdo_bench._annotate_pdo(row)
    assert row["obj"] == "Velocity actual value=1500 · Board temperature=-5"
    assert row["val"] == ""


def test_annotate_pdo_unknown_node_and_missing_mapping_leave_row_untouched(pdo_bench):
    unknown = _emcy_row("0x185", "DC 05 00 00 FB FF")  # node 5: not in bench.devices
    pdo_bench._annotate_pdo(unknown)
    assert unknown["obj"] == "" and unknown["val"] == ""

    no_mapping = _emcy_row("0x303", "DC 05 00 00 FB FF")  # RxPDO2 node 3, 0x1601 absent
    pdo_bench._annotate_pdo(no_mapping)
    assert no_mapping["obj"] == "" and no_mapping["val"] == ""


def test_annotate_pdo_truncated_payload_decodes_only_the_fitting_signal(pdo_bench):
    row = _emcy_row("0x183", "DC 05 00 00")  # only 4 bytes: second signal doesn't fit
    pdo_bench._annotate_pdo(row)
    assert row["obj"] == "0x606C:00 Velocity actual value"
    assert row["val"] == "1500"


def test_annotate_pdo_ignores_non_pdo_rows(pdo_bench):
    hb = _emcy_row("0x701", "05")  # heartbeat, not a PDO
    pdo_bench._annotate_pdo(hb)
    assert hb["obj"] == "" and hb["val"] == ""


# -- signal plot (Trace page) ------------------------------------------------

def test_plot_toggle_adds_then_removes_signal(connected_bench):
    bench = connected_bench  # node 1 carries dut_alpha_v2.eds (SEED_EDS)
    bench.dispatch("dev_toggle", {"node": 1})
    assert bench.plot_sel == []
    bench.dispatch("plot_toggle", {"idx": "0x2040", "sub": "01"})
    assert bench.plot_sel == [{"idx": "0x2040", "sub": "01",
                                "label": "Product identification.Product code"}]
    assert bench._plot_keys == {"0x2040:01"}
    assert "added" in bench.logs[-1]["msg"]

    bench.dispatch("plot_toggle", {"idx": "0x2040", "sub": "01"})  # toggle off
    assert bench.plot_sel == []
    assert bench._plot_keys == set()
    assert "removed" in bench.logs[-1]["msg"]


def test_plot_toggle_caps_at_plot_sel_max(connected_bench):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})
    signals = [("0x1000", "00"), ("0x2040", "00"), ("0x2040", "01"), ("0x2050", "00")]
    for idx, sub in signals:
        bench.dispatch("plot_toggle", {"idx": idx, "sub": sub})
    assert len(bench.plot_sel) == core_mod.PLOT_SEL_MAX == 4

    bench.dispatch("plot_toggle", {"idx": "0x2000", "sub": "00"})  # 5th signal
    assert len(bench.plot_sel) == 4  # rejected, cap holds
    last = bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "at most 4 signals" in last["msg"]


def test_plot_toggle_removal_drops_the_series(connected_bench):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("plot_toggle", {"idx": "0x2040", "sub": "01"})
    bench.plot_series["0x2040:01"] = deque([(1.0, 2.0)])

    bench.dispatch("plot_toggle", {"idx": "0x2040", "sub": "01"})  # remove
    assert "0x2040:01" not in bench.plot_series


def test_plot_clear_empties_selection_and_series(connected_bench):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})
    for idx, sub in [("0x1000", "00"), ("0x2040", "01")]:
        bench.dispatch("plot_toggle", {"idx": idx, "sub": sub})
    bench.plot_series["0x1000:00"] = deque([(1.0, 2.0)])
    bench.plot_series["0x2040:01"] = deque([(1.0, 3.0)])

    bench.dispatch("plot_clear", {})
    assert bench.plot_sel == []
    assert bench.plot_series == {}


def test_plot_sample_ignores_unselected_signal(bench):
    bench._plot_sample(0x2040, 1, "42")
    assert bench.plot_series == {}


def test_plot_sample_appends_numeric_value_for_selected_signal(connected_bench):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("plot_toggle", {"idx": "0x2040", "sub": "01"})

    bench._plot_sample(0x2040, 1, "42")
    series = bench.plot_series["0x2040:01"]
    assert len(series) == 1
    ts, val = series[0]
    assert val == 42.0
    assert isinstance(val, float)


def test_plot_sample_ignores_non_numeric_value(connected_bench):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("plot_toggle", {"idx": "0x2040", "sub": "01"})

    bench._plot_sample(0x2040, 1, "abort 0x06020000")
    assert "0x2040:01" not in bench.plot_series


def test_plot_sample_deque_caps_at_plot_points(connected_bench):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("plot_toggle", {"idx": "0x2040", "sub": "01"})

    for i in range(core_mod.PLOT_POINTS + 50):
        bench._plot_sample(0x2040, 1, i)
    series = bench.plot_series["0x2040:01"]
    assert len(series) == core_mod.PLOT_POINTS
    first_val = series[0][1]
    assert first_val == 50  # the oldest 50 samples fell off the deque


def test_annotate_sdo_samples_plot_on_expedited_success_not_on_abort(pdo_bench):
    bench = pdo_bench  # node 3 carries DemoDevice.eds
    bench.dispatch("dev_toggle", {"node": 3})
    bench.dispatch("plot_toggle", {"idx": "0x1000", "sub": "00"})

    row = _trace_row("0x583", "43 00 10 00 99 00 00 00")  # expedited upload resp
    bench._annotate_sdo(row)
    assert row["val"] == "153"
    series = bench.plot_series["0x1000:00"]
    assert len(series) == 1
    assert series[0][1] == 153.0

    abort = _trace_row("0x583", "80 00 10 00 00 00 02 06")
    bench._annotate_sdo(abort)
    assert len(bench.plot_series["0x1000:00"]) == 1  # abort path does not sample


def test_annotate_pdo_samples_plot_for_every_mapped_signal(pdo_bench):
    bench = pdo_bench  # node 3 carries DemoDevice.eds, TxPDO1: 0x606C(32) + 0x2002(16)
    bench.dispatch("dev_toggle", {"node": 3})
    bench.dispatch("plot_toggle", {"idx": "0x606C", "sub": "00"})
    bench.dispatch("plot_toggle", {"idx": "0x2002", "sub": "00"})

    row = _emcy_row("0x183", "DC 05 00 00 FB FF")  # node 3, TxPDO1
    bench._annotate_pdo(row)
    assert bench.plot_series["0x606C:00"][-1][1] == 1500.0
    assert bench.plot_series["0x2002:00"][-1][1] == -5.0


def test_plot_snapshot_reflects_selection_and_series(connected_bench):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("plot_toggle", {"idx": "0x2040", "sub": "01"})
    bench._plot_sample(0x2040, 1, 7)

    plot = bench.snapshot()["trace"]["plot"]
    assert plot["sel"] == bench.plot_sel
    series = plot["series"]["0x2040:01"]
    assert isinstance(series, list)
    assert series[-1][1] == 7.0


def test_plot_toggle_persists_selection_to_db(connected_bench):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("plot_toggle", {"idx": "0x2040", "sub": "01"})

    saved = bench.db.get("plot_sel")
    assert {"idx": "0x2040", "sub": "01",
            "label": "Product identification.Product code"} in saved


def test_emcy_codes_merge_plugin_wins_over_cia_table(tmp_path):
    class TestPlugin(BenchPlugin):
        name = "test"

        def emcy_codes(self):
            return {0x8130: "Overridden", 0xFF01: "Special"}

    bench = Bench(Db(tmp_path / "t.db"), plugins=[TestPlugin()])
    assert bench._emcy_text(0x8130) == "Overridden"  # plugin wins on conflict
    assert bench._emcy_text(0xFF01) == "Special"  # plugin-only code


def test_emit_emcy_end_to_end_on_demo_bus(connected_bench):
    bench = connected_bench
    bench.bus.emit_emcy(3, 0x8130, 0x11)
    frames = bench.bus.poll_frames(50)
    emcy = next(f for f in frames if f.cob_id == "0x083")
    assert emcy.decoded == "EMCY node 03"
    assert emcy.data.startswith("30 81 11")


def test_adapter_change_while_connected_disconnects(connected_bench):
    connected_bench.dispatch("set_adapter", {"adapter": "ixxat"})
    assert not connected_bench.connected
    assert connected_bench.devices == []
    assert connected_bench.adapter == "ixxat"


def test_set_bitrate_while_connected_reconnects_immediately(connected_bench):
    bench = connected_bench
    bench.dispatch("set_bitrate", {"bitrate": "250"})
    assert bench.connected is True
    assert bench.bitrate == "250"
    assert Db(bench.db.path).get("bitrate") == "250"
    assert any("bitrate applied — reconnected @" in ln["msg"] for ln in bench.logs)


def test_set_bitrate_while_disconnected_only_stores_value(bench):
    bench.dispatch("set_bitrate", {"bitrate": "250"})
    assert bench.connected is False
    assert bench.bitrate == "250"
    assert Db(bench.db.path).get("bitrate") == "250"
    assert not any("reconnected" in ln["msg"] for ln in bench.logs)


def test_set_bitrate_reconnect_failure_disconnects_and_logs(tmp_path):
    class FlakyBus(CanopenBus):
        def __init__(self):
            super().__init__()
            self._connect_calls = 0

        def connect(self, adapter: str, bitrate: int) -> None:
            self._connect_calls += 1
            if self._connect_calls > 1:
                raise RuntimeError("VCI device not found")
            # first call: pretend the connect succeeded, no real hardware touched

    bench = Bench(Db(tmp_path / "test.db"), bus=FlakyBus())
    bench.dispatch("set_adapter", {"adapter": "ixxat"})  # hardware path, not demo
    bench.dispatch("connect_toggle", {})
    assert bench.connected is True

    bench.dispatch("set_bitrate", {"bitrate": "250"})

    assert bench.connected is False
    assert bench.devices == []
    assert any("reconnect failed" in ln["msg"] and ln["type"] == "emcy0" for ln in bench.logs)


def test_apply_iface_action_removed(bench):
    with pytest.raises(ValueError):
        bench.dispatch("apply_iface", {})


def test_shutdown_disconnects_bus(connected_bench):
    assert connected_bench.bus.connected
    connected_bench.shutdown()
    assert not connected_bench.connected
    assert not connected_bench.bus.connected


def test_mc_verify_requires_connection(bench):
    bench.dispatch("mc_verify", {})
    assert not bench.mc["busy"]
    assert "not connected" in bench.logs[-1]["msg"]


def test_mc_verify_requires_active_setup(connected_bench):
    assert connected_bench.mc_ref is None
    connected_bench.dispatch("mc_verify", {})
    assert not connected_bench.mc["busy"]
    assert "no expected state adopted" in connected_bench.logs[-1]["msg"]


def test_mc_defaults_are_empty_on_a_fresh_bench(bench):
    mc = bench.snapshot()["mc"]
    assert mc["expected"] == 0
    assert mc["found"] == 0
    assert mc["last"] == ""
    assert mc["result"] == ""


def test_mc_becomes_real_after_adopt_and_verify(connected_bench):
    bench = connected_bench
    bench.dispatch("mc_adopt", {})
    drive_verify(bench)
    n_devices = len(bench.devices)
    assert bench.mc["expected"] == n_devices
    assert bench.mc["found"] == n_devices
    assert bench.mc["result"] == "ok"
    assert bench.mc["last"] != ""


def test_mc_verify_scans_and_validates_against_active_setup(connected_bench):
    bench = connected_bench
    bench.dispatch("mc_adopt", {})  # adopts the current bus state as the reference
    assert bench.mc["expected"] == 3  # F-5: adopted from the reference
    bench.devices = []  # F-4: verify must rescan, not reuse a stale list
    drive_verify(bench)
    assert bench.mc["result"] == "ok"
    assert bench.mc["found"] == 3
    assert any("expected state valid" in ln["msg"] for ln in bench.logs)


def test_mc_verify_detects_missing_device(connected_bench):
    bench = connected_bench
    bench.dispatch("mc_adopt", {})  # adopts the current bus state as the reference
    bench.mc["autoReaddr"] = False  # keep the mismatch result observable
    (bench.db.eds_dir / "dut_gamma_v5.eds").unlink()  # its demo DUT disappears
    drive_verify(bench)
    assert bench.mc["result"] == "mismatch"
    assert bench.mc["found"] == 2


def test_suites_save_load_delete(bench):
    bench.test_sel = {"0001", "1000"}
    bench.repeat_case = 2
    bench.stop_on_err = False
    bench.dispatch("suite_save", {"name": "Regression"})
    bench.test_sel = set()
    bench.repeat_case = 1
    bench.stop_on_err = True
    bench.dispatch("suite_load", {"name": "Regression"})
    assert bench.test_sel == {"0001", "1000"}
    assert bench.repeat_case == 2 and bench.stop_on_err is False
    assert bench.active_suite == "Regression"
    again = Bench(Db(bench.db.path))  # persisted
    assert "Regression" in again.suites
    bench.dispatch("suite_delete", {"name": "Regression"})
    assert bench.suites == {}
    assert bench.active_suite == ""


def test_run_stops_on_error(bench):
    bench.test_sel = {"0000", "4455", "4622"}  # 4455 fails deterministically
    bench.stop_on_err = True
    bench.dispatch("run_start", {})
    run_to_completion(bench)
    assert bench.results["4455"] == "FAIL"
    assert "4622" not in bench.results  # aborted before reaching it
    assert bench.reports[0]["score"] == "1/2"


def test_run_continues_without_stop_on_error(bench):
    bench.test_sel = {"0000", "4455", "4622"}
    bench.stop_on_err = False
    bench.dispatch("run_start", {})
    run_to_completion(bench)
    assert bench.results == {"0000": "PASS", "4455": "FAIL", "4622": "PASS"}
    assert bench.reports[0]["score"] == "2/3"


def test_repeat_counts_expand_run_order(bench):
    bench.test_sel = {"0000"}
    bench.repeat_case = 3
    bench.repeat_run = 2
    bench.dispatch("run_start", {})
    assert len(bench.run_order) == 6


def test_mc_adopt_stores_ref_and_persists(connected_bench):
    bench = connected_bench
    bench.dispatch("mc_adopt", {})
    ref = bench.mc_ref
    assert ref is not None
    assert ref["expected"] == 3
    assert ref["assignments"]["1"] == "dut_alpha_v2.eds"
    assert ref["session"] == bench.mc["session"]
    assert bench.mc["expected"] == 3
    assert any("expected state adopted" in ln["msg"] for ln in bench.logs)
    # persisted to kv — a fresh Bench for the same workspace picks it up
    again = Bench(Db(bench.db.path))
    assert again.mc_ref == ref
    assert again.mc["expected"] == 3
    snap = bench.snapshot()
    assert snap["mc"]["ref"] == ref

    bench.dispatch("mc_adopt", {})  # adopting again replaces the reference
    assert bench.mc_ref["adopted"] >= ref["adopted"]


def test_mc_adopt_without_devices_logs_and_leaves_ref_unchanged(bench):
    assert bench.mc_ref is None
    bench.dispatch("mc_adopt", {})
    assert bench.mc_ref is None
    assert "nothing to adopt" in bench.logs[-1]["msg"]
    assert bench.logs[-1]["type"] == "emcy0"


def test_raw_sdo_persisted_across_restart(bench, tmp_path):
    bench.dispatch("raw_add", {})
    bench.dispatch("raw_update", {"row": 1, "field": "i", "value": "0x1017"})
    reborn = Bench(Db(tmp_path / "test.db"))
    assert len(reborn.raw_rows) == 2
    assert reborn.raw_rows[1]["i"] == "0x1017"


# -- raw rows: typed SDO / PDO / NMT rows ------------------------------------

def test_raw_row_legacy_backfills_type_and_node(bench):
    row = bench.raw_rows[0]
    assert row["type"] == "sdo"
    assert row["node"] == ""


def test_raw_row_per_row_node_overrides_selection(connected_bench):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})  # selected device would otherwise win
    bench.dispatch("raw_update", {"row": 0, "field": "node", "value": "2"})
    bench.dispatch("raw_update", {"row": 0, "field": "i", "value": "0x2040"})
    bench.dispatch("raw_update", {"row": 0, "field": "s", "value": "01"})
    bench.dispatch("raw_read", {"row": 0})
    assert "(node 2)" in bench.logs[-1]["msg"]


def test_raw_row_empty_node_falls_back_to_selected_device(connected_bench):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 2})  # select node 2
    bench.dispatch("raw_update", {"row": 0, "field": "node", "value": ""})
    bench.dispatch("raw_update", {"row": 0, "field": "i", "value": "0x2040"})
    bench.dispatch("raw_update", {"row": 0, "field": "s", "value": "01"})
    bench.dispatch("raw_read", {"row": 0})
    assert "(node 2)" in bench.logs[-1]["msg"]


def test_raw_read_on_default_row_skips_invalid_index_without_raising(connected_bench):
    bench = connected_bench
    bench.dispatch("raw_add", {})
    row = len(bench.raw_rows) - 1
    assert bench.raw_rows[row]["i"] == "0x"  # act_raw_add's default, previously raised

    bench.dispatch("raw_read", {"row": row})  # must not raise

    last = bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "RAW  SDO read skipped — invalid index/sub/node" in last["msg"]


def test_raw_write_on_default_row_skips_invalid_index_without_raising(connected_bench):
    bench = connected_bench
    bench.dispatch("raw_add", {})
    row = len(bench.raw_rows) - 1

    bench.dispatch("raw_write", {"row": row})  # must not raise

    last = bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "RAW  SDO write skipped — invalid index/sub/node" in last["msg"]


def test_raw_read_recovers_once_a_valid_index_is_set(connected_bench):
    bench = connected_bench
    bench.dispatch("raw_add", {})
    row = len(bench.raw_rows) - 1
    bench.dispatch("raw_update", {"row": row, "field": "node", "value": "1"})
    bench.dispatch("raw_update", {"row": row, "field": "i", "value": "0x2040"})
    bench.dispatch("raw_update", {"row": row, "field": "s", "value": "01"})

    bench.dispatch("raw_read", {"row": row})

    last = bench.logs[-1]
    assert last["type"] == "sdo"
    assert "SDO  read 0x2040:01" in last["msg"]


def test_raw_node_out_of_range_skips_with_log(connected_bench):
    bench = connected_bench
    bench.dispatch("raw_update", {"row": 0, "field": "node", "value": "255"})
    bench.dispatch("raw_update", {"row": 0, "field": "i", "value": "0x2040"})
    bench.dispatch("raw_update", {"row": 0, "field": "s", "value": "01"})

    bench.dispatch("raw_read", {"row": 0})

    last = bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "invalid index/sub/node" in last["msg"]


def test_raw_node_accepts_hex(connected_bench):
    bench = connected_bench
    bench.dispatch("raw_update", {"row": 0, "field": "node", "value": "0x02"})
    bench.dispatch("raw_update", {"row": 0, "field": "i", "value": "0x2040"})
    bench.dispatch("raw_update", {"row": 0, "field": "s", "value": "01"})

    bench.dispatch("raw_read", {"row": 0})

    assert "(node 2)" in bench.logs[-1]["msg"]
    assert bench.logs[-1]["type"] == "sdo"


def test_raw_update_rejects_unknown_field(bench):
    before = dict(bench.raw_rows[0])
    bench.dispatch("raw_update", {"row": 0, "field": "bogus", "value": "x"})
    assert bench.raw_rows[0] == before


def test_raw_update_ignores_out_of_range_row(bench):
    n_logs = len(bench.logs)
    bench.dispatch("raw_update", {"row": 99, "field": "i", "value": "0x1234"})
    bench.dispatch("raw_update", {"row": -1, "field": "i", "value": "0x1234"})
    assert len(bench.logs) == n_logs  # no exception, no side effect


def test_raw_add_defaults_and_caps_at_eight(bench):
    bench.dispatch("raw_add", {})
    new_row = bench.raw_rows[-1]
    assert new_row == {"type": "sdo", "node": "", "i": "0x", "s": "00", "l": "1", "v": "",
                       "cyc": "100", "run": False, "id": new_row["id"]}
    assert isinstance(new_row["id"], int)
    for _ in range(10):
        bench.dispatch("raw_add", {})
    assert len(bench.raw_rows) == 8


def test_raw_send_pdo_happy_path(connected_bench):
    bench = connected_bench
    bench.dispatch("raw_update", {"row": 0, "field": "type", "value": "pdo"})
    bench.dispatch("raw_update", {"row": 0, "field": "pdo", "value": "RxPDO2"})
    bench.dispatch("raw_update", {"row": 0, "field": "node", "value": "1"})
    bench.dispatch("raw_update", {"row": 0, "field": "data", "value": "01 A0 00 FF"})
    bench.dispatch("raw_send", {"row": 0})
    assert "PDO  RxPDO2 → node 01" in bench.logs[-1]["msg"]
    frames = bench.bus.poll_frames(50)
    tx = next(f for f in frames if f.cob_id == "0x301")
    assert tx.data == "01 A0 00 FF"
    assert "RxPDO2" in tx.decoded


def test_raw_send_pdo_rejects_non_hex_data(connected_bench):
    bench = connected_bench
    bench.dispatch("raw_update", {"row": 0, "field": "type", "value": "pdo"})
    bench.dispatch("raw_update", {"row": 0, "field": "pdo", "value": "RxPDO1"})
    bench.dispatch("raw_update", {"row": 0, "field": "node", "value": "1"})
    bench.dispatch("raw_update", {"row": 0, "field": "data", "value": "zz"})
    bench.dispatch("raw_send", {"row": 0})
    last = bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "data must be hex bytes" in last["msg"]


def test_raw_send_pdo_rejects_too_many_bytes(connected_bench):
    bench = connected_bench
    bench.dispatch("raw_update", {"row": 0, "field": "type", "value": "pdo"})
    bench.dispatch("raw_update", {"row": 0, "field": "pdo", "value": "RxPDO1"})
    bench.dispatch("raw_update", {"row": 0, "field": "node", "value": "1"})
    bench.dispatch("raw_update", {"row": 0, "field": "data", "value": "00 01 02 03 04 05 06 07 08"})
    bench.dispatch("raw_send", {"row": 0})
    last = bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "> 8" in last["msg"]


def test_raw_send_pdo_rejects_invalid_node(connected_bench):
    bench = connected_bench
    bench.dispatch("raw_update", {"row": 0, "field": "type", "value": "pdo"})
    bench.dispatch("raw_update", {"row": 0, "field": "pdo", "value": "RxPDO1"})
    bench.dispatch("raw_update", {"row": 0, "field": "node", "value": "0"})
    bench.dispatch("raw_send", {"row": 0})
    assert bench.logs[-1]["type"] == "emcy0"


def test_raw_send_nmt_broadcast(connected_bench):
    bench = connected_bench
    bench.dispatch("raw_update", {"row": 0, "field": "type", "value": "nmt"})
    bench.dispatch("raw_update", {"row": 0, "field": "cmd", "value": "stop"})
    bench.dispatch("raw_update", {"row": 0, "field": "node", "value": ""})
    bench.dispatch("raw_send", {"row": 0})
    assert "NMT  stop → all nodes" in bench.logs[-1]["msg"]
    assert all(d["nmt"] == "Stopped" for d in bench.devices)
    frames = bench.bus.poll_frames(50)
    assert any(f.cob_id == "0x000" and f.data == "02 00" for f in frames)


def test_raw_send_nmt_single_node(connected_bench):
    bench = connected_bench
    bench.dispatch("raw_update", {"row": 0, "field": "type", "value": "nmt"})
    bench.dispatch("raw_update", {"row": 0, "field": "cmd", "value": "start"})
    bench.dispatch("raw_update", {"row": 0, "field": "node", "value": "1"})
    bench.dispatch("raw_send", {"row": 0})
    states = {d["node"]: d["nmt"] for d in bench.devices}
    assert states[1] == "Operational"
    assert states[2] == "Pre-Operational"  # untouched
    assert "NMT  start → node 01" in bench.logs[-1]["msg"]


def test_raw_send_when_not_connected_logs_and_does_not_raise(bench):
    bench.dispatch("raw_update", {"row": 0, "field": "type", "value": "pdo"})
    bench.dispatch("raw_send", {"row": 0})
    last = bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "interface not connected" in last["msg"]


WO_EDS = """\
[FileInfo]
FileName=wo.eds
EDSVersion=4.0

[DeviceInfo]
VendorName=T
VendorNumber=0x0001
ProductName=WoDev
ProductNumber=0x0002

[MandatoryObjects]
SupportedObjects=1
1=0x1000

[1000]
ParameterName=Device type
ObjectType=0x7
DataType=0x0007
AccessType=ro
DefaultValue=0x0

[ManufacturerObjects]
SupportedObjects=2
1=0x2001
2=0x2002

[2001]
ParameterName=Trigger command
ObjectType=0x7
DataType=0x0005
AccessType=wo
DefaultValue=0

[2002]
ParameterName=Status
ObjectType=0x7
DataType=0x0005
AccessType=ro
DefaultValue=0
"""


def test_fav_read_all_skips_write_only_objects(bench, monkeypatch):
    bench.add_eds_file("wo.eds", WO_EDS)
    bench.devices = [{"node": 1, "eds": "wo.eds", "sel": True, "name": "WoDev",
                      "nmt": "Operational", "fw": "1", "sn": "1"}]
    bench.favorites = [{"idx": "0x2001", "sub": "00", "label": "wo"},
                       {"idx": "0x2002", "sub": "00", "label": "ro"}]
    read: list[str] = []
    monkeypatch.setattr(bench, "act_obj_read", lambda p: read.append(f"{p['idx']}:{p['sub']}"))
    bench.dispatch("fav_read_all", {})
    assert read == ["0x2002:00"]  # the write-only favorite is skipped


def test_pad_hex_normalizes_to_object_width():
    assert Bench._pad_hex("0x42", 2) == "0x0042"
    assert Bench._pad_hex("0x12345", 2) == "0x12345"  # longer than width: never truncated
    assert Bench._pad_hex("hello", 4) == "hello"  # non-numeric passes through
    assert Bench._pad_hex("0x42", 0) == "0x42"  # unknown width: unchanged


def test_a_typed_value_is_hex_only_when_it_says_so():
    """This box used to read every number as hex, so typing 30 wrote
    forty-eight and nothing on screen admitted it. A field that quietly
    means something other than it says is worse on a bench than one that
    refuses, so hex now needs its 0x."""
    assert Bench._pad_hex("30", 4) == "0x0000001E"   # thirty, not forty-eight
    assert Bench._pad_hex("0x30", 4) == "0x00000030"
    assert Bench._pad_hex("1E", 4) == "1E"           # ambiguous: not a decimal number


def test_obj_write_pads_value_to_eds_width(connected_bench, monkeypatch):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})
    sent: list[str] = []
    orig = bench.bus.sdo_write
    monkeypatch.setattr(bench.bus, "sdo_write",
                        lambda node, i, s, v: (sent.append(v), orig(node, i, s, v))[1])
    bench.dispatch("obj_set", {"idx": "0x2000", "sub": "00", "val": "0x42"})
    bench.dispatch("obj_write", {"idx": "0x2000", "sub": "00"})  # U32 per seed EDS
    assert sent == ["0x00000042"]


def test_raw_write_pads_value_to_len_field(connected_bench, monkeypatch):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})
    sent: list[str] = []
    orig = bench.bus.sdo_write
    monkeypatch.setattr(bench.bus, "sdo_write",
                        lambda node, i, s, v: (sent.append(v), orig(node, i, s, v))[1])
    for field, value in (("i", "0x2000"), ("s", "00"), ("l", "2"), ("v", "0x42")):
        bench.dispatch("raw_update", {"row": 0, "field": field, "value": value})
    bench.dispatch("raw_write", {"row": 0})
    assert sent == ["0x0042"]


def test_obj_set_stages_value_and_write_sends_it(connected_bench):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})  # select the write target
    bench.dispatch("obj_set", {"idx": "0x2000", "sub": "00", "val": "0x260001"})
    assert bench.obj_vals["0x2000:00"] == "0x260001"  # staged only — no bus write yet
    bench.dispatch("obj_write", {"idx": "0x2000", "sub": "00"})
    res = bench.bus.sdo_read(1, "0x2000", "00")
    assert res.ok and res.value == "0x00260001"  # padded to the EDS's U32 width


def test_last_known_values_restored_on_select(connected_bench):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})  # select
    bench.dispatch("obj_read", {"idx": "0x2040", "sub": "01"})  # remembered for SN 260001
    bench.obj_vals = {}
    bench.dispatch("dev_toggle", {"node": 1})  # deselect
    bench.dispatch("dev_toggle", {"node": 1})  # reselect -> restore from db
    assert bench.obj_vals["0x2040:01"] == "0x00260001"


def test_switching_devices_does_not_carry_values_over(connected_bench):
    """The reported bug: values read from one device stayed in the table
    after switching to another, indistinguishable from that device's own —
    and Write would have sent them there."""
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("obj_read", {"idx": "0x2040", "sub": "01"})
    assert "0x2040:01" in bench.obj_vals
    bench.dispatch("dev_toggle", {"node": 1})            # deselect
    bench.dispatch("dev_toggle", {"node": 2})            # a different device
    assert "0x2040:01" not in bench.obj_vals


def test_deselecting_everything_empties_the_table(connected_bench):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("obj_read", {"idx": "0x2040", "sub": "01"})
    bench.dispatch("dev_toggle", {"node": 1})
    assert bench.obj_vals == {}


def test_a_staged_value_does_not_follow_to_the_next_device(connected_bench):
    """obj_set stages without writing. Staged for node 1, it must not be
    sitting in the field when node 2 is selected."""
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("obj_set", {"idx": "0x2000", "sub": "00", "val": "0x42"})
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("dev_toggle", {"node": 2})
    assert "0x2000:00" not in bench.obj_vals


def test_a_device_without_a_serial_number_gets_no_remembered_values(connected_bench):
    """Every device that answers no serial reports the same "?", so keeping
    values under it would hand one device's readings to the next. Empty
    table, and a line saying why — a silently empty one reads as a bug."""
    bench = connected_bench
    bench.devices[0]["sn"] = NO_SERIAL
    bench.dispatch("dev_toggle", {"node": bench.devices[0]["node"]})
    bench.dispatch("obj_read", {"idx": "0x2040", "sub": "01"})
    bench.dispatch("dev_toggle", {"node": bench.devices[0]["node"]})
    bench.dispatch("dev_toggle", {"node": bench.devices[0]["node"]})
    assert bench.obj_vals == {}
    assert any("no serial number" in row["msg"] for row in bench.logs)


def test_a_scan_reloads_the_table_for_whoever_is_at_that_node(connected_bench):
    """Selection survives a scan by node-id; the unit at that node need not
    be the same one, so the values are re-fetched rather than kept."""
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("obj_read", {"idx": "0x2040", "sub": "01"})
    bench.obj_vals["0x2040:01"] = "0xDEADBEEF"       # never read from anything
    connect_and_scan(bench)
    assert bench.obj_vals.get("0x2040:01") == "0x00260001"   # what the db holds


def test_nmt_applies_to_selection_only(connected_bench):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("nmt", {"cmd": "stop"})
    states = {d["node"]: d["nmt"] for d in bench.devices}
    assert states[1] == "Stopped"
    assert states[2] == "Pre-Operational"  # not selected


def test_dev_menu_nmt_actions_send_on_bus_and_update_state(connected_bench, monkeypatch):
    bench = connected_bench
    calls = []
    orig_nmt = bench.bus.nmt

    def spy(cmd, node=None):
        calls.append((cmd, node))
        return orig_nmt(cmd, node)

    monkeypatch.setattr(bench.bus, "nmt", spy)
    dev = next(d for d in bench.devices if d["node"] == 1)

    bench.dispatch("dev_menu", {"node": 1, "what": "restart"})
    assert dev["nmt"] == "Pre-Operational"
    assert bench.logs[-1] == {"t": bench.logs[-1]["t"], "type": "nmt",
                              "msg": "NMT  reset node → node 01"}

    bench.dispatch("dev_menu", {"node": 1, "what": "op"})
    assert dev["nmt"] == "Operational"
    assert bench.logs[-1]["msg"] == "NMT  start → node 01"
    assert bench.logs[-1]["type"] == "nmt"

    bench.dispatch("dev_menu", {"node": 1, "what": "preop"})
    assert dev["nmt"] == "Pre-Operational"
    assert bench.logs[-1]["msg"] == "NMT  pre-op → node 01"

    bench.dispatch("dev_menu", {"node": 1, "what": "resetcomm"})
    assert dev["nmt"] == "Pre-Operational"  # resetcomm has no displayed state of its own
    assert bench.logs[-1]["msg"] == "NMT  reset comm → node 01"

    assert calls == [("reset", 1), ("start", 1), ("preop", 1), ("resetcomm", 1)]


def test_dev_menu_nmt_skipped_when_not_connected(connected_bench, monkeypatch):
    bench = connected_bench
    calls = []
    monkeypatch.setattr(bench.bus, "nmt", lambda *a, **k: calls.append((a, k)))
    bench.connected = False  # devices stay populated, only the connection drops
    dev = next(d for d in bench.devices if d["node"] == 1)
    before_state = dev["nmt"]
    n_logs = len(bench.logs)

    bench.dispatch("dev_menu", {"node": 1, "what": "op"})

    assert calls == []
    assert dev["nmt"] == before_state
    assert len(bench.logs) == n_logs


def test_eds_toggle_and_code(bench):
    bench.dispatch("eds_toggle", {"file": "dut_beta_v7.eds"})
    assert "dut_beta_v7.eds" in bench.eds_enabled
    bench.dispatch("eds_code", {"file": "dut_beta_v7.eds", "code": "X24"})
    reloaded = {e["file"]: e for e in Db(bench.db.path).eds_list()}
    assert reloaded["dut_beta_v7.eds"]["code"] == "X24"


def test_swdl_serial_updates_firmware(connected_bench):
    bench = connected_bench
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("swdl_fw", {"ver": "1.0.0"})
    bench.dispatch("swdl_start", {})
    for _ in range(200):
        if not bench.swdl_run:
            break
        bench._swdl.step(bench)
    assert bench.swdl_done
    assert bench.devices[0]["fw"] == "1.0.0"
    assert bench.devices[1]["fw"] == "1.0.0-demo"  # not selected, untouched


def test_add_eds_file_parses_real_eds(bench):
    ok, msg = bench.add_eds_file("test_device.eds", MINIMAL_EDS)
    assert ok, msg
    entries = {e["file"]: e for e in bench.db.eds_list()}
    entry = entries["test_device.eds"]
    assert entry["dev"] == "TEST_DEV"
    assert entry["ident"] == "0x2D·0x64"  # 45 = 0x2D, 100 = 0x64 — canonical minimal width
    assert entry["code"] == "TES"  # default: first 3 letters of filename
    assert entry["enabled"] is True
    assert "content" not in entry  # not a DB blob


def test_add_eds_file_writes_a_real_file_not_a_db_blob(bench):
    ok, msg = bench.add_eds_file("test_device.eds", MINIMAL_EDS)
    assert ok, msg
    on_disk = bench.db.eds_dir / "test_device.eds"
    assert on_disk.is_file()
    assert on_disk.read_text() == MINIMAL_EDS


def test_add_eds_file_sanitizes_path_traversal_in_filename(bench):
    ok, msg = bench.add_eds_file("../../etc/evil.eds", MINIMAL_EDS)
    assert ok, msg  # sanitized down to a bare filename inside eds_dir, still succeeds
    assert (bench.db.eds_dir / "evil.eds").is_file()
    assert not (bench.db.eds_dir.parent / "etc").exists()  # nothing escaped eds_dir


def test_add_eds_file_rejects_malformed_content(bench):
    ok, msg = bench.add_eds_file("garbage.eds", "this is not an eds file {{{")
    assert not ok
    assert "parse" in msg
    assert not (bench.db.eds_dir / "garbage.eds").exists()


def test_add_eds_file_rejects_missing_vendor_product(bench):
    eds_without_identity = "[FileInfo]\nFileName=x.eds\n\n[DeviceInfo]\nProductName=X\n"
    ok, msg = bench.add_eds_file("x.eds", eds_without_identity)
    assert not ok
    assert "Vendor" in msg or "Product" in msg
    assert not (bench.db.eds_dir / "x.eds").exists()


def test_eds_remove_deletes_the_file_from_disk(bench):
    bench.add_eds_file("test_device.eds", MINIMAL_EDS)
    on_disk = bench.db.eds_dir / "test_device.eds"
    assert on_disk.is_file()
    bench.dispatch("eds_remove", {"file": "test_device.eds"})
    assert not on_disk.exists()


def test_eds_upload_action_logs_rejection(bench):
    n_logs = len(bench.logs)
    bench.dispatch("eds_upload", {"filename": "bad.eds", "content": "not an eds"})
    assert len(bench.logs) == n_logs + 1
    assert bench.logs[-1]["type"] == "emcy0"
    assert "bad.eds" in bench.logs[-1]["msg"]


def test_eds_remove(bench):
    assert "dut_beta_v7.eds" in {e["file"] for e in bench.db.eds_list()}
    bench.dispatch("eds_remove", {"file": "dut_beta_v7.eds"})
    assert "dut_beta_v7.eds" not in {e["file"] for e in bench.db.eds_list()}


def test_eds_list_shows_only_rows_whose_file_is_there(bench):
    """A plugin seeds the profiles of a whole device family, so most rows point
    at files nobody has dropped in yet. Those are pre-configuration, not
    devices to look at, and listing them invites the question which are real."""
    assert "dut_beta_v7.eds" in {e["file"] for e in bench.db.eds_list()}
    assert "dut_beta_v7.eds" not in {e["file"] for e in bench.snapshot()["eds"]["files"]}

    bench.db.eds_write_file("dut_beta_v7.eds", SEED_EDS)
    shown = bench.snapshot()["eds"]["files"]
    entry = next(e for e in shown if e["file"] == "dut_beta_v7.eds")
    # hidden, not dropped: the row comes back with what the seed configured
    assert entry["dev"] == "DUT_BETA" and entry["code"] == "DTB"


def test_eds_conflict_ignores_rows_whose_file_is_missing(bench):
    """Otherwise a visible file is marked as conflicting with one the list
    does not show, and there is nothing the user can do about it."""
    bench.db.eds_add("dut_alpha_twin.eds", "DUT_ALPHA", "0x4D2·0x1150", "DTT", True)
    shown = bench.snapshot()["eds"]["files"]
    assert "dut_alpha_twin.eds" not in {e["file"] for e in shown}
    alpha = next(e for e in shown if e["file"] == "dut_alpha_v2.eds")
    assert alpha["conflict"] == []

    # with the file in place it is a real conflict and both rows say so
    bench.db.eds_write_file("dut_alpha_twin.eds", SEED_EDS)
    alpha = next(e for e in bench.snapshot()["eds"]["files"] if e["file"] == "dut_alpha_v2.eds")
    assert alpha["conflict"] == ["dut_alpha_twin.eds"]


def test_eds_set_commands_roundtrip(bench):
    commands = [{"key": "su", "label": "SuperUser", "badge": "SU"},
                {"key": "w", "label": "W", "write": {"index": "0x2000", "sub": "00", "on": 1, "off": 0}}]
    bench.db.eds_set_commands("dut_alpha_v2.eds", commands)
    entry = next(e for e in bench.db.eds_list() if e["file"] == "dut_alpha_v2.eds")
    assert entry["device_commands"] == commands
    reloaded = next(e for e in Db(bench.db.path).eds_list() if e["file"] == "dut_alpha_v2.eds")
    assert reloaded["device_commands"] == commands


def test_eds_files_migration_adds_device_commands_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE eds_files(
        file TEXT PRIMARY KEY,
        dev_name TEXT NOT NULL DEFAULT '',
        ident TEXT NOT NULL DEFAULT '',
        code TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1,
        variant_index TEXT NOT NULL DEFAULT '',
        variant_sub TEXT NOT NULL DEFAULT '',
        variant_map TEXT NOT NULL DEFAULT '{}',
        display_slots TEXT NOT NULL DEFAULT '[]')""")
    conn.execute("INSERT INTO eds_files(file, dev_name, ident, code) VALUES (?, ?, ?, ?)",
                 ("legacy.eds", "LEGACY", "0x1·0x2", "LEG"))
    conn.commit()
    conn.close()

    db = Db(path)
    entries = {e["file"]: e for e in db.eds_list()}
    assert entries["legacy.eds"]["device_commands"] == []


def test_first_run_seeds_demo_eds(tmp_path):
    fresh_dir = tmp_path / "fresh"
    assert not fresh_dir.exists()
    b = Bench(Db(fresh_dir / "canopen-bench.db"))
    entries = {e["file"]: e for e in b.db.eds_list()}
    assert "DemoDevice.eds" in entries
    assert entries["DemoDevice.eds"]["enabled"] is True
    on_disk = b.db.eds_dir / "DemoDevice.eds"
    assert on_disk.is_file()
    import_eds(str(on_disk), "DemoDevice.eds")  # parses without error


def test_first_run_seed_is_not_duplicated_on_second_construction(tmp_path):
    fresh_dir = tmp_path / "fresh"
    Bench(Db(fresh_dir / "canopen-bench.db"))
    b2 = Bench(Db(fresh_dir / "canopen-bench.db"))
    matches = [e for e in b2.db.eds_list() if e["file"] == "DemoDevice.eds"]
    assert len(matches) == 1


def test_removed_demo_eds_seed_does_not_come_back(tmp_path):
    fresh_dir = tmp_path / "fresh"
    Bench(Db(fresh_dir / "canopen-bench.db")).db.eds_remove("DemoDevice.eds")
    b2 = Bench(Db(fresh_dir / "canopen-bench.db"))
    assert "DemoDevice.eds" not in {e["file"] for e in b2.db.eds_list()}


def test_ordinary_tmp_path_construction_does_not_seed_demo_eds(bench):
    # tmp_path (and therefore bench.db.path.parent) already exists when the
    # fixture's Db/Bench are constructed, so is_first_run is False here.
    assert bench.db.is_first_run is False
    assert "DemoDevice.eds" not in {e["file"] for e in bench.db.eds_list()}


def test_own_node_id_default_and_set(bench):
    assert bench.own_node_id == 127
    bench.dispatch("set_own_node_id", {"node_id": 126})
    assert bench.own_node_id == 126
    assert Db(bench.db.path).get("own_node_id") == 126


def test_own_node_id_rejects_out_of_range(bench):
    bench.dispatch("set_own_node_id", {"node_id": 200})
    assert bench.own_node_id == 127  # unchanged
    bench.dispatch("set_own_node_id", {"node_id": "not a number"})
    assert bench.own_node_id == 127  # unchanged


def test_set_own_node_id_accepts_hex(bench):
    bench.dispatch("set_own_node_id", {"node_id": "0x7E"})
    assert bench.own_node_id == 126


def test_set_own_node_id_out_of_range_logs_rejection(bench):
    bench.dispatch("set_own_node_id", {"node_id": "999"})
    assert bench.own_node_id == 127  # unchanged
    last = bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "outside 1..127" in last["msg"]


def test_set_own_node_id_garbage_logs_not_a_number(bench):
    bench.dispatch("set_own_node_id", {"node_id": "abc"})
    assert bench.own_node_id == 127  # unchanged
    last = bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "not a number" in last["msg"]


def test_read_variant_no_object_configured(bench):
    entry = next(e for e in bench.db.eds_list() if e["file"] == "dut_alpha_v2.eds")
    assert bench._read_variant(1, entry) == ""


def test_read_variant_maps_sdo_value_to_label(bench):
    connect_and_scan(bench)  # demo DUT node 1 must exist for the SDO read
    bench.db.eds_set_variant("dut_alpha_v2.eds", "0x2050", "00", {"0x00": "HV"})
    entry = next(e for e in bench.db.eds_list() if e["file"] == "dut_alpha_v2.eds")
    assert bench._read_variant(1, entry) == "HV"


def test_read_variant_falls_back_to_raw_value_if_unmapped(bench):
    connect_and_scan(bench)
    bench.db.eds_set_variant("dut_alpha_v2.eds", "0x2050", "00", {"0x01": "LV"})
    entry = next(e for e in bench.db.eds_list() if e["file"] == "dut_alpha_v2.eds")
    assert bench._read_variant(1, entry) == "0x00"  # EDS default 0, no match in map


def _append_trace(bench: Bench, dec: str, cob: str = "0x000") -> dict:
    """Append one trace row the way tick_loop does: classify + tally, no cap
    trimming (callers that need the cap invariant do that bookkeeping too)."""
    row = _trace_row(cob, "")
    row["dec"] = dec
    row["cls"] = trace_class(dec)
    row["node"] = core_mod.trace_node(cob)
    bench.trace.append(row)
    key = (row["cls"], row["node"])
    bench._trace_counts[key] = bench._trace_counts.get(key, 0) + 1
    return row


@pytest.mark.parametrize("dec,expected", [
    ("NMT", "NMT"),
    ("SDO tx node 01", "SDO"),
    ("TxPDO1 node 05", "PDO"),
    ("EMCY node 03", "EMCY"),
    ("Heartbeat node 02", "HB"),
    ("HB · node 01 Operational", "HB"),
    ("0x7E0", ""),
])
def test_trace_class_classification(dec, expected):
    assert trace_class(dec) == expected


def test_trace_filter_keeps_unhidden_row_beyond_the_view_window(bench):
    nmt_row = _append_trace(bench, "NMT")
    for _ in range(59):
        _append_trace(bench, "Heartbeat node 02")
    bench.dispatch("trace_filter", {"hide": ["HB"]})
    snap = bench.snapshot()["trace"]
    assert nmt_row in snap["rows"]

    for _ in range(40):
        _append_trace(bench, "Heartbeat node 02")
    snap = bench.snapshot()["trace"]
    assert nmt_row in snap["rows"]  # still not pushed out by hidden HB rows
    assert snap["total"] == 100
    assert snap["match"] == 1


def test_trace_filter_validates_against_known_classes(bench):
    bench.dispatch("trace_filter", {"hide": ["HB", "bogus"]})
    assert bench.trace_hide == {"HB"}
    assert bench.snapshot()["trace"]["hide"] == ["HB"]
    bench.dispatch("trace_filter", {"hide": []})
    assert bench.trace_hide == set()
    assert bench.snapshot()["trace"]["hide"] == []


def test_trace_snapshot_unfiltered_caps_at_view_size(bench):
    total = core_mod.TRACE_VIEW + 15
    for _ in range(total):
        _append_trace(bench, "Heartbeat node 02")
    snap = bench.snapshot()["trace"]
    assert len(snap["rows"]) == core_mod.TRACE_VIEW
    assert snap["total"] == len(bench.trace) == total


def test_trace_clear_resets_rows_and_counts(bench):
    _append_trace(bench, "NMT")
    _append_trace(bench, "Heartbeat node 02")
    bench.dispatch("trace_clear", {})
    assert bench.trace == []
    assert bench._trace_counts == {}
    snap = bench.snapshot()["trace"]
    assert snap["total"] == 0
    assert snap["match"] == 0


def test_trace_cap_trim_keeps_counts_consistent_with_rows(bench):
    # exercise the same trim bookkeeping tick_loop applies against TRACE_CAP,
    # against a small local cap so the test doesn't need 200k rows
    local_cap = 100
    for i in range(120):
        _append_trace(bench, "Heartbeat node 02" if i % 2 else "NMT")
    cut = len(bench.trace) - local_cap
    if cut > 0:
        for old in bench.trace[:cut]:
            bench._trace_counts[(old["cls"], old["node"])] -= 1
        del bench.trace[:cut]

    assert len(bench.trace) == local_cap
    assert sum(bench._trace_counts.values()) == local_cap

    snap = bench.snapshot()["trace"]
    assert snap["total"] == local_cap
    assert snap["match"] == local_cap  # no filter active

    bench.dispatch("trace_filter", {"hide": ["HB"]})
    snap = bench.snapshot()["trace"]
    assert snap["match"] == bench._trace_counts.get(("NMT", None), 0)


def test_trace_save_then_clear_then_load_round_trip(bench):
    _append_trace(bench, "NMT")
    for _ in range(3):
        _append_trace(bench, "Heartbeat node 02", cob="0x702")
    bench.dispatch("trace_save", {})
    saved = bench.snapshot()["trace"]["saved"]
    assert len(saved) == 1
    file_name = saved[0]["file"]
    assert file_name.endswith("_4f.json")

    bench.dispatch("trace_clear", {})
    assert bench.snapshot()["trace"]["total"] == 0

    bench.dispatch("trace_load", {"file": file_name})
    snap = bench.snapshot()["trace"]
    assert snap["total"] == 4
    assert snap["paused"] is True
    assert snap["loaded"] == file_name
    assert bench._trace_view()[1] == {("NMT", None): 1, ("HB", 2): 3}


def test_trace_load_backfills_missing_cls(bench):
    bench.trace_dir.mkdir(parents=True, exist_ok=True)
    row = _trace_row("0x000", "")
    row["dec"] = "NMT"
    row.pop("cls", None)
    (bench.trace_dir / "manual_capture.json").write_text(
        json.dumps({"v": 1, "rows": [row]}), encoding="utf-8")

    bench.dispatch("trace_load", {"file": "manual_capture.json"})

    assert bench._trace_view()[0][0]["cls"] == "NMT"
    assert bench._trace_view()[1] == {("NMT", None): 1}


def test_trace_load_path_traversal_and_missing_file_leave_buffer_untouched(bench):
    _append_trace(bench, "NMT")
    before = list(bench.trace)

    bench.dispatch("trace_load", {"file": "../outside.json"})
    assert bench.trace == before
    assert bench.trace_loaded is None
    assert any(ln["type"] == "emcy0" and "load failed" in ln["msg"] for ln in bench.logs)

    bench.dispatch("trace_load", {"file": "does_not_exist.json"})
    assert bench.trace == before
    assert bench.trace_loaded is None
    assert any(ln["type"] == "emcy0" and "load failed" in ln["msg"] for ln in bench.logs)


def test_trace_load_corrupt_json_leaves_buffer_untouched(bench):
    _append_trace(bench, "NMT")
    before = list(bench.trace)
    bench.trace_dir.mkdir(parents=True, exist_ok=True)
    (bench.trace_dir / "corrupt.json").write_text("not json", encoding="utf-8")

    bench.dispatch("trace_load", {"file": "corrupt.json"})

    assert bench.trace == before
    assert bench.trace_loaded is None
    assert any(ln["type"] == "emcy0" and "load failed" in ln["msg"] for ln in bench.logs)


def test_trace_toggle_resume_clears_loaded_marker(bench):
    _append_trace(bench, "NMT")
    bench.dispatch("trace_save", {})
    file_name = bench.snapshot()["trace"]["saved"][0]["file"]
    bench.dispatch("trace_clear", {})
    bench.dispatch("trace_load", {"file": file_name})
    assert bench.trace_paused is True
    assert bench.trace_loaded == file_name

    bench.dispatch("trace_toggle", {})

    assert bench.trace_paused is False
    assert bench.trace_loaded is None


def test_trace_clear_clears_loaded_marker(bench):
    _append_trace(bench, "NMT")
    bench.dispatch("trace_save", {})
    file_name = bench.snapshot()["trace"]["saved"][0]["file"]
    bench.dispatch("trace_clear", {})
    bench.dispatch("trace_load", {"file": file_name})
    assert bench.trace_loaded == file_name

    bench.dispatch("trace_clear", {})

    assert bench.trace_loaded is None


def test_trace_del_saved_removes_file_and_listing_and_clears_loaded_if_active(bench):
    _append_trace(bench, "NMT")
    bench.dispatch("trace_save", {})
    file_name = bench.snapshot()["trace"]["saved"][0]["file"]
    bench.dispatch("trace_clear", {})
    bench.dispatch("trace_load", {"file": file_name})
    assert bench.trace_loaded == file_name

    bench.dispatch("trace_del_saved", {"file": file_name})

    assert not (bench.trace_dir / file_name).exists()
    assert bench.snapshot()["trace"]["saved"] == []
    assert bench.trace_loaded is None


def test_trace_save_on_empty_trace_is_a_no_op(bench):
    assert bench.trace == []
    bench.dispatch("trace_save", {})
    assert not bench.trace_dir.exists() or list(bench.trace_dir.glob("*.json")) == []
    assert bench.snapshot()["trace"]["saved"] == []


@pytest.mark.parametrize("cob,expected", [
    ("0x000", None),   # NMT: node bits zero
    ("0x080", None),   # SYNC: node bits zero
    ("0x581", 1),
    ("0x602", 2),
    ("0x701", 1),
    ("0x185", 5),
    ("", None),        # garbage
])
def test_trace_node_mapping(cob, expected):
    assert core_mod.trace_node(cob) == expected


def _trace_dev_filter_bench(bench: Bench) -> Bench:
    """Two devices: node 1 selected, node 2 not — plus a mix of broadcast
    and per-node trace rows to exercise the device filter."""
    bench.devices = [_bare_device(node=1) | {"sel": True},
                      _bare_device(node=2) | {"sel": False}]
    _append_trace(bench, "NMT", cob="0x000")           # broadcast
    _append_trace(bench, "Heartbeat node 01", cob="0x701")  # node 1
    _append_trace(bench, "Heartbeat node 02", cob="0x702")  # node 2
    _append_trace(bench, "SDO tx node 02", cob="0x582")     # node 2
    return bench


def test_trace_devfilter_keeps_selected_devices_and_broadcasts(bench):
    _trace_dev_filter_bench(bench)
    bench.dispatch("trace_devfilter", {})
    snap = bench.snapshot()["trace"]
    decs = {row["dec"] for row in snap["rows"]}
    assert decs == {"NMT", "Heartbeat node 01"}
    assert snap["match"] == 2
    assert snap["total"] == 4
    assert snap["devSel"] is True

    bench.dispatch("trace_devfilter", {})
    snap = bench.snapshot()["trace"]
    assert len(snap["rows"]) == 4
    assert snap["match"] == 4
    assert snap["devSel"] is False


def test_trace_devfilter_combines_with_class_filter(bench):
    _trace_dev_filter_bench(bench)
    bench.dispatch("trace_devfilter", {})
    bench.dispatch("trace_filter", {"hide": ["HB"]})
    snap = bench.snapshot()["trace"]
    decs = {row["dec"] for row in snap["rows"]}
    assert decs == {"NMT"}
    assert snap["match"] == 1


def test_trace_devfilter_with_no_device_selected_hides_all_node_rows(bench):
    _trace_dev_filter_bench(bench)
    for d in bench.devices:
        d["sel"] = False
    bench.dispatch("trace_devfilter", {})
    snap = bench.snapshot()["trace"]
    decs = {row["dec"] for row in snap["rows"]}
    assert decs == {"NMT"}
    assert snap["match"] == 1


def test_trace_devfilter_applies_to_loaded_captures(bench):
    _trace_dev_filter_bench(bench)
    bench.dispatch("trace_save", {})
    file_name = bench.snapshot()["trace"]["saved"][0]["file"]
    bench.dispatch("trace_clear", {})
    bench.dispatch("trace_load", {"file": file_name})

    bench.devices = [_bare_device(node=1) | {"sel": True},
                      _bare_device(node=2) | {"sel": False}]
    bench.dispatch("trace_devfilter", {})
    snap = bench.snapshot()["trace"]
    decs = {row["dec"] for row in snap["rows"]}
    assert decs == {"NMT", "Heartbeat node 01"}
    assert snap["match"] == 2


def test_trace_load_backfills_node_from_cob_on_legacy_captures(bench):
    """Captures saved before the device filter existed carry no "node" key —
    it must be derived from "cob" on load, same as the "cls" backfill."""
    bench.trace_dir.mkdir(parents=True, exist_ok=True)
    row = _trace_row("0x701", "05")
    row["dec"] = "Heartbeat node 01"
    row["cls"] = "HB"
    row.pop("node", None)
    (bench.trace_dir / "legacy_capture.json").write_text(
        json.dumps({"v": 1, "rows": [row]}), encoding="utf-8")

    bench.dispatch("trace_load", {"file": "legacy_capture.json"})

    assert bench._trace_view()[0][0]["node"] == 1
    assert bench._trace_view()[1] == {("HB", 1): 1}

    bench.devices = [_bare_device(node=1) | {"sel": True}]
    bench.dispatch("trace_devfilter", {})
    snap = bench.snapshot()["trace"]
    assert len(snap["rows"]) == 1
    assert snap["match"] == 1


@pytest.mark.parametrize("t,sec", [
    ("00:00:00.000000", 0.0),
    ("12:34:56.789012", 12 * 3600 + 34 * 60 + 56.789012),
    ("23:59:59.999999", 23 * 3600 + 59 * 60 + 59.999999),
])
def test_trace_time_to_seconds_round_trip(t, sec):
    assert core_mod._trace_time_to_seconds(t) == pytest.approx(sec)
    assert core_mod._seconds_to_trace_time(sec) == t


@pytest.mark.parametrize("bad", ["", "garbage", "12:34", "12:aa:00.000000", None])
def test_trace_time_to_seconds_malformed_returns_none(bad):
    assert core_mod._trace_time_to_seconds(bad) is None


def test_seconds_to_trace_time_wraps_at_midnight():
    assert core_mod._seconds_to_trace_time(86400.0) == "00:00:00.000000"
    assert core_mod._seconds_to_trace_time(86400.5) == "00:00:00.500000"
    assert core_mod._seconds_to_trace_time(90000.0) == "01:00:00.000000"  # 86400 + 3600


def test_parse_candump_well_formed_multiline_log():
    text = (
        "(1690000000.000000) can0 181#0102030405060708\n"
        "(1690000000.500000) can0 701#05\n"
    )
    frames, skipped = core_mod.parse_candump(text)
    assert skipped == 0
    assert len(frames) == 2
    rel0, cob0, data0 = frames[0]
    assert rel0 == 0.0
    assert cob0 == 0x181
    assert data0 == bytes.fromhex("0102030405060708")
    rel1, cob1, data1 = frames[1]
    assert rel1 == pytest.approx(0.5)
    assert cob1 == 0x701
    assert data1 == bytes.fromhex("05")


def test_parse_candump_skips_remote_fd_blank_and_garbage_lines():
    text = (
        "(1.0) can0 181#0102030405060708\n"
        "\n"
        "(2.0) can0 181#R\n"
        "(3.0) can0 181##0102030405060708\n"
        "garbage line\n"
    )
    frames, skipped = core_mod.parse_candump(text)
    assert len(frames) == 1
    assert skipped == 3  # remote frame, CAN-FD frame, garbage line — blank line not counted


def test_parse_candump_zero_recognized_lines_returns_empty_frame_list():
    text = "not a candump line\nanother garbage line\n"
    frames, skipped = core_mod.parse_candump(text)
    assert frames == []
    assert skipped == 2


def test_export_trace_rows_uncapped_and_respects_hide_filter(bench):
    total_nmt = core_mod.TRACE_VIEW + 50
    for _ in range(total_nmt):
        _append_trace(bench, "NMT")
    for _ in range(5):
        _append_trace(bench, "Heartbeat node 02")
    bench.dispatch("trace_filter", {"hide": ["HB"]})

    exported = bench._export_trace_rows()
    assert len(exported) == total_nmt
    assert all(row["cls"] == "NMT" for row in exported)
    assert len(exported) > core_mod.TRACE_VIEW  # export isn't capped like a snapshot

    snap_rows = bench.snapshot()["trace"]["rows"]
    assert len(snap_rows) <= core_mod.TRACE_VIEW


def test_trace_filter_predicate_combines_hide_and_device_filter(bench):
    _trace_dev_filter_bench(bench)
    bench.dispatch("trace_devfilter", {})
    bench.dispatch("trace_filter", {"hide": ["HB"]})
    passes = bench._trace_filter_predicate()
    assert passes("NMT", None) is True    # broadcast, never hidden by device filter
    assert passes("HB", 1) is False       # hidden class
    assert passes("SDO", 2) is False      # node not selected
    assert passes("SDO", 1) is True       # node selected


def test_trace_csv_header_and_rows_exact(bench):
    bench.trace = [
        {"time": "12:00:00.123456", "dir": "RX", "cob": "0x581", "len": "3",
         "data": "01 02 03", "dec": "SDO tx node 01", "flag": "", "cls": "SDO",
         "node": 1, "obj": "0x1000:00 test", "val": "42"},
        {"time": "12:00:01.000000", "dir": "TX", "cob": "0x000", "len": "0",
         "data": "", "dec": "NMT", "flag": "", "cls": "NMT",
         "node": None, "obj": "", "val": ""},
    ]
    csv_text = bench._trace_csv()
    assert csv_text == (
        "time,dir,cob,len,data,dec,flag,node,obj,val\r\n"
        "12:00:00.123456,RX,0x581,3,01 02 03,SDO tx node 01,,1,0x1000:00 test,42\r\n"
        "12:00:01.000000,TX,0x000,0,,NMT,,,,\r\n"
    )


def test_trace_candump_export_relative_timestamps_and_cob_format(bench):
    bench.trace = [
        {"time": "12:00:00.000000", "dir": "RX", "cob": "0x581", "len": "2",
         "data": "01 02", "dec": "SDO tx node 01", "flag": "", "cls": "SDO",
         "node": 1, "obj": "", "val": ""},
        {"time": "12:00:00.500000", "dir": "RX", "cob": "0x000", "len": "0",
         "data": "", "dec": "NMT", "flag": "", "cls": "NMT",
         "node": None, "obj": "", "val": ""},
    ]
    text = bench._trace_candump()
    assert text == (
        "(0.000000) can0 581#0102\n"
        "(0.500000) can0 000#\n"
    )


def _candump_text(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def test_trace_import_candump_happy_path(bench):
    text = _candump_text(
        "(0.000000) can0 181#0102030405060708",
        "(0.100000) can0 701#05",
    )
    bench.dispatch("trace_import", {"filename": "capture.log", "fmt": "candump",
                                     "data": _b64(text)})

    assert len(bench._trace_view()[0]) == 2
    assert bench.trace_paused is True
    assert bench.trace_loaded is not None
    assert bench.trace_loaded.startswith("import_") and bench.trace_loaded.endswith("_2f.json")
    assert (bench.trace_dir / bench.trace_loaded).exists()
    saved_names = {f["file"] for f in bench._trace_saved}
    assert bench.trace_loaded in saved_names


def test_trace_import_suppresses_live_side_effects_for_emcy_and_plot(pdo_bench):
    """Importing historical frames must not spam the state log with backdated
    EMCY "alarms" nor inject backdated samples into the live signal plot —
    the whole point of running the annotators with live=False."""
    bench = pdo_bench  # node 3 carries DemoDevice.eds (0x1000:00 mapped)
    bench.dispatch("dev_toggle", {"node": 3})
    bench.dispatch("plot_toggle", {"idx": "0x1000", "sub": "00"})
    logs_before = len(bench.logs)

    text = _candump_text(
        "(0.000000) can0 583#4300100099000000",  # node 3 SDO tx, 0x1000:00 = 153
        "(0.100000) can0 083#100001000000",       # node 3 EMCY: code 0x0010, reg 0x01
    )
    bench.dispatch("trace_import", {"filename": "capture.log", "fmt": "candump",
                                     "data": _b64(text)})

    assert len(bench._trace_view()[0]) == 2
    new_logs = bench.logs[logs_before:]
    assert len(new_logs) == 1
    assert "imported" in new_logs[0]["msg"]
    assert not any(ln["type"] == "emcy" for ln in new_logs)
    assert "0x1000:00" not in bench.plot_series


def test_trace_import_bad_base64_leaves_state_untouched(bench):
    before = list(bench.trace)
    bench.dispatch("trace_import", {"filename": "capture.log", "fmt": "candump",
                                     "data": "not-valid-base64!!"})
    assert bench.trace == before
    assert bench.trace_loaded is None
    assert any(ln["type"] == "emcy0" and "import failed" in ln["msg"] for ln in bench.logs)


def test_trace_import_zero_recognized_frames_leaves_state_untouched(bench):
    before = list(bench.trace)
    text = _candump_text("garbage line", "another garbage line")
    bench.dispatch("trace_import", {"filename": "capture.log", "fmt": "candump",
                                     "data": _b64(text)})
    assert bench.trace == before
    assert bench.trace_loaded is None
    assert any(ln["type"] == "emcy0" and "no recognized candump frames" in ln["msg"]
               for ln in bench.logs)


def test_trace_import_unsupported_format_leaves_state_untouched(bench):
    before = list(bench.trace)
    text = _candump_text("(0.000000) can0 181#0102030405060708")
    bench.dispatch("trace_import", {"filename": "capture.trc", "fmt": "trc",
                                     "data": _b64(text)})
    assert bench.trace == before
    assert bench.trace_loaded is None
    assert any(ln["type"] == "emcy0" and "unsupported format" in ln["msg"] for ln in bench.logs)


def test_trace_import_reports_skipped_line_count_on_partial_success(bench):
    text = _candump_text(
        "(0.000000) can0 181#0102030405060708",
        "garbage line",
        "(0.200000) can0 701#05",
    )
    bench.dispatch("trace_import", {"filename": "capture.log", "fmt": "candump",
                                     "data": _b64(text)})

    assert len(bench._trace_view()[0]) == 2
    assert any("1 unrecognized line(s) skipped" in ln["msg"] for ln in bench.logs)


def test_scan_assigns_variant_to_matched_device(bench):
    bench.db.eds_set_variant("dut_alpha_v2.eds", "0x2050", "00", {"0x00": "HV"})
    connect_and_scan(bench)
    dev = next(d for d in bench.devices if d["node"] == 1)
    assert dev["eds"] == "dut_alpha_v2.eds"
    assert dev["variant"] == "HV"


def test_startup_fresh_workspace_mc_stays_off_even_with_expected_state(bench):
    bench.mc_ref = {"expected": 2, "session": "0xAB", "assignments": {}}
    bench._adopt_ref()
    bench.startup()
    assert bench.mc["enabled"] is False


def test_startup_restores_mc_on_when_remembered_on_with_expected_state(bench):
    bench.db.set("mc_ref", {"expected": 2, "session": "0xAB", "assignments": {}})
    bench.mc_ref = bench.db.get("mc_ref")
    bench._adopt_ref()
    bench.dispatch("mc_toggle", {})  # turn MC on, persisted
    assert bench.mc["enabled"] is True

    again = Bench(Db(bench.db.path))
    again.startup()
    assert again.mc["enabled"] is True
    assert any("machine control restored" in ln["msg"] for ln in again.logs)


def test_startup_stays_off_when_remembered_off(bench):
    bench.db.set("mc_ref", {"expected": 2, "session": "0xAB", "assignments": {}})
    bench.mc_ref = bench.db.get("mc_ref")
    bench._adopt_ref()
    bench.dispatch("mc_toggle", {})  # on
    bench.dispatch("mc_toggle", {})  # off again, persisted
    assert bench.mc["enabled"] is False

    again = Bench(Db(bench.db.path))
    again.startup()
    assert again.mc["enabled"] is False
    assert not any("machine control restored" in ln["msg"] for ln in again.logs)


def test_startup_forces_off_when_autostart_disabled(bench):
    bench.db.set("mc_ref", {"expected": 2, "session": "0xAB", "assignments": {}})
    bench.mc_ref = bench.db.get("mc_ref")
    bench._adopt_ref()
    bench.dispatch("mc_toggle", {})  # remembered on
    bench.dispatch("mc_opt", {"key": "autoStart"})  # autoStart off, persisted

    again = Bench(Db(bench.db.path))
    again.startup()
    assert again.mc["enabled"] is False


def test_startup_forces_off_when_no_expected_state(bench):
    bench.dispatch("mc_toggle", {})  # remembered on, but no expected state adopted
    assert bench.mc_ref is None

    again = Bench(Db(bench.db.path))
    again.startup()
    assert again.mc["enabled"] is False
    assert any("no expected state adopted" in ln["msg"] for ln in again.logs)


# -- heartbeat-loss monitoring (Machine Control) -----------------------------

def _mc_ready(bench: Bench, assignments: dict[int, str]) -> None:
    """Wire up a bench so `_check_heartbeats` actually monitors — MC
    enabled, an expected state with the given node->EDS assignments, and
    a live connection — without needing a real scan/adopt round-trip."""
    bench.mc["enabled"] = True
    bench.mc_ref = {"expected": len(assignments), "session": "",
                     "assignments": {str(n): f for n, f in assignments.items()}}
    bench.connected = True


def test_check_heartbeats_noop_when_mc_disabled(bench):
    _mc_ready(bench, {1: "a.eds"})
    bench.mc["enabled"] = False
    bench._check_heartbeats()
    assert bench._hb_lost == set()
    assert bench.logs == []


def test_check_heartbeats_noop_without_expected_state(bench):
    _mc_ready(bench, {1: "a.eds"})
    bench.mc_ref = None
    bench._check_heartbeats()
    assert bench._hb_lost == set()
    assert bench.logs == []


def test_check_heartbeats_noop_when_not_connected_and_clears_stale_alerts(bench):
    _mc_ready(bench, {1: "a.eds"})
    bench.connected = False
    bench._hb_lost = {5}  # a fake alarm left over from a different configuration
    bench._check_heartbeats()
    assert bench._hb_lost == set()
    assert bench.logs == []


def test_check_heartbeats_grace_window_suppresses_first_report(bench):
    _mc_ready(bench, {1: "a.eds"})
    bench._reset_hb_monitor()  # anchors the grace window to "now"
    # node 1 never sent a heartbeat — still within the grace window though
    bench._check_heartbeats()
    assert bench._hb_lost == set()
    assert bench.logs == []


def test_check_heartbeats_detects_loss_after_grace_window(bench):
    _mc_ready(bench, {1: "a.eds"})
    bench._hb_monitor_since = time.monotonic() - 10  # long past any grace window
    bench._check_heartbeats()
    assert bench._hb_lost == {1}
    assert any("heartbeat lost" in ln["msg"] and ln["type"] == "emcy0" for ln in bench.logs)


def test_check_heartbeats_only_flags_the_stale_node(bench):
    _mc_ready(bench, {1: "a.eds", 2: "b.eds"})
    bench.mc["hbTimeoutMs"] = 1000
    bench._hb_monitor_since = time.monotonic() - 10
    now = time.monotonic()
    bench._hb_seen[1] = now - 0.1   # fresh
    bench._hb_seen[2] = now - 100   # long silent
    bench._check_heartbeats()
    assert bench._hb_lost == {2}
    assert any("node 02" in ln["msg"] and "heartbeat lost" in ln["msg"] for ln in bench.logs)
    assert not any("node 01" in ln["msg"] for ln in bench.logs)


def test_check_heartbeats_logs_resume_as_info_not_emcy(bench):
    _mc_ready(bench, {1: "a.eds"})
    bench._hb_monitor_since = time.monotonic() - 10
    bench._hb_lost = {1}  # currently marked as lost
    bench._hb_seen[1] = time.monotonic()  # now sending again
    bench._check_heartbeats()
    assert bench._hb_lost == set()
    resumed = [ln for ln in bench.logs if "heartbeat resumed" in ln["msg"]]
    assert len(resumed) == 1
    assert resumed[0]["type"] == "info"
    assert not any(ln["type"] == "emcy0" for ln in bench.logs)


def test_check_heartbeats_does_not_spam_while_still_lost(bench):
    _mc_ready(bench, {1: "a.eds"})
    bench._hb_monitor_since = time.monotonic() - 10
    bench._check_heartbeats()
    bench._check_heartbeats()  # node 1 is still silent — must not log again
    lost_logs = [ln for ln in bench.logs if "heartbeat lost" in ln["msg"]]
    assert len(lost_logs) == 1
    assert bench._hb_lost == {1}


def test_mc_set_hb_timeout_accepts_valid_value(bench):
    bench.dispatch("mc_set_hb_timeout", {"ms": "5000"})
    assert bench.mc["hbTimeoutMs"] == 5000
    assert bench.db.get("mc_opts")["hbTimeoutMs"] == 5000


def test_mc_set_hb_timeout_clamps_to_minimum(bench):
    bench.dispatch("mc_set_hb_timeout", {"ms": "100"})
    assert bench.mc["hbTimeoutMs"] == 500


def test_mc_set_hb_timeout_clamps_to_maximum(bench):
    bench.dispatch("mc_set_hb_timeout", {"ms": "9999999"})
    assert bench.mc["hbTimeoutMs"] == 600_000


def test_mc_set_hb_timeout_ignores_invalid_value(bench):
    before = bench.mc["hbTimeoutMs"]
    bench.dispatch("mc_set_hb_timeout", {"ms": "abc"})
    assert bench.mc["hbTimeoutMs"] == before


def test_mc_adopt_resets_hb_monitor(connected_bench):
    bench = connected_bench
    bench._hb_lost = {99}  # stale alarm from a previous configuration
    bench.dispatch("mc_adopt", {})
    assert bench._hb_lost == set()


def test_mc_toggle_on_resets_hb_monitor(connected_bench):
    bench = connected_bench
    assert bench.mc["enabled"] is False
    bench._hb_lost = {99}
    bench.dispatch("mc_toggle", {})  # turns MC on
    assert bench.mc["enabled"] is True
    assert bench._hb_lost == set()


def test_snapshot_reports_hb_lost_sorted(bench):
    bench._hb_lost = {3, 1}
    assert bench.snapshot()["mc"]["hbLost"] == [1, 3]


def test_tick_loop_populates_hb_seen_from_demo_heartbeats(connected_bench):
    bench = connected_bench
    bench.dispatch("mc_adopt", {})
    bench.dispatch("mc_toggle", {})  # MC on, small timeout doesn't matter here
    nodes = {d["node"] for d in bench.devices}
    assert nodes  # scan found something to monitor

    orig_tick = core_mod.TICK_S
    core_mod.TICK_S = 0.05
    try:
        async def go():
            task = asyncio.create_task(bench._tick_loop_body())
            await asyncio.sleep(1.2)
            task.cancel()
        asyncio.run(go())
    finally:
        core_mod.TICK_S = orig_tick

    assert set(bench._hb_seen) >= nodes


# -- scan range (configurable, persisted) -----------------------------------

def test_set_scan_range_updates_state_snapshot_and_persists(bench):
    bench.dispatch("set_scan_range", {"from": 2, "to": 10})
    assert bench.scan_range == (2, 10)
    assert bench.snapshot()["scanRange"] == [2, 10]

    again = Bench(Db(bench.db.path))
    assert again.scan_range == (2, 10)
    assert again.snapshot()["scanRange"] == [2, 10]


def test_set_scan_range_clamps_out_of_bounds_values(bench):
    bench.dispatch("set_scan_range", {"from": 0, "to": 300})
    assert bench.scan_range == (1, 127)


def test_set_scan_range_swaps_when_from_greater_than_to(bench):
    bench.dispatch("set_scan_range", {"from": 20, "to": 5})
    assert bench.scan_range == (5, 20)


def test_set_scan_range_ignores_garbage_input(bench):
    before = bench.scan_range
    bench.dispatch("set_scan_range", {"from": "x"})
    assert bench.scan_range == before


def test_set_scan_range_accepts_hex_bounds(bench):
    bench.dispatch("set_scan_range", {"from": "0x02", "to": "0x10"})
    assert bench.scan_range == (2, 16)


def test_scan_honors_configured_range_in_demo_mode(bench):
    connect_and_scan(bench)  # default range: node 1 is found
    assert 1 in {d["node"] for d in bench.devices}

    bench.dispatch("connect_toggle", {})  # disconnect to reconnect cleanly
    bench.dispatch("set_scan_range", {"from": 2, "to": 10})
    connect_and_scan(bench)
    nodes = {d["node"] for d in bench.devices}
    assert nodes  # the range still covers the demo DUTs (nodes 2, 3)
    assert 1 not in nodes
    assert all(n >= 2 for n in nodes)


# -- directory picker (Browse… buttons) --------------------------------------

def test_browse_open_falls_back_when_configured_path_does_not_exist(bench):
    bench.dispatch("set_path", {"which": "tc", "value": "C:\\Bench\\TestCases"})  # not a dir on Linux
    bench.dispatch("browse_open", {"which": "tc"})
    assert bench.browse is not None
    assert bench.browse["which"] == "tc"
    from pathlib import Path
    assert Path(bench.browse["path"]).is_dir()
    assert bench.snapshot()["browse"] == bench.browse


def test_browse_open_lists_subdirs_and_excludes_files(bench, tmp_path):
    root = tmp_path / "tc_root"
    (root / "A").mkdir(parents=True)
    (root / "B").mkdir()
    (root / "afile.txt").write_text("x")
    bench.dispatch("set_path", {"which": "tc", "value": str(root)})

    bench.dispatch("browse_open", {"which": "tc"})
    assert bench.browse["dirs"] == ["A", "B"]
    assert bench.browse["hasParent"] is True
    assert bench.browse["error"] == ""


def test_browse_nav_descends_and_goes_up(bench, tmp_path):
    root = tmp_path / "tc_root"
    (root / "A").mkdir(parents=True)
    bench.dispatch("set_path", {"which": "tc", "value": str(root)})
    bench.dispatch("browse_open", {"which": "tc"})

    bench.dispatch("browse_nav", {"dir": "A"})
    assert bench.browse["path"].replace("\\", "/").endswith("/A")

    bench.dispatch("browse_nav", {"dir": ".."})
    assert bench.browse["path"].rstrip("/\\").replace("\\", "/") == str(root).replace("\\", "/")


def test_browse_nav_into_nonexistent_dir_sets_error(bench, tmp_path):
    root = tmp_path / "tc_root"
    root.mkdir()
    bench.dispatch("set_path", {"which": "tc", "value": str(root)})
    bench.dispatch("browse_open", {"which": "tc"})

    bench.dispatch("browse_nav", {"dir": "nonexistent"})
    assert bench.browse["error"] != ""
    assert bench.browse["dirs"] == []


def test_browse_nav_rejects_path_traversal(bench, tmp_path):
    root = tmp_path / "tc_root"
    (root / "etc").mkdir(parents=True)
    bench.dispatch("set_path", {"which": "tc", "value": str(root)})
    bench.dispatch("browse_open", {"which": "tc"})

    bench.dispatch("browse_nav", {"dir": "../../etc"})
    # basename reduction: it tries the "etc" subdir of the current path,
    # never escapes upward via ".."
    assert bench.browse["path"].replace("\\", "/").endswith("/etc")
    assert bench.browse["error"] == ""


def test_browse_select_applies_path_and_persists(bench, tmp_path):
    root = tmp_path / "tc_root"
    (root / "A").mkdir(parents=True)
    bench.dispatch("set_path", {"which": "tc", "value": str(root)})
    bench.dispatch("browse_open", {"which": "tc"})
    bench.dispatch("browse_nav", {"dir": "A"})

    bench.dispatch("browse_select", {})
    assert bench.browse is None
    assert bench.paths["tc"].replace("\\", "/").endswith("/A")

    again = Bench(Db(bench.db.path))
    assert again.paths["tc"] == bench.paths["tc"]


def test_browse_nav_select_close_are_noop_when_browse_closed(bench):
    assert bench.browse is None
    bench.dispatch("browse_nav", {"dir": "whatever"})
    assert bench.browse is None
    bench.dispatch("browse_select", {})
    assert bench.browse is None
    bench.dispatch("browse_close", {})
    assert bench.browse is None


def test_browse_open_invalid_which_leaves_browse_closed(bench):
    bench.dispatch("browse_open", {"which": "xx"})
    assert bench.browse is None


# -- EDS auto-rematch + Objects-page hint -------------------------------------

def test_scanned_devices_carry_ident(connected_bench):
    assert connected_bench.devices  # sanity: the demo bus produced DUTs
    assert all(d["ident"] for d in connected_bench.devices)


def _bare_device(node: int = 1, ident: str = "0x4D2·0x1150") -> dict:
    """Field shape from Bench._apply_scan, with no EDS assigned yet."""
    return {"node": node, "name": "", "nmt": "Pre-Operational", "sel": True,
            "cmds": {}, "fw": "", "sn": "", "variant": "", "ident": ident,
            "eds": "—"}


def test_rematch_on_upload_assigns_matching_device(bench):
    bench.devices = [_bare_device()]
    bench.dispatch("eds_upload", {"filename": "new_match.eds", "content": SEED_EDS})
    dev = bench.devices[0]
    assert dev["eds"] == "new_match.eds"
    snap = bench.snapshot()
    assert snap["objects"]["hint"] == ""
    assert snap["objects"]["groups"]
    assert any("⇒" in ln["msg"] for ln in bench.logs)


def test_rematch_on_enable_assigns_matching_device(bench):
    bench.db.eds_remove("dut_alpha_v2.eds")  # avoid a same-identity collision with the seed row
    bench.db.eds_add("disabled_match.eds", "SEED_DEV", "0x4D2·0x1150", "SEE", False)
    bench.db.eds_write_file("disabled_match.eds", SEED_EDS)
    bench.devices = [_bare_device()]

    bench.dispatch("eds_toggle", {"file": "disabled_match.eds"})

    assert "disabled_match.eds" in bench.eds_enabled
    dev = bench.devices[0]
    assert dev["eds"] == "disabled_match.eds"
    assert any("⇒" in ln["msg"] for ln in bench.logs)


def test_toggle_to_disabled_does_not_rematch(bench):
    bench.db.eds_add("enabled_match.eds", "SEED_DEV", "0x4D2·0x1150", "SEE", True)
    bench.db.eds_write_file("enabled_match.eds", SEED_EDS)
    bench.devices = [_bare_device()]

    bench.dispatch("eds_toggle", {"file": "enabled_match.eds"})  # disable it

    assert "enabled_match.eds" not in bench.eds_enabled
    dev = bench.devices[0]
    assert dev["eds"] == "—"
    assert not any("⇒" in ln["msg"] for ln in bench.logs)


def test_rematch_ignores_devices_with_different_identity(bench):
    bench.devices = [_bare_device(ident="0x1·0x2")]
    bench.dispatch("eds_upload", {"filename": "no_match.eds", "content": SEED_EDS})
    dev = bench.devices[0]
    assert dev["eds"] == "—"
    assert "no EDS file assigned" in bench.snapshot()["objects"]["hint"]


def test_objects_hint_no_device_selected(bench):
    hint = bench.snapshot()["objects"]["hint"]
    assert "select a device in the Devices box" in hint


def test_objects_hint_device_without_eds(bench):
    bench.devices = [_bare_device(node=5)]
    hint = bench.snapshot()["objects"]["hint"]
    assert "node 05" in hint
    assert "no EDS file assigned" in hint


def test_objects_hint_eds_missing_from_disk(bench):
    dev = _bare_device()
    dev["eds"] = "nonexistent.eds"
    bench.devices = [dev]
    hint = bench.snapshot()["objects"]["hint"]
    assert "missing from the workspace" in hint


def test_objects_hint_unparseable_eds(bench):
    (bench.db.eds_dir / "garbage.eds").write_text("not an eds file {{{", encoding="utf-8")
    dev = _bare_device()
    dev["eds"] = "garbage.eds"
    bench.devices = [dev]
    hint = bench.snapshot()["objects"]["hint"]
    assert hint.startswith('EDS "garbage.eds" could not be parsed')


def test_objects_hint_valid_eds(bench):
    dev = _bare_device()
    dev["eds"] = "dut_alpha_v2.eds"
    bench.devices = [dev]
    snap = bench.snapshot()
    assert snap["objects"]["hint"] == ""
    assert snap["objects"]["groups"]


def test_parse_failure_hint_is_cached_and_recovers_after_fix(bench):
    import os

    garbage_path = bench.db.eds_dir / "garbage.eds"
    garbage_path.write_text("not an eds file {{{", encoding="utf-8")
    dev = _bare_device()
    dev["eds"] = "garbage.eds"
    bench.devices = [dev]

    hint1 = bench.snapshot()["objects"]["hint"]
    hint2 = bench.snapshot()["objects"]["hint"]
    assert hint1 == hint2
    assert hint1.startswith('EDS "garbage.eds" could not be parsed')

    garbage_path.write_text(SEED_EDS, encoding="utf-8")
    st = os.stat(garbage_path)
    os.utime(garbage_path, (st.st_atime + 5, st.st_mtime + 5))  # force a distinct mtime

    snap = bench.snapshot()
    assert snap["objects"]["hint"] == ""
    assert snap["objects"]["groups"]


# -- EDS identity conflicts (_eds_by_identity / _eds_conflicts) ---------------

def _make_conflicting_pair(bench: Bench) -> None:
    """Two enabled, same-identity (0x4D2·0x1150) files: conflict_a.eds and
    conflict_b.eds, both parseable — mtimes not yet ordered."""
    bench.db.eds_remove("dut_alpha_v2.eds")  # same identity — avoid interference
    bench.db.eds_add("conflict_a.eds", "SEED_DEV", "0x4D2·0x1150", "CFA", True)
    bench.db.eds_write_file("conflict_a.eds", SEED_EDS)
    bench.db.eds_add("conflict_b.eds", "SEED_DEV", "0x4D2·0x1150", "CFB", True)
    bench.db.eds_write_file("conflict_b.eds", SEED_EDS)


def _age(bench: Bench, file: str, seconds: float) -> None:
    """Make file's mtime `seconds` older than its current mtime."""
    import os
    path = bench.db.eds_dir / file
    st = os.stat(path)
    os.utime(path, (st.st_atime - seconds, st.st_mtime - seconds))


def test_rematch_newest_conflicting_file_wins(bench):
    _make_conflicting_pair(bench)
    _age(bench, "conflict_a.eds", 10)  # conflict_b.eds is newer

    bench.devices = [_bare_device()]
    bench.dispatch("eds_toggle", {"file": "conflict_b.eds"})  # disable
    bench.dispatch("eds_toggle", {"file": "conflict_b.eds"})  # re-enable -> rematch

    assert bench.devices[0]["eds"] == "conflict_b.eds"

    # now age conflict_b.eds so conflict_a.eds becomes the newer file
    _age(bench, "conflict_b.eds", 20)
    bench.devices = [_bare_device()]
    bench.dispatch("eds_toggle", {"file": "conflict_a.eds"})  # disable
    bench.dispatch("eds_toggle", {"file": "conflict_a.eds"})  # re-enable -> rematch

    assert bench.devices[0]["eds"] == "conflict_a.eds"


def test_snapshot_flags_conflict_and_winner(bench):
    _make_conflicting_pair(bench)
    _age(bench, "conflict_a.eds", 10)  # conflict_b.eds is newer

    snap = bench.snapshot()
    files = {e["file"]: e for e in snap["eds"]["files"]}

    assert files["conflict_a.eds"]["conflict"] == ["conflict_b.eds"]
    assert files["conflict_b.eds"]["conflict"] == ["conflict_a.eds"]
    assert files["conflict_a.eds"]["conflictWin"] is False
    assert files["conflict_b.eds"]["conflictWin"] is True

    # a unique-identity entry is unaffected
    unique = files["dut_gamma_v5.eds"]
    assert unique["conflict"] == []
    assert unique["conflictWin"] is False


def test_disabling_one_conflicting_file_clears_flags_on_the_other(bench):
    _make_conflicting_pair(bench)
    _age(bench, "conflict_a.eds", 10)

    bench.dispatch("eds_toggle", {"file": "conflict_a.eds"})  # disable

    snap = bench.snapshot()
    files = {e["file"]: e for e in snap["eds"]["files"]}
    assert files["conflict_b.eds"]["conflict"] == []
    assert files["conflict_b.eds"]["conflictWin"] is False
    assert files["conflict_a.eds"]["conflict"] == []  # disabled: never flagged
    assert files["conflict_a.eds"]["conflictWin"] is False


def test_scan_logs_identity_conflict(bench):
    _make_conflicting_pair(bench)
    _age(bench, "conflict_a.eds", 10)  # conflict_b.eds is newer

    connect_and_scan(bench)

    conflict_logs = [ln["msg"] for ln in bench.logs if "identity conflict" in ln["msg"]]
    assert conflict_logs, bench.logs
    msg = conflict_logs[0]
    assert "newest file wins" in msg
    assert "conflict_a.eds" in msg and "conflict_b.eds" in msg
    assert "conflict_b.eds)" in msg  # the winner is named in parentheses


# -- configurable EDS folder ---------------------------------------------

def test_eds_dir_defaults_to_workspace_eds_subfolder(bench):
    assert bench.db.eds_dir == bench.db.path.parent / "eds"
    assert bench.snapshot()["paths"]["eds"] == str(bench.db.eds_dir)


def test_set_path_eds_switches_folder_creates_and_persists(bench, tmp_path):
    pool = tmp_path / "pool"  # does not exist yet
    bench.dispatch("set_path", {"which": "eds", "value": str(pool)})

    assert bench.db.eds_dir == pool
    assert pool.is_dir()
    assert bench.snapshot()["paths"]["eds"] == str(pool)

    again = Bench(Db(bench.db.path))
    assert again.db.eds_dir == pool


@pytest.mark.parametrize("reset_value", ["", "   "])
def test_set_path_eds_reset_returns_to_workspace_default(bench, tmp_path, reset_value):
    default_dir = bench.db.eds_dir
    pool = tmp_path / "pool"
    bench.dispatch("set_path", {"which": "eds", "value": str(pool)})
    assert bench.db.eds_dir == pool

    bench.dispatch("set_path", {"which": "eds", "value": reset_value})
    assert bench.db.eds_dir == default_dir

    again = Bench(Db(bench.db.path))
    assert again.db.eds_dir == default_dir


def test_set_path_eds_logs_reset_suffix(bench, tmp_path):
    pool = tmp_path / "pool"
    bench.dispatch("set_path", {"which": "eds", "value": str(pool)})
    bench.dispatch("set_path", {"which": "eds", "value": ""})
    assert "(workspace default)" in bench.logs[-1]["msg"]


def test_eds_upload_lands_in_the_configured_folder(bench, tmp_path):
    default_dir = bench.db.eds_dir
    pool = tmp_path / "pool"
    bench.dispatch("set_path", {"which": "eds", "value": str(pool)})

    bench.dispatch("eds_upload", {"filename": "pool_upload.eds", "content": MINIMAL_EDS})

    assert (pool / "pool_upload.eds").is_file()
    assert not (default_dir / "pool_upload.eds").exists()


def test_object_catalog_follows_folder_switch(bench, tmp_path):
    import shutil

    ok, msg = bench.add_eds_file("match.eds", SEED_EDS)
    assert ok, msg
    dev = _bare_device()
    dev["eds"] = "match.eds"
    bench.devices = [dev]
    assert bench.snapshot()["objects"]["hint"] == ""
    assert bench.snapshot()["objects"]["groups"]

    pool = tmp_path / "pool"  # empty pool: the file isn't there
    bench.dispatch("set_path", {"which": "eds", "value": str(pool)})
    hint = bench.snapshot()["objects"]["hint"]
    assert "missing from the workspace" in hint

    shutil.copy(bench.db.path.parent / "eds" / "match.eds", pool / "match.eds")
    snap = bench.snapshot()
    assert snap["objects"]["hint"] == ""
    assert snap["objects"]["groups"]


def test_browse_open_eds_starts_at_the_configured_folder(bench):
    bench.dispatch("browse_open", {"which": "eds"})
    assert bench.browse is not None
    assert bench.browse["which"] == "eds"
    assert bench.browse["path"] == str(bench.db.eds_dir)


def test_browse_select_applies_eds_folder(bench, tmp_path):
    root = tmp_path / "eds_root"
    (root / "A").mkdir(parents=True)
    bench.dispatch("set_path", {"which": "eds", "value": str(root)})  # start point for the browser
    bench.dispatch("browse_open", {"which": "eds"})
    bench.dispatch("browse_nav", {"dir": "A"})

    bench.dispatch("browse_select", {})

    assert bench.browse is None
    assert bench.db.eds_dir == (root / "A").resolve()


def test_switching_eds_folder_triggers_rematch(bench, tmp_path):
    # matching is purely identity/registry driven — the file's physical
    # location doesn't affect whether an entry matches a device, so this
    # demonstrates that _set_eds_dir calls _rematch_devices, not that the
    # pool's contents specifically caused the match (see task note).
    pool = tmp_path / "pool"
    pool.mkdir()
    (pool / "pool_match.eds").write_text(SEED_EDS, encoding="utf-8")
    bench.db.eds_add("pool_match.eds", "POOLDEV", "0x4D2·0x9999", "PLM", True)

    bench.devices = [_bare_device(ident="0x4D2·0x9999")]
    assert bench.devices[0]["eds"] == "—"  # nothing re-matched it yet

    bench.dispatch("set_path", {"which": "eds", "value": str(pool)})

    assert bench.devices[0]["eds"] == "pool_match.eds"
    assert any("⇒" in ln["msg"] for ln in bench.logs)


# -- demo-content gating (snapshot/tests/swdl) --------------------------------

def test_demo_adapter_no_testcases_falls_back_to_demo_content(bench):
    assert bench.adapter == "demo"
    snap = bench.snapshot()
    assert snap["tests"]["catalog"] != []
    assert snap["tests"]["lastRes"] != {}
    assert snap["tests"]["reports"] != []
    assert snap["swdl"]["fw"] != []
    assert snap["swdl"]["vendor"] is False


def test_real_adapter_no_testcases_has_no_demo_content(bench):
    bench.dispatch("set_adapter", {"adapter": "ixxat"})
    snap = bench.snapshot()
    assert snap["tests"]["catalog"] == []
    assert snap["tests"]["lastRes"] == {}
    assert snap["tests"]["reports"] == []
    assert snap["swdl"]["fw"] == []  # bare Bench has no plugins
    assert snap["swdl"]["vendor"] is False


def test_swdl_start_refuses_on_real_adapter_without_vendor_strategy(bench):
    bench.dispatch("set_adapter", {"adapter": "ixxat"})
    bench.devices = [_bare_device()]  # sel=True, no connection needed

    bench.dispatch("swdl_start", {})

    assert bench.swdl_run is False
    last = bench.logs[-1]
    assert "no vendor download protocol" in last["msg"]
    assert last["type"] == "emcy0"


def test_swdl_start_simulates_on_demo_adapter(bench):
    assert bench.adapter == "demo"
    bench.devices = [_bare_device()]  # sel=True

    bench.dispatch("swdl_start", {})

    assert bench.swdl_run is True


def test_real_testcases_win_regardless_of_adapter(bench, tmp_path):
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    (tc_dir / "TC0001_min.yaml").write_text(
        'id: "0001"\nname: "minimal"\nsteps:\n  - log: "hi"\n')
    bench.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})
    bench.dispatch("set_adapter", {"adapter": "ixxat"})

    snap = bench.snapshot()
    ids = [row[0] for row in snap["tests"]["catalog"]]
    assert ids == ["0001"]


# -- catalog row shape: [id, name, tools, est, err, grade, variants, file,
#    errmsg, needs_dut] --

def test_catalog_row_carries_grade_variants_and_empty_errmsg_for_a_valid_case(bench, tmp_path):
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    (tc_dir / "TC0001_valid.yaml").write_text(
        'id: "0001"\nname: "valid"\ngrade: automated\nvariants: ["820", "920"]\n'
        'steps:\n  - log: "hi"\n')
    bench.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})

    snap = bench.snapshot()
    row = next(r for r in snap["tests"]["catalog"] if r[0] == "0001")
    tid, name, tools, est, err, grade, variants, file, errmsg, needs_dut = row
    assert err is False
    assert grade == "automated"
    assert variants == ["820", "920"]
    assert file == "TC0001_valid.yaml"
    assert errmsg == ""


def test_catalog_row_for_a_broken_file_carries_err_file_and_errmsg(bench, tmp_path):
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    (tc_dir / "TC0002_broken.yaml").write_text(
        'id: "0002"\nname: "broken"\nexpekt: "typo"\nsteps:\n  - log: "hi"\n')
    bench.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})

    snap = bench.snapshot()
    # the top-level schema error (unknown key "expekt") is caught before id
    # parsing, so the catalog falls back to the filename as the row's id —
    # find the row by file instead
    row = next(r for r in snap["tests"]["catalog"] if r[7] == "TC0002_broken.yaml")
    tid, name, tools, est, err, grade, variants, file, errmsg, needs_dut = row
    assert err is True
    assert file == "TC0002_broken.yaml"
    assert errmsg  # the UI shows this instead of "see file"


def test_catalog_says_which_cases_need_the_device_picked_in_the_devices_box(bench, tmp_path):
    """The Start button greys out when a selected case needs a DUT and none
    is picked — the same condition ``act_run_start`` refuses on. It can only
    apply it if the catalog says which cases those are: "selected" needs one
    picked, a case naming its device by code brings its own.
    """
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    (tc_dir / "TC0001_picked.yaml").write_text(
        'id: "0001"\nname: "picked"\nsteps:\n  - log: "hi"\n')
    (tc_dir / "TC0002_by_code.yaml").write_text(
        'id: "0002"\nname: "by code"\ndut:\n  code: "EFS2"\nsteps:\n  - log: "hi"\n')
    bench.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})

    rows = {r[0]: r[9] for r in bench.snapshot()["tests"]["catalog"]}
    assert rows == {"0001": True, "0002": False}


def test_the_demo_catalog_row_needs_no_device(bench):
    """Demo rows are shorter than real ones, so the column simply is not
    there — and read as "no DUT needed", which is what that catalog does:
    ``act_run_start`` puts it in sim mode and never touches a device. A
    Start button greyed out over a device the run would not use would be
    the wrong answer.
    """
    row = bench.snapshot()["tests"]["catalog"][0]
    assert len(row) < 10


def test_grades_and_variants_dropdowns_only_list_what_the_folder_has(bench, tmp_path):
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    (tc_dir / "TC0001_a.yaml").write_text(
        'id: "0001"\nname: "a"\ngrade: automated\nvariants: ["820"]\nsteps:\n  - log: "hi"\n')
    (tc_dir / "TC0002_b.yaml").write_text(
        'id: "0002"\nname: "b"\ngrade: manual\nvariants: ["920"]\nsteps:\n  - log: "hi"\n')
    bench.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})

    snap = bench.snapshot()
    assert snap["tests"]["grades"] == ["automated", "manual"]
    assert snap["tests"]["variants"] == ["820", "920"]


def test_grades_and_variants_dropdowns_empty_when_folder_declares_none(bench, tmp_path):
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    (tc_dir / "TC0001_plain.yaml").write_text(
        'id: "0001"\nname: "plain"\nsteps:\n  - log: "hi"\n')
    bench.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})

    snap = bench.snapshot()
    assert snap["tests"]["grades"] == []
    assert snap["tests"]["variants"] == []


# -- act_tc_open: hands a test case to the system's file opener --------------

def test_tc_open_happy_path_opens_the_resolved_absolute_path(bench, tmp_path, monkeypatch):
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    (tc_dir / "TC0001_min.yaml").write_text('id: "0001"\nname: "minimal"\nsteps:\n  - log: "hi"\n')
    bench.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})

    opened = []
    monkeypatch.setattr(core_mod, "_open_in_editor", lambda path: opened.append(path))

    bench.dispatch("tc_open", {"id": "0001"})

    assert opened == [tc_dir / "TC0001_min.yaml"]
    assert any("opening TC0001_min.yaml in the system editor" in ln["msg"] for ln in bench.logs)


def test_tc_open_unknown_id_opens_nothing_and_logs(bench, monkeypatch):
    opened = []
    monkeypatch.setattr(core_mod, "_open_in_editor", lambda path: opened.append(path))

    bench.dispatch("tc_open", {"id": "does-not-exist"})

    assert opened == []
    assert any("no such test case" in ln["msg"] for ln in bench.logs)


def test_tc_open_case_with_no_file_logs_no_such_test_case(bench, monkeypatch):
    bench.testcases["x"] = tclib.TestCase(id="x", name="in-memory only", file="")

    opened = []
    monkeypatch.setattr(core_mod, "_open_in_editor", lambda path: opened.append(path))

    bench.dispatch("tc_open", {"id": "x"})

    assert opened == []
    assert any("no such test case" in ln["msg"] for ln in bench.logs)


def test_tc_open_missing_file_on_disk_opens_nothing_and_logs(bench, tmp_path, monkeypatch):
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    (tc_dir / "TC0001_min.yaml").write_text('id: "0001"\nname: "minimal"\nsteps:\n  - log: "hi"\n')
    bench.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})
    (tc_dir / "TC0001_min.yaml").unlink()  # the catalog still knows it; the disk does not

    opened = []
    monkeypatch.setattr(core_mod, "_open_in_editor", lambda path: opened.append(path))

    bench.dispatch("tc_open", {"id": "0001"})

    assert opened == []
    assert any("CFG  open" in ln["msg"] and "TC0001_min.yaml" in ln["msg"] for ln in bench.logs)


def test_tc_open_editor_oserror_is_logged_and_swallowed(bench, tmp_path, monkeypatch):
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    (tc_dir / "TC0001_min.yaml").write_text('id: "0001"\nname: "minimal"\nsteps:\n  - log: "hi"\n')
    bench.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})

    def boom(path):
        raise OSError("no application registered")
    monkeypatch.setattr(core_mod, "_open_in_editor", boom)

    bench.dispatch("tc_open", {"id": "0001"})  # must not raise

    assert any("CFG  open" in ln["msg"] and "no application registered" in ln["msg"]
               for ln in bench.logs)
    assert bench.logs[-1]["type"] == "emcy0"


def test_tc_open_path_traversal_guard_refuses_to_escape_the_folder(bench, tmp_path, monkeypatch):
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    bench.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})
    (tmp_path / "outside.yaml").write_text("steps: []\n")  # a real file, just outside the folder
    bench.testcases["esc"] = tclib.TestCase(id="esc", name="escape", file="../outside.yaml")

    opened = []
    monkeypatch.setattr(core_mod, "_open_in_editor", lambda path: opened.append(path))

    bench.dispatch("tc_open", {"id": "esc"})

    assert opened == []
    assert any("CFG  open" in ln["msg"] for ln in bench.logs)


# -- live bus statistics + version/workspace snapshot fields ------------------

def test_frontend_fetches_nothing_from_the_internet():
    """The UI must load from this package alone.

    A bench PC on a machine network typically has no route out, and a
    webfont request also hands every operator's IP to whoever hosts it.
    The fonts used to come from Google Fonts; they are vendored now, and
    this keeps them that way — including their license text, which the
    OFL requires to travel with the files.
    """
    import canopen_bench

    static = Path(canopen_bench.__file__).resolve().parent / "static"
    markup = (static / "index.html").read_text(encoding="utf-8")
    css = (static / "styles.css").read_text(encoding="utf-8")

    for name, text in (("index.html", markup), ("styles.css", css)):
        assert "//fonts.googleapis.com" not in text, f"{name} still fetches Google Fonts"
        assert "//fonts.gstatic.com" not in text, f"{name} still fetches Google Fonts"

    fonts = static / "fonts"
    faces = sorted(p.name for p in fonts.glob("*.woff2"))
    assert faces, "no vendored fonts"
    for face in faces:
        assert f"fonts/{face}" in css, f"{face} ships but no @font-face references it"
    assert (fonts / "LICENSE.txt").is_file(), "OFL requires the license to ship with the fonts"


def test_demo_seed_eds_ships_inside_the_package():
    """The seed EDS has to be package data, or a wheel install has no demo.

    It used to live in `examples/` at the repository root, which pip does
    not install. From a source checkout it resolved fine and every test
    passed; from `pip install canopen-bench` it was simply absent, and
    `_seed_demo_eds` swallowed the OSError — so demo mode scanned and
    found nothing, with no message anywhere.

    Two conditions make it shippable, and both are checked here: the file
    lives under the package directory, and pyproject declares a
    package-data pattern that covers it.
    """
    import canopen_bench

    pkg_dir = Path(canopen_bench.__file__).resolve().parent
    assert core_mod.SEED_EDS.is_file(), f"seed EDS missing at {core_mod.SEED_EDS}"
    assert core_mod.SEED_EDS.is_relative_to(pkg_dir), (
        f"{core_mod.SEED_EDS} is outside {pkg_dir} — it cannot ship as package data")

    # the package-data entry itself, not just any mention of the pattern —
    # a comment naming it would otherwise satisfy this check
    pyproject = (pkg_dir.parent / "pyproject.toml").read_text(encoding="utf-8")
    entry = next((ln for ln in pyproject.splitlines()
                  if ln.strip().startswith("canopen_bench = [")), "")
    assert "seed/*.eds" in entry, f"package-data does not cover the seed EDS: {entry!r}"


def test_version_has_one_source_of_truth():
    """The version lives in pyproject.toml and nowhere else.

    It used to be duplicated into ``canopen_bench.__version__``, which
    drifted to 2.0.0 while pyproject said 1.0.0 — and since nothing read
    it, nothing noticed. This project bumps the version on every commit,
    so a second hand-maintained copy would drift again immediately.

    Checked against the file as text: ``tomllib`` is 3.11+, this project
    supports 3.10, and the version has to hold on every version we claim
    to run on.
    """
    from pathlib import Path

    import canopen_bench

    pp = Path(canopen_bench.__file__).resolve().parent.parent / "pyproject.toml"
    declared = pp.read_text(encoding="utf-8")

    assert canopen_bench.__version__ != "dev", "version not resolved from pyproject.toml"
    assert f'version = "{canopen_bench.__version__}"' in declared
    assert core_mod.VERSION == canopen_bench.__version__


def test_snapshot_version_and_workspace(bench):
    snap = bench.snapshot()
    assert snap["version"] == core_mod.VERSION
    assert snap["version"]  # non-empty
    assert snap["workspace"] == bench.db.path.stem
    assert snap["busLoad"] == 0.0
    assert snap["errFrames"] == 0


def _stat_row(length: str = "8", flag: str = "", cob: str = "0x181") -> dict:
    return {"len": length, "flag": flag, "cob": cob,
            "dec": "TxPDO1 node 01", "cls": "PDO"}


def test_update_bus_stats_computes_bus_load_from_a_single_batch(bench):
    bench.bitrate = "500"
    rows = [_stat_row() for _ in range(10)]  # 10 * (47 + 8*8) = 1110 bits
    bench._update_bus_stats(rows)
    expected = 100 * 1110 / (core_mod.TICK_S * 500_000)
    assert bench.bus_load == pytest.approx(expected, rel=1e-3)


def test_update_bus_stats_caps_at_100(bench):
    bench.bitrate = "500"
    rows = [_stat_row() for _ in range(100_000)]
    bench._update_bus_stats(rows)
    assert bench.bus_load == 100.0


def test_update_bus_stats_counts_red_flags_only(bench):
    bench.bitrate = "500"
    rows = [_stat_row(flag="red"), _stat_row(flag=""), _stat_row(flag="red")]
    bench._update_bus_stats(rows)
    assert bench.err_frames == 2

    bench._update_bus_stats([_stat_row(flag="red")])
    assert bench.err_frames == 3  # accumulates across calls


def test_update_bus_stats_unparseable_len_counts_as_dlc_8(bench):
    bench.bitrate = "500"
    rows = [_stat_row(length="?")]
    bench._update_bus_stats(rows)  # must not raise
    expected = 100 * (47 + 8 * 8) / (core_mod.TICK_S * 500_000)
    assert bench.bus_load == pytest.approx(expected, rel=1e-3)


def test_connect_resets_bus_stats_disconnect_zeroes_bus_load(bench):
    bench.bus_load = 42.0
    bench.err_frames = 7
    bench._load_win.append((0.0, 100))

    bench.dispatch("connect_toggle", {})  # demo adapter: connects

    assert bench.connected
    assert bench.bus_load == 0.0
    assert bench.err_frames == 0
    assert len(bench._load_win) == 0

    bench.dispatch("connect_toggle", {})  # disconnects

    assert not bench.connected
    assert bench.bus_load == 0.0


def test_update_bus_stats_accumulates_per_cob_counters(bench):
    bench.bitrate = "500"
    bench._update_bus_stats([_stat_row(cob="0x181"), _stat_row(cob="0x201")])
    bench._update_bus_stats([
        _stat_row(cob="0x181"),
        {**_stat_row(cob="0x201"), "dec": "TxPDO2 node 01"},
    ])

    assert bench._cob_stats["0x181"]["n"] == 2
    assert bench._cob_stats["0x181"]["dec"] == "TxPDO1 node 01"
    assert bench._cob_stats["0x181"]["cls"] == "PDO"
    assert bench._cob_stats["0x201"]["n"] == 2
    assert bench._cob_stats["0x201"]["dec"] == "TxPDO2 node 01"  # latest row's label
    assert bench._cob_stats["0x201"]["cls"] == "PDO"  # from first occurrence


def test_update_bus_stats_empty_tick_decays_load_and_extends_history(bench):
    bench.bitrate = "500"
    bench._update_bus_stats([_stat_row() for _ in range(10)])
    load_after_frames = bench.bus_load
    hist_len = len(bench._load_hist)

    bench._update_bus_stats([])

    assert len(bench._load_hist) == hist_len + 1
    assert bench.bus_load <= load_after_frames
    assert bench._rate_win[-1][1] == {}


def test_trace_stats_caps_top_n_and_aggregates_the_rest(bench):
    bench.bitrate = "500"
    for i in range(45):
        n = 3 if i == 0 else 1  # cob 0x000 gets the most frames
        for _ in range(n):
            bench._update_bus_stats([_stat_row(cob=f"0x{i:03X}")])

    stats = bench._trace_stats()

    assert len(stats["cobs"]) == 40
    assert stats["restCobs"] == 5
    assert stats["restN"] == 5
    assert stats["total"] == 45 + 2  # 45 cobs, one of which got 2 extra frames
    assert stats["cobs"][0]["cob"] == "0x000"
    assert stats["cobs"][0]["n"] == 3


def test_trace_stats_classes_group_empty_cls_as_other(bench):
    bench.bitrate = "500"
    bench._update_bus_stats([_stat_row(cob="0x181")])
    bench._update_bus_stats([{**_stat_row(cob="0x080"), "cls": ""}])

    stats = bench._trace_stats()

    assert stats["classes"]["PDO"] == 1
    assert stats["classes"]["other"] == 1


def test_trace_stats_span_zero_until_first_observation(bench):
    assert bench._stats_t0 == 0.0
    assert bench._trace_stats()["span"] == 0.0

    bench._update_bus_stats([_stat_row()])
    bench._stats_t0 -= 1.0  # simulate elapsed time without a real sleep

    assert bench._stats_t0 > 0.0
    assert bench._trace_stats()["span"] > 0.0


def test_snapshot_trace_stats_carries_expected_keys(bench):
    bench.dispatch("connect_toggle", {})  # demo adapter: connects
    bench._update_bus_stats([_stat_row()])

    stats = bench.snapshot()["trace"]["stats"]

    for key in ("cobs", "restN", "restCobs", "classes", "total", "rate",
                "span", "loadHist", "err"):
        assert key in stats


def test_trace_clear_resets_cob_stats_and_span_but_keeps_err_frames(bench):
    bench._update_bus_stats([_stat_row(flag="red")])
    assert bench.err_frames == 1
    assert bench._cob_stats
    assert bench._stats_t0 > 0.0

    bench.dispatch("trace_clear", {})

    assert bench._cob_stats == {}
    assert bench._stats_t0 == 0.0
    assert bench.err_frames == 1  # tracks bus health across the buffer clear


def test_rate_window_trims_entries_older_than_five_seconds(bench):
    bench.bitrate = "500"
    bench._rate_win.append((time.monotonic() - 10, {"0x181": 1}))

    bench._update_bus_stats([])

    assert len(bench._rate_win) == 1
    assert bench._rate_win[0][1] == {}


def test_eds_by_identity_missing_file_falls_back_to_mtime_zero(bench):
    bench.db.eds_remove("dut_alpha_v2.eds")
    bench.db.eds_add("real.eds", "SEED_DEV", "0x4D2·0x1150", "REA", True)
    bench.db.eds_write_file("real.eds", SEED_EDS)
    # registry entry with no backing file on disk — same identity
    bench.db.eds_add("ghost.eds", "SEED_DEV", "0x4D2·0x1150", "GHO", True)

    by_ident = bench._eds_by_identity()  # must not raise

    assert by_ident[normalize_identity("0x4D2·0x1150")]["file"] == "real.eds"


# -- multi-workspace support -------------------------------------------------

@pytest.fixture()
def ws_root(tmp_path):
    root = tmp_path / "ws"
    (root / "A").mkdir(parents=True)
    (root / "B").mkdir(parents=True)
    return root


@pytest.fixture()
def ws_bench(ws_root):
    b = Bench(Db(ws_root / "A" / "canopen-bench.db"), workspaces_root=ws_root)
    write_seed_eds_files(b)
    return b


def test_workspace_name_root_mode_is_folder_name(ws_bench):
    assert ws_bench.workspace_name == "A"


def test_workspace_name_without_root_is_db_stem(bench):
    assert bench.workspace_name == bench.db.path.stem


def test_workspace_names_lists_sorted_subfolders_ignoring_dotdirs_and_files(ws_root, ws_bench):
    (ws_root / "z_folder").mkdir()
    (ws_root / ".hidden").mkdir()
    (ws_root / "not_a_dir.txt").write_text("x")
    assert ws_bench._workspace_names() == ["A", "B", "z_folder"]


def test_snapshot_workspaces_can_switch_false_without_callback(ws_bench):
    snap = ws_bench.snapshot()
    assert snap["workspace"] == "A"
    assert snap["workspaces"]["list"] == ["A", "B"]
    assert snap["workspaces"]["canSwitch"] is False


def test_snapshot_workspaces_can_switch_true_with_callback(ws_bench):
    ws_bench.on_workspace_switch = lambda name: None
    snap = ws_bench.snapshot()
    assert snap["workspaces"]["canSwitch"] is True


class _RecordingSwitch:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, name: str) -> None:
        self.calls.append(name)


def test_workspace_create_valid_name_creates_folder_and_invokes_callback(ws_root, ws_bench):
    cb = _RecordingSwitch()
    ws_bench.on_workspace_switch = cb
    ws_bench.dispatch("workspace_create", {"name": "NewLine"})
    assert (ws_root / "NewLine").is_dir()
    assert cb.calls == ["NewLine"]


@pytest.mark.parametrize("bad_name", [
    "", "../x", ".hidden", "a" * 60, "a/b",
])
def test_workspace_create_invalid_names_logged_and_not_invoked(ws_root, ws_bench, bad_name):
    cb = _RecordingSwitch()
    ws_bench.on_workspace_switch = cb
    ws_bench.dispatch("workspace_create", {"name": bad_name})
    assert cb.calls == []
    last = ws_bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "invalid workspace name" in last["msg"]


def test_workspace_create_existing_folder_invokes_callback_without_error(ws_root, ws_bench):
    cb = _RecordingSwitch()
    ws_bench.on_workspace_switch = cb
    ws_bench.dispatch("workspace_create", {"name": "B"})  # already created by ws_root fixture
    assert cb.calls == ["B"]


def test_workspace_switch_unknown_name_logs_emcy0_no_callback(ws_bench):
    cb = _RecordingSwitch()
    ws_bench.on_workspace_switch = cb
    ws_bench.dispatch("workspace_switch", {"name": "nope"})
    assert cb.calls == []
    last = ws_bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "unknown workspace" in last["msg"]


def test_workspace_switch_same_as_active_is_a_noop(ws_bench):
    cb = _RecordingSwitch()
    ws_bench.on_workspace_switch = cb
    ws_bench.dispatch("workspace_switch", {"name": "A"})
    assert cb.calls == []


def test_workspace_switch_valid_other_invokes_callback(ws_bench):
    cb = _RecordingSwitch()
    ws_bench.on_workspace_switch = cb
    ws_bench.dispatch("workspace_switch", {"name": "B"})
    assert cb.calls == ["B"]


def test_workspace_switch_path_traversal_reduced_to_basename(ws_bench):
    # forward-slash traversal is reduced to its basename by Path(...).name
    # on every platform.
    cb = _RecordingSwitch()
    ws_bench.on_workspace_switch = cb
    ws_bench.dispatch("workspace_switch", {"name": "x/B"})
    assert cb.calls == ["B"]


def test_workspace_switch_backslash_name_cannot_leave_the_root(ws_bench):
    """A backslash is a separator on one platform and an ordinary character
    on the other, and the guard has to hold either way.

    What must be true everywhere is that nothing with a separator or a
    ".." in it reaches the callback — that is what reducing to a basename
    is for. What differs is where "..\\..\\B" then lands: on Windows
    Path(...).name gives "B", a workspace that exists, so the switch
    happens; on POSIX the string survives whole, matches no workspace, and
    is logged as unknown. Asserting only the POSIX half made this fail on
    Windows for a behaviour that was never wrong.
    """
    cb = _RecordingSwitch()
    ws_bench.on_workspace_switch = cb
    ws_bench.dispatch("workspace_switch", {"name": "..\\..\\B"})
    assert all("/" not in c and "\\" not in c and c != ".." for c in cb.calls)
    if os.name == "nt":
        assert cb.calls == ["B"]
    else:
        assert cb.calls == []
        assert "unknown workspace" in ws_bench.logs[-1]["msg"]


def test_workspace_actions_without_root_log_switching_disabled(bench):
    bench.dispatch("workspace_create", {"name": "X"})
    last = bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "switching disabled" in last["msg"]

    bench.dispatch("workspace_switch", {"name": "X"})
    last = bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "switching disabled" in last["msg"]


# -- cyclic transmit (Sendeliste + SYNC producer) ----------------------------

def test_raw_row_load_normalizes_legacy_rows_without_cyc_run_id(tmp_path):
    db = Db(tmp_path / "test.db")
    db.set("raw_sdo", [
        {"type": "sdo", "node": "", "i": "0x2040", "s": "01", "l": "4", "v": "0x00260001"},
        {"type": "pdo", "node": "1", "i": "0x", "s": "00", "l": "1", "v": ""},
    ])
    bench = Bench(db)
    assert len(bench.raw_rows) == 2
    for r in bench.raw_rows:
        assert r["cyc"] == "100"
        assert r["run"] is False
        assert isinstance(r["id"], int)
    assert len({r["id"] for r in bench.raw_rows}) == 2  # unique


def test_raw_cycle_ignored_on_sdo_row(bench):
    row = bench.raw_rows[0]
    assert row["type"] == "sdo"
    n_logs = len(bench.logs)
    bench.dispatch("raw_cycle", {"row": 0})
    assert row["run"] is False
    assert len(bench.logs) == n_logs  # no effect, no log


def test_raw_cycle_start_skipped_when_not_connected(bench):
    bench.dispatch("raw_update", {"row": 0, "field": "type", "value": "pdo"})
    bench.dispatch("raw_cycle", {"row": 0})
    assert bench.raw_rows[0]["run"] is False
    last = bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "CYC" in last["msg"]
    assert "not connected" in last["msg"]


def test_raw_cycle_starts_and_stops_when_connected(connected_bench):
    bench = connected_bench
    bench.dispatch("raw_update", {"row": 0, "field": "type", "value": "pdo"})
    bench.dispatch("raw_update", {"row": 0, "field": "pdo", "value": "RxPDO1"})
    bench.dispatch("raw_update", {"row": 0, "field": "node", "value": "1"})
    bench.dispatch("raw_update", {"row": 0, "field": "data", "value": "DC 05 00 00"})
    row = bench.raw_rows[0]

    bench.dispatch("raw_cycle", {"row": 0})
    assert row["run"] is True
    assert bench._cyc_next[row["id"]] == 0.0

    bench.dispatch("raw_cycle", {"row": 0})
    assert row["run"] is False


def test_raw_cycle_start_rejects_invalid_data(connected_bench):
    bench = connected_bench
    bench.dispatch("raw_update", {"row": 0, "field": "type", "value": "pdo"})
    bench.dispatch("raw_update", {"row": 0, "field": "pdo", "value": "RxPDO1"})
    bench.dispatch("raw_update", {"row": 0, "field": "node", "value": "1"})
    bench.dispatch("raw_update", {"row": 0, "field": "data", "value": "ZZ"})
    bench.dispatch("raw_cycle", {"row": 0})
    assert bench.raw_rows[0]["run"] is False
    last = bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "CYC" in last["msg"]


def test_sync_toggle_requires_connection(bench):
    bench.dispatch("sync_toggle", {})
    assert bench.sync_run is False
    last = bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "SYNC" in last["msg"]


def test_sync_toggle_starts_and_stops_when_connected(connected_bench):
    bench = connected_bench
    bench.dispatch("sync_toggle", {})
    assert bench.sync_run is True
    assert "SYNC producer started" in bench.logs[-1]["msg"]

    bench.dispatch("sync_toggle", {})
    assert bench.sync_run is False
    assert "SYNC producer stopped" in bench.logs[-1]["msg"]


def test_set_sync_ms_clamps_and_persists(bench):
    bench.dispatch("set_sync_ms", {"ms": "3"})
    assert bench.sync_ms == 5

    bench.dispatch("set_sync_ms", {"ms": "70000"})
    assert bench.sync_ms == 60000

    before = bench.sync_ms
    bench.dispatch("set_sync_ms", {"ms": "abc"})
    assert bench.sync_ms == before

    assert Db(bench.db.path).get("sync") == {"ms": 60000}


def test_disconnect_stops_all_cyclic_senders(connected_bench):
    bench = connected_bench
    bench.dispatch("raw_update", {"row": 0, "field": "type", "value": "pdo"})
    bench.dispatch("raw_update", {"row": 0, "field": "pdo", "value": "RxPDO1"})
    bench.dispatch("raw_update", {"row": 0, "field": "node", "value": "1"})
    bench.dispatch("raw_update", {"row": 0, "field": "data", "value": "DC 05 00 00"})
    bench.dispatch("raw_cycle", {"row": 0})
    bench.dispatch("sync_toggle", {})
    assert bench.raw_rows[0]["run"] is True
    assert bench.sync_run is True

    bench.dispatch("connect_toggle", {})  # disconnect

    assert bench.raw_rows[0]["run"] is False
    assert bench.sync_run is False
    assert bench._cyc_next == {}


def test_cyc_fire_stops_a_row_that_became_invalid_mid_run(connected_bench):
    bench = connected_bench
    row = bench.raw_rows[0]
    row["type"] = "pdo"
    row["run"] = True
    row["data"] = "ZZ"

    bench._cyc_fire(row)

    assert row["run"] is False
    last = bench.logs[-1]
    assert last["type"] == "emcy0"
    assert "CYC  row stopped" in last["msg"]


def test_cyclic_loop_sends_pdo_row_frames_repeatedly(connected_bench):
    bench = connected_bench
    bench.dispatch("raw_update", {"row": 0, "field": "type", "value": "pdo"})
    bench.dispatch("raw_update", {"row": 0, "field": "pdo", "value": "RxPDO1"})
    bench.dispatch("raw_update", {"row": 0, "field": "node", "value": "1"})
    bench.dispatch("raw_update", {"row": 0, "field": "data", "value": "DC 05 00 00"})
    bench.dispatch("raw_update", {"row": 0, "field": "cyc", "value": "10"})
    bench.dispatch("raw_cycle", {"row": 0})

    async def go():
        task = asyncio.create_task(bench._cyclic_loop())
        await asyncio.sleep(0.08)
        task.cancel()
    asyncio.run(go())

    frames = bench.bus.poll_frames(200)
    hits = [f for f in frames if f.cob_id == "0x201"]
    assert len(hits) >= 3


def test_cyclic_loop_sends_sync_frames_repeatedly(connected_bench):
    bench = connected_bench
    bench.dispatch("sync_toggle", {})
    bench.dispatch("set_sync_ms", {"ms": "10"})  # minimum is 5

    async def go():
        task = asyncio.create_task(bench._cyclic_loop())
        await asyncio.sleep(0.08)
        task.cancel()
    asyncio.run(go())

    frames = bench.bus.poll_frames(200)
    hits = [f for f in frames if f.cob_id == "0x080"]
    assert len(hits) >= 3
    assert all(f.decoded == "SYNC" for f in hits)


# -- _hexstr_width: byte width of an sdo_read answer (core.core._hexstr_width) --
# `adjust` uses this to size an operator-typed value against the object the
# device just answered for, instead of a hardcoded 4 bytes.

def test_hexstr_width_reads_digit_count_from_the_answer():
    assert core_mod._hexstr_width("0x001E") == 2
    assert core_mod._hexstr_width("0x0000001E") == 4
    assert core_mod._hexstr_width("0x1E") == 1
    assert core_mod._hexstr_width("42") == 0       # no 0x prefix: not this shape
    assert core_mod._hexstr_width("0x") == 0        # 0x with no digits
    assert core_mod._hexstr_width("hello") == 0


# -- _variant_matches: numeric-vs-numeric and label comparison (core.core) --
# Already has one coverage test in test_executor.py
# (test_a_variant_matches_however_the_two_sides_spell_the_number); this is
# the focused unit test for the helper itself.

def test_variant_matches_decimal_declared_against_hex_actual():
    assert core_mod._variant_matches(["820"], "0x334") is True  # 0x334 == 820


def test_variant_matches_hex_declared_against_decimal_actual():
    assert core_mod._variant_matches(["0x334"], "820") is True


def test_variant_matches_a_genuine_numeric_mismatch():
    assert core_mod._variant_matches(["820"], "0x335") is False  # 0x335 == 821


def test_variant_matches_non_numeric_label_case_insensitively():
    # an EDS variant map can answer a label instead of a number
    assert core_mod._variant_matches(["Rev-A"], "rev-a") is True


def test_variant_matches_number_declared_against_non_numeric_actual_mismatches():
    assert core_mod._variant_matches(["820"], "not-a-number") is False


# -- _open_in_editor: never shares the running server's own stdin/session ---
# (core.core._open_in_editor). A Popen call that inherits the server's
# stdin/session can suspend the server the moment the launched app tries to
# read from stdin, or tie the child to the server's own process group.

def test_open_in_editor_does_not_share_the_servers_stdin(tmp_path, monkeypatch):
    import subprocess
    import sys as sys_mod
    if sys_mod.platform == "win32":
        pytest.skip("win32 uses os.startfile, not subprocess.Popen")
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return object()
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    core_mod._open_in_editor(tmp_path / "case.yaml")

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True


def test_a_string_object_reads_back_as_the_string_it_is():
    """0x0000003332315F4F4D4544 is "DEMO_123" written back to front — the
    bus builds the answer with int.from_bytes(data, "little"), so the
    characters stand in the hex reversed, and the number's leading zeros
    are the string's trailing padding. Nobody recognises their device name
    in the number."""
    from canopen_bench.core import _hex_to_text

    assert _hex_to_text("0x0000003332315F4F4D4544") == "DEMO_123"
    # a number is not text, whatever its bytes happen to spell
    assert _hex_to_text("0x260001") is None
    assert _hex_to_text("R3") is None
    assert _hex_to_text("0x") is None
    assert _hex_to_text("0x123") is None      # odd digit count


def test_a_text_expectation_is_compared_against_the_decoded_answer():
    """The expectation is written "DEMO_123" and the answer arrives as a
    number. Comparing the raw hex against the wanted characters could only
    ever fail — which is what it did, on a device that had answered
    correctly."""
    from canopen_bench.bus.interface import SdoResult
    from canopen_bench.core import _judge_read

    spec = {"index": "0x1008", "sub": "0x00", "expect": '"DEMO_123"'}
    good = SdoResult(ok=True, value="0x0000003332315F4F4D4544")
    assert _judge_read(spec, good)[0] == "ok"

    other = SdoResult(ok=True, value="0x0000003432315F4F4D4544")   # DEMO_124
    status, why = _judge_read(spec, other)
    assert status == "fail"
    assert '"DEMO_124"' in why and "DEMO_123" in why, why


def test_select_all_takes_only_what_the_list_shows(tmp_path, monkeypatch):
    """Variant, category and the search box are filters the frontend applies
    to the catalog it was sent — the server never hears about them. Picking
    from its own idea of "shown" therefore selected cases that were not on
    screen: with the category set to `automated`, a semi-automated case
    joined the run and stopped it at a question nobody was expecting."""
    monkeypatch.setattr("canopen_bench.core.load_plugins", lambda: [])
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    for tid, grade in (("4646", "automated"), ("4647", "semi")):
        (tc_dir / f"TC{tid}_c.yaml").write_text(
            f'id: "{tid}"\nname: c\ngrade: {grade}\nsteps:\n  - log: "x"\n')
    bench = Bench(Db(tmp_path / "t.db"))
    bench.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})

    bench.dispatch("tests_all", {"ids": ["4646"]})       # what the filter showed
    assert bench.test_sel == {"4646"}

    # an id the folder does not have cannot be selected by asking for it
    bench.dispatch("tests_all", {"ids": ["4646", "9999"]})
    assert bench.test_sel == {"4646"}

    # no list at all still means everything, for any caller that sends none
    bench.dispatch("tests_all", {})
    assert bench.test_sel == {"4646", "4647"}


# -- what wait_for sees: the trace as the record, not a live subscription -----
#
# The bug these pin down: a step used to start listening when it ran, so a
# device that answered a few ms earlier was never heard and the step failed
# on a bus that had done nothing wrong.

def _stamp_ago(seconds: float) -> str:
    return (datetime.now() - timedelta(seconds=seconds)).strftime("%H:%M:%S.%f")


def _traced(bench: Bench, cob: str, data: str, *, ago: float = 0.0,
            direction: str = "RX") -> dict:
    """Put one row into the record, `ago` seconds old."""
    row = _trace_row(cob, data)
    row["time"] = _stamp_ago(ago)
    row["dir"] = direction
    row["cls"] = ""
    row["node"] = None
    bench.trace.append(row)
    return row


def test_match_traced_finds_a_frame_that_arrived_before_the_step_started(bench):
    _traced(bench, "0x181", "01 02", ago=0.3)
    assert bench._match_traced([(0x181, b"")], 0.5) == 0


def test_match_traced_ignores_a_frame_older_than_the_window(bench):
    _traced(bench, "0x181", "01 02", ago=0.9)
    assert bench._match_traced([(0x181, b"")], 0.5) is None


def test_match_traced_ignores_our_own_sent_frame(bench):
    """A can_send followed by a wait_for on the same COB-ID must not be
    answered by the send itself — that would be a step passing on its own
    echo without the device having said anything."""
    _traced(bench, "0x780", "01", ago=0.05, direction="TX")
    assert bench._match_traced([(0x780, b"")], 0.5) is None


def test_match_traced_races_pairs_and_reports_the_winning_index(bench):
    _traced(bench, "0x783", "02 AA", ago=0.1)  # only the second pair matches
    assert bench._match_traced([(0x700, b"\x01"), (0x783, b"\x02")], 0.5) == 1


def test_match_traced_same_cob_different_prefix_picks_the_matching_prefix(bench):
    _traced(bench, "0x783", "02 AA", ago=0.1)
    assert bench._match_traced([(0x783, b"\x01"), (0x783, b"\x02")], 0.5) == 1


def test_match_traced_without_a_match_returns_none(bench):
    _traced(bench, "0x700", "01", ago=0.1)
    assert bench._match_traced([(0x783, b"\x02")], 0.5) is None


# -- the record keeps recording, whatever the panel is showing ---------------

def test_pausing_freezes_the_view_but_not_the_recording(connected_bench):
    bench = connected_bench
    bench.dispatch("trace_toggle", {})  # pause
    frozen = bench.snapshot()["trace"]["total"]

    bench.bus.queue_raw(0x181, b"\x01\x02")
    bench._drain_frames()

    assert bench.snapshot()["trace"]["total"] == frozen  # view stands still
    assert bench._match_traced([(0x181, b"\x01")], 5.0) == 0  # record does not


def test_opening_a_capture_leaves_the_live_record_intact(connected_bench):
    """A capture is a second source, not a halt of the first: opening one
    during a run must not cost a waiting step the frame it needs."""
    bench = connected_bench
    bench.bus.queue_raw(0x181, b"\x01\x02")
    bench._drain_frames()
    live = list(bench.trace)

    bench.trace_dir.mkdir(parents=True, exist_ok=True)
    (bench.trace_dir / "other.json").write_text(
        json.dumps({"v": 1, "rows": [_trace_row("0x000", "")]}), encoding="utf-8")
    bench.dispatch("trace_load", {"file": "other.json"})

    assert bench.snapshot()["trace"]["total"] == 1  # the capture is shown
    assert bench.trace == live  # the record is untouched
    assert bench._match_traced([(0x181, b"\x01")], 5.0) == 0

    bench.dispatch("trace_toggle", {})  # resume: back to live data
    assert bench.trace_loaded is None
    assert bench.snapshot()["trace"]["total"] == len(live)
