"""How object values are shown and how typed input is read back.

Three things a bench needs and hex-only string handling does not give:

* a **number base** the operator picks — most values read better in
  decimal, a few only make sense in hex, and neither choice may hide the
  other,
* **symbolic interpretation** where the device's own headers define one
  (``canopen_bench/symbols.py``), including values that pack several
  fields into one word,
* **typing a name instead of a number**, resolved before anything is
  written.

Two rules run through all of it. The raw number is never replaced by a
name — a bench where you cannot see the bits you actually read is not a
bench. And bits nobody accounted for are shown, not dropped: an unknown
flag in a status word is exactly the thing worth noticing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_QUALIFIED = re.compile(r"^[A-Za-z_]\w*(?::[A-Za-z_]\w*)?$")


#: the prefixes a written value may carry, and what they mean
BASES = {"0x": 16, "0b": 2}


def base_of(text: str) -> int | None:
    """Which base a written value is in, or None if it claims one this
    does not know.

    Base 16 is the default for a bare string, because that is what a
    CANopen value normally is — but a prefix has to be recognised *by
    name* before that default may apply. "0b1100" is valid hexadecimal
    (0, B, 1, 1, 0, 0) and so are "0d15" and "0e10"; read as hex they are
    725248, 3349 and 3600, quietly and without error. A value that looks
    like it names a base therefore has to name one that exists, or it is
    not a number here at all — better an unreadable step than a step that
    runs on a number nobody wrote.
    """
    if len(text) > 1 and text[0] == "0" and text[1].isalpha():
        return BASES.get(text[:2].lower())
    return 16


@dataclass(frozen=True)
class Field:
    """One interpreted slice of an object's value.

    A whole-value enum is the common case and needs only ``table``.
    Anything nested is the same construct with a mask: two fields packed
    into a byte, a byte lane inside a 32-bit word, one channel of several.

    ``shift`` is normally left alone, because the two kinds of table in the
    wild need opposite treatment and the table itself says which it is:

    * **pre-positioned** — the values already sit where they belong in the
      word (``eLamp_On = 0x20`` inside mask ``0x30``). No shift.
    * **logical** — the values are plain small numbers that live in a lane
      (``2, 4, 5`` inside mask ``0xFF0000``). Shift by the mask's lowest
      bit.

    Which one a table is follows from whether all its values fit inside
    the mask unshifted, so it is read off the data rather than guessed.
    Set ``shift`` explicitly only to overrule that.

    ``flags=True`` switches from "value names one thing" to "each set bit
    names one thing", which is what a flag register needs.
    """

    table: str
    mask: int = 0xFFFFFFFF
    shift: int | None = None
    label: str = ""
    flags: bool = False

    def extract(self, value: int, symbols=None) -> int:
        return (value & self.mask) >> self.resolved_shift(symbols)

    def resolved_shift(self, symbols=None) -> int:
        if self.shift is not None:
            return self.shift
        values = [sym.value for sym in
                  (symbols.tables.get(self.table, {}) if symbols else {}).values()]
        if values and all(v & ~self.mask == 0 for v in values) and any(values):
            return 0                       # pre-positioned table
        return _lowest_bit(self.mask)


def _lowest_bit(mask: int) -> int:
    return (mask & -mask).bit_length() - 1 if mask else 0


def describe(value: int, fields: list[Field], symbols) -> str:
    """Symbolic reading of a value, or "" when nothing is known about it.

    Multiple fields are joined with "·"; a labelled field shows its label.
    Bits outside every field's mask are appended as ``+0x..`` — dropping
    them would turn "there is something here I don't understand" into
    silence.
    """
    if not fields:
        return ""
    parts: list[str] = []
    covered = 0
    for field in fields:
        covered |= field.mask
        raw = field.extract(value, symbols)
        if field.flags:
            hits = [sym for sym in symbols.tables.get(field.table, {}).values()
                    if _is_flag(sym.value, field, symbols) and raw & sym.value == sym.value]
            named = [sym.value for sym in hits]
            names = [sym.name for sym in hits]
            leftover = raw & ~_union(named)
            text = "+".join(names) if names else ("none" if raw == 0 else "")
            if leftover:
                text = f"{text}+?0x{leftover:X}" if text else f"?0x{leftover:X}"
        else:
            # a value inside the mask that no symbol names is still a fact
            # about the device — "?0x7" rather than nothing at all
            text = symbols.name(field.table, raw) or f"?0x{raw:X}"
        parts.append(f"{field.label} {text}" if field.label else text)
    rest = value & ~covered
    if rest:
        parts.append(f"+0x{rest:X}")
    return " · ".join(parts)


def _is_flag(value: int, field: Field, symbols) -> bool:
    """Whether a table member names a set bit, in a table read as flags.

    Zero does not: it is the "nothing set" member, and it matches every
    value. Neither does a member covering the field's whole mask —
    headers keep the mask itself in the table (a bit filter, an "all"
    alias), and reporting that as a state would say "everything is set"
    at the one moment everything is. The individual names still match, so
    nothing is lost by leaving it out.
    """
    return bool(value) and value != (field.mask >> field.resolved_shift(symbols))


def _union(values: list[int]) -> int:
    total = 0
    for v in values:
        total |= v
    return total


def format_number(value: int, base: str, width: int = 0) -> str:
    """``base`` is "hex" or "dec". ``width`` is the object's width in hex
    digits, so a byte stays two digits instead of collapsing to one."""
    if base == "dec":
        return str(value)
    return f"0x{value:0{width or 2}X}"


def alternatives(value: int, fields: list[Field], symbols, width: int = 0,
                 dec: int | None = None) -> str:
    """Every reading of one value, for a tooltip: both bases, then the
    symbolic one. This is what makes the base switch safe to use — the
    other representation is always one hover away.

    ``dec`` is the decimal reading where it is not the number itself: a
    signed object is 0xFE0C and -500 at the same time, and a tooltip that
    offered 65036 beside the hex would be offering the same reading twice.
    Passed in rather than worked out here, because what carries a sign is
    something only the EDS knows (``eds_od.ObjectInfo``).
    """
    parts = [format_number(value, "hex", width), str(value if dec is None else dec)]
    text = describe(value, fields, symbols) if fields else ""
    if text:
        parts.append(text)
    return " · ".join(parts)


class ValueError_(ValueError):
    """Input that could not be read. The message is shown to the operator."""


def parse_value(text: str, fields: list[Field], symbols) -> int:
    """Typed input to a number.

    Accepts, in this order: an explicit ``0x``/``0b`` literal, a symbol
    name (optionally ``origin:NAME``), several of those joined with ``+``
    for flag registers, and finally bare digits — which are **decimal**.

    That is the same rule the write path has always had (core's
    ``_typed_number``): everywhere a *file* supplies a value a bare
    string is hex, because it was written against a datasheet and the
    format says so, and everywhere a *person* types one it is not. This
    used to follow the display base instead, so with the table showing
    hex a typed 12345678 became 0x12345678 — the same digits, a
    different number, and nothing on screen admitting it. Which base the
    values are *shown* in is a separate question and stays one.
    """
    raw = text.strip()
    if not raw:
        raise ValueError_("empty value")
    if "+" in raw:
        total = 0
        for part in raw.split("+"):
            total |= parse_value(part, fields, symbols)
        return total

    lowered = raw.lower()
    try:
        if lowered.startswith("0x"):
            return int(raw, 16)
        if lowered.startswith("0b"):
            return int(raw, 2)
    except ValueError:
        raise ValueError_(f'"{raw}" is not a valid number') from None

    if _QUALIFIED.match(raw) and not raw.isdigit():
        return _resolve_symbol(raw, fields, symbols)

    try:
        return int(raw, 10)
    except ValueError:
        raise ValueError_(
            f'"{raw}" is neither a number nor a known symbol') from None


def _resolve_symbol(name: str, fields: list[Field], symbols) -> int:
    """A full symbol name, or a short one that is unambiguous within the
    tables this object actually uses — typing ``Run`` instead of
    ``eMode_Run`` is the point of having tables at all."""
    try:
        return symbols.value(name)
    except Exception:
        pass
    matches = {sym.name: sym.value
               for field in fields
               for sym in symbols.tables.get(field.table, {}).values()
               if sym.name == name or sym.name.split("_", 1)[-1] == name}
    if len(set(matches.values())) == 1:
        return next(iter(matches.values()))
    if matches:
        raise ValueError_(f'"{name}" matches several symbols here: '
                          f"{', '.join(sorted(matches))}")
    raise ValueError_(f'unknown symbol "{name}"')
