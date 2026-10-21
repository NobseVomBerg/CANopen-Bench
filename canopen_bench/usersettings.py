"""Settings of the user rather than of a workspace: where the data folder is.

They cannot live in the data folder — that is what they find. And the data
folder cannot simply be ``./data``: that is wherever the bench was started
from, so a double-clicked canopen-bench.exe kept its workspaces next to the
exe, and a start from a console somewhere else found an empty one.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

#: Names the data folder outright, for this process only (start scripts, a
#: second bench beside the everyday one); Setup cannot change it then.
ENV_DATA = "CANOPEN_BENCH_DATA"


def data_fixed() -> bool:
    """Whether CANOPEN_BENCH_DATA names the data folder for this process."""
    return bool(os.environ.get(ENV_DATA))


def settings_file() -> Path:
    """Per user, outside every data folder. CANOPEN_BENCH_SETTINGS names
    another file (tests, a bench that must not share the user's)."""
    if explicit := os.environ.get("CANOPEN_BENCH_SETTINGS"):
        return Path(explicit)
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "CANopen-Bench" / "settings.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "canopen-bench" / "settings.json"


def load() -> dict:
    """The settings, empty where there are none or the file is unreadable."""
    try:
        data = json.loads(settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(**changes) -> None:
    path = settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(load() | changes, indent=2), encoding="utf-8")


def data_root() -> Path:
    """The data folder: CANOPEN_BENCH_DATA, else the one chosen under Setup,
    else ./data where there is one (how every bench so far found its own),
    else CANopen-Bench in the home folder. What it came to is remembered,
    so the next start finds the same folder wherever it is started from."""
    if env := os.environ.get(ENV_DATA):
        return Path(env)
    if chosen := load().get("data"):
        return Path(chosen)
    here = Path("data")
    root = (here if here.is_dir() else Path.home() / "CANopen-Bench").resolve()
    try:
        save(data=str(root))
    except OSError:
        pass    # not remembered: the next start comes to the same answer
    return root
