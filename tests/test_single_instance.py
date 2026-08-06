"""One bench per data folder — see canopen_bench/__main__.claim_data_folder."""
import os
import subprocess
import sys
import textwrap

from canopen_bench.__main__ import LOCK_NAME, claim_data_folder, take_over


def test_an_empty_folder_is_free(tmp_path):
    assert claim_data_folder(tmp_path / "data") is None
    assert (tmp_path / "data" / LOCK_NAME).is_file()


def test_a_second_bench_on_the_same_folder_is_refused(tmp_path):
    """The failure this prevents is quiet: the second bench takes the serial
    port, and the first one then reports that no power supply was found."""
    root = tmp_path / "data"
    assert claim_data_folder(root) is None
    assert claim_data_folder(root) == os.getpid()


def test_another_folder_is_a_different_bench(tmp_path):
    """A release bench on its own folder and port is meant to run beside an
    everyday one — the guard is per data folder, not per machine."""
    assert claim_data_folder(tmp_path / "everyday") is None
    assert claim_data_folder(tmp_path / "release") is None


def test_the_lock_file_is_not_mistaken_for_a_workspace(tmp_path):
    """It is dot-prefixed, which is what Bench._workspace_names skips."""
    assert LOCK_NAME.startswith(".")


def test_a_folder_left_behind_by_a_dead_bench_is_free(tmp_path):
    """The lock lives on the open file, so a bench that crashed leaves
    nothing to clean up — only the file, which says nothing by itself."""
    root = tmp_path / "data"
    (root).mkdir()
    (root / LOCK_NAME).write_text("\n4242", encoding="utf-8")
    assert claim_data_folder(root) is None


def test_takeover_stops_the_holder_and_claims_the_folder(tmp_path):
    """Against a real process, because that is the whole claim being made:
    the pid in the lock file names something that can be stopped, and what
    it was holding — a serial port above all — comes back with it."""
    root = tmp_path / "data"
    code = textwrap.dedent(f"""
        from pathlib import Path
        from canopen_bench.__main__ import claim_data_folder
        import time
        assert claim_data_folder(Path(r"{root}")) is None
        print("held", flush=True)
        time.sleep(60)
    """)
    holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert claim_data_folder(root) == holder.pid
        assert take_over(root, holder.pid) is True
        assert holder.wait(timeout=5) is not None
    finally:
        if holder.poll() is None:
            holder.kill()


def test_taking_over_nothing_is_not_a_takeover(tmp_path):
    """0 is what an unreadable lock file reports, and it is not a pid."""
    assert take_over(tmp_path / "data", 0) is False
