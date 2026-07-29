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
from canopen_bench.plugin import BenchPlugin

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


# -- primitives that exist so foreign test suites can be translated ----------
# A test bench is not the only tool a device family has ever had. These
# steps come from what such a suite needs to say — a second device on the
# bus, an EMCY that arrived a moment ago, "not applicable to this variant"
# — and each is here because a case could not be expressed without it.

MULTI_NODE_TC = """\
id: "0010"
name: "a second device on the bus"
steps:
  - sdo_read: {index: "0x2040", sub: "0x01", expect: "0x260001"}
  - sdo_read: {index: "0x2040", sub: "0x01", node: 2, into: R5}
  - jump_ne: {a: R5, b: 0, to: other_answered}
  - fail: "node 2 answered nothing"
  - label: other_answered
"""

SKIP_TC_MID = """\
id: "0011"
name: "not applicable to this device"
steps:
  - sdo_read: {index: "0x2050", sub: "0x00", into: R0}
  - jump_eq: {a: R0, b: "0x99", to: applicable}
  - skip: "variant not covered by this case"
  - label: applicable
  - fail: "should not get here"
"""

MATH_TC = """\
id: "0012"
name: "the arithmetic a translated case needs"
steps:
  - mov: {to: R0, value: 5}
  - mul: {to: R0, value: "0x100"}
  - add: {to: R0, value: 3}
  - xor: {to: R0, value: 1}
  - div: {to: R0, value: 2}
  - jump_ge: {a: R0, b: "0x281", to: big_enough}
  - fail: "arithmetic went wrong"
  - label: big_enough
  - jump_le: {a: R0, b: "0x281", to: exact}
  - fail: "arithmetic went wrong the other way"
  - label: exact
"""

DIV0_TC = """\
id: "0013"
name: "division by a value the device supplied"
steps:
  - mov: {to: R1, value: 0}
  - div: {to: R0, value: R1}
"""


def test_a_step_can_target_another_node(tc_bench):
    """The case is about one DUT, but the bus has more devices on it — a
    second feeder consuming yarn, a gateway. Without a per-step node such a
    case cannot be written down at all."""
    _add_tc(tc_bench, "TC0010_multi.yaml", MULTI_NODE_TC)
    run_selected(tc_bench, {"0010"})
    assert tc_bench.results == {"0010": "PASS"}


def test_skip_ends_the_case_as_skipped_not_failed(tc_bench):
    """"Does not apply here" is not a failure. A case that says so must not
    turn a full test run red."""
    _add_tc(tc_bench, "TC0011_skip.yaml", SKIP_TC_MID)
    run_selected(tc_bench, {"0011"})
    assert tc_bench.results == {"0011": "SKIP"}


def test_the_full_arithmetic_set(tc_bench):
    _add_tc(tc_bench, "TC0012_math.yaml", MATH_TC)
    run_selected(tc_bench, {"0012"})
    assert tc_bench.results == {"0012": "PASS"}


def test_division_by_zero_is_an_error_not_a_crash(tc_bench):
    """The divisor can come from a register the device filled, so this is a
    run-time outcome, not a broken file — ERROR, with the register named."""
    _add_tc(tc_bench, "TC0013_div0.yaml", DIV0_TC)
    run_selected(tc_bench, {"0013"})
    assert tc_bench.results == {"0013": "ERROR"}


# -- EMCY -------------------------------------------------------------------

EMCY_TC = """\
id: "0014"
name: "an emergency arrived"
steps:
  - expect_emcy: {code: "0x7100", timeout: 0.2}
"""

EMCY_MASK_TC = """\
id: "0015"
name: "only the manufacturer part is known"
steps:
  - expect_emcy: {code: "0x0071", mask: "0x00FF", timeout: 0.2}
"""

EMCY_CLEAR_TC = """\
id: "0016"
name: "cleared, so it must not count any more"
steps:
  - emcy_clear:
  - expect_emcy: {code: "0x7100", timeout: 0.2}
"""


def _emcy(bench: Bench, node: int = 1, code: int = 0x7100) -> None:
    """Feed one EMCY frame through the same path the bus takes."""
    row = {"cls": "EMCY", "node": node, "data": f"{code & 0xFF:02X} {code >> 8:02X} 00",
           "obj": "", "val": ""}
    bench._annotate_emcy(row)


def test_an_emcy_that_already_arrived_counts(tc_bench):
    """The device sends when it is ready, not when a step happens to be
    waiting. A check that only looks forward turns timing into a failure."""
    _emcy(tc_bench)
    _add_tc(tc_bench, "TC0014_emcy.yaml", EMCY_TC)
    run_selected(tc_bench, {"0014"})
    assert tc_bench.results == {"0014": "PASS"}


def test_no_emcy_fails_after_the_timeout(tc_bench):
    _add_tc(tc_bench, "TC0014_emcy.yaml", EMCY_TC)
    run_selected(tc_bench, {"0014"})
    assert tc_bench.results == {"0014": "FAIL"}


def test_a_mask_matches_the_manufacturer_part_only(tc_bench):
    """Which class byte a device puts in front of its own error number is
    not always documented — the mask is how a case says "this part I know"."""
    _emcy(tc_bench, code=0x7100 | 0x71)
    _add_tc(tc_bench, "TC0015_emcy_mask.yaml", EMCY_MASK_TC)
    run_selected(tc_bench, {"0015"})
    assert tc_bench.results == {"0015": "PASS"}


def test_emcy_clear_forgets_what_came_before(tc_bench):
    _emcy(tc_bench)
    _add_tc(tc_bench, "TC0016_emcy_clear.yaml", EMCY_CLEAR_TC)
    run_selected(tc_bench, {"0016"})
    assert tc_bench.results == {"0016": "FAIL"}


# -- the bench's own equipment ----------------------------------------------

PSU_TC = """\
id: "0017"
name: "walk the supply down and back"
tools: [PSU]
steps:
  - psu: {ch: 2, volt: 26}
  - psu: {output: on}
  - sdo_read: {index: "0x2040", sub: "0x01"}
  - psu: {ch: 2, volt: 57.5, curr: 2}
  - psu: {output: off}
"""


def test_a_case_can_drive_the_power_supply(tc_bench):
    """The reason this exists: an under-voltage case that has to ask an
    operator to turn a knob is not an automated case any more."""
    from conftest import FakeSupplyPort  # noqa: PLC0415  (test-only helper)
    port = FakeSupplyPort()
    tc_bench._psu_opener = lambda device, baud, timeout: port
    assert tc_bench._psu_connect("COM6")
    _add_tc(tc_bench, "TC0017_psu.yaml", PSU_TC)
    run_selected(tc_bench, {"0017"})
    assert tc_bench.results == {"0017": "PASS"}
    # what the case asked for, without the driver's own housekeeping
    # (identity, settings readback, the one-off measurement probe)
    commands = [w for w in port.written
                if ";V " in w or ";C " in w or w.startswith("EX")]
    assert commands == ["SEL 2;V 26.00", "EX 1", "SEL 2;V 57.50",
                        "SEL 2;C 2.000", "EX 0"]


def test_without_a_supply_the_case_errors_rather_than_blaming_the_device(tc_bench):
    """No instrument is a bench problem. FAIL would read as "the DUT did
    the wrong thing", which is a different message entirely."""
    _add_tc(tc_bench, "TC0017_psu.yaml", PSU_TC)
    run_selected(tc_bench, {"0017"})
    assert tc_bench.results == {"0017": "ERROR"}


# -- sdo_read expect/mask as a register reference (docs/ablaeufe/testfall-format.md:49) --
# A case that derives what it expects — read the variant, work out the
# screen code, then compare — writes `expect: R<n>` / `mask: R<n>`. Before
# the fix `_judge_read` got the literal string "R1", which could never equal
# a numeric SDO value, so such a case could only ever FAIL.

EXPECT_FROM_REGISTER_PASS_TC = """\
id: "0020"
name: "expect resolves from a computed register"
steps:
  - mov: {to: R1, value: "0x26"}
  - mul: {to: R1, value: "0x10000"}
  - add: {to: R1, value: 1}
  - sdo_read: {index: "0x2040", sub: "0x01", expect: R1}
"""

EXPECT_FROM_REGISTER_FAIL_TC = """\
id: "0021"
name: "expect resolves from a computed register but the arithmetic is wrong"
steps:
  - mov: {to: R1, value: "0x26"}
  - mul: {to: R1, value: "0x10000"}
  - add: {to: R1, value: 2}
  - sdo_read: {index: "0x2040", sub: "0x01", expect: R1}
"""

MASK_FROM_REGISTER_TC = """\
id: "0022"
name: "mask resolves from a register too"
steps:
  - mov: {to: R2, value: "0xFF"}
  - sdo_read: {index: "0x2040", sub: "0x01", mask: R2, expect: "0x01"}
"""

EXPECT_R0_SELF_COMPARE_TC = """\
id: "0023"
name: "expect R0 compares against what the device answers, not against itself"
steps:
  - mov: {to: R0, value: "0x99"}
  - sdo_read: {index: "0x2040", sub: "0x01", expect: R0}
"""

LITERAL_EXPECT_REGRESSION_TC = """\
id: "0024"
name: "plain literal expect and non-numeric literal expect are unaffected"
steps:
  - sdo_read: {index: "0x2040", sub: "0x01", expect: "0x260001"}
  - sdo_read: {index: "0x1008", sub: "0x00", expect: "DUT_ALPHA"}
"""


def test_sdo_read_expect_resolves_from_a_computed_register(tc_bench):
    # R1 is computed to exactly 0x00260001 — the value 0x2040:01 actually
    # holds — so this only PASSes if `expect: R1` was resolved to that
    # number before the comparison, not left as the literal string "R1".
    _add_tc(tc_bench, "TC0020_expect_register.yaml", EXPECT_FROM_REGISTER_PASS_TC)
    run_selected(tc_bench, {"0020"})
    assert tc_bench.results == {"0020": "PASS"}


def test_sdo_read_expect_from_register_fails_on_wrong_arithmetic(tc_bench):
    # Same shape, but R1 comes out to 0x260002 — one off from the device's
    # real 0x260001 — so the step must FAIL, and the message must show the
    # resolved number (proving the register was actually read), not the
    # bare register name "R1".
    _add_tc(tc_bench, "TC0021_expect_register_wrong.yaml", EXPECT_FROM_REGISTER_FAIL_TC)
    run_selected(tc_bench, {"0021"})
    assert tc_bench.results == {"0021": "FAIL"}
    fail_lines = [ln["msg"] for ln in tc_bench.logs if "0x260002" in ln["msg"]]
    assert fail_lines, tc_bench.logs
    assert not any("R1" in ln for ln in fail_lines)


def test_sdo_read_mask_resolves_from_a_register(tc_bench):
    # 0x2040:01 is 0x00260001, so "expect 0x01" only holds under a mask
    # that keeps the low byte. An unresolved "0xFF" would leave the mask
    # None and compare the whole word, which does not match — so this
    # PASSes only if the register really became 0xFF.
    _add_tc(tc_bench, "TC0022_mask_register.yaml", MASK_FROM_REGISTER_TC)
    run_selected(tc_bench, {"0022"})
    assert tc_bench.results == {"0022": "PASS"}


def test_expect_r0_compares_against_the_device_not_against_itself(tc_bench):
    """Regression for the ordering bug: R0 is preloaded to 0x99 before the
    read, and `into` defaults to R0 too. Resolving `expect: R0` *after*
    storing the read result would compare the value against itself and
    PASS unconditionally; resolving it first (against the preloaded 0x99)
    correctly FAILs since the device answers 0x260001."""
    _add_tc(tc_bench, "TC0023_expect_r0_self_compare.yaml", EXPECT_R0_SELF_COMPARE_TC)
    run_selected(tc_bench, {"0023"})
    assert tc_bench.results == {"0023": "FAIL"}


def test_literal_expect_is_unaffected_by_register_resolution(tc_bench):
    """Regression: a plain hex literal (numeric compare path) and a
    non-numeric literal like a device name (string compare path in
    `_judge_read`) must behave exactly as before — neither one is a
    register name, so `_with_registers` must leave them untouched."""
    _add_tc(tc_bench, "TC0024_literal_expect.yaml", LITERAL_EXPECT_REGRESSION_TC)
    run_selected(tc_bench, {"0024"})
    assert tc_bench.results == {"0024": "PASS"}


# -- `variants:` header enforcement (docs/ablaeufe/testfall-format.md) -------
# A case can declare which hardware variants it applies to. _exec_case skips
# it against a device reporting a different one, runs it against a matching
# one, and — because an unread variant is not a mismatch — still runs it
# against a device that never reported a variant at all: refusing to run
# over a number the bench could not read is how coverage disappears without
# a trace.

class _VariantSeedPlugin(BenchPlugin):
    """Seeds one EDS row carrying a variant config, so a scan fills the
    device's variant field — same plugin-seeded-variant approach as
    tests/test_plugins.py."""
    name = "variantseed"

    def __init__(self, row: dict):
        self._row = row

    def seed_eds(self) -> list[dict]:
        return [self._row]


VARIANT_MATCH_TC = """\
id: "0030"
name: "runs for its declared variant"
variants: ["820"]
steps:
  - log: "ran"
"""

VARIANT_MISMATCH_TC = """\
id: "0031"
name: "skipped for a variant it does not cover"
variants: ["920"]
steps:
  - fail: "should not run"
"""


@pytest.fixture()
def variant_bench(tmp_path):
    """A single DUT whose scanned variant is "820" — the seed EDS's 0x2050
    object defaults to 0, which reads back as "0x00" and is mapped to "820"
    here, same as the seeded-variant tests in tests/test_plugins.py."""
    row = {"file": "variant_dev.eds", "dev": "VARIANT_DEV", "ident": "0x4D2·0x1150",
           "code": "VAR", "enabled": True,
           "variant": {"index": "0x2050", "sub": "00", "map": {"0x00": "820"}}}
    bench = Bench(Db(tmp_path / "v.db"), plugins=[_VariantSeedPlugin(row)])
    write_seed_eds_files(bench)
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    bench.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})
    connect_and_scan(bench)
    bench.dispatch("dev_toggle", {"node": 1})
    return bench


def test_variants_header_skips_a_device_reporting_a_different_variant(variant_bench):
    dev = next(d for d in variant_bench.devices if d["node"] == 1)
    assert dev["variant"] == "820"
    _add_tc(variant_bench, "TC0031_mismatch.yaml", VARIANT_MISMATCH_TC)
    run_selected(variant_bench, {"0031"})
    assert variant_bench.results == {"0031": "SKIP"}
    assert any("920" in ln["msg"] and "820" in ln["msg"] for ln in variant_bench.logs)


def test_variants_header_runs_a_matching_device(variant_bench):
    _add_tc(variant_bench, "TC0030_match.yaml", VARIANT_MATCH_TC)
    run_selected(variant_bench, {"0030"})
    assert variant_bench.results == {"0030": "PASS"}


def test_variants_header_still_runs_against_an_unreported_variant(tc_bench):
    # tc_bench's DUTs never scan a variant (no variant config seeded), so
    # rec.variant stays "". This must still RUN, not SKIP.
    dev = next(d for d in tc_bench.devices if d["node"] == 1)
    assert dev["variant"] == ""
    _add_tc(tc_bench, "TC0030_match.yaml", VARIANT_MATCH_TC)
    run_selected(tc_bench, {"0030"})
    assert tc_bench.results == {"0030": "PASS"}


def test_a_variant_matches_however_the_two_sides_spell_the_number(tmp_path):
    """The device's variant is whatever the SDO read produced — a hex
    string like "0x00260001". A case names it the way a person writes it,
    "2490369". Comparing those as text skipped every case that declared a
    variant against real hardware, while passing any test that happened to
    spell the value byte for byte.
    """
    # no value map: the device already answers a usable number, which is
    # the shape cob-memiro and anything like it ships
    row = {"file": "variant_dev.eds", "dev": "VARIANT_DEV", "ident": "0x4D2·0x1150",
           "code": "VAR", "enabled": True, "variant": {"index": "0x2040", "sub": "01"}}
    bench = Bench(Db(tmp_path / "v.db"), plugins=[_VariantSeedPlugin(row)])
    write_seed_eds_files(bench)
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    bench.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})
    connect_and_scan(bench)
    bench.dispatch("dev_toggle", {"node": 1})
    assert next(d for d in bench.devices if d["node"] == 1)["variant"] == "0x00260001"

    # the same number three ways, then one that really is another variant
    for tid, declared, expected in (("0040", "2490369", "PASS"),
                                    ("0041", '"0x260001"', "PASS"),
                                    ("0042", '"0x00260001"', "PASS"),
                                    ("0043", "2490370", "SKIP")):
        _add_tc(bench, f"TC{tid}_v.yaml",
                f'id: "{tid}"\nname: "v"\nvariants: [{declared}]\n'
                'steps:\n  - log: "ran"\n')
        bench.results = {}
        run_selected(bench, {tid})
        assert bench.results[tid] == expected, f"variants: [{declared}]"


# -- on_fail: continue -------------------------------------------------------

FAIL_STEP_STOPS_UNDER_CONTINUE_TC = """\
id: "0050"
name: "an explicit fail: step always stops the case, even under on_fail: continue"
on_fail: continue
steps:
  - fail: "first reason"
  - log: "should not run"
"""


def test_explicit_fail_step_stops_even_under_on_fail_continue(tc_bench):
    """on_fail: continue exists so a case that already failed can still
    reach its own cleanup steps after a *checked* failure (a bad sdo_read).
    An explicit `fail:` step is the case declaring the run over right there
    — letting it fall through to `key != "fail"`'s absence meant a `fail:`
    step became a no-op under on_fail: continue, and the step after it ran
    anyway."""
    _add_tc(tc_bench, "TC0050_fail_continue.yaml", FAIL_STEP_STOPS_UNDER_CONTINUE_TC)
    run_selected(tc_bench, {"0050"})
    assert tc_bench.results == {"0050": "FAIL"}
    assert any("first reason" in ln["msg"] for ln in tc_bench.logs)
    assert not any("should not run" in ln["msg"] for ln in tc_bench.logs)


SKIP_AFTER_RECORDED_FAILURE_TC = """\
id: "0051"
name: "a skip after a recorded failure does not throw the failure away"
on_fail: continue
steps:
  - sdo_read: {index: "0x2050", sub: "0x00", expect: "0x99"}
  - skip: "cleanup, not applicable further"
"""


def test_skip_after_a_recorded_failure_still_reports_fail(tc_bench):
    """Under on_fail: continue, a case that fails an expectation and then
    hits `skip:` on its way out must still report FAIL with the original
    failure's reason — reporting SKIP here says the run "did not apply",
    which is not what happened: the device failed the check."""
    _add_tc(tc_bench, "TC0051_skip_after_fail.yaml", SKIP_AFTER_RECORDED_FAILURE_TC)
    run_selected(tc_bench, {"0051"})
    assert tc_bench.results == {"0051": "FAIL"}
    assert any("0x2050" in ln["msg"] and "0x99" in ln["msg"] for ln in tc_bench.logs)


# -- rand masks its result to 32 bits, like every other register write ------

RAND_MASKS_TO_32_BITS_TC = """\
id: "0052"
name: "rand masks its result to 32 bits"
steps:
  - rand: {to: R1, min: "0x100000001", max: "0x100000001"}
  - jump_eq: {a: R1, b: 1, to: ok}
  - fail: "rand did not mask to 32 bits"
  - label: ok
  - end:
"""


def test_rand_masks_its_result_to_32_bits(tc_bench):
    # min == max forces a deterministic draw: 0x100000001 masked to 32 bits
    # is 1 — every other register write (mov/add/.../sdo_read into) masks
    # the same way, so rand leaving its own result unmasked was the odd one
    # out, and it is where a case's arithmetic afterwards would go wrong.
    _add_tc(tc_bench, "TC0052_rand_mask.yaml", RAND_MASKS_TO_32_BITS_TC)
    run_selected(tc_bench, {"0052"})
    assert tc_bench.results == {"0052": "PASS"}


# -- adjust: the operator value is sized from the object just read, and a
# value that does not fit is a schema-visible ERROR, not a silent truncation

ADJUST_TOO_WIDE_TC = """\
id: "0053"
name: "adjust rejects an operator value that does not fit the read object's width"
steps:
  - adjust: {index: "0x2050", sub: "0x00"}
  - fail: "should not be reached"
"""


def test_adjust_value_that_does_not_fit_is_an_error_not_a_truncation(tc_bench):
    """0x2050 answers "0x00" — a 1-byte object per _hexstr_width. Typing 300
    (needs 9 bits) must not silently mask down to a different number and
    write it — the report would then disagree with what the operator saw
    on the meter. It has to be an ERROR that names the size, and the step
    after `adjust` must never run."""
    _add_tc(tc_bench, "TC0053_adjust_too_wide.yaml", ADJUST_TOO_WIDE_TC)

    def answer_adjust(bench):
        if bench.manual_prompt and bench.manual_prompt.get("kind") == "adjust":
            bench.dispatch("manual_answer", {"choice": "ok", "value": "300"})
    run_selected(tc_bench, {"0053"}, during=answer_adjust)
    assert tc_bench.results == {"0053": "ERROR"}
    assert any("does not fit" in ln["msg"] and "1 byte" in ln["msg"] for ln in tc_bench.logs)
    assert not any("should not be reached" in ln["msg"] for ln in tc_bench.logs)
