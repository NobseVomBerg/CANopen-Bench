"""Panel specs: a device dashboard described as data, not as code.

A panel is a set of boxes over one device's object dictionary — named
values, grouped the way the people at the machine group them, each with
the unit and the scaling that no EDS carries. The core renders it and
holds no device knowledge; a plugin ships the file that says what to
show. Everything below a box (reading, staging, writing, the EDS width a
download is sized to) is the object machinery that was already there.

Values are shown in decimal, always. The object table's hex/dec chip is a
developer's reading habit; a box that says "mA" is read by someone who
wants 167, not 0xA7.

The format, at a glance::

    name: Sample Feeder                 # what the view is called
    match: {eds: "Sample*"}             # which devices it applies to
    groups:
      - title: Temperatures
        cols: 2
        fields:
          - {label: MCU, obj: "0x2100:01", unit: "°C"}
      - title: Tension
        collapsed: true
        fields:
          - {label: Working, obj: "0x2007:02", unit: cN, scale: 0.1, rw: true}

``obj`` is the only required key of a field, ``title`` the only one of a
group. A field without ``rw`` is read-only — the safe default, since a
panel is written from a device's documentation and a typo that only
displays is cheaper than one that writes.

Unknown keys are an error rather than a shrug: a misspelled ``unit`` that
is silently dropped leaves someone staring at a box wondering why the
file they just edited changed nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

#: what a field may say — anything else is a typo, and typos are loud here
_FIELD_KEYS = {"label", "obj", "unit", "scale", "digits", "rw", "widget", "bit"}
_WIDGETS = {"number", "enum", "flag"}
_GROUP_KEYS = {"title", "fields", "cols", "collapsed", "when"}
_WHEN_KEYS = {"obj", "bit", "value"}
_PANEL_KEYS = {"name", "match", "groups"}
_MATCH_KEYS = {"eds", "name"}

_ADDR = re.compile(r"^\s*(?:0[xX])?([0-9a-fA-F]{1,4})\s*"
                   r"(?::\s*(?:0[xX])?([0-9a-fA-F]{1,2})\s*)?$")


class PanelError(ValueError):
    """A panel file the core will not render, with the reason why."""


def _addr(text: str) -> tuple[str, str]:
    """"0x2007:01" -> ("0x2007", "01"), in the exact spelling the object
    catalog and ``obj_vals`` use — a key that differs by a leading zero
    finds nothing, silently, which is the one failure a panel cannot show
    on screen. A bare index means sub-index 0, the way a scalar object is
    written everywhere else.

    Both halves are hex, with or without ``0x``: an object dictionary is
    written in hex everywhere — in the EDS, in the catalog, in the
    device's own documentation — and a sub-index that meant ten written
    one way and sixteen written another would be a trap with no upside.
    """
    found = _ADDR.match(str(text))
    if not found:
        raise PanelError(f"obj: {text!r} is not an object address (\"0x2007:01\")")
    idx, sub = found.group(1), found.group(2) or "0"
    return f"0x{int(idx, 16):04X}", f"{int(sub, 16):02X}"


def _digits_for(scale: float) -> int:
    """How many decimals a scale implies: 0.1 -> 1, 0.01 -> 2. Spelling it
    out per field would be one more thing to keep in step with the factor
    next to it."""
    text = f"{scale:.10f}".rstrip("0")
    return len(text.partition(".")[2])


@dataclass
class PanelField:
    """One value: what to call it, where it lives, how to read it."""

    label: str
    idx: str
    sub: str
    unit: str = ""
    scale: float = 1.0
    digits: int = 0
    rw: bool = False
    #: how it is shown and written. ``number`` is the default; ``enum``
    #: takes its choices from the symbol table a plugin declared for this
    #: object (``BenchPlugin.object_fields``), so the names come from the
    #: firmware's own headers rather than from a list kept in step by
    #: hand; ``flag`` is one bit of a word as a checkbox, and says which
    #: bit itself — a status word does not need a table to have bit 3.
    widget: str = "number"
    bit: int | None = None

    @property
    def key(self) -> str:
        return f"{self.idx}:{self.sub}"

    def show(self, raw: str | None, signed_bits: int = 0) -> str:
        """The value as the box prints it. ``raw`` is what the bus answered
        (a hex string) or None for "not read yet"; a value that is not a
        number — a device name, a serial — is passed through untouched.

        ``signed_bits`` is the object's width where the EDS declares it a
        signed integer, and 0 where it does not. A word is bits on the
        wire and says nothing about its own sign, so a motor turning
        backwards reads as 65036 rather than -500 unless somebody says
        how wide it is. That is the worst kind of wrong number: it is in
        range, it moves when the device moves, and it is not the value.
        """
        if raw in (None, "", "—"):
            return ""
        try:
            value = int(str(raw), 16)
        except ValueError:
            return str(raw)
        if signed_bits and value >= 1 << (signed_bits - 1):
            value -= 1 << signed_bits
        scaled = value * self.scale
        return f"{scaled:.{self.digits}f}" if self.digits else f"{round(scaled)}"

    def to_raw(self, text: str, signed_bits: int = 0) -> int:
        """What somebody typed into the box, back to the number the device
        stores. Reads the way the rest of the bench reads typed values:
        ``0x…`` is hex, anything else decimal — a scaled field is decimal
        by its nature ("16.0 cN"), and the two must not disagree.

        A minus sign is only accepted where the EDS says the object is
        signed, and comes back as the two's complement of that width: a
        box that shows -500 has to be able to send it, and one that does
        not know the width cannot tell -500 from a very large number.
        """
        text = str(text).strip()
        if not text:
            raise PanelError("empty")
        try:
            value = int(text, 16) if text.lower().startswith("0x") else float(text)
        except ValueError:
            raise PanelError(f"{text!r} is not a number") from None
        raw = round(value / self.scale) if self.scale != 1.0 else round(value)
        if raw < 0:
            if not signed_bits:
                raise PanelError(f"{text!r} is negative — the EDS declares this "
                                 f"object unsigned")
            if raw < -(1 << (signed_bits - 1)):
                raise PanelError(f"{text!r} does not fit in {signed_bits} signed bits")
            raw += 1 << signed_bits
        elif signed_bits and raw >= 1 << (signed_bits - 1):
            raise PanelError(f"{text!r} does not fit in {signed_bits} signed bits")
        return raw


@dataclass
class PanelCondition:
    """When a box applies at all: a bit of an object, or a value it has.

    Different from folding. Folding says "not interested right now" and is
    the operator's; this says "this machine does not have that part" and
    is the device's — an axis the device does not carry has no box, rather
    than an empty one to open.

    ``idx``/``sub`` name the object; ``bit`` tests one bit of it,
    ``values`` are the values that count as yes. Exactly one of the two.

    Several values, because a device family is usually numbered rather
    than flagged: the part is on two of the variants and on none of the
    others, and the variant object answers one number or the other.
    Object dictionaries rarely carry a bit that says "has that part", and
    writing it as two boxes with one value each would duplicate every
    field in them to express an "or".
    """

    idx: str
    sub: str
    bit: int | None = None
    values: tuple[int, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.idx}:{self.sub}"

    def holds(self, raw: str | None) -> bool:
        """Whether the box applies, given what is known about that object.

        Unknown means yes. A condition may take a box away once the device
        has answered; it may not keep one hidden before anything has been
        asked, or the object that would settle it sits behind the box it
        is hiding.
        """
        if raw in (None, "", "—"):
            return True
        try:
            number = int(str(raw), 16)
        except ValueError:
            return True
        return bool(number >> self.bit & 1) if self.bit is not None else number in self.values


@dataclass
class PanelGroup:
    """One box. ``collapsed`` is the state it opens in, not a fixed one —
    what the operator folds away is remembered per workspace."""

    title: str
    fields: list[PanelField] = field(default_factory=list)
    cols: int = 1
    collapsed: bool = False
    when: PanelCondition | None = None


@dataclass
class Panel:
    name: str
    groups: list[PanelGroup] = field(default_factory=list)
    match: dict = field(default_factory=dict)
    source: str = ""

    def matches(self, dev: dict) -> bool:
        """Whether this panel is the one for ``dev`` (a row of
        ``bench.devices``). Globs, case-sensitively, against the assigned
        EDS file name and the product name; a panel without ``match`` takes
        every device, which is what a generic panel wants."""
        for key, pattern in self.match.items():
            value = str(dev.get("eds" if key == "eds" else "name") or "")
            if not fnmatchcase(value, str(pattern)):
                return False
        return True


def _fields(raw, where: str) -> list[PanelField]:
    if not isinstance(raw, list):
        raise PanelError(f"{where}: fields must be a list")
    out: list[PanelField] = []
    for i, item in enumerate(raw, start=1):
        at = f"{where}, field {i}"
        if not isinstance(item, dict):
            raise PanelError(f"{at}: must be a mapping")
        unknown = set(item) - _FIELD_KEYS
        if unknown:
            raise PanelError(f"{at}: unknown key(s) {', '.join(sorted(unknown))}")
        if "obj" not in item:
            raise PanelError(f"{at}: needs an obj")
        idx, sub = _addr(item["obj"])
        try:
            scale = float(item.get("scale", 1.0))
        except (TypeError, ValueError):
            raise PanelError(f"{at}: scale must be a number") from None
        if scale == 0:
            raise PanelError(f"{at}: scale must not be zero")
        widget = str(item.get("widget", "number"))
        if widget not in _WIDGETS:
            raise PanelError(f"{at}: unknown widget {widget!r} "
                             f"(one of {', '.join(sorted(_WIDGETS))})")
        bit = item.get("bit")
        if widget == "flag":
            if bit is None:
                raise PanelError(f"{at}: a flag needs the bit it stands for")
            if not isinstance(bit, int) or not 0 <= bit <= 31:
                raise PanelError(f"{at}: bit must be 0…31")
        elif bit is not None:
            raise PanelError(f"{at}: bit belongs to a flag, not to a {widget}")
        # a scale on a name or a bit would have to mean something, and there
        # is nothing it could mean
        if widget != "number" and (scale != 1.0 or "digits" in item or item.get("unit")):
            raise PanelError(f"{at}: scale, digits and unit belong to a number, "
                             f"not to a {widget}")
        out.append(PanelField(
            label=str(item.get("label") or f"{idx}:{sub}"),
            idx=idx, sub=sub,
            unit=str(item.get("unit", "")),
            scale=scale,
            digits=int(item.get("digits", _digits_for(scale))),
            rw=bool(item.get("rw", False)),
            widget=widget,
            bit=bit if widget == "flag" else None,
        ))
    return out


def _when(raw, where: str) -> PanelCondition | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PanelError(f"{where}: when must be a mapping "
                         f"({{obj: \"0x2001:00\", bit: 3}})")
    unknown = set(raw) - _WHEN_KEYS
    if unknown:
        raise PanelError(f"{where}: when: unknown key(s) {', '.join(sorted(unknown))}")
    if "obj" not in raw:
        raise PanelError(f"{where}: when needs an obj")
    idx, sub = _addr(raw["obj"])
    has_bit, has_value = "bit" in raw, "value" in raw
    if has_bit == has_value:
        raise PanelError(f"{where}: when takes either a bit or a value, not "
                         f"{'both' if has_bit else 'neither'}")
    if has_bit and (not isinstance(raw["bit"], int) or not 0 <= raw["bit"] <= 31):
        raise PanelError(f"{where}: when: bit must be 0…31")
    values: tuple[int, ...] = ()
    if not has_bit:
        # one value or a list of them — "these two variants have it" is a
        # list in the file rather than the same box written out twice
        given = raw["value"]
        given = given if isinstance(given, list) else [given]
        if not given:
            raise PanelError(f"{where}: when: value must name at least one value")
        try:
            values = tuple(int(str(v), 0) for v in given)
        except (TypeError, ValueError):
            raise PanelError(f"{where}: when: value must be a number, "
                             f"or a list of numbers") from None
    return PanelCondition(idx=idx, sub=sub, bit=raw["bit"] if has_bit else None, values=values)


def parse_panel(text: str, source: str = "") -> Panel:
    """Parse one panel file. Raises ``PanelError`` with a message naming
    the place — a panel is written by hand, so the message is the only
    debugger its author gets."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PanelError(f"not valid YAML — {exc}") from None
    if not isinstance(doc, dict):
        raise PanelError("must be a mapping with name/match/groups")
    unknown = set(doc) - _PANEL_KEYS
    if unknown:
        raise PanelError(f"unknown key(s) {', '.join(sorted(unknown))}")
    match = doc.get("match") or {}
    if not isinstance(match, dict):
        raise PanelError("match must be a mapping ({eds: \"Sample*\"})")
    unknown = set(match) - _MATCH_KEYS
    if unknown:
        raise PanelError(f"match: unknown key(s) {', '.join(sorted(unknown))}")
    groups_raw = doc.get("groups")
    if not isinstance(groups_raw, list) or not groups_raw:
        raise PanelError("groups must be a non-empty list")

    groups: list[PanelGroup] = []
    for i, item in enumerate(groups_raw, start=1):
        at = f"group {i}"
        if not isinstance(item, dict):
            raise PanelError(f"{at}: must be a mapping")
        unknown = set(item) - _GROUP_KEYS
        if unknown:
            raise PanelError(f"{at}: unknown key(s) {', '.join(sorted(unknown))}")
        title = str(item.get("title") or "").strip()
        if not title:
            raise PanelError(f"{at}: needs a title")
        at = f"group {title!r}"
        groups.append(PanelGroup(
            title=title,
            fields=_fields(item.get("fields") or [], at),
            cols=max(1, min(4, int(item.get("cols", 1)))),
            collapsed=bool(item.get("collapsed", False)),
            when=_when(item.get("when"), at),
        ))
    titles = [g.title for g in groups]
    if len(set(titles)) != len(titles):
        raise PanelError("two groups share a title — the folded state is kept by title")
    return Panel(name=str(doc.get("name") or Path(source).stem or "Panel"),
                 groups=groups, match=match, source=source)


def load_panels(paths: list[Path], log=None) -> list[Panel]:
    """Every readable panel in ``paths`` (files or directories of
    ``*.panel.yaml``). A file that does not parse is reported through
    ``log`` and skipped — one bad panel from one plugin must not cost the
    others theirs, and never the page."""
    out: list[Panel] = []
    for path in paths:
        try:
            files = sorted(path.glob("*.panel.yaml")) if path.is_dir() else [path]
        except OSError as exc:
            if log:
                log(f"PANEL {path} unreadable — {exc}")
            continue
        for file in files:
            try:
                out.append(parse_panel(file.read_text(encoding="utf-8"), str(file)))
            except (PanelError, OSError, UnicodeDecodeError) as exc:
                if log:
                    log(f"PANEL {file.name} ignored — {exc}")
    return out
