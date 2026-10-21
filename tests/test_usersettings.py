"""Where the data folder is: chosen under Setup, kept per user, so the bench
finds it wherever it is started from (usersettings)."""
from __future__ import annotations

from pathlib import Path

import pytest

from canopen_bench import usersettings
from canopen_bench.core import Bench
from canopen_bench.db import Db


@pytest.fixture()
def elsewhere(tmp_path, monkeypatch):
    """Started from a folder of its own, no CANOPEN_BENCH_DATA, a home of its own."""
    start = tmp_path / "started-here"
    start.mkdir()
    monkeypatch.chdir(start)
    monkeypatch.delenv(usersettings.ENV_DATA, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return start


def test_a_data_folder_where_it_is_started_is_kept_and_remembered(elsewhere, monkeypatch, tmp_path):
    """How every bench so far found its workspaces; from now on it finds
    them from wherever it is started."""
    (elsewhere / "data").mkdir()
    assert usersettings.data_root() == (elsewhere / "data").resolve()
    monkeypatch.chdir(tmp_path)
    assert usersettings.data_root() == (elsewhere / "data").resolve()


def test_without_one_it_is_in_the_home_folder(elsewhere, tmp_path):
    assert usersettings.data_root() == (tmp_path / "home" / "CANopen-Bench").resolve()
    assert not (elsewhere / "data").exists(), "not next to wherever it was started"


def test_the_environment_names_it_for_this_start_only(elsewhere, monkeypatch, tmp_path):
    monkeypatch.setenv(usersettings.ENV_DATA, str(tmp_path / "scripted"))
    assert usersettings.data_root() == tmp_path / "scripted"
    assert usersettings.load() == {}, "a start script does not move the user's folder"


def _bench(tmp_path) -> Bench:
    root = tmp_path / "data"
    return Bench(Db(root / "default" / "canopen-bench.db"), plugins=[], workspaces_root=root)


def test_setup_moves_it_from_the_next_start_on(elsewhere, tmp_path):
    bench = _bench(tmp_path)
    now = bench.snapshot()["workspaces"]
    assert now["data"] == now["dataNext"] == str((tmp_path / "data").resolve())

    bench.dispatch("set_path", {"which": "data", "value": str(tmp_path / "shared")})
    later = bench.snapshot()["workspaces"]
    assert later["dataNext"] == str((tmp_path / "shared").resolve())
    assert later["data"] == now["data"], "this start runs on where it started"
    assert "from the next start on" in bench.logs[-1]["msg"]
    assert usersettings.data_root() == (tmp_path / "shared").resolve()


def test_setup_leaves_a_folder_the_environment_names_alone(elsewhere, monkeypatch, tmp_path):
    monkeypatch.setenv(usersettings.ENV_DATA, str(tmp_path / "data"))
    bench = _bench(tmp_path)
    assert bench.snapshot()["workspaces"]["dataFixed"]
    bench.dispatch("set_path", {"which": "data", "value": str(tmp_path / "shared")})
    assert usersettings.load() == {}
    assert "set by CANOPEN_BENCH_DATA" in bench.logs[-1]["msg"]
