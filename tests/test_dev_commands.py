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
    bench.db.eds_set_commands("dut_alpha_v2.eds", [{"key": "su", "label": "SuperUser", "badge": "SU"}])
    bench.dispatch("dev_toggle", {"node": 1})  # node 1 carries dut_alpha_v2.eds

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
    bench.db.eds_set_commands("dut_alpha_v2.eds", [{"key": "su", "label": "SuperUser", "badge": "SU"}])
    # node 1 and 2 both carry dut_alpha_v2.eds (2 DUTs for the first profile)
    assert bench.devices[0]["eds"] == bench.devices[1]["eds"] == "dut_alpha_v2.eds"

    bench.dispatch("dev_cmd", {"key": "su", "node": 1})
    assert bench.devices[0]["cmds"]["su"] is True
    assert bench.devices[1]["cmds"].get("su", False) is False


def test_dev_cmd_skips_devices_whose_eds_lacks_the_key(tmp_path):
    bench = _bench(tmp_path)
    connect_and_scan(bench)
    bench.db.eds_set_commands("dut_alpha_v2.eds", [{"key": "su", "label": "SuperUser", "badge": "SU"}])
    # node 3 carries a different EDS (dut_gamma_v5.eds) with no commands declared
    node3 = next(d for d in bench.devices if d["eds"] == "dut_gamma_v5.eds")
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
    bench.db.eds_set_commands("dut_alpha_v2.eds", [{"key": "su", "label": "SuperUser", "badge": "SU"}])
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("dev_cmd", {"key": "su"})
    assert bench.devices[0]["cmds"]["su"] is True

    connect_and_scan(bench)  # re-scan without disconnecting
    dev = next(d for d in bench.devices if d["node"] == 1)
    assert dev["cmds"]["su"] is True


def test_dev_cmd_write_spec_sdo_writes_on_off(tmp_path):
    bench = _bench(tmp_path)
    connect_and_scan(bench)
    bench.db.eds_set_commands("dut_alpha_v2.eds", [
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
    bench.db.eds_set_commands("dut_alpha_v2.eds", [
        {"key": "w", "label": "W", "write": {"index": "0x1000", "sub": "00", "on": 1, "off": 0}},
    ])
    bench.dispatch("dev_toggle", {"node": 1})

    bench.dispatch("dev_cmd", {"key": "w"})
    dev = next(d for d in bench.devices if d["node"] == 1)
    assert dev["cmds"].get("w", False) is False  # write to a ro object aborts: state unchanged
    abort_log = next(log for log in reversed(bench.logs) if "abort" in log["msg"])
    assert abort_log["type"] == "emcy0"
    assert "W node 01" in abort_log["msg"]


def test_a_boot_up_switches_off_what_the_device_had_on(tmp_path):
    """What a command switched on lives in the device's RAM, and a restart
    ends it — whatever made the restart, the device says so with a boot-up.
    A badge still saying "on" for a device that forgot is how a write gets
    refused for no visible reason. Only the device that booted is cleared."""
    bench = _bench(tmp_path)
    connect_and_scan(bench)
    bench.db.eds_set_commands("dut_alpha_v2.eds", [{"key": "su", "label": "SuperUser", "badge": "SU"}])
    bench.dispatch("dev_cmd", {"key": "su", "node": 1})
    bench.dispatch("dev_cmd", {"key": "su", "node": 2})

    bench.bus.queue_raw(0x701, b"\x00")  # node 1 has just started
    bench._drain_frames()

    node1 = next(d for d in bench.devices if d["node"] == 1)
    node2 = next(d for d in bench.devices if d["node"] == 2)
    assert node1["cmds"]["su"] is False
    assert node2["cmds"]["su"] is True
    assert bench.logs[-1]["msg"] == "CMD  SuperUser off → node 01 (the device restarted)"


def test_a_heartbeat_is_not_a_restart(tmp_path):
    """The heartbeat has the boot-up's COB-ID; only the single 00 byte says
    "I just started". Reading every heartbeat as a boot-up would switch a
    command off the moment after it was switched on."""
    bench = _bench(tmp_path)
    connect_and_scan(bench)
    bench.db.eds_set_commands("dut_alpha_v2.eds", [{"key": "su", "label": "SuperUser", "badge": "SU"}])
    bench.dispatch("dev_cmd", {"key": "su", "node": 1})

    bench.bus.queue_raw(0x701, b"\x05")  # operational
    bench._drain_frames()

    node1 = next(d for d in bench.devices if d["node"] == 1)
    assert node1["cmds"]["su"] is True
    assert not [log for log in bench.logs if "CMD " in log["msg"] and " off " in log["msg"]]


def test_a_reset_from_the_bench_switches_off_at_once(tmp_path):
    """The demo bus does not answer a reset with a boot-up, and a real
    device sends it only a moment later — a badge that waited for it would
    show "on" for a device the operator has just reset. The reset the bench
    sends itself clears the state right away, with nothing received yet."""
    bench = _bench(tmp_path)
    connect_and_scan(bench)
    bench.db.eds_set_commands("dut_alpha_v2.eds", [{"key": "su", "label": "SuperUser", "badge": "SU"}])
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("dev_cmd", {"key": "su"})
    node1 = next(d for d in bench.devices if d["node"] == 1)
    assert node1["cmds"]["su"] is True

    bench.dispatch("nmt", {"cmd": "reset"})  # no boot-up queued, nothing drained

    assert node1["cmds"]["su"] is False
    # cause before effect: the reset is logged, then what it switched off
    assert bench.logs[-2]["msg"].startswith("NMT  reset node")
    assert bench.logs[-1]["msg"] == "CMD  SuperUser off → node 01 (reset node)"


def test_a_state_change_that_is_no_restart_leaves_the_commands_on(tmp_path):
    """Start, pre-operational and stop change what a device does on the
    bus, not what it remembers — a command switched on stays on through
    them, from the Devices box and from the device's own menu alike."""
    bench = _bench(tmp_path)
    connect_and_scan(bench)
    bench.db.eds_set_commands("dut_alpha_v2.eds", [{"key": "su", "label": "SuperUser", "badge": "SU"}])
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("dev_cmd", {"key": "su"})
    node1 = next(d for d in bench.devices if d["node"] == 1)

    for cmd in ("start", "preop", "stop"):
        bench.dispatch("nmt", {"cmd": cmd})
    for what in ("op", "preop"):
        bench.dispatch("dev_menu", {"node": 1, "what": what})

    assert node1["cmds"]["su"] is True
    assert not [log for log in bench.logs if "CMD " in log["msg"] and " off " in log["msg"]]


def test_a_restart_from_the_device_menu_switches_off_too(tmp_path):
    """The device's own menu sends resets as well, reset communication
    among them, and a device that was reset from there has forgotten just
    the same."""
    bench = _bench(tmp_path)
    connect_and_scan(bench)
    bench.db.eds_set_commands("dut_alpha_v2.eds", [{"key": "su", "label": "SuperUser", "badge": "SU"}])
    bench.dispatch("dev_cmd", {"key": "su", "node": 2})
    node2 = next(d for d in bench.devices if d["node"] == 2)
    assert node2["cmds"]["su"] is True

    bench.dispatch("dev_menu", {"node": 2, "what": "resetcomm"})
    assert node2["cmds"]["su"] is False
    assert "(reset communication)" in bench.logs[-1]["msg"]

    bench.dispatch("dev_cmd", {"key": "su", "node": 2})
    assert node2["cmds"]["su"] is True
    bench.dispatch("dev_menu", {"node": 2, "what": "restart"})
    assert node2["cmds"]["su"] is False
    assert "(reset node)" in bench.logs[-1]["msg"]


def test_a_boot_up_says_nothing_when_nothing_was_on(tmp_path):
    """Every device boots now and then, and most of the time no command is
    on: the log line is for the restart that changed what the screen shows,
    not for each one."""
    bench = _bench(tmp_path)
    connect_and_scan(bench)
    bench.db.eds_set_commands("dut_alpha_v2.eds", [{"key": "su", "label": "SuperUser", "badge": "SU"}])

    bench.bus.queue_raw(0x701, b"\x00")
    bench._drain_frames()

    assert not [log for log in bench.logs if "CMD " in log["msg"]]
