"""Shared EDS object-dictionary access: mtime-cached loading + variable lookup.

Used by the demo bus (serving SDO from EDS content) and by the trace
interpreter (object names for SDO frames).
"""
from __future__ import annotations

import io
from pathlib import Path

from canopen.objectdictionary import ObjectDictionary, ODVariable
from canopen.objectdictionary.eds import import_eds

#: How to read the bytes of an EDS. CiA 306 calls the file ASCII, and the
#: ones vendors actually ship are INI files written on Windows: a
#: ParameterName with an umlaut in it, a CreatedBy with a name in it, and
#: a byte no UTF-8 decoder accepts. canopen's own importer opens the path
#: in the platform default, which on a Linux bench is UTF-8 and raises —
#: and the failure lands where nothing shows it: OdCache swallows it and
#: every object width, PDO signal name and data type is quietly gone.
#:
#: utf-8-sig first, so a BOM and a genuinely UTF-8 file are read as
#: themselves; cp1252 second, because that is what wrote the other kind.
_ENCODINGS = ("utf-8-sig", "cp1252")


def eds_text(raw: bytes) -> str:
    """The text of an EDS, whatever it was written in. Never raises: a
    file that decodes as nothing recognisable still parses far enough to
    give the object dictionary, and one unreadable character in a
    device's name is worth more than no object dictionary at all."""
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp1252", "replace")


def load_eds(source: Path | str | bytes) -> ObjectDictionary:
    """An EDS from a path or from its bytes, read the way EDS files are
    actually written. The one place canopen's importer is called from."""
    raw = source if isinstance(source, bytes) else Path(source).read_bytes()
    return import_eds(io.StringIO(eds_text(raw)), None)


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
            od = load_eds(path)
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
