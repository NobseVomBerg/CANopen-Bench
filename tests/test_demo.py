"""Demo mode: virtual DUTs generated from real uploaded EDS files.

No hardware, no simulator shortcuts — devices, identities and SDO data all
come from the EDS file the user uploaded on the Setup page.
"""
from __future__ import annotations

import asyncio

import pytest
from conftest import connect_and_scan, seed_test_registry

import canopen_bench.core as core_mod
from canopen_bench.core import Bench
from canopen_bench.db import Db

# -- sidebar display-mirror panel (device-dependent LCD readout) -----------

def test_eds_set_display_scopes_slots_to_the_targeted_entry(demo_bench):
    other = next(e for e in demo_bench.db.eds_list() if e["file"] != "demo_device.eds")
    assert other["display_slots"] == []
    demo_bench.db.eds_set_display(
        "demo_device.eds", [{"label": "x", "idx": "0x2000", "sub": "00"}])
    entries = {e["file"]: e for e in demo_bench.db.eds_list()}
    assert entries["demo_device.eds"]["display_slots"] == [
        {"label": "x", "idx": "0x2000", "sub": "00"}]
    # the untouched entry is unaffected
    assert entries[other["file"]]["display_slots"] == []


def test_first_run_seeds_demo_device_display_slots(tmp_path):
    fresh_dir = tmp_path / "fresh"
    assert not fresh_dir.exists()
    bench = Bench(Db(fresh_dir / "canopen-bench.db"))
    entries = {e["file"]: e for e in bench.db.eds_list()}
    assert entries["DemoDevice.eds"]["display_slots"] == [
        {"label": "m/min", "idx": "0x606C", "sub": "00"},
        {"label": "°C", "idx": "0x2002", "sub": "00"},
    ]


def test_mirror_data_none_without_selection_or_configured_slots(demo_bench):
    connect_and_scan(demo_bench)
    assert demo_bench._mirror_data() is None
    demo_bench.dispatch("dev_toggle", {"node": 1})
    # demo_device.eds (this fixture's own EDS) has no display_slots configured
    assert demo_bench._mirror_data() is None


def test_mirror_data_shows_eds_default_before_any_read(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    demo_bench.db.eds_set_display(
        "demo_device.eds", [{"label": "x", "idx": "0x2000", "sub": "00"}])
    assert demo_bench._mirror_data() == {
        "node": 1, "values": [{"label": "x", "value": "42"}]}


def test_mirror_data_reflects_obj_vals_cache_immediately(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    demo_bench.db.eds_set_display(
        "demo_device.eds", [{"label": "x", "idx": "0x2000", "sub": "00"}])
    demo_bench.dispatch("obj_write", {"idx": "0x2000", "sub": "00", "val": "0x00000063"})
    assert demo_bench._mirror_data() == {
        "node": 1, "values": [{"label": "x", "value": "99"}]}


def test_mirror_refresh_does_a_real_read_with_no_automatic_polling(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    demo_bench.db.eds_set_display(
        "demo_device.eds", [{"label": "x", "idx": "0x2000", "sub": "00"}])
    demo_bench.dispatch("obj_write", {"idx": "0x2000", "sub": "00", "val": "0x00000063"})  # 99

    # bus-level write bypassing obj_vals, like a RAW-row write
    res = demo_bench.bus.sdo_write(1, "0x2000", "00", "0x0000002A")  # 42
    assert res.ok

    # no automatic polling: the mirror still shows the stale cached value
    assert demo_bench._mirror_data() == {
        "node": 1, "values": [{"label": "x", "value": "99"}]}

    demo_bench.dispatch("mirror_refresh", {})

    # after an explicit refresh, the mirror reflects the real bus value
    assert demo_bench._mirror_data() == {
        "node": 1, "values": [{"label": "x", "value": "42"}]}


def test_mirror_data_none_again_after_deselecting_device(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    demo_bench.db.eds_set_display(
        "demo_device.eds", [{"label": "x", "idx": "0x2000", "sub": "00"}])
    assert demo_bench._mirror_data() is not None
    demo_bench.dispatch("dev_toggle", {"node": 1})  # deselect
    assert demo_bench._mirror_data() is None

DEMO_EDS = """\
[FileInfo]
FileName=demo_device.eds
FileVersion=1
FileRevision=1
EDSVersion=4.0

[DeviceInfo]
VendorName=Demo Vendor
VendorNumber=45
ProductName=DEMO_DEV
ProductNumber=100
RevisionNumber=1

[1000]
ParameterName=Device type
ObjectType=0x7
DataType=0x0007
AccessType=ro
DefaultValue=0x00050195

[1018]
ParameterName=Identity
ObjectType=0x9
SubNumber=2

[1018sub0]
ParameterName=Highest sub-index
ObjectType=0x7
DataType=0x0005
AccessType=ro
DefaultValue=1

[1018sub1]
ParameterName=Vendor-ID
ObjectType=0x7
DataType=0x0007
AccessType=ro
DefaultValue=45

[2000]
ParameterName=Demo counter
ObjectType=0x7
DataType=0x0007
AccessType=rw
DefaultValue=42

[2001]
ParameterName=Fixed value
ObjectType=0x7
DataType=0x0006
AccessType=ro
DefaultValue=7

[2010]
ParameterName=Ranged counter
ObjectType=0x7
DataType=0x0007
AccessType=rw
DefaultValue=50
LowLimit=10
HighLimit=100
"""


@pytest.fixture()
def demo_bench(tmp_path):
    bench = Bench(Db(tmp_path / "demo.db"))
    seed_test_registry(bench)
    # only the uploaded EDS should produce demo DUTs — disable the seeds
    for e in bench.db.eds_list():
        bench.db.eds_set_enabled(e["file"], False)
    ok, msg = bench.add_eds_file("demo_device.eds", DEMO_EDS)
    assert ok, msg
    bench.dispatch("set_adapter", {"adapter": "demo"})
    return bench


def test_demo_scan_creates_duts_from_uploaded_eds(demo_bench):
    connect_and_scan(demo_bench)
    assert len(demo_bench.devices) == 2  # two DUTs for the first (only) EDS
    for d in demo_bench.devices:
        assert d["name"] == "DEMO_DEV"
        assert d["eds"] == "demo_device.eds"
        assert d["nmt"] == "Pre-Operational"
    assert demo_bench.devices[0]["sn"] != demo_bench.devices[1]["sn"]


def test_demo_scan_self_heals_by_seeding_the_bundled_demo_device(tmp_path):
    """Regression test: a workspace whose data/ directory already existed
    (so Db.is_first_run didn't fire — e.g. the .db file alone was deleted
    and the app restarted) previously left demo-mode scans permanently
    empty, since none of the seed registry entries have real files on
    disk. _ensure_demo_eds_available (called from _scan_async) self-heals
    this the moment a scan actually needs a device to find."""
    bench = Bench(Db(tmp_path / "empty.db"))  # tmp_path already exists -> is_first_run is False
    assert bench.db.is_first_run is False
    assert "DemoDevice.eds" not in {e["file"] for e in bench.db.eds_list()}
    bench.dispatch("set_adapter", {"adapter": "demo"})
    connect_and_scan(bench)
    assert bench.devices  # self-healed: no longer stuck empty
    assert all(d["eds"] == "DemoDevice.eds" for d in bench.devices)
    assert any('EDS  "DemoDevice.eds" added' in ln["msg"] for ln in bench.logs)


def test_demo_scan_yields_hint_when_demo_device_is_explicitly_disabled(tmp_path):
    """The self-heal never overrides an explicit choice: once DemoDevice.eds
    is a registered entry — even disabled — a scan with nothing else
    enabled still shows the old "upload a file" hint instead of silently
    re-enabling it."""
    bench = Bench(Db(tmp_path / "empty.db"))
    bench.dispatch("set_adapter", {"adapter": "demo"})
    connect_and_scan(bench)
    assert bench.devices  # first scan self-heals and finds the seeded demo device
    for e in bench.db.eds_list():
        bench.db.eds_set_enabled(e["file"], False)

    connect_and_scan(bench)
    assert bench.devices == []
    assert any("upload and enable at least one real EDS file" in ln["msg"] for ln in bench.logs)


def test_demo_mc_verify_scan_also_self_heals(tmp_path):
    """The self-heal lives inside _scan_async, shared by the manual Scan
    button and machine-control's own scan-&-verify — which is what
    actually runs first on Connect whenever MC is active with a saved
    setup, per the real startup sequence — not just the manual path."""
    bench = Bench(Db(tmp_path / "empty.db"))
    bench.mc_ref = {"expected": 1, "session": "", "assignments": {}}  # mc_verify needs an expected state
    bench._adopt_ref()
    bench.dispatch("set_adapter", {"adapter": "demo"})
    bench.dispatch("connect_toggle", {})
    bench.mc["enabled"] = True

    orig = core_mod.SCAN_DELAY_S
    core_mod.SCAN_DELAY_S = 0.02
    try:
        async def go():
            bench.dispatch("mc_verify", {})
            await asyncio.sleep(0.3)
        asyncio.run(go())
    finally:
        core_mod.SCAN_DELAY_S = orig

    assert bench.devices
    assert all(d["eds"] == "DemoDevice.eds" for d in bench.devices)


def test_demo_sdo_read_serves_eds_default(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    demo_bench.dispatch("obj_read", {"idx": "0x2000", "sub": "00"})
    assert demo_bench.obj_vals["0x2000:00"] == "0x0000002A"  # 42 as U32


def test_demo_sdo_write_then_read_round_trip(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    demo_bench.dispatch("obj_write", {"idx": "0x2000", "sub": "00", "val": "0x00000063"})
    demo_bench.obj_vals = {}
    demo_bench.dispatch("obj_read", {"idx": "0x2000", "sub": "00"})
    assert demo_bench.obj_vals["0x2000:00"] == "0x00000063"


def test_demo_sdo_write_to_readonly_object_aborts(demo_bench):
    connect_and_scan(demo_bench)
    res = demo_bench.bus.sdo_write(1, "0x2001", "00", "0x0001")
    assert not res.ok
    assert "0x06010002" in res.abort


def test_demo_sdo_read_of_missing_object_aborts(demo_bench):
    connect_and_scan(demo_bench)
    res = demo_bench.bus.sdo_read(1, "0x3000", "00")
    assert not res.ok
    assert "0x06020000" in res.abort


def test_demo_sdo_read_of_unknown_sub_index_of_known_object_aborts(demo_bench):
    # 0x1018 is defined (SubNumber=2, subs 0 and 1 only) — sub 2 is unknown,
    # so this must be distinguished from an entirely unknown index.
    connect_and_scan(demo_bench)
    res = demo_bench.bus.sdo_read(1, "0x1018", "02")
    assert not res.ok
    assert "0x06090011" in res.abort


def test_demo_sdo_write_of_missing_object_aborts(demo_bench):
    connect_and_scan(demo_bench)
    res = demo_bench.bus.sdo_write(1, "0x3000", "00", "0x01")
    assert not res.ok
    assert "0x06020000" in res.abort


def test_demo_sdo_write_of_unknown_sub_index_of_known_object_aborts(demo_bench):
    connect_and_scan(demo_bench)
    res = demo_bench.bus.sdo_write(1, "0x1018", "02", "0x01")
    assert not res.ok
    assert "0x06090011" in res.abort


def test_demo_identity_specials(demo_bench):
    connect_and_scan(demo_bench)
    assert demo_bench.bus.sdo_read(1, "0x1008", "00").value == "DEMO_DEV"
    assert demo_bench.bus.sdo_read(1, "0x1018", "04").value == demo_bench.devices[0]["sn"]


def test_demo_objects_catalog_is_built_from_the_eds(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    snap = demo_bench.snapshot()
    catalog = snap["objects"]["catalog"]
    manu_rows = {(r[0], r[1]): r for r in catalog["manu"]}
    row = manu_rows[("0x2000", "00")]
    assert row[2] == "Demo counter"
    assert row[3] == "U32"
    assert row[4] == "rw"
    assert row[5] == "0x0000002A"
    comm_rows = {(r[0], r[1]): r for r in catalog["comm"]}
    assert ("0x1000", "00") in comm_rows
    assert ("0x1018", "01") in comm_rows  # record member
    groups = {g["key"]: g for g in snap["objects"]["groups"]}
    assert groups["manu"]["count"] == len(catalog["manu"])


def test_demo_catalog_is_empty_without_selection(demo_bench):
    snap = demo_bench.snapshot()
    # no device selected -> no invented placeholder objects
    assert snap["objects"]["catalog"] == {}
    assert snap["objects"]["groups"] == []


def test_demo_nmt_state_tracked_across_rescan(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    demo_bench.dispatch("nmt", {"cmd": "start"})
    assert demo_bench.devices[0]["nmt"] == "Operational"
    connect_and_scan(demo_bench)  # rescan keeps the bus-side NMT state
    assert demo_bench.devices[0]["nmt"] == "Operational"


def test_demo_heartbeat_frames(demo_bench):
    connect_and_scan(demo_bench)
    frames = demo_bench.bus.poll_frames(4)
    assert frames
    assert all(f.cob_id.startswith("0x70") for f in frames)
    assert "HB" in frames[0].decoded


def _frame_row(frame) -> dict:
    return {"time": "", "dir": frame.direction, "cob": frame.cob_id,
            "len": frame.length, "data": frame.data, "dec": frame.decoded,
            "flag": "", "obj": "", "val": ""}


def test_demo_sdo_read_queues_annotated_request_response_frames(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    demo_bench.bus.sdo_read(1, "0x2000", "00")
    req, resp = demo_bench.bus.poll_frames(2)

    assert req.direction == "TX"
    assert req.cob_id == "0x601"
    assert req.data == "40 00 20 00 00 00 00 00"
    assert req.decoded == "SDO rx node 01"

    assert resp.direction == "RX"
    assert resp.cob_id == "0x581"
    assert resp.decoded == "SDO tx node 01"

    req_row, resp_row = _frame_row(req), _frame_row(resp)
    demo_bench._annotate_sdo(req_row)
    demo_bench._annotate_sdo(resp_row)
    assert "Demo counter" in req_row["obj"]
    assert req_row["val"] == ""  # read request carries no value
    assert "Demo counter" in resp_row["obj"]
    assert resp_row["val"] == "42"


def test_demo_sdo_write_queues_annotated_frames_with_no_value_in_ack(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    demo_bench.bus.sdo_write(1, "0x2000", "00", "0x00000063")
    req, resp = demo_bench.bus.poll_frames(2)

    assert req.direction == "TX"
    assert req.cob_id == "0x601"
    assert resp.direction == "RX"
    assert resp.cob_id == "0x581"
    assert resp.data == "60 00 20 00 00 00 00 00"  # download response: no value

    req_row, resp_row = _frame_row(req), _frame_row(resp)
    demo_bench._annotate_sdo(req_row)
    demo_bench._annotate_sdo(resp_row)
    assert req_row["val"] == "99"  # 0x63 written, decoded decimal
    assert resp_row["val"] == ""  # write ack carries no value


def test_demo_sdo_write_above_high_limit_aborts_and_does_not_store(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    res = demo_bench.bus.sdo_write(1, "0x2010", "00", "0x00000065")  # 101 > 100
    assert not res.ok
    assert res.abort.startswith("0x06090031")
    still = demo_bench.bus.sdo_read(1, "0x2010", "00")
    assert still.value == "0x00000032"  # default (50) unchanged


def test_demo_sdo_write_below_low_limit_aborts_and_does_not_store(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    res = demo_bench.bus.sdo_write(1, "0x2010", "00", "0x00000005")  # 5 < 10
    assert not res.ok
    assert res.abort.startswith("0x06090032")
    still = demo_bench.bus.sdo_read(1, "0x2010", "00")
    assert still.value == "0x00000032"  # default (50) unchanged


def test_demo_sdo_write_within_range_still_succeeds(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    res = demo_bench.bus.sdo_write(1, "0x2010", "00", "0x0000004B")  # 75, in [10,100]
    assert res.ok
    still = demo_bench.bus.sdo_read(1, "0x2010", "00")
    assert still.value == "0x0000004B"


def test_demo_poll_frames_drains_sdo_frames_before_heartbeats(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    demo_bench.bus.sdo_read(1, "0x2000", "00")  # queues 2 SDO frames
    frames = demo_bench.bus.poll_frames(2)
    assert len(frames) == 2
    assert frames[0].cob_id == "0x601"
    assert frames[1].cob_id == "0x581"
    assert all("SDO" in f.decoded for f in frames)


def test_demo_raw_read_of_missing_object_logs_abort_as_emcy0(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    demo_bench.dispatch("raw_update", {"row": 0, "field": "i", "value": "0x3000"})
    demo_bench.dispatch("raw_update", {"row": 0, "field": "s", "value": "00"})
    demo_bench.dispatch("raw_read", {"row": 0})
    entry = demo_bench.logs[-1]
    assert entry["type"] == "emcy0"
    assert "✗ abort" in entry["msg"]
    assert "0x06020000" in entry["msg"]


def test_demo_raw_write_to_readonly_object_logs_abort_as_emcy0(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    demo_bench.dispatch("raw_update", {"row": 0, "field": "i", "value": "0x2001"})
    demo_bench.dispatch("raw_update", {"row": 0, "field": "s", "value": "00"})
    demo_bench.dispatch("raw_update", {"row": 0, "field": "v", "value": "0x0001"})
    demo_bench.dispatch("raw_write", {"row": 0})
    entry = demo_bench.logs[-1]
    assert entry["type"] == "emcy0"
    assert "✗ abort" in entry["msg"]
    assert "0x06010002" in entry["msg"]


def test_demo_raw_read_and_write_success_log_sdo_type_without_abort(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})

    demo_bench.dispatch("raw_update", {"row": 0, "field": "i", "value": "0x2000"})
    demo_bench.dispatch("raw_update", {"row": 0, "field": "s", "value": "00"})
    demo_bench.dispatch("raw_read", {"row": 0})
    read_entry = demo_bench.logs[-1]
    assert read_entry["type"] == "sdo"
    assert "✗" not in read_entry["msg"]
    assert "0x0000002A" in read_entry["msg"]  # 42 as U32

    demo_bench.dispatch("raw_update", {"row": 0, "field": "v", "value": "0x00000063"})
    demo_bench.dispatch("raw_write", {"row": 0})
    write_entry = demo_bench.logs[-1]
    assert write_entry["type"] == "sdo"
    assert "✗" not in write_entry["msg"]
    assert "0x00000063" in write_entry["msg"]


def test_demo_catalog_rows_carry_eds_min_max(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.dispatch("dev_toggle", {"node": 1})
    snap = demo_bench.snapshot()
    catalog = snap["objects"]["catalog"]
    manu_rows = {(r[0], r[1]): r for r in catalog["manu"]}

    ranged_row = manu_rows[("0x2010", "00")]
    assert len(ranged_row) == 8
    assert ranged_row[6] == 10
    assert ranged_row[7] == 100

    plain_row = manu_rows[("0x2000", "00")]
    assert len(plain_row) == 8
    assert plain_row[6] is None
    assert plain_row[7] is None


def test_demo_sdo_no_frames_for_unscanned_node(demo_bench):
    connect_and_scan(demo_bench)
    unscanned_node = max(demo_bench.bus._devices) + 1
    assert unscanned_node not in demo_bench.bus._devices
    res = demo_bench.bus.sdo_read(unscanned_node, "0x2000", "00")
    assert not res.ok
    res_w = demo_bench.bus.sdo_write(unscanned_node, "0x2000", "00", "0x01")
    assert not res_w.ok
    frames = demo_bench.bus.poll_frames(10)
    assert not any(f.cob_id.startswith("0x60") or f.cob_id.startswith("0x58") for f in frames)


# -- lss_assign / send_raw / press_button without hooks (A-03, A-05) --------

def test_lss_assign_renumbers_to_contiguous_range_and_returns_count(demo_bench):
    connect_and_scan(demo_bench)
    count = len(demo_bench.devices)
    assert count == 2  # two DUTs for the fixture's one enabled EDS
    assigned = demo_bench.bus.lss_assign(count)
    assert assigned == count
    assert sorted(demo_bench.bus._devices) == list(range(1, count + 1))


def test_lss_assign_caps_at_the_number_of_found_devices(demo_bench):
    connect_and_scan(demo_bench)
    count = len(demo_bench.devices)
    assigned = demo_bench.bus.lss_assign(count + 5)
    assert assigned == count


def test_lss_assign_returns_zero_when_disconnected(demo_bench):
    connect_and_scan(demo_bench)
    demo_bench.bus.disconnect()
    assert demo_bench.bus.lss_assign(2) == 0


# -- TPDO1 emission from the EDS default mapping (0x1A00) -------------------

@pytest.fixture()
def demo_device_bench(tmp_path):
    """A fresh workspace: the first-run seed installs the real
    canopen_bench/seed/DemoDevice.eds (with its 0x1800/0x1A00 objects) as the sole
    loadable EDS, so scan finds only its DUTs (nodes 1 and 2)."""
    bench = Bench(Db(tmp_path / "fresh" / "canopen-bench.db"))
    assert bench.adapter == "demo"
    return bench


def test_tpdo1_emitted_only_once_operational(demo_device_bench):
    connect_and_scan(demo_device_bench)
    bus = demo_device_bench.bus

    frames = bus.poll_frames(50)
    assert not any(f.decoded.startswith("TxPDO1") for f in frames)  # pre-operational

    bus.nmt("start", None)
    frames = bus.poll_frames(50)
    tpdo = next(f for f in frames if f.decoded.startswith("TxPDO1"))
    assert tpdo.cob_id == "0x181"
    assert tpdo.data == "00 00 00 00 17 00"


def test_tpdo1_reflects_store_override(demo_device_bench):
    connect_and_scan(demo_device_bench)
    bus = demo_device_bench.bus
    bus.nmt("start", None)
    # the store holds hex strings everywhere else (sdo_write payloads, obj_vals) —
    # _parse_int reads them with base 16, so 1500 decimal is written as "0x5DC"
    bus._store[(1, 0x606C, 0)] = "0x5DC"

    frames = bus.poll_frames(50)
    tpdo = next(f for f in frames if f.decoded.startswith("TxPDO1") and f.cob_id == "0x181")
    assert tpdo.data.startswith("DC 05 00 00")


# -- RPDO applied on the device side (0x1400/0x1600 default mapping) -------

def test_apply_rpdo_stores_mapped_value_and_reflects_in_next_tpdo1(demo_device_bench):
    connect_and_scan(demo_device_bench)
    bus = demo_device_bench.bus
    bus.nmt("start", None)  # node 1 operational -> TPDO1 gets emitted

    bus.send_raw(0x201, bytes.fromhex("DC050000"))  # RxPDO1 node 1: 0x60FF = 1500

    assert bus._store[(1, 0x60FF, 0)] == "0x5DC"
    assert bus._store[(1, 0x606C, 0)] == "0x5DC"  # drive model mirrors into actual value

    frames = bus.poll_frames(50)
    tpdo = next(f for f in frames if f.decoded.startswith("TxPDO1") and f.cob_id == "0x181")
    assert tpdo.data.startswith("DC 05 00 00")


def test_apply_rpdo_ignores_unknown_node_and_unmapped_cob(demo_device_bench):
    connect_and_scan(demo_device_bench)
    bus = demo_device_bench.bus

    unknown_node = max(bus._devices) + 1
    bus.send_raw(0x200 + unknown_node, bytes.fromhex("DC050000"))
    assert not any(k[0] == unknown_node for k in bus._store)

    bus.send_raw(0x301, bytes.fromhex("DC050000"))  # RxPDO2 node 1: no 0x1601 mapping
    assert (1, 0x60FF, 0) not in bus._store


def test_sdo_write_to_target_velocity_mirrors_into_actual_value(demo_device_bench):
    connect_and_scan(demo_device_bench)
    bus = demo_device_bench.bus
    res = bus.sdo_write(1, "0x60FF", "0x00", "0x100")
    assert res.ok
    assert bus._store[(1, 0x606C, 0)] == "0x100"


def test_press_button_and_send_raw_without_hooks_are_noops(demo_bench):
    """EdsDemoBus itself has no built-in vendor-protocol simulation any
    more — without hooks installed, both calls must be silent no-ops: no
    exception, no queued raw frames, no session captured."""
    connect_and_scan(demo_bench)
    demo_bench.bus.press_button()
    demo_bench.bus.send_raw(0x780, b"\x01")
    demo_bench.bus.send_raw(0x781, b"\x00\x00\x00\x00\x01\x02\x00\x00")
    assert demo_bench.bus._raw_frames == []
    assert demo_bench.bus.session is None
