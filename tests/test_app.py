"""App-level integration coverage for multi-workspace mode (canopen_bench/app.py)."""
from __future__ import annotations

import contextlib
import time

import pytest
from starlette.testclient import TestClient

from canopen_bench.app import create_app


@pytest.fixture(autouse=True)
def _no_installed_plugins(monkeypatch):
    """Keep the suite hermetic, same as test_core.py's fixture."""
    monkeypatch.setattr("canopen_bench.core.load_plugins", lambda **kw: [])


def test_root_mode_defaults_to_default_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPEN_BENCH_DATA", str(tmp_path))
    monkeypatch.delenv("CANOPEN_BENCH_DB", raising=False)

    app = create_app()
    with TestClient(app) as client:
        state = client.get("/api/state").json()

    assert state["workspace"] == "default"
    assert state["workspaces"]["canSwitch"] is True
    assert (tmp_path / "default").is_dir()


def test_workspace_create_swaps_bench_and_persists_active_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPEN_BENCH_DATA", str(tmp_path))
    monkeypatch.delenv("CANOPEN_BENCH_DB", raising=False)

    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/api/action", json={"action": "workspace_create",
                                                 "params": {"name": "LinieA"}})
        assert resp.status_code == 200

        deadline = time.monotonic() + 2.0
        state = client.get("/api/state").json()
        while state["workspace"] != "LinieA" and time.monotonic() < deadline:
            time.sleep(0.05)
            state = client.get("/api/state").json()

    assert state["workspace"] == "LinieA"
    assert (tmp_path / "active-workspace").read_text(encoding="utf-8") == "LinieA"
    assert set(state["workspaces"]["list"]) >= {"default", "LinieA"}


def test_explicit_db_path_disables_workspace_switching(tmp_path, monkeypatch):
    monkeypatch.delenv("CANOPEN_BENCH_DATA", raising=False)
    monkeypatch.delenv("CANOPEN_BENCH_DB", raising=False)

    app = create_app(db_path=str(tmp_path / "x.db"))
    with TestClient(app) as client:
        state = client.get("/api/state").json()

    assert state["workspaces"]["canSwitch"] is False
    assert state["workspace"] == "x"


def _seed_trace(app) -> None:
    app.state.bench.trace = [
        {"time": "12:00:00.000000", "dir": "RX", "cob": "0x581", "len": "2",
         "data": "01 02", "dec": "SDO tx node 01", "flag": "", "cls": "SDO",
         "node": 1, "obj": "", "val": ""},
        {"time": "12:00:00.500000", "dir": "RX", "cob": "0x000", "len": "0",
         "data": "", "dec": "NMT", "flag": "", "cls": "NMT",
         "node": None, "obj": "", "val": ""},
    ]


def test_trace_export_csv_endpoint(tmp_path, monkeypatch):
    monkeypatch.delenv("CANOPEN_BENCH_DATA", raising=False)
    monkeypatch.delenv("CANOPEN_BENCH_DB", raising=False)

    app = create_app(db_path=str(tmp_path / "x.db"))
    with TestClient(app) as client:
        _seed_trace(app)
        expected = app.state.bench._trace_csv()
        resp = client.get("/api/trace/export.csv")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment; filename=\"trace_")
    assert disposition.endswith(".csv\"")
    assert resp.text == expected


def test_trace_export_candump_endpoint(tmp_path, monkeypatch):
    monkeypatch.delenv("CANOPEN_BENCH_DATA", raising=False)
    monkeypatch.delenv("CANOPEN_BENCH_DB", raising=False)

    app = create_app(db_path=str(tmp_path / "x.db"))
    with TestClient(app) as client:
        _seed_trace(app)
        expected = app.state.bench._trace_candump()
        resp = client.get("/api/trace/export/candump")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment; filename=\"trace_")
    assert disposition.endswith(".candump.log\"")
    assert resp.text == expected


# -- the loop everything on screen comes from -------------------------------

def test_a_failing_notifier_does_not_end_the_tick_loop():
    """The tick loop drains the trace, updates bus load, checks heartbeats
    and pushes every state change. It had no guard at all, so one raise out
    of the notifier ended it for good — and since nothing closes the
    WebSocket, the browser kept a healthy socket that never received
    another message. A finished run still read "Running…".

    The tick no longer awaits the notifier itself — every push goes through
    the one coalescing task — so the guard that reports a raising notifier
    lives there now. What must hold is unchanged: the loop keeps going, the
    failure is on screen, and it is said once rather than every tick.
    """
    import asyncio
    import tempfile
    from pathlib import Path

    import canopen_bench.core as core_mod
    from canopen_bench.core import Bench
    from canopen_bench.db import Db

    bench = Bench(Db(Path(tempfile.mkdtemp()) / "t.db"))
    calls = []

    async def exploding() -> None:
        calls.append(1)
        raise RuntimeError("Set changed size during iteration")

    bench.set_notifier(exploding)
    bench.swdl_run = True          # makes every tick dirty, so it notifies

    async def go():
        orig, core_mod.TICK_S = core_mod.TICK_S, 0.01
        task = asyncio.ensure_future(bench._tick_loop_body())
        try:
            await asyncio.sleep(0.15)
        finally:
            core_mod.TICK_S = orig
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    asyncio.run(go())

    assert len(calls) > 2, "the loop stopped after the first failure"
    assert any("state push failed" in ln["msg"] for ln in bench.logs)
    # the same failure every tick must not fill the log with itself
    assert sum("state push failed" in ln["msg"] for ln in bench.logs) == 1


def test_broadcast_survives_a_client_connecting_while_it_sends():
    """`for ws in clients` with an await inside: a browser connecting or a
    tab closing during a send mutated the set mid-iteration. That raised
    out of the whole broadcast, so every client after that point silently
    missed the update."""
    import asyncio

    app = create_app(db_path=":memory:")
    clients = app.state.ws_clients
    delivered = []
    joined = []

    class Client:
        """Whichever of these is served first is when the next browser
        connects — set iteration order is not ours to choose, so every
        client mutates on the first send and none on the rest."""

        async def send_json(self, msg):
            delivered.append(self)
            if not joined:
                joined.append(Client())
                clients.add(joined[0])

    originals = [Client() for _ in range(3)]
    clients.update(originals)
    asyncio.run(app.state.bench._notify())
    missed = [c for c in originals if c not in delivered]
    assert not missed, f"{len(missed)} of 3 clients never got the update"


def test_state_pushes_coalesce_but_never_drop_the_last_one():
    """The executor asks for a push after every step, including the register
    arithmetic and jumps a case spends most of its steps on. Measured against
    a browser before this: 3627 pushes and 41 MB through the socket in a
    five-second run, a panel that never repainted between the first case and
    the end, and a run that took 17 times as long as it needed to.

    So a request arriving during a push becomes one trailing push rather than
    another queued snapshot. The trailing half is what makes that safe: the
    last request is the one carrying "the run has finished", and dropping it
    is exactly what plain throttling would do.
    """
    import asyncio
    import tempfile
    from pathlib import Path

    from canopen_bench.core import Bench
    from canopen_bench.db import Db

    bench = Bench(Db(Path(tempfile.mkdtemp()) / "t.db"))
    seen = []

    async def slow_push() -> None:
        await asyncio.sleep(0.02)      # a real one serialises and sends
        seen.append(bench.run_idx)

    bench.set_notifier(slow_push)

    async def go():
        for i in range(200):           # 200 steps, as fast as they execute
            bench.run_idx = i
            bench._changed()
            await asyncio.sleep(0)
        while bench._push_task is not None and not bench._push_task.done():
            await asyncio.sleep(0.01)
    asyncio.run(go())

    assert len(seen) < 20, f"{len(seen)} pushes for 200 requests — not coalescing"
    assert seen[-1] == 199, "the state asked for last never reached the screen"


# -- reports are files somebody wants to open --------------------------------

def test_a_report_can_be_fetched_and_only_from_the_results_folder(tmp_path, monkeypatch):
    """The run panel used to render the summary's name as underlined accent
    text with a pointer cursor and no handler — a link that is not one. It is
    served now, and the reports link to each other and to their stylesheet by
    bare file name, so a summary opened this way still reaches its per-case
    pages.

    The name comes out of the URL and the results folder is a path the
    operator chose, so "anything under that folder" must not widen into
    "anything on the disk".
    """
    monkeypatch.delenv("CANOPEN_BENCH_DATA", raising=False)
    monkeypatch.delenv("CANOPEN_BENCH_DB", raising=False)

    app = create_app(db_path=str(tmp_path / "x.db"))
    with TestClient(app) as client:
        folder = app.state.bench._results_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "20260101_010101__summary.html").write_text("<h1>run</h1>", encoding="utf-8")
        (folder / "testReportStyle.css").write_text("body{}", encoding="utf-8")
        secret = folder.parent / "bench.db"
        secret.write_text("not yours", encoding="utf-8")

        assert client.get("/api/report/20260101_010101__summary.html").text == "<h1>run</h1>"
        assert client.get("/api/report/testReportStyle.css").status_code == 200

        for probe in ("../bench.db", "..%2Fbench.db", "%2e%2e%2fbench.db",
                      "nothing.html", "20260101_010101__summary.txt"):
            resp = client.get(f"/api/report/{probe}", follow_redirects=False)
            assert resp.status_code in (307, 404), f"{probe} was served: {resp.status_code}"
        assert "not yours" not in client.get("/api/report/..%2Fbench.db").text


def test_only_a_report_that_exists_is_offered_as_a_link(tmp_path, monkeypatch):
    """The demo's example runs name files that were never on any disk. A
    link to a 404 is worse than plain text, so the entry the UI links to is
    the one a run actually wrote — it carries `file`, the examples do not."""
    monkeypatch.delenv("CANOPEN_BENCH_DATA", raising=False)
    monkeypatch.delenv("CANOPEN_BENCH_DB", raising=False)

    from canopen_bench import data

    assert not any("file" in seed for seed in data.SEED_REPORTS)

    app = create_app(db_path=str(tmp_path / "x.db"))
    with TestClient(app) as client:
        bench = app.state.bench
        bench.results = {"0001": "PASS"}
        bench._run_cases = []
        bench._push_report(["0001"])
        entry = client.get("/api/state").json()["tests"]["reports"][0]
        assert entry["file"] == entry["name"]
        # and what it points at is really there
        assert client.get(f"/api/report/{entry['file']}").status_code == 200


# -- the window the panel is scrolled to ------------------------------------

def test_trace_rows_endpoint_answers_a_window_newest_first(tmp_path, monkeypatch):
    monkeypatch.delenv("CANOPEN_BENCH_DATA", raising=False)
    monkeypatch.delenv("CANOPEN_BENCH_DB", raising=False)

    app = create_app(db_path=str(tmp_path / "x.db"))
    with TestClient(app) as client:
        _seed_trace(app)
        newest = client.get("/api/trace/rows?end=0&n=1").json()
        older = client.get("/api/trace/rows?end=1&n=1").json()
        past_the_end = client.get("/api/trace/rows?end=99&n=10").json()

    assert [r["cob"] for r in newest["rows"]] == ["0x000"]  # the last row recorded
    assert [r["cob"] for r in older["rows"]] == ["0x581"]
    assert newest["total"] == older["total"] == 2
    assert past_the_end["rows"] == []  # scrolled past the oldest frame: nothing, not an error


def test_trace_rows_endpoint_falls_back_on_junk_and_caps_the_page(tmp_path, monkeypatch):
    """The query string comes from a scroll handler, and a request for the
    whole 200k-row buffer is what the exports are for."""
    monkeypatch.delenv("CANOPEN_BENCH_DATA", raising=False)
    monkeypatch.delenv("CANOPEN_BENCH_DB", raising=False)

    app = create_app(db_path=str(tmp_path / "x.db"))
    bench = None
    with TestClient(app) as client:
        bench = app.state.bench
        bench.trace = [dict(_seed_trace_row(i)) for i in range(3000)]
        junk = client.get("/api/trace/rows?end=nonsense&n=").json()
        huge = client.get("/api/trace/rows?end=0&n=999999").json()
        negative = client.get("/api/trace/rows?end=-5&n=-5").json()

    assert len(junk["rows"]) == 200 and junk["end"] == 0   # the documented defaults
    assert len(huge["rows"]) == 2000                       # TRACE_PAGE_MAX
    assert negative["end"] == 0 and len(negative["rows"]) == 1


def _seed_trace_row(i: int) -> dict:
    return {"time": f"12:00:{i % 60:02d}.000000", "dir": "RX", "cob": "0x581", "len": "1",
            "data": "01", "dec": "SDO tx node 01", "flag": "", "cls": "SDO",
            "node": 1, "obj": "", "val": str(i)}
