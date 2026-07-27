"""Step executor: real YAML test cases running against the demo bus.

The demo DUTs come from the seed EDS written by conftest (0x2040:01 =
0x00260001, 0x2050 = 0x00), so expectations here exercise real SDO reads
through the BusInterface — no simulated verdicts involved.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from conftest import connect_and_scan, write_seed_eds_files

from canopen_bench.core import Bench
from canopen_bench.db import Db

PASS_TC = """\
id: "0001"
name: "identity readable"
steps:
  - nmt: start
  - wait_for: {heartbeat: operational, timeout: 2.0}
  - sdo_read: {index: "0x2040", sub: "0x01", expect: "0x260001"}
  - sdo_read: {index: "0x2050", sub: "0x00", mask: "0x04", expect: "0x00"}
"""

FAIL_TC = """\
id: "0002"
name: "wrong expectation"
steps:
  - sdo_read: {index: "0x2050", sub: "0x00", expect: "0x01"}
"""

SKIP_TC = """\
id: "0003"
name: "unmet precondition"
preconditions:
  - sdo_read: {index: "0x3000", sub: "0x00"}
steps:
  - log: "never reached"
"""

MANUAL_TC = """\
id: "0004"
name: "operator step"
steps:
  - manual: {text: "flip aux supply", timeout: 0.3}
  - sdo_read: {index: "0x1000", sub: "0x00"}
"""

ABORT_TC = """\
id: "0005"
name: "expected abort"
steps:
  - sdo_read: {index: "0x3000", sub: "0x00", expect_abort: "0x06020000"}
"""


@pytest.fixture()
def tc_bench(tmp_path):
    bench = Bench(Db(tmp_path / "t.db"))
    write_seed_eds_files(bench)
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    for name, text in (("TC0001_pass.yaml", PASS_TC), ("TC0002_fail.yaml", FAIL_TC),
                       ("TC0003_skip.yaml", SKIP_TC), ("TC0004_manual.yaml", MANUAL_TC),
                       ("TC0005_abort.yaml", ABORT_TC)):
        (tc_dir / name).write_text(text)
    bench.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})
    connect_and_scan(bench)
    bench.dispatch("dev_toggle", {"node": 1})
    return bench


def run_selected(bench: Bench, ids: set[str], during=None, timeout: float = 5.0) -> None:
    """Dispatch run_start inside an event loop and wait for the executor task."""
    bench.test_sel = set(ids)

    async def go():
        bench.dispatch("run_start", {})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while bench.running and loop.time() < deadline:
            if during:
                during(bench)  # must be idempotent — called every poll
            await asyncio.sleep(0.02)
    asyncio.run(go())
    assert not bench.running, "executor did not finish in time"


def test_catalog_comes_from_folder(tc_bench):
    snap = tc_bench.snapshot()
    ids = [row[0] for row in snap["tests"]["catalog"]]
    assert ids == ["0001", "0002", "0003", "0004", "0005"]
    assert snap["tests"]["lastRes"] == {}


def test_pass_and_expected_abort(tc_bench):
    run_selected(tc_bench, {"0001", "0005"})
    assert tc_bench.results == {"0001": "PASS", "0005": "PASS"}
    assert tc_bench.reports[0]["score"] == "2/2"
    assert tc_bench.reports[0]["ok"] is True


def test_fail_stops_run_when_stop_on_err(tc_bench):
    tc_bench.stop_on_err = True
    run_selected(tc_bench, {"0002", "0001"})  # order: 0001, 0002
    assert tc_bench.results["0002"] == "FAIL"
    assert any("stop on error" in ln["msg"] for ln in tc_bench.logs)


def test_precondition_failure_skips(tc_bench):
    tc_bench.stop_on_err = True
    run_selected(tc_bench, {"0003"})
    assert tc_bench.results == {"0003": "SKIP"}  # SKIP does not trip stop_on_err
    assert any("SKIPPED" in ln["msg"] and "precondition" in ln["msg"] for ln in tc_bench.logs)
    assert tc_bench.reports[0]["ok"] is False  # 0/1 passed


def test_manual_confirm_lets_test_pass(tc_bench):
    def confirm(bench):
        if bench.manual_prompt:
            bench.dispatch("manual_confirm", {})
    run_selected(tc_bench, {"0004"}, during=confirm)
    assert tc_bench.results == {"0004": "PASS"}


def test_manual_timeout_is_error(tc_bench):
    tc_bench.stop_on_err = False
    run_selected(tc_bench, {"0004"})  # nobody confirms; step timeout 0.3s
    assert tc_bench.results == {"0004": "ERROR"}
    assert any("manual step timed out" in ln["msg"] for ln in tc_bench.logs)


def test_run_without_selected_device_refuses(tc_bench):
    tc_bench.dispatch("dev_toggle", {"node": 1})  # deselect
    tc_bench.test_sel = {"0001"}
    tc_bench.dispatch("run_start", {})
    assert not tc_bench.running
    assert any("no target device" in ln["msg"] for ln in tc_bench.logs)


def test_progress_is_published_during_run(tc_bench):
    seen: list[dict] = []

    async def notify():  # the executor pushes state after every step
        if tc_bench.run_prog:
            seen.append(dict(tc_bench.run_prog))
    tc_bench.set_notifier(notify)
    run_selected(tc_bench, {"0001"})
    assert seen and seen[0] == {"tid": "0001", "step": 1, "of": 4, "text": "NMT start"}
    assert tc_bench.run_prog is None  # cleared after the run


# -- format v2 VM behaviour: registers, jumps, arithmetic (docs/ablaeufe/testfall-format.md) --

LOOP_TC = """\
id: "0006"
name: "count to 3 via a loop"
steps:
  - mov: {to: R1, value: 0}
  - label: loop
  - add: {to: R1, value: 1}
  - jump_lt: {a: R1, b: 3, to: loop}
  - jump_eq: {a: R1, b: 3, to: ok}
  - fail: "count wrong"
  - label: ok
  - end:
"""

DATA_PATH_TC = """\
id: "0007"
name: "register data path through real SDO"
steps:
  - sdo_read: {index: "0x2040", sub: "0x01", into: R3}
  - sdo_write: {index: "0x2000", sub: "0x00", value: R3, size: 4}
  - sdo_read: {index: "0x2000", sub: "0x00", expect: "0x260001"}
"""

FAIL_REASON_TC = """\
id: "0008"
name: "explicit fail with reason"
steps:
  - fail: "deliberately failed for the reason-logging test"
"""

END_STOPS_TC = """\
id: "0009"
name: "end stops execution before later steps"
steps:
  - end:
  - fail: "not reached"
"""

ENDLESS_LOOP_TC = """\
id: "0010"
name: "endless loop hits the step guard"
steps:
  - label: spin
  - jump: spin
"""


def _add_tc(bench: Bench, filename: str, text: str) -> None:
    """Drop another test-case file into the already-configured TC folder and
    reload the catalog — same folder + dispatch pattern as tc_bench itself,
    without disturbing the fixture's own file set or its catalog-shape test."""
    Path(bench.paths["tc"]).joinpath(filename).write_text(text)
    bench.dispatch("tc_rescan", {})


def test_loop_with_registers_and_jumps(tc_bench):
    _add_tc(tc_bench, "TC0006_loop.yaml", LOOP_TC)
    run_selected(tc_bench, {"0006"})
    assert tc_bench.results == {"0006": "PASS"}


def test_register_data_path_through_real_sdo(tc_bench):
    _add_tc(tc_bench, "TC0007_data_path.yaml", DATA_PATH_TC)
    run_selected(tc_bench, {"0007"})
    assert tc_bench.results == {"0007": "PASS"}


def test_fail_reports_reason_in_log(tc_bench):
    _add_tc(tc_bench, "TC0008_fail_reason.yaml", FAIL_REASON_TC)
    run_selected(tc_bench, {"0008"})
    assert tc_bench.results == {"0008": "FAIL"}
    assert any("deliberately failed for the reason-logging test" in ln["msg"] for ln in tc_bench.logs)


def test_end_stops_execution_before_later_steps(tc_bench):
    _add_tc(tc_bench, "TC0009_end.yaml", END_STOPS_TC)
    run_selected(tc_bench, {"0009"})
    assert tc_bench.results == {"0009": "PASS"}  # the fail step after `end` never runs


def test_endless_loop_hits_step_limit_guard(tc_bench):
    _add_tc(tc_bench, "TC0010_endless.yaml", ENDLESS_LOOP_TC)
    run_selected(tc_bench, {"0010"})  # all-local steps (label/jump) — 10000 iterations, still fast
    assert tc_bench.results == {"0010": "ERROR"}
    assert any("step limit exceeded" in ln["msg"] for ln in tc_bench.logs)


ON_TIMEOUT_TC = """\
id: "0011"
name: "wait_for on_timeout jumps over a fail step"
steps:
  - wait_for: {heartbeat: operational, timeout: 0.2, on_timeout: skipped}
  - fail: "should have been jumped over"
  - label: skipped
  - end:
"""


def test_wait_for_on_timeout_jumps_instead_of_failing(tc_bench):
    # node 1 stays Pre-Operational (nobody sent NMT start), so the heartbeat
    # wait times out and on_timeout sends the VM to "skipped", past the fail.
    _add_tc(tc_bench, "TC0011_on_timeout.yaml", ON_TIMEOUT_TC)
    run_selected(tc_bench, {"0011"})
    assert tc_bench.results == {"0011": "PASS"}


LSS_ASSIGN_TC = """\
id: "0012"
name: "lss_assign renumbers and reports the assigned count into a register"
steps:
  - lss_assign: {count: 2, into: R1}
  - jump_eq: {a: R1, b: 2, to: ok}
  - fail: "lss_assign did not report the expected count"
  - label: ok
  - end:
"""


def test_lss_assign_reports_assigned_count_into_register(tc_bench):
    # tc_bench's demo bus scans 3 devices (dut_alpha_v2 x2, dut_gamma_v5 x1)
    # at nodes 1..3; lss_assign(2) renumbers the first two into 1..2 and
    # reports 2 assigned.
    _add_tc(tc_bench, "TC0012_lss_assign.yaml", LSS_ASSIGN_TC)
    run_selected(tc_bench, {"0012"})
    assert tc_bench.results == {"0012": "PASS"}


# -- wait_for list-form cob/data: races several (COB, prefix) pairs at once --

WAIT_FOR_LIST_TC = """\
id: "0013"
name: "wait_for races two cobs and reports the winning index"
steps:
  - wait_for: {cob: ["0x700", "0x783"], data: ["", "02"], timeout: 2.0, into: R5}
  - jump_eq: {a: R5, b: 1, to: ok}
  - fail: "wrong index reported"
  - label: ok
  - end:
"""


def test_wait_for_list_form_reports_index_of_matching_pair(tc_bench):
    # only the second pair's COB/prefix ever arrives — the first (0x700)
    # never does, proving the wait actually raced both, not just the first.
    _add_tc(tc_bench, "TC0013_wait_for_list.yaml", WAIT_FOR_LIST_TC)
    tc_bench.bus.queue_raw(0x783, b"\x02\x00")
    run_selected(tc_bench, {"0013"})
    assert tc_bench.results == {"0013": "PASS"}


WAIT_FOR_LIST_TIMEOUT_TC = """\
id: "0014"
name: "wait_for list-form on_timeout fires when neither cob matches"
steps:
  - wait_for: {cob: ["0x700", "0x783"], data: ["", "02"], timeout: 0.2, on_timeout: skipped}
  - fail: "should have been jumped over"
  - label: skipped
  - end:
"""


def test_wait_for_list_form_on_timeout_fires_when_neither_cob_matches(tc_bench):
    _add_tc(tc_bench, "TC0014_wait_for_list_timeout.yaml", WAIT_FOR_LIST_TIMEOUT_TC)
    run_selected(tc_bench, {"0014"})
    assert tc_bench.results == {"0014": "PASS"}


# -- sdo_write expect_abort: mirrors sdo_read's expect_abort handling --

SDO_WRITE_EXPECT_ABORT_MATCH_TC = """\
id: "0015"
name: "sdo_write expected abort matches"
steps:
  - sdo_write: {index: "0x2040", sub: "0x01", value: "0x01", expect_abort: "0x06010002"}
"""

SDO_WRITE_EXPECT_ABORT_BUT_WROTE_OK_TC = """\
id: "0016"
name: "sdo_write expected abort but write succeeded"
steps:
  - sdo_write: {index: "0x2000", sub: "0x00", value: "0x01", expect_abort: "0x06010002"}
"""

SDO_WRITE_EXPECT_ABORT_WRONG_CODE_TC = """\
id: "0017"
name: "sdo_write expected abort but got a different code"
steps:
  - sdo_write: {index: "0x2040", sub: "0x01", value: "0x01", expect_abort: "0x06020000"}
"""


def test_sdo_write_expected_abort_matches_is_pass(tc_bench):
    # 0x2040:01 is read-only per the seed EDS, so the write aborts with
    # 0x06010002 — exactly the expected code.
    _add_tc(tc_bench, "TC0015_sdo_write_expect_abort.yaml", SDO_WRITE_EXPECT_ABORT_MATCH_TC)
    run_selected(tc_bench, {"0015"})
    assert tc_bench.results == {"0015": "PASS"}


def test_sdo_write_expected_abort_but_write_succeeds_is_fail(tc_bench):
    # 0x2000 is writable, so the write actually succeeds despite expect_abort.
    _add_tc(tc_bench, "TC0016_sdo_write_expect_abort_wrote_ok.yaml",
            SDO_WRITE_EXPECT_ABORT_BUT_WROTE_OK_TC)
    run_selected(tc_bench, {"0016"})
    assert tc_bench.results == {"0016": "FAIL"}
    assert any("expected abort" in ln["msg"] and "wrote ok" in ln["msg"] for ln in tc_bench.logs)


def test_sdo_write_expected_abort_wrong_code_is_fail(tc_bench):
    # 0x2040:01 aborts with 0x06010002 (read-only), not the 0x06020000 expected.
    _add_tc(tc_bench, "TC0017_sdo_write_expect_abort_wrong_code.yaml",
            SDO_WRITE_EXPECT_ABORT_WRONG_CODE_TC)
    run_selected(tc_bench, {"0017"})
    assert tc_bench.results == {"0017": "FAIL"}


SDO_WRITE_PLAIN_ABORT_TC = """\
id: "0018"
name: "sdo_write without expect_abort still fails unconditionally on abort"
steps:
  - sdo_write: {index: "0x2040", sub: "0x01", value: "0x01"}
"""


def test_sdo_write_without_expect_abort_still_fails_on_abort(tc_bench):
    # Regression: adding expect_abort support must not change the plain,
    # no-expect_abort behaviour — any abort is still an unconditional FAIL.
    _add_tc(tc_bench, "TC0018_sdo_write_plain_abort.yaml", SDO_WRITE_PLAIN_ABORT_TC)
    run_selected(tc_bench, {"0018"})
    assert tc_bench.results == {"0018": "FAIL"}
