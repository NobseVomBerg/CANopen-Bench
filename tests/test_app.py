"""App-level integration coverage for multi-workspace mode (canopen_bench/app.py)."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

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
