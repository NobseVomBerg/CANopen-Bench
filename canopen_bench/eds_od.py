"""Shared EDS object-dictionary access: mtime-cached loading, variable
lookup, and the one description of what an object *is*.

Used by the demo bus (serving SDO from EDS content), by the trace
interpreter (object names for SDO frames), and by every view that has to
know whether a word carries a sign, spells a word, or may be padded.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from canopen import objectdictionary as odlib
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


#: What the bench needs to know about a CiA-301 data type, in three
#: tables and no more. Each of these used to be written out in two or
#: three places with two or three different memberships — the object
#: table read every value as an unsigned word while the panel read the
#: same object as signed, and one of the two was wrong on every screen.
#:
#: text: bytes that are characters, so a device name prints as the word
#: it is rather than as nineteen digits.
TEXT_TYPES = frozenset({odlib.VISIBLE_STRING, odlib.UNICODE_STRING})
#: bytes that are not characters. Not padded either — an OCTET_STRING
#: widened to its declared length is a different value — but guessing an
#: encoding for them would invent one, so they stay the bytes they are.
BYTE_TYPES = frozenset({odlib.OCTET_STRING, odlib.DOMAIN})
#: the signed integers and how wide each is. A word carries no sign of
#: its own, so this is the only thing that can tell -500 from 65036.
SIGNED_BITS = {odlib.INTEGER8: 8, odlib.INTEGER16: 16,
               odlib.INTEGER32: 32, odlib.INTEGER64: 64}
#: neither of those is a number: never widened to a declared length,
#: never read as an integer, never plotted
RAW_TYPES = TEXT_TYPES | BYTE_TYPES

#: short names for the type column of the object table
TYPE_NAMES = {
    odlib.BOOLEAN: "BOOL", odlib.INTEGER8: "I8", odlib.INTEGER16: "I16",
    odlib.INTEGER32: "I32", odlib.INTEGER64: "I64", odlib.UNSIGNED8: "U8",
    odlib.UNSIGNED16: "U16", odlib.UNSIGNED32: "U32", odlib.UNSIGNED64: "U64",
    odlib.REAL32: "F32", odlib.REAL64: "F64", odlib.VISIBLE_STRING: "STR",
    odlib.OCTET_STRING: "OCT", odlib.UNICODE_STRING: "USTR", odlib.DOMAIN: "DOM",
}


@dataclass(frozen=True)
class ObjectInfo:
    """Everything the EDS says about one object, in the terms the bench
    asks in.

    One place to ask, because the questions are not independent: whether
    a value may be zero-padded on write, whether it is signed, and
    whether it is text are three readings of the same declared type, and
    answering them apart is how a table and a panel ended up showing one
    device two different numbers.

    ``lo``/``hi`` are the EDS's own limits where it states them, and the
    only thing that can say a value is outside the range the device
    accepts — nothing else on a bench knows.
    """

    index: int
    sub: int
    name: str
    data_type: int
    bits: int
    access: str
    lo: int | None = None
    hi: int | None = None

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.data_type, f"0x{self.data_type:02X}")

    @property
    def is_text(self) -> bool:
        return self.data_type in TEXT_TYPES

    @property
    def signed_bits(self) -> int:
        """The width a negative number of this object is written at, or 0
        where it has no sign. ``PanelField.show``/``to_raw`` and the
        table's own formatting both need exactly this."""
        return SIGNED_BITS.get(self.data_type, 0)

    @property
    def width(self) -> int:
        """Byte width for padding a written value, 0 where padding would
        change the value: a string, an octet string, a domain."""
        if self.data_type in TEXT_TYPES or self.data_type in BYTE_TYPES:
            return 0
        return max(self.bits // 8, 1)

    def signed(self, value: int, bits: int = 0) -> int:
        """One word read with its sign. ``bits`` overrides the declared
        width for a value that arrived narrower than the object is — a
        PDO carries whatever the mapping said, which need not be the
        whole object."""
        width = bits or self.signed_bits
        if not self.signed_bits or not width:
            return value
        return value - (1 << width) if value >= 1 << (width - 1) else value


def object_info(od: ObjectDictionary | None, idx: int, sub: int) -> ObjectInfo | None:
    """What the EDS says about one address, or None where it says
    nothing — no EDS, or no such object in it. Every caller treats that
    as "the plain unsigned word it always was": a guessed sign bit turns
    half a range into negatives, and a guessed width truncates a write."""
    var = find_var(od, idx, sub) if od is not None else None
    return None if var is None else info_of(var)


def info_of(var: ODVariable) -> ObjectInfo:
    """The same, for a caller that already has the variable in hand —
    the trace decodes thousands of frames and must not look one up twice.

    The name is qualified the way the bench writes an address everywhere
    else: canopen joins a record member to its object with a dot, and
    this tool separates index from sub-index with a slash, in a step's
    report line and in the object table alike.
    """
    parent = getattr(var, "parent", None)
    name = (f"{parent.name}/{var.name}"
            if parent is not None and hasattr(parent, "subindices") else var.name)
    access = var.access_type if var.access_type in ("ro", "rw", "wo") else \
        ("ro" if not var.writable else "rw")
    return ObjectInfo(index=var.index, sub=var.subindex, name=name or "",
                      data_type=var.data_type or 0, bits=len(var) or 0,
                      access=access, lo=var.min, hi=var.max)


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
