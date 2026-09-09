"""Button-teach orchestration (A-05): core-only coverage — the run/abort/
fail/auto-gating/procedure-selection machinery in `Bench`, driven by small
inline flows written straight into `bench.flows_dir`. No vendor protocol
here: the shipped bare-core default is the standard-LSS flow
(`canopen_bench/flows/lss_standard.yaml`), and $session only ever appears
in the one negative test that proves a flow using it fails cleanly without
an addressing provider.

The full button-teach protocol (session distribution, offer/claim loop,
Addr-End) lives with whichever vendor plugin actually implements it, in
that plugin's own test suite.
"""
from __future__ import annotations

import asyncio

import pytest
from conftest import connect_and_scan, write_seed_eds_files

import canopen_bench.core as core_mod
from canopen_bench.core import Bench
from canopen_bench.db import Db
from canopen_bench.plugin import BenchPlugin


def _run(coro_fn) -> None:
    """Run a no-arg async body inside asyncio.run(), with SCAN_DELAY_S shrunk
    for the duration — a successful teach re-scans/verifies afterward, and
    mc_verify always scans first, so tests stay fast either way."""
    orig = core_mod.SCAN_DELAY_S
    core_mod.SCAN_DELAY_S = 0.02
    try:
        asyncio.run(coro_fn())
    finally:
        core_mod.SCAN_DELAY_S = orig


def _write_flow(bench: Bench, filename: str, text: str) -> None:
    (bench.flows_dir / filename).write_text(text, encoding="utf-8")


@pytest.fixture()
def teach_bench(tmp_path):
    bench = Bench(Db(tmp_path / "teach.db"))
    write_seed_eds_files(bench)
    connect_and_scan(bench)  # 3 demo devices (dut_alpha_v2 x2, dut_gamma_v5 x1)
    bench.dispatch("mc_adopt", {})  # adopts the current bus state as the reference
    assert bench.mc["expected"] == 3
    return bench


def test_standard_lss_end_to_end_readdresses_and_adopts(teach_bench):
    """Operator-triggered teach (the button, not auto re-address): runs
    open-ended across the whole address range, then the freshly scanned
    bus becomes the new expected state — no verify against a prior
    reference, since the button press itself is the operator's
    confirmation."""
    bench = teach_bench
    assert bench.mc["teachFlow"] == "lss_standard.yaml"  # bare-core default

    async def go():
        bench.dispatch("mc_readdress", {})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while bench.teach is not None and loop.time() < deadline:
            await asyncio.sleep(0.02)
        deadline = loop.time() + 5.0
        while bench.mc["busy"] and loop.time() < deadline:
            await asyncio.sleep(0.02)
    _run(go)

    assert bench.teach is None, "teach did not finish in time"
    assert not bench.mc["busy"], "post-teach scan/adopt did not finish in time"
    assert {d["node"] for d in bench.devices} == {1, 2, 3}
    assert bench.mc_ref["expected"] == 3
    assert any("teach complete" in ln["msg"] for ln in bench.logs)
    assert any("expected state adopted" in ln["msg"] for ln in bench.logs)
    assert not any("session" in ln["msg"] and "distributed" in ln["msg"] for ln in bench.logs)


def test_operator_abort_stops_teach(teach_bench):
    bench = teach_bench
    _write_flow(bench, "blocks.yaml",
                'id: "blocks"\nname: "blocks for abort"\nsteps:\n'
                '  - wait_for: {cob: "0x784", timeout: 5}\n')
    bench.dispatch("mc_flow", {"file": "blocks.yaml"})

    async def go():
        bench.dispatch("mc_readdress", {})
        await asyncio.sleep(0.05)  # let the task start waiting
        assert bench.teach is not None
        bench.dispatch("mc_teach_abort", {})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while bench.teach is not None and loop.time() < deadline:
            await asyncio.sleep(0.02)
    _run(go)

    assert bench.teach is None, "teach did not end in time"
    assert any("teach aborted" in ln["msg"] for ln in bench.logs)


def test_failure_path_logs_teach_fail(teach_bench):
    bench = teach_bench
    _write_flow(bench, "fails.yaml",
                'id: "fails"\nname: "always fails"\nsteps:\n'
                '  - fail: "deliberate failure"\n')
    bench.dispatch("mc_flow", {"file": "fails.yaml"})

    async def go():
        bench.dispatch("mc_readdress", {})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while bench.teach is not None and loop.time() < deadline:
            await asyncio.sleep(0.02)
    _run(go)

    assert bench.teach is None, "teach did not end in time"
    assert any("teach FAIL" in ln["msg"] for ln in bench.logs)


def test_auto_gating_starts_teach_when_mc_enabled(teach_bench):
    bench = teach_bench
    (bench.db.eds_dir / "dut_gamma_v5.eds").unlink()  # scan will now find only 2 of 3
    _write_flow(bench, "quick.yaml",
                'id: "quick"\nname: "harmless flow"\nsteps:\n'
                '  - log: "auto teach"\n  - wait: 1.0\n')
    bench.dispatch("mc_flow", {"file": "quick.yaml"})
    bench.mc["enabled"] = True
    bench.mc["autoReaddr"] = True

    async def go():
        bench.dispatch("mc_verify", {})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while bench.teach is None and loop.time() < deadline:
            await asyncio.sleep(0.02)
        assert bench.teach is not None, "teach did not start"
        bench.dispatch("mc_teach_abort", {})  # cleanup: don't leave a task running
        deadline = loop.time() + 3.0
        while bench.teach is not None and loop.time() < deadline:
            await asyncio.sleep(0.02)
    _run(go)

    assert bench.teach is None, "teach did not clean up in time"


def test_auto_gating_skips_teach_when_mc_disabled(teach_bench):
    bench = teach_bench
    (bench.db.eds_dir / "dut_gamma_v5.eds").unlink()  # scan will now find only 2 of 3
    bench.mc["enabled"] = False

    async def go():
        bench.dispatch("mc_verify", {})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while bench.mc["busy"] and loop.time() < deadline:
            await asyncio.sleep(0.02)
    _run(go)

    assert not bench.mc["busy"], "verify did not finish in time"
    assert bench.mc["result"] == "mismatch"
    assert bench.teach is None


def test_mc_flow_selects_alternate_procedure_and_persists(teach_bench):
    bench = teach_bench
    _write_flow(bench, "alt_flow.yaml",
                'id: "teach-alt"\nname: "alt flow"\nsteps:\n  - log: "alt"\n')

    bench.dispatch("mc_flow", {"file": "alt_flow.yaml"})
    assert bench.mc["teachFlow"] == "alt_flow.yaml"

    reloaded = Bench(Db(bench.db.path))
    assert reloaded.mc["teachFlow"] == "alt_flow.yaml"

    bench.dispatch("mc_flow", {"file": "does_not_exist.yaml"})
    assert bench.mc["teachFlow"] == "alt_flow.yaml"


def test_can_send_session_without_provider_fails_the_teach(teach_bench):
    """No plugin -> bench.addressing is None -> $session in a flow's
    can_send data can never be resolved; the step fails with a clear
    reason instead of crashing, and the run ends "teach FAIL"."""
    bench = teach_bench
    _write_flow(bench, "wants_session.yaml",
                'id: "wants-session"\nname: "needs session"\nsteps:\n'
                '  - can_send: {cob: "0x781", data: [$session, "0x02", 0, 0]}\n')
    bench.dispatch("mc_flow", {"file": "wants_session.yaml"})

    async def go():
        bench.dispatch("mc_readdress", {})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while bench.teach is not None and loop.time() < deadline:
            await asyncio.sleep(0.02)
    _run(go)

    assert bench.teach is None, "teach did not end in time"
    fail_logs = [ln["msg"] for ln in bench.logs if "teach FAIL" in ln["msg"]]
    assert fail_logs, "no teach FAIL log line found"
    assert "no addressing provider" in fail_logs[-1]


def test_fresh_bench_normalizes_teach_flow_to_lss_standard(tmp_path):
    bench = Bench(Db(tmp_path / "fresh.db"))
    assert bench.mc["teachFlow"] == "lss_standard.yaml"


class _FlowOnlyPlugin(BenchPlugin):
    """Minimal plugin that only contributes flow files — enough to prove
    teachFlow normalization prefers the procedure a plugin brought over the
    standard-LSS flow the core ships."""
    name = "flowonly"

    def __init__(self, flow_dir):
        self._flow_dir = flow_dir

    def flow_dirs(self):
        return [self._flow_dir]


def _vendor_flow(tmp_path, name: str = "acme_buttons.yaml"):
    flow_src = tmp_path / "flow_src"
    flow_src.mkdir(exist_ok=True)
    (flow_src / name).write_text(
        'id: "vendor"\nname: "vendor addressing"\nsteps:\n  - log: "hi"\n',
        encoding="utf-8")
    return flow_src


def test_a_fresh_workspace_prefers_the_procedure_a_plugin_brought(tmp_path):
    """How a device family is addressed is that family's business, and the
    LSS flow this package ships is what is left when nobody says otherwise.
    By where the file came from, not by what it is called — a core that
    knew the vendor's file name would be carrying a fact about the
    vendor."""
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_FlowOnlyPlugin(_vendor_flow(tmp_path))])
    assert bench.mc["teachFlow"] == "acme_buttons.yaml"


def test_a_chosen_procedure_survives_the_next_start(tmp_path):
    """The preference is for a workspace nobody has decided about yet.
    Somebody who picked the LSS flow with a plugin installed meant it."""
    db_path = tmp_path / "x.db"
    plugins = [_FlowOnlyPlugin(_vendor_flow(tmp_path))]
    bench = Bench(Db(db_path), plugins=plugins)
    bench.dispatch("mc_flow", {"file": "lss_standard.yaml"})
    assert Bench(Db(db_path), plugins=plugins).mc["teachFlow"] == "lss_standard.yaml"


# -- operator teach is bounded by the address range, not an adopted count --

def test_operator_teach_starts_without_any_adopted_state(tmp_path):
    """The address range (Bus interface) is the only thing that governs
    addressing — an operator teach must work on a completely fresh
    workspace, before anything has ever been adopted."""
    bench = Bench(Db(tmp_path / "count.db"))
    write_seed_eds_files(bench)
    connect_and_scan(bench)
    assert bench.mc_ref is None
    assert bench.mc["expected"] == 0

    async def go():
        bench.dispatch("mc_readdress", {})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while bench.teach is not None and loop.time() < deadline:
            await asyncio.sleep(0.02)
        deadline = loop.time() + 5.0
        while bench.mc["busy"] and loop.time() < deadline:
            await asyncio.sleep(0.02)
    _run(go)

    assert bench.teach is None, "teach did not finish in time"
    assert not any("expected device count unknown" in ln["msg"] for ln in bench.logs)
    assert bench.mc_ref is not None and bench.mc_ref["expected"] == 3, \
        "the freshly scanned bus should be adopted, with no prior state required"


def test_operator_teach_adopts_freshly_scanned_count_over_a_stale_reference(teach_bench):
    """A teach the operator triggers always ends by scanning and adopting
    what it actually finds — never by verifying against whatever was
    adopted before, which would be meaningless once the machine changed."""
    bench = teach_bench  # 3 real demo devices, already adopted with expected=3
    bench.mc_ref["expected"] = 1  # stale reference (machine grew since then)
    bench.mc["expected"] = 1

    async def go():
        bench.dispatch("mc_readdress", {})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while bench.teach is not None and loop.time() < deadline:
            await asyncio.sleep(0.02)
        deadline = loop.time() + 5.0
        while bench.mc["busy"] and loop.time() < deadline:
            await asyncio.sleep(0.02)
    _run(go)

    assert bench.teach is None, "teach did not finish in time"
    assert bench.mc_ref["expected"] == 3, "the freshly scanned state should be re-adopted"
    assert any("expected state adopted" in ln["msg"] for ln in bench.logs)
    assert not any("scan & verify against the expected state" in ln["msg"] for ln in bench.logs)


def test_operator_teach_bounded_by_a_narrower_address_range(teach_bench):
    """Narrowing the address range narrows both what teach can address and
    what the post-teach scan (hence the adopted count) can find — the
    range is the single source of truth for both."""
    bench = teach_bench  # 3 demo devices at node-ids 1, 2, 3
    bench.dispatch("set_scan_range", {"from": 1, "to": 2})

    async def go():
        bench.dispatch("mc_readdress", {})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while bench.teach is not None and loop.time() < deadline:
            await asyncio.sleep(0.02)
        deadline = loop.time() + 5.0
        while bench.mc["busy"] and loop.time() < deadline:
            await asyncio.sleep(0.02)
    _run(go)

    assert bench.teach is None, "teach did not finish in time"
    assert bench.mc_ref["expected"] == 2, "node 3 is outside the narrowed range"


def test_operator_teach_logs_when_post_teach_scan_finds_nothing(teach_bench, monkeypatch):
    """A scan finding nothing after an otherwise successful teach must not
    silently wipe out the previously adopted expected state."""
    bench = teach_bench
    expected_before = bench.mc_ref["expected"]

    async def empty_scan():
        bench.devices = []
    monkeypatch.setattr(bench, "_scan_async", empty_scan)

    async def go():
        bench.dispatch("mc_readdress", {})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while bench.teach is not None and loop.time() < deadline:
            await asyncio.sleep(0.02)
    _run(go)

    assert bench.teach is None, "teach did not finish in time"
    assert any("scan found nothing" in ln["msg"] for ln in bench.logs)
    assert bench.mc_ref["expected"] == expected_before, "unchanged — nothing to adopt"


def test_auto_readdress_verifies_and_never_adopts_a_shrunken_bus(teach_bench):
    """The automatic re-address after a failed verification is the opposite
    of an operator teach: it stays bounded by the adopted count and ends
    with a verify, so a device that merely went offline can never
    silently vanish from the expected state."""
    bench = teach_bench  # adopted with expected=3
    (bench.db.eds_dir / "dut_gamma_v5.eds").unlink()  # scan will now find only 2 of 3
    _write_flow(bench, "quick.yaml",
                'id: "quick"\nname: "harmless flow"\nsteps:\n'
                '  - log: "auto teach"\n')
    bench.dispatch("mc_flow", {"file": "quick.yaml"})
    bench.mc["enabled"] = True
    bench.mc["autoReaddr"] = True
    n_logs_before = len(bench.logs)  # the fixture's own mc_adopt already logged once

    async def go():
        bench.dispatch("mc_verify", {})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while not bench.mc["busy"] and loop.time() < deadline:
            await asyncio.sleep(0.02)  # let the first (failing) verify start
        deadline = loop.time() + 5.0
        while bench.mc["busy"] and loop.time() < deadline:
            await asyncio.sleep(0.02)
    _run(go)

    assert bench.teach is None, "teach did not finish in time"
    assert not bench.mc["busy"], "verify did not finish in time"
    assert bench.mc_ref["expected"] == 3, "the shrunken bus must not be silently adopted"
    assert bench.mc["result"] == "mismatch"
    new_logs = bench.logs[n_logs_before:]
    assert not any("expected state adopted" in ln["msg"] for ln in new_logs)


# -- a flow may ask for a button of its own ---------------------------------

BUTTON_FLOW = '''\
id: "marker"
name: "put a marker on the bus"
button: "Marker frame"
steps:
  - can_send: {cob: "0x333", data: ["0x01"]}
'''


def test_a_flow_that_declares_a_button_is_offered_as_one(teach_bench):
    """A procedure somebody presses a key for — force a single device onto
    a known node-ID, put a family into a service mode — is a sequence of
    frames like the addressing procedure is. The only thing the tool
    cannot work out is what to call it, so the file says."""
    bench = teach_bench
    _write_flow(bench, "marker.yaml", BUTTON_FLOW)
    procedures = bench.snapshot()["mc"]["procedures"]
    assert procedures == [{"file": "marker.yaml", "label": "Marker frame"}]


def test_a_procedure_with_a_button_is_not_the_addressing_procedure(teach_bench):
    """The two are different jobs and a file is one of them. Auto
    re-address runs the selected procedure by itself, on a bus with every
    device still on it — a procedure that puts one device on a fixed
    node-ID must not be reachable from there."""
    bench = teach_bench
    _write_flow(bench, "marker.yaml", BUTTON_FLOW)
    assert "marker.yaml" not in bench.snapshot()["mc"]["flows"]

    bench.dispatch("mc_flow", {"file": "marker.yaml"})
    assert bench.mc["teachFlow"] == "lss_standard.yaml", "the choice was refused"


def test_pressing_it_runs_the_flow_once(teach_bench):
    """No session distributed, nothing adopted, nothing verified: it runs
    the frames in the file and says how it went."""
    bench = teach_bench
    _write_flow(bench, "marker.yaml", BUTTON_FLOW)
    before = dict(bench.mc_ref or {})

    async def go():
        bench.dispatch("mc_procedure", {"file": "marker.yaml"})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while bench.teach is not None and loop.time() < deadline:
            await asyncio.sleep(0.02)
    _run(go)

    assert bench.teach is None, "the procedure did not finish in time"
    sent = [f for f in bench.bus.poll_frames(4096)
            if f.cob_id == "0x333" and f.direction == "TX"]
    assert sent, "the frame never reached the bus"
    assert any('procedure "Marker frame" done' in ln["msg"] for ln in bench.logs)
    assert bench.mc_ref == before, "a procedure adopts nothing"
    assert not bench.mc["session"], "and distributes no session"


def test_a_procedure_without_a_button_cannot_be_pressed(teach_bench):
    """The button is what says a flow is meant to be run on its own. Any
    file name from a browser would otherwise start any flow in the folder,
    including the addressing procedure, without the guards that has."""
    bench = teach_bench
    _write_flow(bench, "quiet.yaml",
                'id: "quiet"\nname: "no button"\nsteps:\n  - log: "hi"\n')
    bench.dispatch("mc_procedure", {"file": "quiet.yaml"})
    assert bench.teach is None
    assert any("no procedure button" in ln["msg"] for ln in bench.logs)


def test_a_procedure_needs_the_interface(tmp_path):
    """Same rule the addressing button follows: a procedure that cannot
    reach the bus says so instead of running against nothing."""
    bench = Bench(Db(tmp_path / "off.db"))
    _write_flow(bench, "marker.yaml", BUTTON_FLOW)
    bench.dispatch("mc_procedure", {"file": "marker.yaml"})
    assert bench.teach is None
    assert any("not connected" in ln["msg"] for ln in bench.logs)
