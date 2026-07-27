"""Symbol tables parsed from a device's own C headers.

Object indices, sub-indices and enum values are what a device's firmware
calls them; the bench should say the same thing rather than keep a second
copy that drifts. So plugins ship (or the operator drops in) the firmware
headers, and this module turns them into two lookups:

* **name → value**, so flows and test cases can write
  ``$eObjIdx_LampControl`` instead of ``0x220C``,
* **value → name**, so an object holding ``4`` reads as
  ``WorkingTension (4)`` instead of ``0x04``.

Only a small subset of C is accepted — enums, ``#define`` constants and
constant expressions over symbols already parsed. Anything else is
reported, never guessed: a table that is subtly wrong is worse than no
table, because it makes the bench write to the wrong object and then
blame the device.
"""
from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_DOC_BRIEF = re.compile(r"///\s*(?:@brief\s*)?(.*)")
_TRAILING_DOC = re.compile(r"//!?<?\s*(.*)$")
_ENUM_OPEN = re.compile(r"^\s*(?:typedef\s+)?enum\s+(\w+)?\s*\{")
_ENUM_CLOSE = re.compile(r"^\s*\}\s*(\w+)?\s*;")
_MEMBER = re.compile(r"^\s*(\w+)\s*(?:=\s*(.+?))?\s*,?\s*$")
_DEFINE = re.compile(r"^\s*#\s*define\s+(\w+)\s+(.+?)\s*$")
_INT_SUFFIX = re.compile(r"\b(0[xX][0-9a-fA-F]+|\d+)[uUlL]+\b")
_LEADING_ZERO = re.compile(r"\b0[0-9]+\b")


class SymbolError(Exception):
    """A header the parser refuses to interpret."""


@dataclass(frozen=True)
class Symbol:
    name: str
    value: int
    table: str          # owning enum, "" for a #define
    desc: str           # the //!< comment, verbatim
    source: str         # "<origin>/<file>", e.g. "memiro/objects.h"
    line: int

    @property
    def origin(self) -> str:
        return self.source.split("/", 1)[0]


@dataclass
class SymbolTables:
    """Everything parsed, plus what could not be. Lookups are by bare name;
    a name defined twice with different values is dropped from the bare
    index and stays reachable only as ``origin:NAME`` — quietly picking one
    of two definitions is how a bench ends up writing to the wrong object.
    """

    by_name: dict[str, Symbol] = field(default_factory=dict)
    tables: dict[str, dict[str, Symbol]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    ambiguous: dict[str, list[Symbol]] = field(default_factory=dict)

    def value(self, name: str) -> int:
        """Resolve a symbol, optionally qualified as ``origin:NAME``.
        Raises SymbolError with a usable message — callers surface it at
        parse time, not mid-run."""
        origin, _, bare = name.rpartition(":")
        if origin:
            for sym in self.ambiguous.get(bare, []) + [self.by_name.get(bare)]:
                if sym is not None and sym.origin == origin:
                    return sym.value
            raise SymbolError(f'unknown symbol "{name}"')
        if bare in self.ambiguous:
            where = ", ".join(sorted(s.source for s in self.ambiguous[bare]))
            raise SymbolError(f'"{bare}" is defined differently in {where} — '
                              f'qualify it as <origin>:{bare}')
        sym = self.by_name.get(bare)
        if sym is None:
            raise SymbolError(f'unknown symbol "{bare}"')
        return sym.value

    def name(self, table: str, value: int) -> str:
        """Reverse lookup inside one table, or "" when it has no such value.
        First definition wins: aliases like eLampState_RedOff = eLamp_Off
        are the same value under two names, and the table's own name is the
        one worth showing."""
        for sym in self.tables.get(table, {}).values():
            if sym.value == value:
                return sym.name
        return ""

    def describe(self, name: str) -> str:
        sym = self.by_name.get(name.rpartition(":")[2])
        return sym.desc if sym else ""

    def summary(self) -> dict:
        return {"tables": len(self.tables), "symbols": len(self.by_name),
                "errors": list(self.errors)}


# -- expression evaluation --------------------------------------------------

_BINOPS = {
    ast.LShift: lambda a, b: a << b, ast.RShift: lambda a, b: a >> b,
    ast.BitOr: lambda a, b: a | b, ast.BitAnd: lambda a, b: a & b,
    ast.BitXor: lambda a, b: a ^ b, ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
}


def _normalize_int_literals(expr: str) -> str:
    """C integer literals to Python ones.

    Leading zeros are the interesting case: ``0755`` is octal in C and a
    syntax error in Python, and ``08150815`` is neither — a C compiler
    rejects it too. Refusing beats picking a reading for a number the tool
    is about to write to a device.
    """
    expr = _INT_SUFFIX.sub(r"\1", expr)  # 0x10U, 1UL -> 0x10, 1

    def octal(m: re.Match) -> str:
        digits = m.group(0)
        if any(d in "89" for d in digits[1:]):
            raise SymbolError(f'"{digits}" is not a valid octal literal '
                              "(leading zero, but contains 8 or 9)")
        return str(int(digits, 8))

    return _LEADING_ZERO.sub(octal, expr)


def _evaluate(expr: str, known: dict[str, Symbol]) -> int:
    """A constant expression over already-parsed symbols.

    lamp.h needs this: its composed states are ``(eLamp_Off << 8)``, so a
    literal-only reader would reject the file outright. Everything beyond
    integers, the listed operators and known names is refused — no macro
    expansion, no calls, and an unresolvable name is an error rather than a
    zero.
    """
    try:
        tree = ast.parse(_normalize_int_literals(expr.strip()), mode="eval")
    except SyntaxError as exc:
        raise SymbolError(f'cannot read the value "{expr.strip()}" ({exc.msg})') from exc

    def walk(node: ast.AST) -> int:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in known:
                raise SymbolError(f'"{expr.strip()}" refers to unknown symbol "{node.id}"')
            return known[node.id].value
        if isinstance(node, ast.BinOp) and isinstance(node.op, tuple(_BINOPS)):
            return _BINOPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.Invert)):
            operand = walk(node.operand)
            return -operand if isinstance(node.op, ast.USub) else ~operand
        raise SymbolError(f'"{expr.strip()}" is more than a constant expression')

    return walk(tree)


# -- parsing ----------------------------------------------------------------

_ENUM = re.compile(r"(?:typedef\s+)?enum\s+(\w+)?\s*\{(.*?)\}\s*(\w+)?\s*;", re.S)
_DEFINE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)[ \t]+(.+?)[ \t]*$", re.M)
_LINE_COMMENT = re.compile(r"//.*$")


def _strip_comments(text: str) -> str:
    """Block comments out, newlines kept, so reported line numbers stay
    the ones a person sees in an editor."""
    return _BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def _members(body: str, first_line: int):
    """Enum body to (name, expression, description, line).

    Members are split on commas rather than on newlines: `enum eBtn { A, B }`
    on one line is ordinary C, and a parser that only understands one member
    per line would silently see nothing at all. A line's trailing //!<
    comment describes the last member on that line, which is how these
    headers are written.
    """
    for offset, raw in enumerate(body.splitlines()):
        line = first_line + offset
        code = _LINE_COMMENT.sub("", raw)
        desc = _TRAILING_DOC.sub(r"\1", raw[len(code):]).strip() if len(code) < len(raw) else ""
        parts = [p.strip() for p in code.split(",")]
        entries = [p for p in parts if p]
        for i, part in enumerate(entries):
            name, sep, expr = part.partition("=")
            if not name.strip().isidentifier():
                continue
            yield (name.strip(), expr.strip() if sep else "",
                   desc if i == len(entries) - 1 else "", line)


def parse_header(text: str, source: str) -> tuple[list[Symbol], list[str]]:
    """One header to symbols. Returns (symbols, errors); a bad member costs
    that member, not the file, so one unreadable constant does not take a
    whole enum with it."""
    text = _strip_comments(text)
    symbols: list[Symbol] = []
    errors: list[str] = []
    known: dict[str, Symbol] = {}

    def add(name: str, expr: str, desc: str, line: int, table: str) -> None:
        try:
            value = _evaluate(expr, known) if expr else running[0]
        except SymbolError as exc:
            errors.append(f"{source}:{line}: {exc}")
            return
        sym = Symbol(name, value, table, desc, source, line)
        symbols.append(sym)
        known[name] = sym
        running[0] = value + 1

    running = [0]
    for match in _ENUM.finditer(text):
        # `typedef enum eFoo { … } eFoo;` names the table twice; anonymous
        # enums are named only at the closing brace
        table = match.group(3) or match.group(1) or ""
        first_line = text[:match.start(2)].count("\n") + 1
        running[0] = 0
        for name, expr, desc, line in _members(match.group(2), first_line):
            add(name, expr, desc, line, table)

    enum_spans = [(m.start(), m.end()) for m in _ENUM.finditer(text)]
    for match in _DEFINE.finditer(text):
        if any(lo <= match.start() < hi for lo, hi in enum_spans):
            continue
        body = _LINE_COMMENT.sub("", match.group(2)).strip()
        tail = match.group(2)[len(body):]
        line = text[:match.start()].count("\n") + 1
        running[0] = 0
        add(match.group(1), body, _TRAILING_DOC.sub(r"\1", tail).strip(), line, "")

    return symbols, errors


def load_symbols(dirs: list[tuple[str, Path]]) -> SymbolTables:
    """Parse every ``*.h`` under each (origin, directory). ``origin`` labels
    where a header came from — the plugin name — and qualifies a symbol when
    two origins disagree about it."""
    tables = SymbolTables()
    for origin, directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.h")):
            source = f"{origin}/{path.name}"
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                tables.errors.append(f"{source}: unreadable — {exc}")
                continue
            symbols, errors = parse_header(text, source)
            tables.errors.extend(errors)
            for sym in symbols:
                if sym.table:
                    tables.tables.setdefault(sym.table, {})[sym.name] = sym
                prev = tables.by_name.get(sym.name)
                if prev is None:
                    tables.by_name[sym.name] = sym
                elif prev.value != sym.value:
                    tables.ambiguous.setdefault(sym.name, []).extend([prev, sym])
                    del tables.by_name[sym.name]
                    tables.errors.append(
                        f'"{sym.name}" defined as {prev.value} in {prev.source} and '
                        f"{sym.value} in {sym.source} — qualify it as <origin>:{sym.name}")
    return tables
