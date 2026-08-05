"""One bench per data folder — see canopen_bench/__main__.claim_data_folder."""
import os

from canopen_bench.__main__ import LOCK_NAME, claim_data_folder


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
