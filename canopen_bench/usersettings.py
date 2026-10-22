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

#: The settings file; None is the user's own. Tests point it elsewhere.
SETTINGS_FILE: Path | None = None


def settings_file() -> Path:
    """Per user, outside every data folder."""
    if SETTINGS_FILE is not None:
        return SETTINGS_FILE
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
    """The data folder: the one chosen under Setup, else ./data where there
    is one (how every bench so far found its own), else CANopen-Bench in the
    home folder. What it came to is remembered, so the next start finds the
    same folder wherever it is started from. (``--data`` names one for a
    single start instead, and is not remembered: see __main__.)"""
    if chosen := load().get("data"):
        return Path(chosen)
    here = Path("data")
    root = (here if here.is_dir() else Path.home() / "CANopen-Bench").resolve()
    try:
        save(data=str(root))
    except OSError:
        pass    # not remembered: the next start comes to the same answer
    return root
