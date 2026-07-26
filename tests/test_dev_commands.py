"""Per-EDS device commands (act_dev_cmd / db.eds_set_commands) — replaces the
removed hardcoded SuperUser toggle (act_su_toggle / act_dev_menu "su")."""
from __future__ import annotations

from conftest import connect_and_scan, write_seed_eds_files

from canopen_bench.core import Bench
from canopen_bench.db import Db


def _bench(tmp_path) -> Bench:
    b = Bench(Db(tmp_path / "test.db"))
    write_seed_eds_files(b)
    return b


def test_dev_cmd_toggles_selection_and_logs(tmp_path):
    bench = _bench(tmp_path)
    connect_and_scan(bench)
    bench.db.eds_set_commands("dut2_800_v14.eds", [{"key": "su", "label": "SuperUser", "badge": "SU"}])
    bench.dispatch("dev_toggle", {"node": 1})  # node 1 carries dut2_800_v14.eds

    bench.dispatch("dev_cmd", {"key": "su"})
    dev = next(d for d in bench.devices if d["node"] == 1)
    assert dev["cmds"]["su"] is True
    assert "CMD  SuperUser on → node 01" in bench.logs[-1]["msg"]

    bench.dispatch("dev_cmd", {"key": "su"})
    assert dev["cmds"]["su"] is False
    assert "CMD  SuperUser off → node 01" in bench.logs[-1]["msg"]


def test_dev_cmd_via_menu_targets_only_that_node(tmp_path):
    bench = _bench(tmp_path)
    connect_and_scan(bench)
    bench.db.eds_set_commands("dut2_800_v14.eds", [{"key": "su", "label": "SuperUser", "badge": "SU"}])
    # node 1 and 2 both carry dut2_800_v14.eds (2 DUTs for the first profile)
    assert bench.devices[0]["eds"] == bench.devices[1]["eds"] == "dut2_800_v14.eds"

    bench.dispatch("dev_cmd", {"key": "su", "node": 1})
    assert bench.devices[0]["cmds"]["su"] is True
    assert bench.devices[1]["cmds"].get("su", False) is False


def test_dev_cmd_skips_devices_whose_eds_lacks_the_key(tmp_path):
    bench = _bench(tmp_path)
    connect_and_scan(bench)
    bench.db.eds_set_commands("dut2_800_v14.eds", [{"key": "su", "label": "SuperUser", "badge": "SU"}])
    # node 3 carries a different EDS (dut3_ht_v03.eds) with no commands declared
    node3 = next(d for d in bench.devices if d["eds"] == "dut3_ht_v03.eds")
    bench.dispatch("dev_toggle", {"node": node3["node"]})

    n_logs = len(bench.logs)
    bench.dispatch("dev_cmd", {"key": "su"})
    assert node3["cmds"] == {}
    assert len(bench.logs) == n_logs  # clean no-op: nothing hit, nothing logged


def test_dev_cmd_unknown_key_is_a_clean_noop(tmp_path):
    bench = _bench(tmp_path)
    connect_and_scan(bench)
    bench.dispatch("dev_toggle", {"node": 1})
    n_logs = len(bench.logs)
    bench.dispatch("dev_cmd", {"key": "does-not-exist"})
    assert len(bench.logs) == n_logs
    assert bench.devices[0]["cmds"] == {}


def test_dev_cmd_state_survives_a_rescan(tmp_path):
    bench = _bench(tmp_path)
    connect_and_scan(bench)
    bench.db.eds_set_commands("dut2_800_v14.eds", [{"key": "su", "label": "SuperUser", "badge": "SU"}])
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("dev_cmd", {"key": "su"})
    assert bench.devices[0]["cmds"]["su"] is True

    connect_and_scan(bench)  # re-scan without disconnecting
    dev = next(d for d in bench.devices if d["node"] == 1)
    assert dev["cmds"]["su"] is True


def test_dev_cmd_write_spec_sdo_writes_on_off(tmp_path):
    bench = _bench(tmp_path)
    connect_and_scan(bench)
    bench.db.eds_set_commands("dut2_800_v14.eds", [
        {"key": "w", "label": "W", "write": {"index": "0x2000", "sub": "00", "on": 1, "off": 0}},
    ])
    bench.dispatch("dev_toggle", {"node": 1})

    bench.dispatch("dev_cmd", {"key": "w"})
    dev = next(d for d in bench.devices if d["node"] == 1)
    assert dev["cmds"]["w"] is True
    res = bench.bus.sdo_read(1, "0x2000", "00")
    assert res.ok
    assert res.value == "1"

    bench.dispatch("dev_cmd", {"key": "w"})
    assert dev["cmds"]["w"] is False
    res = bench.bus.sdo_read(1, "0x2000", "00")
    assert res.ok
    assert res.value == "0"


def test_dev_cmd_write_abort_keeps_state_and_logs_emcy(tmp_path):
    bench = _bench(tmp_path)
    connect_and_scan(bench)
    bench.db.eds_set_commands("dut2_800_v14.eds", [
        {"key": "w", "label": "W", "write": {"index": "0x1000", "sub": "00", "on": 1, "off": 0}},
    ])
    bench.dispatch("dev_toggle", {"node": 1})

    bench.dispatch("dev_cmd", {"key": "w"})
    dev = next(d for d in bench.devices if d["node"] == 1)
    assert dev["cmds"].get("w", False) is False  # write to a ro object aborts: state unchanged
    abort_log = next(log for log in reversed(bench.logs) if "abort" in log["msg"])
    assert abort_log["type"] == "emcy0"
    assert "W node 01" in abort_log["msg"]
