"""Shared EDS object-dictionary access: mtime-cached loading + variable lookup.

Used by the demo bus (serving SDO from EDS content) and by the trace
interpreter (object names for SDO frames).
"""
from __future__ import annotations

from pathlib import Path

from canopen.objectdictionary import ObjectDictionary, ODVariable
from canopen.objectdictionary.eds import import_eds


class OdCache:
    """Loads EDS files from a directory as ObjectDictionary, cached by mtime."""

    def __init__(self, eds_dir: str | Path) -> None:
        self.eds_dir = Path(eds_dir)
        self._cache: dict[str, tuple[float, ObjectDictionary | None]] = {}

    def retarget(self, eds_dir: str | Path) -> None:
        """Point at another folder and forget everything cached."""
        self.eds_dir = Path(eds_dir)
        self._cache.clear()

    def load(self, file: str) -> ObjectDictionary | None:
        path = self.eds_dir / file
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        cached = self._cache.get(file)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            od = import_eds(str(path), None)
        except Exception:  # unparsable file -> cache the failure until it changes
            od = None
        self._cache[file] = (mtime, od)
        return od


def find_var(od: ObjectDictionary, idx: int, sub: int) -> ODVariable | None:
    """The ODVariable at index/sub — top-level VAR or record/array member."""
    obj = od.get(idx)
    if obj is None:
        return None
    if isinstance(obj, ODVariable):
        return obj if sub == 0 else None
    try:
        member = obj[sub]
    except (KeyError, IndexError):
        return None
    return member if isinstance(member, ODVariable) else None


def pdo_mapping(od: ObjectDictionary, mapping_index: int) -> list[tuple[int, int, int]]:
    """The EDS default mapping of one PDO (0x1600-/0x1A00-series object)
    as [(index, sub, bit-length), ...]; [] when the object is absent or
    empty. Each entry is the CiA-301 u32: index<<16 | sub<<8 | bits."""
    entries: list[tuple[int, int, int]] = []
    count_var = find_var(od, mapping_index, 0)
    try:
        count = int(count_var.default or 0) if count_var is not None else 0
    except (TypeError, ValueError):
        return entries
    for i in range(1, count + 1):
        var = find_var(od, mapping_index, i)
        try:
            raw = int(var.default or 0) if var is not None else 0
        except (TypeError, ValueError):
            continue
        if raw:
            entries.append(((raw >> 16) & 0xFFFF, (raw >> 8) & 0xFF, raw & 0xFF))
    return entries
