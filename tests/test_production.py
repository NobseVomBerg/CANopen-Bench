"""Production procedures: flows marked `grade: production`, their `inputs:`,
the `scan` and `swdl` steps, and running one from the Production view."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from conftest import connect_and_scan, write_seed_eds_files

import canopen_bench.core as core_mod
from canopen_bench import testcases as tclib
from canopen_bench.core import Bench
from canopen_bench.db import Db
from canopen_bench.plugin import BenchPlugin, SwdlStrategy

FLOW = """\
id: "flash"
name: "flash one board"
grade: production
desc: "Press Start."
inputs:
  fw: "Firmware"
steps:
  - log: "looking for the board"
  - scan: {expect: 1}
  - log: "downloading"
  - swdl: {file: $fw, mode: pdo}
"""


class _InstantSwdl(SwdlStrategy):
    """A download that is done one event-loop turn after it started, or
    fails with ``fail`` set — what a vendor strategy reports, without one."""

    name = "instant"
    fail = ""

    def start(self, bench) -> None:
        bench.swdl_run, bench.swdl_done = True, False
        bench.swdl_prog, bench.swdl_phase, bench.swdl_err = {}, {}, {}
        nodes = [d["node"] for d in bench.sel_devices]
        self.flashed = (bench.fw_sel, nodes)
        self.mode = bench.swdl_mode

        async def run():
            await asyncio.sleep(0.05)
            for n in nodes:
                if self.fail:
                    bench.swdl_err[n] = self.fail
                else:
                    bench.swdl_prog[n], bench.swdl_phase[n] = 100, "done"
            bench.swdl_run, bench.swdl_done = False, True
        bench.spawn(run())


class _Firmware(BenchPlugin):
    name = "fwtest"

    def __init__(self) -> None:
        self.strategy = _InstantSwdl()

    def describe_firmware(self, path: Path) -> dict | None:
        return {"ver": path.stem} if path.suffix == ".bin" else None

    def swdl_strategy(self):
        return self.strategy


@pytest.fixture()
def bench(tmp_path):
    plugin = _Firmware()
    bench = Bench(Db(tmp_path / "prod.db"), plugins=[plugin])
    write_seed_eds_files(bench)
    bench.dispatch("set_scan_range", {"from": 1, "to": 1})
    connect_and_scan(bench)
    folder = Path(bench.paths["fw"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "board_2.bin").write_bytes(b"image")
    (folder / "notes.txt").write_text("not firmware")
    (bench.flows_dir / "flash.yaml").write_text(FLOW, encoding="utf-8")
    bench.plugin = plugin
    return bench


def _run(bench, body):
    orig = core_mod.SCAN_DELAY_S
    core_mod.SCAN_DELAY_S = 0.02
    try:
        asyncio.run(body())
    finally:
        core_mod.SCAN_DELAY_S = orig


def _flash(bench, during=None):
    async def go():
        bench.dispatch("flow_run", {"file": "flash.yaml"})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        while bench.teach is not None and loop.time() < deadline:
            if during:
                during(bench)
            await asyncio.sleep(0.02)
        # asked here: once the loop is closed, a run still going is cancelled
        # and its clean-up says it ended, whether it did or not
        assert bench.teach is None, "the run did not finish in time"
    _run(bench, go)


# -- the format ------------------------------------------------------------------

def test_the_header_says_production_and_what_it_asks_for():
    head = tclib.flow_header(FLOW)
    assert head["production"] and head["inputs"] == {"fw": "Firmware"} and head["desc"] == "Press Start."


def test_an_input_is_not_taken_for_a_header_symbol():
    class NoSymbols:
        def value(self, name):
            raise KeyError(name)
    tc = tclib.parse_testcase(FLOW, "flash.yaml", require_prefix=False, symbols=NoSymbols())
    assert tc.error is None and tc.inputs == {"fw": "Firmware"}


@pytest.mark.parametrize("change, error", [
    (("$fw,", "$bl,"), "$bl is not one of the inputs"),
    (('  fw: "Firmware"', '  Fw: "Firmware"'), "input name 'Fw'"),
    (("scan: {expect: 1}", "scan: {expect: one}"), "scan: expect must be a number"),
    (("swdl: {file: $fw, mode: pdo}", 'swdl: {file: ""}'), "swdl: file must be"),
    (("mode: pdo}", "mode: can}"), "swdl: mode must be sdo or pdo"),
])
def test_a_broken_flow_says_what_is_wrong(change, error):
    tc = tclib.parse_testcase(FLOW.replace(*change), "flash.yaml", require_prefix=False)
    assert tc.error and error in tc.error, tc.error


# -- the Production view ------------------------------------------------------------

def test_a_production_flow_is_listed_there_and_nowhere_else(bench):
    snap = bench.snapshot()
    assert snap["production"] == [{"file": "flash.yaml", "name": "flash one board",
                                   "desc": "Press Start.", "last": None,
                                   "inputs": [{"name": "fw", "label": "Firmware", "value": ""}]}]
    assert "flash.yaml" not in snap["mc"]["flows"]
    bench.dispatch("mc_flow", {"file": "flash.yaml"})
    assert bench.mc["teachFlow"] != "flash.yaml", "machine control never re-addresses with it"


def test_only_a_firmware_file_can_be_picked_and_the_pick_is_kept(bench):
    bench.dispatch("flow_input", {"file": "flash.yaml", "name": "fw", "value": "board_2.bin"})
    bench.dispatch("flow_input", {"file": "flash.yaml", "name": "fw", "value": "notes.txt"})
    assert bench.db.get("flow_inputs") == {"flash.yaml": {"fw": "board_2.bin"}}, "the refusal kept the pick"
    again = Bench(Db(bench.db.path), plugins=[_Firmware()])
    assert again.snapshot()["production"][0]["inputs"][0]["value"] == "board_2.bin"


def test_it_does_not_start_without_its_inputs(bench):
    bench.dispatch("flow_run", {"file": "flash.yaml"})
    assert bench.teach is None
    assert "choose Firmware first" in bench.logs[-1]["msg"]


def test_a_run_scans_and_flashes_the_one_board(bench):
    bench.dispatch("flow_input", {"file": "flash.yaml", "name": "fw", "value": "board_2.bin"})
    bench.devices[0]["sel"] = False
    _flash(bench)
    assert bench.production_last["flash.yaml"] == {"ok": True, "text": "downloading"}
    assert bench.plugin.strategy.flashed == ("board_2.bin", [1])
    assert bench.plugin.strategy.mode == "pdo" and bench.swdl_mode == "sdo", "the page's mode is back"
    assert not bench.devices[0]["sel"], "the page's selection is back as it was"


def test_a_failed_download_fails_the_run_with_its_reason(bench):
    bench.dispatch("flow_input", {"file": "flash.yaml", "name": "fw", "value": "board_2.bin"})
    bench.plugin.strategy.fail = "flash write error"
    _flash(bench)
    last = bench.production_last["flash.yaml"]
    assert not last["ok"] and "swdl board_2.bin node 01: flash write error" in last["text"]
    assert 'at "downloading"' in last["text"], "the card says where it failed"


def test_abort_lets_a_waiting_prompt_go(bench):
    (bench.flows_dir / "flash.yaml").write_text(
        FLOW.replace('  - log: "looking for the board"', '  - manual: "plug in the board"'),
        encoding="utf-8")
    bench.dispatch("flow_input", {"file": "flash.yaml", "name": "fw", "value": "board_2.bin"})
    _flash(bench, during=lambda b: b.manual_prompt and b.dispatch("mc_teach_abort", {}))
    assert bench.production_last["flash.yaml"]["text"].startswith("aborted")


def test_the_swdl_page_cannot_start_a_download_during_a_run(bench):
    bench.dispatch("flow_input", {"file": "flash.yaml", "name": "fw", "value": "board_2.bin"})
    bench.teach = {"step": 1, "of": 4, "text": "", "name": "x", "file": "flash.yaml"}
    bench.devices[0]["sel"] = True
    bench.dispatch("swdl_start", {})
    assert not bench.swdl_run and "a procedure is running" in bench.logs[-1]["msg"]


def test_a_test_case_names_its_files_instead():
    """Inputs are asked for on the Production page; a case from the catalog
    would run with `$fw` meaning nothing, and fail every time."""
    tc = tclib.parse_testcase(FLOW.replace('id: "flash"', 'id: "9001"'), "TC9001_flash.yaml")
    assert tc.error and "only a production flow" in tc.error


CLEANING = FLOW.replace("grade: production\n", "grade: production\non_fail: continue\n") + """\
  - jump_on_error: clean
  - log: "all done"
  - end:
  - label: clean
  - log: "cleaning up"
"""


@pytest.mark.parametrize("on_fail, cleaned", [(True, True), (False, False)])
def test_a_failure_reaches_the_clean_up_only_when_the_file_asks(bench, on_fail, cleaned):
    """on_fail: continue lets a production flow put the board right after a
    failed step; without it the run ends where the failure found it, so a
    failed download is never followed by the next one."""
    text = CLEANING if on_fail else CLEANING.replace("on_fail: continue\n", "")
    (bench.flows_dir / "flash.yaml").write_text(text, encoding="utf-8")
    bench.dispatch("flow_input", {"file": "flash.yaml", "name": "fw", "value": "board_2.bin"})
    bench.plugin.strategy.fail = "flash write error"
    _flash(bench)
    last = bench.production_last["flash.yaml"]
    assert not last["ok"] and "flash write error" in last["text"]
    assert 'at "downloading"' in last["text"], "the phase is where it failed, not where it cleaned up"
    assert any(ln["msg"].endswith("cleaning up") for ln in bench.logs) == cleaned
    assert not any(ln["msg"].endswith("all done") for ln in bench.logs)


def test_a_button_flow_asks_for_nothing():
    flow = FLOW.replace("grade: production\n", 'button: "Flash"\n')
    tc = tclib.parse_testcase(flow, "flash.yaml", require_prefix=False)
    assert tc.error and "only a production flow" in tc.error
