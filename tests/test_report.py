"""Run reports: the HTML a run leaves behind, and the data beside it.

The point of these files is being readable a week later, by somebody who
was not there. So the tests are about what a reader can find in them —
which device, which step failed and why — rather than about markup.
"""
from __future__ import annotations

import json
from pathlib import Path

from conftest import connect_and_scan, write_seed_eds_files

from canopen_bench.core import Bench
from canopen_bench.db import Db
from canopen_bench.report import (
    STYLESHEET,
    CaseRecord,
    RunRecord,
    StepRecord,
    case_html,
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


def test_an_unwritable_results_folder_does_not_lose_the_run(tmp_path):
    """The verdicts are already on screen and in the log. A report that
    cannot be written is a line in the log, not a failed run."""
    bench = _bench(tmp_path)
    bench.dispatch("set_path", {"which": "res", "value": "/proc/nope/results"})
    _run_all(bench)
    assert bench.results.get("0101") == "PASS"
    assert any("report not written" in row["msg"] for row in bench.logs)
