"""Run reports: the HTML a run leaves behind, and the data beside it.

The point of these files is being readable a week later, by somebody who
was not there. So the tests are about what a reader can find in them —
which device, which step failed and why — rather than about markup.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from conftest import connect_and_scan, write_seed_eds_files

from canopen_bench.core import Bench
from canopen_bench.db import Db
from canopen_bench.report import (
    OVERVIEW,
    STYLESHEET,
    SUMMARY_GLOB,
    CaseRecord,
    RunRecord,
    StepRecord,
    case_html,
    collect_overview,
    default_css,
    load_runs,
    overview_html,
    summary_html,
    summary_json,
    write_stylesheet,
)

PASS_TC = """\
id: "0101"
name: "identity readable"
desc: "Checks that the device answers its own serial number."
grade: automated
steps:
  - log: "the part that reads"
  - sdo_read: {index: "0x2040", sub: "0x01", expect: "0x260001"}
"""

FAIL_TC = """\
id: "0102"
name: "wrong expectation"
steps:
  - sdo_read: {index: "0x2050", sub: "0x00", expect: "0x01"}
"""


def _case(**kw) -> CaseRecord:
    base = dict(id="0042", name="Power off handling", device="DUT_ALPHA",
                variant="V2", sn="0026", node=1, started="2026-07-29T09:13:30",
                seconds=8.4, verdict="PASS")
    return CaseRecord(**{**base, **kw})


# -- one case ---------------------------------------------------------------

def test_the_header_says_what_ran_against_what():
    doc = case_html(_case(desc="checks under-voltage", grade="automated",
                          tools=["PSU"], user="bench"))
    for text in ("Power off handling", "checks under-voltage", "automated",
                 "PSU", "DUT_ALPHA", "variant V2", "SN 0026", "node 01",
                 "2026-07-29T09:13:30", "8.4 s"):
        assert text in doc, text


def test_a_failed_step_carries_its_reason_into_the_file():
    """Without the reason the report says "something went wrong", which is
    the one thing the reader already knows."""
    case = _case(verdict="FAIL", reason="step 2 failed",
                 steps=[StepRecord(1, "read 0x2007:02", "ok"),
                        StepRecord(2, "read 0x2050:00", "fail",
                                   "sdo_read 0x2050:00 = 0x00, expected 0x01")])
    doc = case_html(case)
    assert "expected 0x01" in doc
    assert "testStepNok" in doc and "resultNok" in doc


def test_a_log_step_reads_as_a_note_not_as_a_verdict():
    doc = case_html(_case(steps=[StepRecord(1, "the part that reads", "note")]))
    assert "testStepComment" in doc


def test_a_step_the_case_spends_on_itself_reads_as_neither():
    doc = case_html(_case(steps=[StepRecord(1, "jump_gt R15 0 → loop1", "flow")]))
    assert "testStepFlow" in doc


def test_the_style_is_linked_not_copied_into_every_file():
    """Inlining it would repeat the same block in every report of every
    run — and make restyling impossible, which is the one thing a
    stylesheet is for."""
    doc = case_html(_case())
    assert "href='testReportStyle.css'" in doc
    assert "<style>" not in doc


def test_markup_is_escaped():
    doc = case_html(_case(name="a <script>alert(1)</script> case"))
    assert "<script>" not in doc and "&lt;script&gt;" in doc


# -- the run summary --------------------------------------------------------

def _run(*cases: CaseRecord) -> RunRecord:
    return RunRecord(started="2026-07-29T09:13:30", finished="2026-07-29T09:14:02",
                     user="bench", workspace="bench1", cases=list(cases))


def test_the_summary_links_every_case_to_its_own_file():
    run = _run(_case(file="a.html"), _case(id="4857", name="Yarn breakage",
                                           verdict="FAIL", file="b.html"))
    doc = summary_html(run)
    assert "href='a.html'" in doc and "href='b.html'" in doc


def test_one_failed_case_makes_the_run_fail():
    assert _run(_case(), _case(verdict="FAIL")).verdict == "FAIL"
    assert _run(_case(), _case(verdict="ERROR")).verdict == "FAIL"


def test_a_skipped_case_is_not_a_failure():
    """"Does not apply to this variant" must not turn a run red — that is
    the difference between a bench that is trusted and one that is not."""
    assert _run(_case(), _case(verdict="SKIP")).verdict == "PASS"
    assert _run(_case(verdict="SKIP")).verdict == "SKIP"


def test_the_json_beside_it_has_the_numbers_an_overview_needs():
    """A later overview per hardware variant reads this, rather than
    parsing the HTML it is meant to link to."""
    doc = json.loads(summary_json(_run(_case(file="a.html"))))
    assert doc["verdict"] == "PASS"
    (case,) = doc["cases"]
    assert case["variant"] == "V2" and case["verdict"] == "PASS"
    assert case["file"] == "a.html" and case["seconds"] == 8.4
    assert "steps" not in case      # those live in the case's own file


# -- what a real run writes -------------------------------------------------

def _bench(tmp_path) -> Bench:
    bench = Bench(Db(tmp_path / "r.db"))
    write_seed_eds_files(bench)
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    (tc_dir / "TC0101_pass.yaml").write_text(PASS_TC)
    (tc_dir / "TC0102_fail.yaml").write_text(FAIL_TC)
    bench.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})
    bench.dispatch("set_path", {"which": "res", "value": str(tmp_path / "res")})
    connect_and_scan(bench)
    bench.dispatch("dev_toggle", {"node": 1})
    return bench


def _run_all(bench: Bench) -> None:
    import asyncio
    bench.test_sel = {"0101", "0102"}

    async def go():
        bench.dispatch("run_start", {})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while bench.running and loop.time() < deadline:
            await asyncio.sleep(0.02)
    asyncio.run(go())


def test_a_run_writes_a_file_per_case_and_a_summary(tmp_path):
    bench = _bench(tmp_path)
    bench.stop_on_err = False
    _run_all(bench)
    files = sorted(p.name for p in Path(bench.paths["res"]).iterdir())
    assert sum(f.endswith("summary.html") for f in files) == 1
    assert sum(f.endswith("summary.json") for f in files) == 1
    assert sum("__0101__" in f for f in files) == 1
    assert sum("__0102__" in f for f in files) == 1


def test_the_written_report_names_the_device_and_the_failing_step(tmp_path):
    bench = _bench(tmp_path)
    bench.stop_on_err = False
    _run_all(bench)
    failed = next(p for p in Path(bench.paths["res"]).iterdir() if "__0102__" in p.name)
    doc = failed.read_text(encoding="utf-8")
    assert "DUT_ALPHA" in doc          # which device it ran against
    assert "expected" in doc           # why the step failed
    assert "resultNok" in doc


def test_a_written_run_opens_without_the_bench(tmp_path):
    """Every link a report makes is a bare file name, so a results folder
    read straight from the disk still works: the summary reaches its case
    pages and all of them find the stylesheet.

    The bench serves these files while it runs, but it does not own them.
    A run is looked at long after the tool that produced it was closed —
    a report that needed a server to render would be a report nobody can
    open on the machine it gets copied to.
    """
    bench = _bench(tmp_path)
    bench.stop_on_err = False
    _run_all(bench)
    folder = Path(bench.paths["res"])
    for path in folder.glob("*.html"):
        for href in re.findall(r"href='([^']*)'", path.read_text(encoding="utf-8")):
            assert "//" not in href and not href.startswith("/"), \
                f"{path.name} links {href!r} — that needs a server"
            assert (folder / href).exists(), f"{path.name} links {href!r}, which is not there"


LOOP_TC = """\
id: "0103"
name: "seventeen passes over the same object"
steps:
  - mov: {to: R11, value: 17}
  - mov: {to: R15, value: R11}
  - label: loop1
  - sdo_read: {index: "0x2040", sub: "0x01", expect: "0x260001"}
  - sub: {to: R15, value: 1}
  - jump_gt: {a: R15, b: 0, to: loop1}
  - label: loop1_end
"""


def _loop_report(tmp_path) -> str:
    """One case whose loop turns as often as a register says, run for real."""
    import asyncio
    bench = Bench(Db(tmp_path / "r.db"))
    write_seed_eds_files(bench)
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    (tc_dir / "TC0103_loop.yaml").write_text(LOOP_TC)
    bench.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})
    bench.dispatch("set_path", {"which": "res", "value": str(tmp_path / "res")})
    connect_and_scan(bench)
    bench.dispatch("dev_toggle", {"node": 1})
    bench.test_sel = {"0103"}

    async def go():
        bench.dispatch("run_start", {})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20
        while bench.running and loop.time() < deadline:
            await asyncio.sleep(0.02)
    asyncio.run(go())
    path = next(p for p in Path(bench.paths["res"]).iterdir() if "__0103__" in p.name)
    return path.read_text(encoding="utf-8")


def test_a_loop_leaves_one_line_per_pass_in_the_report(tmp_path):
    """A count the case works out at run time is the case that broke: a
    loop written to turn seventeen times but read as turning once passes
    just as green, and the report is the only place that says which."""
    assert _loop_report(tmp_path).count("testStepOk") == 17


def test_the_lines_that_turn_a_loop_are_set_apart_from_the_traffic(tmp_path):
    doc = _loop_report(tmp_path)
    # two movs and the label after the loop, plus a label, a sub and a
    # jump on every one of the seventeen passes
    assert doc.count("testStepFlow") == 3 + 17 * 3


def test_the_run_puts_the_stylesheet_beside_the_reports(tmp_path):
    bench = _bench(tmp_path)
    bench.stop_on_err = False
    _run_all(bench)
    assert (Path(bench.paths["res"]) / STYLESHEET).exists()


def test_an_edited_stylesheet_survives_the_next_run(tmp_path):
    """It is there to be changed. A run that restored the shipped look
    would throw that away every time."""
    folder = tmp_path / "styled"
    folder.mkdir()
    (folder / STYLESHEET).write_text("body { color: hotpink; }")
    write_stylesheet(folder)
    assert (folder / STYLESHEET).read_text() == "body { color: hotpink; }"


def test_the_summary_in_the_list_is_a_file_that_exists(tmp_path):
    """The list used to show a name that looked like a file with nothing
    behind it."""
    bench = _bench(tmp_path)
    bench.stop_on_err = False
    _run_all(bench)
    name = bench.snapshot()["tests"]["reports"][0]["name"]
    assert (Path(bench.paths["res"]) / name).exists()


#: a results folder the filesystem will refuse to create, which takes one
#: string per platform: "/proc/nope" cannot be made on Linux and is an
#: ordinary relative path on Windows, created without complaint, leaving
#: the branch under test unreached. "|" is the mirror image — illegal in a
#: Windows filename, perfectly legal in a POSIX one. Both raise OSError,
#: which is what the writer catches.
UNWRITABLE = "nope|results" if os.name == "nt" else "/proc/nope/results"


def test_an_unwritable_results_folder_does_not_lose_the_run(tmp_path):
    """The verdicts are already on screen and in the log. A report that
    cannot be written is a line in the log, not a failed run."""
    bench = _bench(tmp_path)
    bench.dispatch("set_path", {"which": "res", "value": UNWRITABLE})
    _run_all(bench)
    assert bench.results.get("0101") == "PASS"
    assert any("report not written" in row["msg"] for row in bench.logs)


# -- the overview across runs ------------------------------------------------

def _write_run(folder: Path, name: str, doc: dict | str) -> None:
    text = doc if isinstance(doc, str) else json.dumps(doc)
    (folder / f"{name}{SUMMARY_GLOB[1:]}").write_text(text, encoding="utf-8")


def _case_doc(**kw) -> dict:
    base = dict(id="0042", name="Power off handling", variant="V2", device="DUT_ALPHA",
                verdict="PASS", started="2026-07-29T09:13:30", file="a.html")
    return {**base, **kw}


def test_load_runs_keeps_the_window_and_skips_what_it_cannot_read(tmp_path):
    """A run outside the window is noise, a corrupt file must not blow up
    the overview, and a run whose own timestamp cannot be read is kept —
    an unreadable date is a reason to look, not to hide."""
    now = datetime(2026, 7, 29, 12, 0, 0)
    old = {"started": (now - timedelta(days=10)).isoformat(), "cases": []}
    recent = {"started": (now - timedelta(days=1)).isoformat(), "cases": []}
    unparseable = {"started": "not-a-date", "cases": []}
    _write_run(tmp_path, "old__", old)
    _write_run(tmp_path, "recent__", recent)
    _write_run(tmp_path, "unparseable__", unparseable)
    _write_run(tmp_path, "corrupt__", "{ this is not json")
    runs = load_runs(tmp_path, days=7, now=now)
    started = {r["started"] for r in runs}
    assert old["started"] not in started
    assert recent["started"] in started
    assert "not-a-date" in started
    assert len(runs) == 2


def test_grouping_splits_by_variant_and_falls_back_to_device(tmp_path):
    """Two variants in one run are two groups; a case with no variant
    still has to show up somewhere, so it groups under its device name."""
    run = {"started": "2026-07-28T10:00:00", "cases": [
        _case_doc(id="01", variant="V1", device="D1"),
        _case_doc(id="02", variant="V2", device="D2"),
        _case_doc(id="03", variant="", device="D3"),
    ]}
    variants = collect_overview([run])
    keys = {v.key for v in variants}
    assert keys == {"V1", "V2", "D3"}
    for group in variants:
        assert group.runs == 1


def test_a_runs_worth_of_cases_counts_as_one_run_not_three(tmp_path):
    """The overview answers "how many times was this touched", not "how
    many cases ran" — three cases of the same variant in one run must not
    look like three separate runs of it."""
    run = {"started": "2026-07-28T10:00:00", "cases": [
        _case_doc(id="01", variant="V1"),
        _case_doc(id="02", variant="V1"),
        _case_doc(id="03", variant="V1"),
    ]}
    (variant,) = collect_overview([run])
    assert variant.runs == 1
    assert variant.executions == 3


def test_verdict_is_the_newest_run_not_an_average_over_the_window():
    """The whole point of the feature: a variant that failed three days
    ago and passed yesterday must read PASS today, and the other way
    around must read FAIL — not "2 of 2 passed" hiding a live failure."""
    old_fail = {"started": "2026-07-26T08:00:00",
                "cases": [_case_doc(id="01", variant="V1", verdict="FAIL")]}
    new_pass = {"started": "2026-07-28T08:00:00",
                "cases": [_case_doc(id="01", variant="V1", verdict="PASS")]}
    (variant,) = collect_overview([old_fail, new_pass])
    assert variant.verdict == "PASS"

    old_pass = {"started": "2026-07-26T08:00:00",
                "cases": [_case_doc(id="01", variant="V1", verdict="PASS")]}
    new_fail = {"started": "2026-07-28T08:00:00",
                "cases": [_case_doc(id="01", variant="V1", verdict="FAIL")]}
    (variant,) = collect_overview([old_pass, new_fail])
    assert variant.verdict == "FAIL"


def test_a_cases_last_file_is_the_newest_report_not_the_first_seen():
    """The link in the overview must point at the latest report for that
    case — a reader chasing a failure does not want yesterday's file."""
    older = {"started": "2026-07-28T08:00:00", "cases": [
        _case_doc(id="01", variant="V1", started="2026-07-28T08:00:00", file="old.html"),
    ]}
    newer = {"started": "2026-07-29T08:00:00", "cases": [
        _case_doc(id="01", variant="V1", started="2026-07-29T08:00:00", file="new.html"),
    ]}
    # fed newest-first, so a naive "first wins" implementation would fail
    (variant,) = collect_overview([newer, older])
    (case,) = variant.cases
    assert case.last_file == "new.html"


def test_overview_html_has_one_details_per_variant_and_escapes_names():
    """Every case with a file links to it, markup in a name cannot break
    out of the page, and the stylesheet is linked rather than inlined."""
    run = {"started": "2026-07-28T08:00:00", "cases": [
        _case_doc(id="01", name="a <script>alert(1)</script> case", variant="V1",
                  file="v1.html"),
        _case_doc(id="02", name="no file yet", variant="V2", file=""),
    ]}
    variants = collect_overview([run])
    doc = overview_html(variants, days=7)
    assert doc.count("<details>") == 2
    assert "href='v1.html'" in doc
    assert "<script>" not in doc and "&lt;script&gt;" in doc
    assert f"href='{STYLESHEET}'" in doc
    assert "<style>" not in doc


def _bench_res(tmp_path) -> Bench:
    bench = Bench(Db(tmp_path / "r.db"))
    res = tmp_path / "res"
    res.mkdir()
    bench.dispatch("set_path", {"which": "res", "value": str(res)})
    return bench


def test_report_overview_end_to_end_through_the_bench(tmp_path):
    """The action folds the seeded runs into the overview page and into
    the snapshot the frontend reads, and clamps the day window."""
    bench = _bench_res(tmp_path)
    folder = Path(bench.paths["res"])
    # Dated relative to today, not fixed: this path goes through the real
    # clock (the bench action, unlike load_runs, takes no `now`), so a
    # literal date would sit inside the window on the day it was written
    # and outside it a week later — a test that passes until it doesn't.
    started = datetime.now() - timedelta(days=2)
    run = {"started": started.isoformat(), "cases": [
        _case_doc(id="01", name="power off", variant="V1", verdict="PASS", file="a.html",
                  started=started.isoformat()),
        _case_doc(id="02", name="wrong expect", variant="V2", verdict="FAIL", file="b.html",
                  started=started.isoformat()),
    ]}
    _write_run(folder, started.strftime("%Y%m%d_%H%M%S") + "__", run)

    bench.dispatch("report_overview", {"days": 7})

    assert (folder / OVERVIEW).exists()
    overview = bench.snapshot()["tests"]["overview"]
    assert overview["runs"] == 1
    variants = {v["key"]: v for v in overview["variants"]}
    assert variants["V1"]["verdict"] == "PASS"
    assert variants["V2"]["verdict"] == "FAIL"

    # days=0 falls back to the 7-day default: the run above is two days
    # old, so it is inside that window and still shows up
    bench.dispatch("report_overview", {"days": 0})
    assert bench.snapshot()["tests"]["overview"]["runs"] == 1

    # days=999 clamps to 90 rather than raising or being taken at face value
    bench.dispatch("report_overview", {"days": 999})
    assert bench.snapshot()["tests"]["overview"]["days"] == 90


def test_overview_is_none_until_asked_for(tmp_path):
    bench = _bench_res(tmp_path)
    assert bench.snapshot()["tests"]["overview"] is None


def test_an_unwritable_results_folder_leaves_the_overview_alone(tmp_path):
    """Same contract as a single report: a folder that cannot be written
    to is a log line, not a crash, and the last good overview stays put."""
    bench = _bench_res(tmp_path)
    bench.dispatch("set_path", {"which": "res", "value": UNWRITABLE})
    bench.dispatch("report_overview", {"days": 7})
    assert bench.snapshot()["tests"]["overview"] is None
    assert any("overview not written" in row["msg"] for row in bench.logs)


def test_a_case_that_was_not_re_run_does_not_hold_the_variant_red():
    """The variant's verdict is the newest *run*, not every case's own
    latest verdict. A case that failed a week ago and has not been run
    since would otherwise keep the variant red forever — long after the
    run that would have cleared it stopped including that case."""
    older = {"started": "2026-07-26T08:00:00", "cases": [
        _case_doc(id="4857", name="Yarn breakage", verdict="FAIL",
                  started="2026-07-26T08:00:00")]}
    newest = {"started": "2026-07-28T08:00:00", "cases": [
        _case_doc(id="4602", name="Power off", verdict="PASS",
                  started="2026-07-28T08:00:00")]}
    (group,) = collect_overview([older, newest])
    assert group.verdict == "PASS"
    # the failure is not hidden — it is still in the case's own line
    failing = next(c for c in group.cases if c.id == "4857")
    assert failing.last_verdict == "FAIL" and failing.rate == "0/1"


def test_the_shipped_stylesheet_is_a_file_and_is_what_gets_written(tmp_path):
    """The look lives in canopen_bench/testReportStyle.css, not in a string
    inside report.py, and what lands in the results folder is that file
    unchanged — so editing the shipped stylesheet is editing what a run
    actually produces, and it can be read and diffed as the CSS it is."""
    write_stylesheet(tmp_path)
    written = (tmp_path / STYLESHEET).read_text(encoding="utf-8")
    assert written == default_css()
    assert "tr.testStepFlow { color: #b06010; }" in written


def test_a_flow_line_is_coloured_text_in_every_context(tmp_path):
    """A loop line is set apart by its text colour alone. It carried a
    background tint per colour scheme and a third rule for print; one rule
    now covers all three, so the three cannot drift apart."""
    css = default_css()
    assert css.count("tr.testStepFlow") == 1
    assert "background-color: #3A2E20" not in css and "background-color: #EFE2CC" not in css
