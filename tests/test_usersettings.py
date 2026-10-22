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


# -- the folders of a workspace ------------------------------------------------

def _ws_bench(tmp_path) -> Bench:
    return Bench(Db(tmp_path / "ws" / "canopen-bench.db"), plugins=[])


def test_a_workspace_uses_its_own_folders_and_shows_them_locked(tmp_path):
    bench = _ws_bench(tmp_path)
    snap = bench.snapshot()
    assert not snap["pathsCustom"]
    assert snap["paths"]["res"] == str(tmp_path / "ws" / "results")
    assert (tmp_path / "ws" / "firmware").is_dir()


def test_a_workspace_that_had_a_folder_elsewhere_keeps_it(tmp_path):
    db = Db(tmp_path / "ws" / "canopen-bench.db")
    db.set("paths", {"tc": str(tmp_path / "ws" / "testcases"), "res": str(tmp_path / "ws" / "results"),
                     "fw": str(tmp_path / "build")})
    bench = Bench(db, plugins=[])
    assert bench.paths_custom and bench.paths["fw"] == str(tmp_path / "build")


def test_custom_folders_off_and_on_again(tmp_path):
    """Off, the workspace's own folders are back and cannot be changed by
    accident; the chosen ones stay stored, so on brings them back."""
    bench = _ws_bench(tmp_path)
    bench.dispatch("set_path", {"which": "fw", "value": str(tmp_path / "build")})
    assert bench.paths_custom, "choosing a folder is choosing custom folders"
    bench.dispatch("paths_custom", {"on": False})
    assert bench.paths["fw"] == str(tmp_path / "ws" / "firmware")
    bench.dispatch("browse_open", {"which": "fw"})
    assert bench.browse is None, "a locked folder offers nothing to browse"
    bench.dispatch("paths_custom", {"on": True})
    assert bench.paths["fw"] == str(tmp_path / "build")
    assert Bench(Db(tmp_path / "ws" / "canopen-bench.db"), plugins=[]).paths["fw"] == str(tmp_path / "build")


def test_the_reports_are_counted_in_the_folder_not_in_this_start(tmp_path):
    """After a restart the runs of this start are none, and a full results
    folder read "0 reports"."""
    results = tmp_path / "ws" / "results"
    results.mkdir(parents=True)
    for stamp in ("20261020_090000", "20261021_090000"):
        (results / f"{stamp}__summary.json").write_text("{}", encoding="utf-8")
        (results / f"{stamp}__summary.html").write_text("", encoding="utf-8")
    bench = _ws_bench(tmp_path)
    assert bench.snapshot()["reportCount"] == 2
    (results / "20261022_090000__summary.json").write_text("{}", encoding="utf-8")
    assert bench.snapshot()["reportCount"] == 3


def test_the_eds_folder_is_one_of_them(tmp_path):
    """Off, it is the workspace's own like the other three; a pool chosen
    for it comes back with custom folders on."""
    bench = _ws_bench(tmp_path)
    own = tmp_path / "ws" / "eds"
    bench.dispatch("set_path", {"which": "eds", "value": str(tmp_path / "pool")})
    assert bench.paths_custom and bench.db.eds_dir == tmp_path / "pool"
    bench.dispatch("paths_custom", {"on": False})
    assert bench.db.eds_dir == own and bench.snapshot()["paths"]["eds"] == str(own)
    bench.dispatch("paths_custom", {"on": True})
    assert bench.db.eds_dir == tmp_path / "pool"


def test_a_workspace_with_an_eds_pool_from_before_keeps_it(tmp_path):
    db = Db(tmp_path / "ws" / "canopen-bench.db")
    db.set_eds_dir(str(tmp_path / "pool"))
    bench = Bench(db, plugins=[])
    assert bench.paths_custom and bench.db.eds_dir == tmp_path / "pool"
    bench.dispatch("paths_custom", {"on": False})
    bench.dispatch("paths_custom", {"on": True})
    assert bench.db.eds_dir == tmp_path / "pool", "the pool is one of the stored choices now"
