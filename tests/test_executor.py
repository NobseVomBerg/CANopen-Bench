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

from canopen_bench import data
from canopen_bench.core import Bench, _step_text
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
    """The panel must move while the run does, not jump to the end.

    Which steps reach a client is deliberately not fixed: `Bench._changed`
    coalesces, so a request arriving while a push is in flight becomes one
    trailing push rather than a queue of them (see its docstring — a run
    that pushes every step floods the socket and the page never repaints).
    So this asserts what the executor promises — progress that is about
    this case, that advances, and that is visible *before* the last step —
    and not which particular steps a given event loop let through. It used
    to demand the first push be step 1, which held on 3.10 to 3.12 by
    scheduling accident and stopped holding on 3.13.
    """
    seen: list[dict] = []

    async def notify():  # the executor asks for a push after every step
        if tc_bench.run_prog:
            seen.append(dict(tc_bench.run_prog))
    tc_bench.set_notifier(notify)
    run_selected(tc_bench, {"0001"})

    assert seen, "no progress reached a client during the run"
    assert all(p["tid"] == "0001" and p["of"] == 4 for p in seen)
    steps = [p["step"] for p in seen]
    assert steps == sorted(steps), f"progress went backwards: {steps}"
    assert steps[0] < 4, f"nothing was published before the last step: {steps}"
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


def _once(send, *args, **kw):
    """A `during` hook that fires `send` once, after the case has begun.

    After, not before: a case does not inherit what the bus said before it
    started, so a frame fed in beforehand is a frame about whatever ran
    last. `_sequence_started_at` is what the runner sets when a case takes
    over, which makes it the moment to send."""
    fired: list[bool] = []

    def hook(bench: Bench) -> None:
        if not fired and bench._sequence_started_at:
            fired.append(True)
            send(bench, *args, **kw)
    return hook


def test_an_emcy_that_already_arrived_counts(tc_bench):
    """The device sends when it is ready, not when a step happens to be
    waiting. A check that only looks forward turns timing into a failure."""
    _add_tc(tc_bench, "TC0014_emcy.yaml", EMCY_TC)
    run_selected(tc_bench, {"0014"}, during=_once(_emcy))
    assert tc_bench.results == {"0014": "PASS"}


def test_an_emcy_from_before_the_case_is_not_this_case_s(tc_bench):
    """The record outlives the case. A repeat would otherwise inherit the
    errors of its own previous pass and pass on them."""
    _emcy(tc_bench)                      # before the run: whatever ran last
    _add_tc(tc_bench, "TC0014_emcy.yaml", EMCY_TC)
    run_selected(tc_bench, {"0014"})
    assert tc_bench.results == {"0014": "FAIL"}


def test_every_error_reported_counts_not_only_the_last(tc_bench):
    """Unlike a PDO, an EMCY is a report and not a state: a device with
    three things wrong with it says so three times, and a case may ask
    about any of them."""
    _add_tc(tc_bench, "TC0014_emcy.yaml", EMCY_TC)

    def two(bench):
        _emcy(bench, code=0x7100)        # the one the case asks about…
        _emcy(bench, code=0x5000)        # …and a later one on top of it
    run_selected(tc_bench, {"0014"}, during=_once(two))
    assert tc_bench.results == {"0014": "PASS"}


def test_an_error_reset_ends_the_look_back(tc_bench):
    """Code 0x0000 is the device saying every error has been accepted or
    cleared. Nothing before it is still true, whatever the clock says —
    which is the device's own word and beats any duration."""
    _add_tc(tc_bench, "TC0014_emcy.yaml", EMCY_TC)

    def then_cleared(bench):
        _emcy(bench, code=0x7100)
        _emcy(bench, code=0x0000)        # "…and it is dealt with"
    run_selected(tc_bench, {"0014"}, during=_once(then_cleared))
    assert tc_bench.results == {"0014": "FAIL"}


def test_no_emcy_fails_after_the_timeout(tc_bench):
    _add_tc(tc_bench, "TC0014_emcy.yaml", EMCY_TC)
    run_selected(tc_bench, {"0014"})
    assert tc_bench.results == {"0014": "FAIL"}


def test_a_mask_matches_the_manufacturer_part_only(tc_bench):
    """Which class byte a device puts in front of its own error number is
    not always documented — the mask is how a case says "this part I know"."""
    _add_tc(tc_bench, "TC0015_emcy_mask.yaml", EMCY_MASK_TC)
    run_selected(tc_bench, {"0015"}, during=_once(_emcy, code=0x7100 | 0x71))
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


def test_what_a_case_sets_arrives_in_the_supply_box(tc_bench):
    """The box follows a run without anybody pressing Read: every step
    that changes the supply reads it back (``_psu_read``), exactly as a
    click on the page does, and the tick loop pushes the snapshot.

    Which is also the whole of it — that read is the only one there is.
    A voltage the case never touched, or a current the device started
    drawing, stands still until somebody asks. Nothing polls.
    """
    from conftest import FakeSupplyPort  # noqa: PLC0415  (test-only helper)
    tc_bench._psu_opener = lambda device, baud, timeout: FakeSupplyPort()
    assert tc_bench._psu_connect("COM6")
    before = tc_bench.snapshot()["psu"]
    assert before["channels"][1]["volt"] == 57.0 and before["output"] is True

    _add_tc(tc_bench, "TC0017_psu.yaml", PSU_TC)
    run_selected(tc_bench, {"0017"})
    assert tc_bench.results == {"0017": "PASS"}

    # the case ends on `psu: {ch: 2, volt: 57.5, curr: 2}` then `output: off`
    after = tc_bench.snapshot()["psu"]
    assert after["channels"][1]["volt"] == 57.5
    assert after["channels"][1]["curr"] == 2.0
    assert after["output"] is False


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

FAIL_STEP_UNDER_CONTINUE_TC = """\
id: "0050"
name: "a fail: step is a failure, not an exit"
on_fail: continue
steps:
  - fail: "first reason"
  - log: "the cleanup after it still runs"
"""

FAIL_THEN_END_TC = """\
id: "0051"
name: "fail: then end: is how a case does stop there"
on_fail: continue
steps:
  - fail: "first reason"
  - end:
  - log: "should not run"
"""


def test_a_fail_step_still_reaches_the_cleanup_after_it(tc_bench):
    """A `fail:` used to end the case, on the grounds that somebody had
    written it deliberately. What they write it into is an error branch,
    and cutting the case off there leaves the device wherever the failure
    found it: observed on real hardware, a case failed while the device was
    still booting, its closing wait never ran, and the *next* case failed
    on a device that had never left startup.

    So it records the failure and carries on, like any other one. The
    verdict is unchanged — a case that says fail: has failed."""
    _add_tc(tc_bench, "TC0050_fail_continue.yaml", FAIL_STEP_UNDER_CONTINUE_TC)
    run_selected(tc_bench, {"0050"})
    assert tc_bench.results == {"0050": "FAIL"}
    assert any("first reason" in ln["msg"] for ln in tc_bench.logs)
    assert any("cleanup after it still runs" in ln["msg"] for ln in tc_bench.logs)


def test_a_case_that_really_must_stop_there_says_so(tc_bench):
    """`end:` after the `fail:` — or `on_fail: stop` in the header. Both
    are the case saying it, rather than every fail: meaning it."""
    _add_tc(tc_bench, "TC0051_fail_end.yaml", FAIL_THEN_END_TC)
    run_selected(tc_bench, {"0051"})
    assert tc_bench.results == {"0051": "FAIL"}
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


# -- "no EMCY expected", which expect_emcy cannot express at all ------------

NO_EMCY_TC = """\
id: "0050"
name: "nothing may have gone wrong"
steps:
  - emcy_clear:
  - expect_no_emcy: {}
"""

NO_EMCY_VIOLATED_TC = """\
id: "0051"
name: "something did go wrong"
steps:
  - wait: 0.1
  - expect_no_emcy: {}
"""

NO_EMCY_FILTERED_TC = """\
id: "0052"
name: "that particular error may not have happened"
steps:
  - expect_no_emcy: {code: "0x99", mask: "0x00FF"}
"""


def test_expect_no_emcy_passes_on_a_quiet_bus(tc_bench):
    """The opposite assertion to expect_emcy, and not expressible as one:
    "nothing arrived" cannot be written as a code to match."""
    _add_tc(tc_bench, "TC0050_no_emcy.yaml", NO_EMCY_TC)
    run_selected(tc_bench, {"0050"})
    assert tc_bench.results == {"0050": "PASS"}


def test_expect_no_emcy_fails_and_names_what_it_saw(tc_bench):
    _add_tc(tc_bench, "TC0051_no_emcy_violated.yaml", NO_EMCY_VIOLATED_TC)
    run_selected(tc_bench, {"0051"}, during=_once(_emcy, code=0x1234))
    assert tc_bench.results == {"0051": "FAIL"}
    # naming the code is the difference between "something happened" and a
    # line somebody can act on
    assert any("expected no EMCY, saw 0x1234" in ln["msg"] for ln in tc_bench.logs), \
        [ln["msg"] for ln in tc_bench.logs]


def test_expect_no_emcy_with_a_code_ignores_a_different_one(tc_bench):
    """"this error did not happen" is a narrower claim than "nothing
    happened", and an EMCY with another code must not fail it."""
    _add_tc(tc_bench, "TC0052_no_emcy_filtered.yaml",
            NO_EMCY_FILTERED_TC.replace("steps:\n", "steps:\n  - wait: 0.1\n"))
    # a real EMCY, but not the one asked about
    run_selected(tc_bench, {"0052"}, during=_once(_emcy, code=0x7100))
    assert tc_bench.results == {"0052": "PASS"}


DUP_A = """id: "7700"
name: "First claimant"
steps:
  - log: "a"
"""

DUP_B = """id: "7700"
name: "Second claimant"
steps:
  - log: "b"
"""


def test_two_files_claiming_one_id_are_reported_not_silently_dropped(tc_bench):
    """The catalog is keyed by case id, so the loser of a collision is not
    in it. That used to happen in silence: a folder of 85 files produced a
    list of 81 and nothing said which four were gone."""
    _add_tc(tc_bench, "TC7700_first.yaml", DUP_A)
    _add_tc(tc_bench, "TC7700_second.yaml", DUP_B)

    kept = tc_bench.testcases["7700"]
    assert "duplicate id 7700" in kept.error
    # the file that lost is named, so it can be found and fixed
    other = "TC7700_second.yaml" if kept.file.endswith("first.yaml") else "TC7700_first.yaml"
    assert other in kept.error
    assert any("claimed by 2 files" in ln["msg"] and ln["type"] == "emcy0"
               for ln in tc_bench.logs)


def test_a_unique_id_carries_no_duplicate_error(tc_bench):
    _add_tc(tc_bench, "TC7701_alone.yaml", DUP_A.replace("7700", "7701"))
    assert "duplicate" not in (tc_bench.testcases["7701"].error or "")


def test_a_real_testcases_folder_starts_with_nothing_selected(tmp_path):
    """The demo catalog ships with a few cases ticked so the demo shows what
    a selection looks like. Those ids are ordinary numbers and collide with
    real ones, so a bench pointed at a real folder used to start with cases
    nobody had chosen already ticked — one press of Start away from running
    them against the hardware on the bench."""
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    for tid in ("1000", "4433", "4602"):        # three of the demo defaults
        (tc_dir / f"TC{tid}_real.yaml").write_text(
            f'id: "{tid}"\nname: "Real case"\nsteps:\n  - log: "x"\n')
    first = Bench(Db(tmp_path / "t.db"))
    first.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})

    reopened = Bench(Db(tmp_path / "t.db"))     # the folder is remembered
    assert set(reopened.testcases) == {"1000", "4433", "4602"}
    assert reopened.test_sel == set()


def test_the_demo_catalog_still_comes_with_its_selection(tmp_path):
    """Without a TestCases folder the demo catalog is what is on screen, and
    an empty list there shows nothing about how a run is put together."""
    bench = Bench(Db(tmp_path / "t.db"))
    assert bench.adapter == "demo" and not bench.testcases
    assert bench.test_sel == set(data.DEFAULT_TEST_SEL)


MEC_TC = """\
id: "0017"
name: "the code the device family uses"
steps:
  - expect_emcy: {mec: "0x72", timeout: 0.2}
"""

MEC_AND_REG_TC = """\
id: "0018"
name: "manufacturer code together with the error register"
steps:
  - expect_emcy: {mec: "0x6D", reg: "0x01", timeout: 0.2}
"""


def _emcy_frame(bench: Bench, payload: str, node: int = 1) -> None:
    """Feed one whole EMCY frame in, bytes as they are on the wire."""
    row = {"cls": "EMCY", "node": node, "data": payload, "obj": "", "val": ""}
    bench._annotate_emcy(row)


def test_the_manufacturer_error_code_is_what_a_case_can_ask_about(tc_bench):
    """A real frame off the bus: 00 10 01 72 00 00 00 00 — CiA code 0x1000,
    error register 0x01, and 0x72 in the manufacturer bytes, which is the
    code the device's own test case is written against. Only the CiA code
    used to be recorded, so `expect_emcy 0x72` compared 0x1000 against 0x72
    and reported "none seen" about an EMCY that was sitting right there.
    """
    _add_tc(tc_bench, "TC0017_mec.yaml", MEC_TC)
    run_selected(tc_bench, {"0017"},
                 during=_once(_emcy_frame, "00 10 01 72 00 00 00 00"))
    assert tc_bench.results == {"0017": "PASS"}


def test_the_error_register_is_asked_about_separately(tc_bench):
    _add_tc(tc_bench, "TC0018_mec_reg.yaml", MEC_AND_REG_TC)
    run_selected(tc_bench, {"0018"},
                 during=_once(_emcy_frame, "00 10 01 6D 00 00 00 00"))
    assert tc_bench.results == {"0018": "PASS"}


WIDE_MEC_TC = """\
id: "0019"
name: "a manufacturer code that does not fit in a byte"
steps:
  - expect_emcy: {mec: "0x0134", timeout: 0.2}
"""


def test_the_manufacturer_code_is_two_bytes_little_endian(tc_bench):
    """The frame carries it in bytes 3 and 4, the same way round as every
    other multi-byte field. Reading only byte 3 works until the first code
    above 0xFF and then compares a low byte against a whole number: here
    0x34 against 0x0134, which is "nothing arrived" about a frame that is
    right there."""
    _add_tc(tc_bench, "TC0019_wide.yaml", WIDE_MEC_TC)
    run_selected(tc_bench, {"0019"},
                 during=_once(_emcy_frame, "00 10 01 34 01 00 00 00"))
    assert tc_bench.results == {"0019": "PASS"}


def test_a_narrow_expectation_does_not_match_a_wide_code(tc_bench):
    """0x34 and 0x0134 are different codes, and the high byte is part of
    the number rather than something to ignore."""
    _add_tc(tc_bench, "TC0017_mec.yaml", MEC_TC.replace('"0x72"', '"0x34"'))
    run_selected(tc_bench, {"0017"},
                 during=_once(_emcy_frame, "00 10 01 34 01 00 00 00"))
    assert tc_bench.results == {"0017": "FAIL"}


def test_a_wrong_manufacturer_code_says_what_did_arrive(tc_bench):
    """"none seen" about a frame that arrived is the report that cost an
    evening: what is missing is not the EMCY, it is the match."""
    _add_tc(tc_bench, "TC0017_mec.yaml", MEC_TC)
    run_selected(tc_bench, {"0017"},
                 during=_once(_emcy_frame, "00 10 01 65 00 00 00 00"))
    assert tc_bench.results == {"0017": "FAIL"}
    said = " ".join(ln["msg"] for ln in tc_bench.logs)
    assert "mec 0x0065" in said, said


def test_the_error_register_alone_does_not_make_a_match(tc_bench):
    _add_tc(tc_bench, "TC0018_mec_reg.yaml", MEC_AND_REG_TC)
    _emcy_frame(tc_bench, "00 10 01 72 00 00 00 00")   # right reg, wrong mec
    run_selected(tc_bench, {"0018"})
    assert tc_bench.results == {"0018": "FAIL"}


RESET_TC = """\
id: "001A"
name: "the reset is a report the case waits for"
steps:
  - expect_emcy: {mec: "0x0", timeout: 0.2}
"""


def test_a_case_can_wait_for_the_error_reset_itself(tc_bench):
    """Error code 0x0000 — "everything has been accepted or cleared" — is
    the frame a case waits for after it acknowledges an error, and half of
    a device family's cases end on it.

    It is also what ends the EMCY window, and the window used to cut in
    *front* of it: the boundary frame was never inside. So the one step
    written to see it was the one step that could not, and it reported
    "nothing arrived at all" about a frame sitting two rows up in the
    trace. Whether the reset is inside the window or only ends it is now
    the caller's question, because the two callers want opposite answers.
    """
    _add_tc(tc_bench, "TC001A_reset.yaml", RESET_TC)
    run_selected(tc_bench, {"001A"},
                 during=_once(_emcy_frame, "00 00 00 00 00 00 00 00"))
    assert tc_bench.results == {"001A": "PASS"}, \
        " ".join(ln["msg"] for ln in tc_bench.logs)


def test_the_reset_still_does_not_count_as_something_being_wrong(tc_bench):
    """The other half of the same rule. `expect_no_emcy` asks whether the
    device is reporting a fault, and a device that has just cleared one is
    not — reading the reset as an EMCY would fail every case that ends by
    acknowledging an error, which is the opposite mistake."""
    _add_tc(tc_bench, "TC0051_no_emcy_violated.yaml", NO_EMCY_VIOLATED_TC)
    _emcy_frame(tc_bench, "00 10 01 72 00 00 00 00")   # a real error…
    _emcy_frame(tc_bench, "00 00 00 00 00 00 00 00")   # …then acknowledged
    run_selected(tc_bench, {"0051"})
    assert tc_bench.results == {"0051": "PASS"}, \
        " ".join(ln["msg"] for ln in tc_bench.logs)


WRITE_SHAPES_TC = """\
id: "0020"
name: "what a write puts in the report"
steps:
  - sdo_write: {index: "0x2000", sub: "0x00", value: "0x00000001", note: "a literal"}
  - sdo_read: {index: "0x2040", sub: "0x01", into: R3}
  - sdo_write: {index: "0x2000", sub: "0x00", value: R3, size: 4, note: "a register"}
"""


def test_a_write_that_worked_says_it_once(tc_bench):
    """"write 0x220C:0x00 = 0x00000001" followed by "Response: wrote
    0x00000001" is the same number twice. A write that succeeded answers
    with nothing, so there is nothing to fill a line with — and the value
    is already on the step line.

    Where it is *not* — a register, a builtin — the resolved value joins
    the step line instead, because otherwise what went on the wire appears
    nowhere at all.
    """
    _add_tc(tc_bench, "TC0020_shapes.yaml", WRITE_SHAPES_TC)
    run_selected(tc_bench, {"0020"})
    assert tc_bench.results == {"0020": "PASS"}
    steps = tc_bench._run_cases[0].steps

    literal = steps[0]
    assert literal.text.endswith("= 0x00000001  (Writable counter)"), literal.text
    assert literal.detail == "", f"still a second line: {literal.detail!r}"

    from_register = steps[2]
    assert "= R3 = 0x00260001" in from_register.text, from_register.text
    assert from_register.detail == ""


FRAME_TC = """\
id: "0023"
name: "what a raw frame puts in the report"
steps:
  - mov: {to: R1, value: "0x40"}
  - can_send: {cob: "0x3FF", data: ["0x01", 0, 0, 0, 0, R1, "0x01"], note: "a PDO"}
  - can_send: {cob: "0x80", data: [], note: "SYNC"}
"""


def test_a_raw_frame_says_which_bytes_went_out(tc_bench):
    """"send frame 0x3FF" names a COB-ID and nothing else — the payload is
    the entire content of the step, and it appeared nowhere in the report.
    Reading it back against the trace was the only way to tell what the
    device was actually told, which is the wrong amount of work for the
    question "what did we send".

    Spelled the way the trace spells it, so the two lines can be compared
    without transcribing either. A frame with no payload (SYNC) has
    nothing to add, and adds nothing.
    """
    _add_tc(tc_bench, "TC0023_frame.yaml", FRAME_TC)
    run_selected(tc_bench, {"0023"})
    assert tc_bench.results == {"0023": "PASS"}
    steps = tc_bench._run_cases[0].steps

    assert steps[1].text == "send frame 0x3FF = 01 00 00 00 00 40 01", steps[1].text
    assert steps[2].text == "send frame 0x80", steps[2].text


def test_the_frame_in_the_report_is_the_frame_on_the_bus(tc_bench):
    """One source for both, so the report cannot name a frame the device
    never saw. The register in the payload is resolved once."""
    _add_tc(tc_bench, "TC0023_frame.yaml", FRAME_TC)
    run_selected(tc_bench, {"0023"})
    # straight off the bus, not out of the snapshot: the tick loop that
    # normally drains it does not run in a test
    sent = [f for f in tc_bench.bus.poll_frames(4096)
            if f.cob_id == "0x3FF" and f.direction == "TX"]
    assert sent, "the frame never reached the bus"
    line = tc_bench._run_cases[0].steps[1].text
    assert line.endswith(sent[-1].data), (line, sent[-1].data)


def test_a_write_that_failed_still_says_why(tc_bench):
    """The line is dropped because it was empty of news, not because a
    failing write should go unexplained."""
    _add_tc(tc_bench, "TC0021_bad.yaml",
            'id: "0021"\nname: x\nsteps:\n'
            '  - sdo_write: {index: "0x3000", sub: "0x00", value: 1, size: 4}\n')
    run_selected(tc_bench, {"0021"})
    assert tc_bench.results == {"0021": "FAIL"}
    assert "abort" in tc_bench._run_cases[0].steps[0].detail


BINARY_TC = """\
id: "0022"
name: "values written in bits"
steps:
  - mov: {to: R1, value: "0b1100"}
  - jump_eq: {a: R1, b: 12, to: ok}
  - fail: "0b1100 is not twelve"
  - label: ok
  - sdo_read: {index: "0x2040", sub: "0x01", expect: "0b00000000001001100000000000000001"}
"""


def test_a_binary_literal_is_read_as_binary(tc_bench):
    """"0b1100" is *valid hexadecimal* — 0, B, 1, 1, 0, 0 — so reading it
    with int(s, 16) gives 725248 instead of 12 and raises nothing. Every
    number in such a case is then wrong the same way, which is how it
    passes its own comparisons and tells nobody.
    """
    _add_tc(tc_bench, "TC0022_bits.yaml", BINARY_TC)
    run_selected(tc_bench, {"0022"})
    assert tc_bench.results == {"0022": "PASS"}


def test_a_bit_level_expectation_is_not_a_nibble_one(tc_bench):
    """A "#" in a hex filter is four bits at once, so a single-bit check
    cannot be written that way — 0x2040:01 is 0x00260001, and asserting
    bit 0 alone has to leave the other three bits of that nibble free."""
    _add_tc(tc_bench, "TC0023_bit.yaml",
            'id: "0023"\nname: x\nsteps:\n'
            '  - sdo_read: {index: "0x2040", sub: "0x01", '
            'expect: "0x00000001", mask: "0x00000001"}\n')
    run_selected(tc_bench, {"0023"})
    assert tc_bench.results == {"0023": "PASS"}


def _order_for(bench: Bench, visible: list[str]) -> list[str]:
    """What a run would execute, given those ids on screen. Started and
    stopped inside a loop, because run_start hands the case list to a task."""
    bench.test_sel = {"0001", "0002", "0005"}

    async def go():
        bench.dispatch("run_start", {"ids": visible})
        order = list(bench.run_order)
        bench.dispatch("run_stop", {})
        while bench.running:
            await asyncio.sleep(0.02)
        return order
    return asyncio.run(go())


def test_a_case_that_is_filtered_off_the_screen_does_not_run(tc_bench):
    """A filter narrows the list, not the selection. So a case selected
    before the filter was set stays selected while being invisible — and
    ran: the category said `automated`, the screen showed 47 of 85, and the
    run was 74 long, stopping at a question from a semi-automated case
    nobody could see.

    The Start button has always counted "selected among shown". This is the
    number it was already promising.
    """
    assert _order_for(tc_bench, ["0001", "0005"]) == ["0001", "0005"]


def test_the_run_order_follows_the_catalog_not_the_client(tc_bench):
    """The ids say which, never in what order — a list arriving shuffled
    must not shuffle the run."""
    assert _order_for(tc_bench, ["0005", "0002", "0001"]) == ["0001", "0002", "0005"]


DUMP_TC = """\
id: "0024"
name: "the register state, written down"
steps:
  - mov: {to: R1, value: 12}
  - sdo_read: {index: "0x2040", sub: "0x01", into: R3}
  - dump_registers: {note: "state before the write"}
"""


def test_dump_registers_puts_every_register_in_the_report(tc_bench):
    """53 lines across 26 of the real cases ask for this. What they are
    asking for is the register state at that point, in the report somebody
    reads afterwards — so it goes in the step's own detail, all sixteen of
    them whether the case has touched one or not. "R7 is missing" would be
    a fact about the list rather than about the run."""
    _add_tc(tc_bench, "TC0024_dump.yaml", DUMP_TC)
    run_selected(tc_bench, {"0024"})
    assert tc_bench.results == {"0024": "PASS"}

    step = tc_bench._run_cases[0].steps[-1]
    assert step.text == "dump registers"
    assert step.note == "state before the write"
    # the case looking at its own registers is bookkeeping, not traffic —
    # same kind as the jumps and arithmetic around it
    assert step.state == "flow"
    for name in (f"R{i}" for i in range(16)):
        assert f"{name} = 0x" in step.detail, f"{name} missing from the dump"
    # both bases, because a case mixes them: a screen id reads in hex, a
    # count does not
    assert "R1 = 0x0000000C (12)" in step.detail
    assert "R3 = 0x00260001 (2490369)" in step.detail


def test_dump_registers_needs_no_value_and_takes_no_stray_field():
    from canopen_bench.testcases import parse_testcase

    bare = 'id: "1"\nname: x\nsteps:\n  - dump_registers:\n'
    assert parse_testcase(bare, "TC1_x.yaml").error is None
    stray = 'id: "1"\nname: x\nsteps:\n  - dump_registers: {to: R1}\n'
    assert "unknown field" in parse_testcase(stray, "TC1_x.yaml").error


BASE_TC = """\
id: "0025"
name: "the answer in the base it was asked in"
steps:
  - sdo_read: {index: "0x2040", sub: "0x01", expect: 2490369, note: "decimal"}
  - sdo_read: {index: "0x2040", sub: "0x01", expect: "0x260001", note: "hex"}
  - sdo_read: {index: "0x2040", sub: "0x01", note: "no expectation"}
"""


def test_the_answer_comes_back_in_the_base_it_was_asked_in(tc_bench):
    """A case asks in the base it wants to read: a tension in counts is
    unreadable as hex, a screen id is unreadable as anything else. The old
    tool worked that way and the cases were written against it — the
    expectation's own spelling says which, and the converter keeps it
    (decimal becomes a YAML integer, hex stays a "0x…" string)."""
    _add_tc(tc_bench, "TC0025_base.yaml", BASE_TC)
    run_selected(tc_bench, {"0025"})
    assert tc_bench.results == {"0025": "PASS"}

    decimal, hexed, plain = tc_bench._run_cases[0].steps[:3]
    assert decimal.detail == "Response: 2490369", decimal.detail
    assert hexed.detail == "Response: 0x00260001", hexed.detail
    # nothing to go on, so it stays as the device sent it
    assert plain.detail == "Response: 0x00260001", plain.detail


# -- the folder is re-read when a run starts ---------------------------------

EDIT_TC_FIRST = """\
id: "0031"
name: "edited between runs"
steps:
  - log: "the first version"
  - end:
"""

EDIT_TC_SECOND = """\
id: "0031"
name: "edited between runs"
steps:
  - fail: "the edited version ran"
  - end:
"""

EDIT_TC_BROKEN = """\
id: "0031"
name: "edited between runs"
steps:
  - not_a_step: {}
  - end:
"""


def test_a_case_edited_on_disk_runs_as_edited(tc_bench):
    """Editing a YAML used to need the whole tool restarted: the catalog was
    read at startup and at nothing else the UI could reach. A run now reads
    the folder first, so what runs is what is on disk."""
    _add_tc(tc_bench, "TC0031_edit.yaml", EDIT_TC_FIRST)
    run_selected(tc_bench, {"0031"})
    assert tc_bench.results == {"0031": "PASS"}

    Path(tc_bench.paths["tc"]).joinpath("TC0031_edit.yaml").write_text(EDIT_TC_SECOND)
    run_selected(tc_bench, {"0031"})          # no rescan, no restart
    assert tc_bench.results == {"0031": "FAIL"}


def test_a_case_broken_since_the_last_scan_is_named_rather_than_dropped(tc_bench):
    """A case that a fresh typo made unreadable falls out of the catalog, and
    the start narrows the selection to what is runnable. Silently, that is a
    run that did less than the button promised — so it says which."""
    _add_tc(tc_bench, "TC0031_edit.yaml", EDIT_TC_FIRST)
    Path(tc_bench.paths["tc"]).joinpath("TC0031_edit.yaml").write_text(EDIT_TC_BROKEN)
    tc_bench.logs = []

    run_selected(tc_bench, {"0031"})

    assert any(ln["type"] == "emcy0" and "no longer readable" in ln["msg"]
               and "0031" in ln["msg"] for ln in tc_bench.logs), tc_bench.logs
    assert tc_bench.results == {}             # and it did not run


def test_a_case_hidden_by_the_tool_filter_is_not_reported_as_broken(tc_bench):
    """The filter hiding a case is not the folder losing it. Only what the
    re-read actually cost gets named, or the message cries wolf every time
    somebody runs with a filter set."""
    _add_tc(tc_bench, "TC0031_edit.yaml", EDIT_TC_FIRST)
    tc_bench.logs = []

    run_selected(tc_bench, {"0031"})

    assert not [ln for ln in tc_bench.logs if "no longer readable" in ln["msg"]]


def test_a_sub_index_is_two_digits_wide_in_read_and_write_alike():
    """A sub-index is a byte however the file wrote it — 1, "1" and "0x01"
    are one address, and three renderings of it in one report invite the
    reader to wonder whether they are."""
    assert _step_text("sdo_read", {"index": "0x2345", "sub": 1}) == "read 0x2345:0x01"
    assert _step_text("sdo_write", {"index": "0x2345", "sub": "0x1", "value": "0x0C"}) \
        == "write 0x2345:0x01 = 0x0C"
    assert _step_text("sdo_read", {"index": "0x2345", "sub": "0x10"}) == "read 0x2345:0x10"


# -- loops: LoopBegin / LoopEnd, as the CSV always wrote them ----------------

LOOP_COUNT_TC = """\
id: "0040"
name: "a loop reads as a loop"
steps:
  - mov: {to: R1, value: 0}
  - loop: 3
  - add: {to: R1, value: 1}
  - loop_end:
  - jump_eq: {a: R1, b: 3, to: ok}
  - fail: "the body did not run three times"
  - label: ok
  - end:
"""


def test_a_loop_runs_its_body_n_times_and_counts_down_in_the_report(tc_bench):
    """The counter is the executor's: a case cannot lose track of its own
    loop by writing to the wrong Rn."""
    _add_tc(tc_bench, "TC0040_loop.yaml", LOOP_COUNT_TC)
    run_selected(tc_bench, {"0040"})
    assert tc_bench.results == {"0040": "PASS"}

    lines = [s.text for s in tc_bench._run_cases[0].steps]
    assert "LoopBegin 3" in lines
    # counting down the turns still to come, which is what says "three of
    # these rows are one loop" rather than three unrelated passes
    assert [ln for ln in lines if ln.startswith("LoopEnd")] == [
        "LoopEnd, loopsLeft: 2", "LoopEnd, loopsLeft: 1", "LoopEnd, loopsLeft: 0"]


LOOP_FROM_REGISTER_TC = """\
id: "0043"
name: "a count the case works out first"
steps:
  - mov: {to: R11, value: 0}
  - add: {to: R11, value: 6}
  - add: {to: R11, value: 11}
  - mov: {to: R1, value: 0}
  - loop: {n: R11}
  - add: {to: R1, value: 1}
  - mov: {to: R11, value: 1}
  - loop_end:
  - jump_eq: {a: R1, b: 17, to: ok}
  - fail: "the body did not run seventeen times"
  - label: ok
  - end:
"""


def test_a_loop_may_count_what_the_case_worked_out(tc_bench):
    """A case does not always know the count when it is written: 4613 asks
    the device which variant it is and then loops over as many parameters
    as that variant has. The body writes to that register while the loop
    is turning, which must not move it — the executor took the number when
    the loop opened."""
    _add_tc(tc_bench, "TC0043_from_register.yaml", LOOP_FROM_REGISTER_TC)
    run_selected(tc_bench, {"0043"})
    assert tc_bench.results == {"0043": "PASS"}
    lines = [s.text for s in tc_bench._run_cases[0].steps]
    assert "LoopBegin R11 = 17" in lines   # the register, and what it held
    assert lines.count("LoopEnd, loopsLeft: 0") == 1


LOOP_BREAK_TC = """\
id: "0041"
name: "a loop can be left early"
steps:
  - mov: {to: R1, value: 0}
  - loop: 10
  - add: {to: R1, value: 1}
  - jump_eq: {a: R1, b: 2, to: enough}
  - jump: carry_on
  - label: enough
  - loop_break:
  - label: carry_on
  - loop_end:
  - jump_eq: {a: R1, b: 2, to: ok}
  - fail: "the break did not leave the loop"
  - label: ok
  - end:
"""


def test_loop_break_continues_after_the_loop_end(tc_bench):
    """Out of the loop, not out of the case: the steps after loop_end are
    where a case puts the bench back, and a break that skipped them would
    leave the device wherever the loop stopped."""
    _add_tc(tc_bench, "TC0041_break.yaml", LOOP_BREAK_TC)
    run_selected(tc_bench, {"0041"})
    assert tc_bench.results == {"0041": "PASS"}

    lines = [s.text for s in tc_bench._run_cases[0].steps]
    assert lines.count("LoopBreak") == 1
    # the first turn reached loop_end normally; the break happened on the
    # second, so the loop stopped six turns short of its ten
    assert [ln for ln in lines if ln.startswith("LoopEnd")] == ["LoopEnd, loopsLeft: 9"]
    # and the case carried on past the loop rather than ending there
    assert lines[-1] == "end"


LOOP_ZERO_TC = """\
id: "0042"
name: "a loop of nothing"
steps:
  - mov: {to: R1, value: 0}
  - loop: 0
  - add: {to: R1, value: 1}
  - loop_end:
  - jump_eq: {a: R1, b: 0, to: ok}
  - fail: "the body ran"
  - label: ok
  - end:
"""


def test_a_loop_of_zero_skips_its_body(tc_bench):
    """So a converter that computes the count has no zero to special-case."""
    _add_tc(tc_bench, "TC0042_zero.yaml", LOOP_ZERO_TC)
    run_selected(tc_bench, {"0042"})
    assert tc_bench.results == {"0042": "PASS"}


def test_loop_steps_read_as_flow_not_as_traffic(tc_bench):
    """Same kind as the jumps and arithmetic around them — nothing about a
    loop reaches the bus."""
    _add_tc(tc_bench, "TC0040_loop.yaml", LOOP_COUNT_TC)
    run_selected(tc_bench, {"0040"})
    for step in tc_bench._run_cases[0].steps:
        if step.text.startswith(("LoopBegin", "LoopEnd")):
            assert step.state == "flow", step.text
