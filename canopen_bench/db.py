"""Workspace persistence: sqlite for config/EDS metadata, plain
files under eds/ for the EDS files themselves — so they stay individually
readable/organizable outside the app, not locked away as DB blobs.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class Db:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        # Snapshot before mkdir — Bench uses this to seed the bundled demo EDS once.
        self.is_first_run = not self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()
        # EDS folder: workspace-local by default, configurable via kv (e.g. a
        # central EDS pool shared between workspaces)
        custom = self.get("eds_dir")
        self.eds_dir = Path(custom) if custom else self.path.parent / "eds"
        self.eds_dir.mkdir(parents=True, exist_ok=True)

    def set_eds_dir(self, value: str) -> None:
        """Persist a custom EDS folder; empty value resets to <workspace>/eds."""
        self.set("eds_dir", value)
        self.eds_dir = Path(value) if value else self.path.parent / "eds"
        self.eds_dir.mkdir(parents=True, exist_ok=True)

    def _init(self) -> None:
        with self._lock, self._conn as c:
            c.execute("""CREATE TABLE IF NOT EXISTS kv(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS last_values(
                sn TEXT NOT NULL,
                obj TEXT NOT NULL,
                value TEXT NOT NULL,
                ts TEXT NOT NULL,
                PRIMARY KEY (sn, obj))""")
            c.execute("""CREATE TABLE IF NOT EXISTS eds_files(
                file TEXT PRIMARY KEY,
                dev_name TEXT NOT NULL DEFAULT '',
                ident TEXT NOT NULL DEFAULT '',
                code TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                variant_index TEXT NOT NULL DEFAULT '',
                variant_sub TEXT NOT NULL DEFAULT '',
                variant_map TEXT NOT NULL DEFAULT '{}',
                display_slots TEXT NOT NULL DEFAULT '[]',
                device_commands TEXT NOT NULL DEFAULT '[]')""")
            # migration for workspaces created before device_commands existed
            cols = {r["name"] for r in c.execute("PRAGMA table_info(eds_files)")}
            if "device_commands" not in cols:
                c.execute("ALTER TABLE eds_files ADD COLUMN "
                          "device_commands TEXT NOT NULL DEFAULT '[]'")

    # -- kv --------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        with self._lock, self._conn as c:
            c.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value)))

    # -- last-known values per device serial number ----------------------
    def last_values(self, sn: str) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT obj,value FROM last_values WHERE sn=?", (sn,)).fetchall()
        return {r["obj"]: r["value"] for r in rows}

    def last_values_ts(self, sn: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT MAX(ts) AS ts FROM last_values WHERE sn=?", (sn,)).fetchone()
        return row["ts"] if row and row["ts"] else None

    def remember_value(self, sn: str, obj: str, value: str, ts: str) -> None:
        with self._lock, self._conn as c:
            c.execute("""INSERT INTO last_values(sn,obj,value,ts) VALUES(?,?,?,?)
                         ON CONFLICT(sn,obj) DO UPDATE SET value=excluded.value, ts=excluded.ts""",
                      (sn, obj, value, ts))

    # -- EDS file registry -------------------------------------------------
    # The eds_files table holds only metadata; the actual .eds text lives as
    # a plain file under eds_dir, keyed by the same filename, so it stays
    # individually browsable/copyable outside the app. Renaming it on disk
    # by hand breaks the link to the DB row on purpose — see eds_write_file.
    def eds_list(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM eds_files ORDER BY file").fetchall()
        return [{
            "file": r["file"], "dev": r["dev_name"], "ident": r["ident"],
            "code": r["code"], "enabled": bool(r["enabled"]),
            "variant_index": r["variant_index"], "variant_sub": r["variant_sub"],
            "variant_map": json.loads(r["variant_map"]),
            "display_slots": json.loads(r["display_slots"]),
            "device_commands": json.loads(r["device_commands"]),
        } for r in rows]

    def eds_add(self, file: str, dev_name: str, ident: str, code: str = "", enabled: bool = True) -> None:
        with self._lock, self._conn as c:
            c.execute("""INSERT INTO eds_files(file,dev_name,ident,code,enabled)
                         VALUES(?,?,?,?,?)
                         ON CONFLICT(file) DO UPDATE SET
                            dev_name=excluded.dev_name, ident=excluded.ident""",
                      (file, dev_name, ident, code, int(enabled)))

    def eds_remove(self, file: str) -> None:
        with self._lock, self._conn as c:
            c.execute("DELETE FROM eds_files WHERE file=?", (file,))
        (self.eds_dir / file).unlink(missing_ok=True)

    def eds_write_file(self, file: str, content: str) -> None:
        (self.eds_dir / file).write_text(content, encoding="utf-8")

    def eds_set_code(self, file: str, code: str) -> None:
        with self._lock, self._conn as c:
            c.execute("UPDATE eds_files SET code=? WHERE file=?", (code, file))

    def eds_set_enabled(self, file: str, enabled: bool) -> None:
        with self._lock, self._conn as c:
            c.execute("UPDATE eds_files SET enabled=? WHERE file=?", (int(enabled), file))

    def eds_set_variant(self, file: str, index: str, sub: str, value_map: dict[str, str]) -> None:
        with self._lock, self._conn as c:
            c.execute("UPDATE eds_files SET variant_index=?, variant_sub=?, variant_map=? WHERE file=?",
                      (index, sub, json.dumps(value_map), file))

    def eds_set_display(self, file: str, slots: list[dict]) -> None:
        """Configure the sidebar display-mirror panel for this EDS: a list
        of {label, idx, sub} readouts, or [] (the default) for a device
        that has none — the panel this backs is too device-specific
        (mimics one machine family's own front-panel LCD) to show for
        every device generically."""
        with self._lock, self._conn as c:
            c.execute("UPDATE eds_files SET display_slots=? WHERE file=?",
                      (json.dumps(slots), file))

    def eds_set_commands(self, file: str, commands: list[dict]) -> None:
        """Configure the device commands this EDS's device family offers:
        a list of {key, label, badge?, write?: {index, sub, on, off}}
        toggles, or [] (the default). Same idea as display_slots/variant_*:
        special functions like a vendor's SuperUser mode are per-device
        data, not a fixed app-wide concept — the UI renders chips, badges
        and menu entries from this list generically."""
        with self._lock, self._conn as c:
            c.execute("UPDATE eds_files SET device_commands=? WHERE file=?",
                      (json.dumps(commands), file))

    def eds_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM eds_files").fetchone()
        return row["n"]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
