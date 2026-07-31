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
    monkeypatch.setattr("canopen_bench.core.load_plugins", lambda: [])


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
