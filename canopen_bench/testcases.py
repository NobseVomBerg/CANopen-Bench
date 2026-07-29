"""Test-case catalog: YAML files describing declarative step sequences.

Format spec: docs/ablaeufe/testfall-format.md. Parsing is strict — unknown
keys anywhere are schema errors, so a typo like ``expekt`` surfaces in the
catalog instead of silently dropping an expectation mid-run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

MANUAL_TIMEOUT_S = 120.0  # default confirmation window for `manual` steps
MAX_STEPS = 10_000        # executed steps per case — loop runaway guard (v2)

REGISTERS = {f"R{i}" for i in range(16)}  # the predefined variables (v2)
_BUILTINS = {"$node", "$expected", "$session"}  # $session: only as can_send data
#: $eObjIdx_Foo / $acme:eObjIdx_Foo — a symbol from the device's
#: own headers (canopen_bench/symbols.py), substituted before validation
_SYMBOL_REF = re.compile(r"^\$([A-Za-z_]\w*(?::[A-Za-z_]\w*)?)$")

_HEAD_KEYS = {"id", "name", "desc", "grade", "tools", "est", "dut",
              "variants", "on_fail", "preconditions", "steps"}
#: what a failed expectation does to the rest of the case. "continue"
#: (the default) records the failure and keeps going; "stop" ends the
#: case there.
#:
#: Continuing is the default because stopping loses two things a bench
#: needs. A case whose last steps put the equipment back never reaches
#: them — a run that fails at 12 V leaves the device at 12 V. And a case
#: that would have told you about four broken things tells you about one,
#: so you fix it, run again, and find the next. Where the first failure
#: really does invalidate everything after it, the file says so.
_ON_FAIL = {"stop", "continue"}
#: how much of the case runs without a person at the bench — catalog and
#: report show it; "" when the file does not say
_GRADES = {"automated", "semi", "manual", "production"}
_NMT_COMMANDS = {"start", "preop", "stop", "reset", "resetcomm"}
_HB_STATES = {"boot", "stopped", "operational", "pre-operational"}
_ARITH = {"mov", "add", "sub", "mul", "div", "and", "or", "xor"}
_COND_JUMPS = {"jump_eq", "jump_ne", "jump_gt", "jump_lt", "jump_ge", "jump_le"}
#: every mapping-valued step may carry a `note`: the sentence somebody
#: wrote next to it, which the report shows under the step. The old tool's
#: cases carry one on most lines and they are half of what makes a report
#: readable a week later — "Reboot DUT" says why, where "write 0x1F51:0x02
#: = 2" only says what.
_NOTE = {"note"}

# mapping-valued primitives: required fields, optional fields
_STEP_FIELDS = {
    "sdo_read": ({"index", "sub"}, {"expect", "expect_abort", "mask", "into", "node"}),
    "sdo_write": ({"index", "sub", "value"}, {"size", "expect_abort", "node"}),
    "expect_emcy": ({"code"}, {"mask", "node", "timeout"}),
    "psu": (set(), {"ch", "volt", "curr", "output"}),
    "rand": ({"to"}, {"min", "max"}),
    "adjust": ({"index", "sub"}, {"text", "size", "node", "timeout"}),
}


def _is_value(v: object) -> bool:
    """Literal int, hex string, register name, or builtin (format v2)."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    if not isinstance(v, str):
        return False
    if v in REGISTERS or v in _BUILTINS:
        return True
    try:
        int(v, 16)
        return True
    except ValueError:
        return False


def _is_number(v: object) -> bool:
    """A value, or a plain decimal number — volts and amps are not
    integers, and "26.5" is an ordinary thing to ask a supply for."""
    if isinstance(v, float):
        return True
    return _is_value(v)


@dataclass
class TestCase:
    id: str = ""
    name: str = ""
    desc: str = ""
    grade: str = ""
    tools: list[str] = field(default_factory=list)
    est: str = ""
    dut: object = "selected"  # "selected" | {"code": "<DUT code>"}
    on_fail: str = "continue"  # "continue" | "stop" — see _ON_FAIL
    #: hardware variants this case applies to, as the device reports them
    #: ("820", "920"). Empty means every variant. Declared rather than
    #: checked in preconditions so the catalog can filter on it without
    #: running anything, and so a case states its scope in one place.
    variants: list[str] = field(default_factory=list)
    preconditions: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    file: str = ""
    error: str | None = None  # parse/schema problem; entry is not runnable


def _check_step(step: object, extensions: dict | None = None) -> str | None:
    if not isinstance(step, dict) or len(step) != 1:
        return f"step must be a single-key mapping: {step!r}"
    key, val = next(iter(step.items()))
    if isinstance(val, dict) and "note" in val \
            and not (isinstance(val["note"], str) and val["note"]):
        return f"{key}: note must be a text"
    if extensions and key in extensions:  # plugin step "<plugin>.<key>"
        return extensions[key].validate(val)
    if key == "nmt":
        if isinstance(val, dict):
            if unknown := set(val) - {"cmd", "node"} - _NOTE:
                return f"nmt: unknown field(s) {sorted(unknown)}"
            if val.get("cmd") not in _NMT_COMMANDS:
                return f"nmt: unknown command {val.get('cmd')!r}"
            n = val.get("node")
            if n is not None and n != "all" and not _is_value(n):
                return f"nmt: node must be 'all' or a value, got {n!r}"
            return None
        return None if val in _NMT_COMMANDS else f"nmt: unknown command {val!r}"
    if key == "wait":
        return None if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 0 \
            else "wait: needs a duration in seconds"
    if key == "log":
        return None if isinstance(val, str) and val else "log: needs a text"
    if key in ("fail", "skip"):
        return None if isinstance(val, str) and val else f"{key}: needs a reason text"
    if key == "end":
        return None  # value is ignored ("- end:")
    if key == "emcy_clear":
        return None  # value is ignored ("- emcy_clear:")
    if key in ("label", "jump", "jump_on_error"):
        return None if isinstance(val, str) and val else f"{key}: needs a name"
    if key == "ask":
        # a three-way question to the person at the bench: yes carries on,
        # no is a FAIL with the question as the reason, cancel is a SKIP.
        # Two-way `manual` cannot express "the operator looked and it was
        # wrong", which is a verdict, not an aborted run.
        if isinstance(val, str):
            return None if val else "ask: needs a text"
        if not isinstance(val, dict):
            return "ask: needs a text"
        if unknown := set(val) - {"text", "title", "timeout"} - _NOTE:
            return f"ask: unknown field(s) {sorted(unknown)}"
        if "timeout" in val and not isinstance(val["timeout"], (int, float)):
            return "ask: timeout must be a duration in seconds"
        return None if val.get("text") else "ask: needs a text"
    if key in _ARITH:
        if not isinstance(val, dict) or set(val) != {"to", "value"}:
            return f"{key}: needs {{to, value}}"
        if val["to"] not in REGISTERS:
            return f"{key}: to must be a register R0–R15, got {val['to']!r}"
        return None if _is_value(val["value"]) else f"{key}: invalid value {val['value']!r}"
    if key in _COND_JUMPS:
        if not isinstance(val, dict) or set(val) != {"a", "b", "to"}:
            return f"{key}: needs {{a, b, to}}"
        for operand in ("a", "b"):
            if not _is_value(val[operand]):
                return f"{key}: invalid operand {val[operand]!r}"
        return None if isinstance(val["to"], str) and val["to"] else f"{key}: needs a target label"
    if key == "lss_assign":
        if not isinstance(val, dict) or not {"count"} <= set(val) <= {"count", "into"}:
            return "lss_assign: needs {count, into?}"
        if not _is_value(val["count"]):
            return f"lss_assign: invalid count {val['count']!r}"
        if "into" in val and val["into"] not in REGISTERS:
            return f"lss_assign: into must be a register R0–R15, got {val['into']!r}"
        return None
    if key == "can_send":
        if not isinstance(val, dict) or set(val) - _NOTE != {"cob", "data"}:
            return "can_send: needs {cob, data}"
        if not _is_value(val["cob"]):
            return f"can_send: invalid cob {val['cob']!r}"
        data = val["data"]
        if data == "$session":
            return None
        # an empty list is a frame with no data, which is a real thing to
        # send: a CiA-301 SYNC carries none unless a counter is configured
        if not isinstance(data, list):
            return "can_send: data must be a byte list or $session"
        for b in data:
            if b != "$session" and not _is_value(b):  # "$session" expands in place
                return f"can_send: invalid data byte {b!r}"
        return None
    if key == "manual":
        if isinstance(val, str) and val:
            return None
        if isinstance(val, dict):
            unknown = set(val) - {"text", "timeout"} - _NOTE
            if unknown:
                return f"manual: unknown field(s) {sorted(unknown)}"
            return None if val.get("text") else "manual: needs a text"
        return "manual: needs a text"
    if key == "wait_for":
        if not isinstance(val, dict):
            return "wait_for: needs a mapping"
        if "on_timeout" in val and not (isinstance(val["on_timeout"], str) and val["on_timeout"]):
            return "wait_for: on_timeout needs a target label"
        if "cob" in val:  # frame form (v2)
            if unknown := set(val) - {"cob", "timeout", "data", "on_timeout", "into"} - _NOTE:
                return f"wait_for: unknown field(s) {sorted(unknown)}"
            if "timeout" not in val:
                return "wait_for: missing timeout"
            if "into" in val and not (isinstance(val["into"], str) and val["into"] in REGISTERS):
                return f"wait_for: invalid into {val['into']!r}"
            # cob (and, in lockstep, data) may be a single value or a list —
            # a list races every (cob, prefix) pair in the same wait
            cobs = val["cob"] if isinstance(val["cob"], list) else [val["cob"]]
            if not cobs or not all(_is_value(c) for c in cobs):
                return f"wait_for: invalid cob {val['cob']!r}"
            data = val.get("data")
            if isinstance(data, list) and len(data) != len(cobs):
                return "wait_for: cob and data lists must be the same length"
            return None
        if unknown := set(val) - {"heartbeat", "timeout", "node", "on_timeout"} - _NOTE:
            return f"wait_for: unknown field(s) {sorted(unknown)}"
        if "timeout" not in val:
            return "wait_for: missing timeout"
        if val.get("heartbeat") not in _HB_STATES:
            return f"wait_for: heartbeat must be one of {sorted(_HB_STATES)}"
        return None
    if key in _STEP_FIELDS:
        required, optional = _STEP_FIELDS[key]
        if not isinstance(val, dict):
            return f"{key}: needs a mapping"
        if unknown := set(val) - required - optional - _NOTE:
            return f"{key}: unknown field(s) {sorted(unknown)}"
        if missing := required - set(val):
            return f"{key}: missing field(s) {sorted(missing)}"
        if key == "sdo_read":
            if "expect" in val and "expect_abort" in val:
                return "sdo_read: expect and expect_abort are mutually exclusive"
            if "mask" in val and "expect" not in val:
                return "sdo_read: mask requires expect"
            if "into" in val and val["into"] not in REGISTERS:
                return f"sdo_read: into must be a register R0–R15, got {val['into']!r}"
        if key == "sdo_write":
            if not _is_value(val["value"]):
                return f"sdo_write: invalid value {val['value']!r}"
            if "size" in val and val["size"] not in (1, 2, 4):
                return "sdo_write: size must be 1, 2 or 4"
        if key == "rand":
            for name in ("min", "max"):
                if name in val and not _is_value(val[name]):
                    return f"rand: {name} must be a value or a register"
            if val["to"] not in REGISTERS:
                return f"rand: to must be a register R0–R15, got {val['to']!r}"
        if key == "adjust":
            if "size" in val and val["size"] not in (1, 2, 4):
                return "adjust: size must be 1, 2 or 4"
            if "timeout" in val and not isinstance(val["timeout"], (int, float)):
                return "adjust: timeout must be a duration in seconds"
            if "text" in val and not (isinstance(val["text"], str) and val["text"]):
                return "adjust: text must say what the operator is adjusting"
        if key == "psu":
            if not set(val) & {"volt", "curr", "output"}:
                return "psu: needs at least one of volt, curr, output"
            for name in ("volt", "curr"):
                if name in val and not _is_number(val[name]):
                    return f"psu: {name} must be a number or a register"
            if "ch" in val and not isinstance(val["ch"], int):
                return f"psu: ch must be a channel number, got {val['ch']!r}"
            # YAML reads bare on/off as booleans, which is exactly how a
            # file wants to spell this — both spellings are the same thing
            if "output" in val and val["output"] not in (True, False, "on", "off"):
                return "psu: output must be on or off"
        if key == "expect_emcy":
            for name in ("code", "mask", "node"):
                if name in val and not _is_value(val[name]):
                    return f"expect_emcy: invalid {name} {val[name]!r}"
            if "timeout" in val and not isinstance(val["timeout"], (int, float)):
                return "expect_emcy: timeout must be a duration in seconds"
        if "node" in val and not _is_value(val["node"]):
            return f"{key}: invalid node {val['node']!r}"
        return None
    return f"unknown step primitive {key!r}"


def _check_labels(steps: list) -> str | None:
    """Labels unique, jump targets resolvable — within one step list."""
    labels: set[str] = set()
    for step in steps:
        if isinstance(step, dict) and len(step) == 1 and "label" in step:
            name = step["label"]
            if name in labels:
                return f"duplicate label {name!r}"
            labels.add(name)
    for step in steps:
        if not (isinstance(step, dict) and len(step) == 1):
            continue
        key, val = next(iter(step.items()))
        if key in ("jump", "jump_on_error"):
            target = val
        elif key in _COND_JUMPS and isinstance(val, dict):
            target = val.get("to")
        elif key == "wait_for" and isinstance(val, dict):
            target = val.get("on_timeout")
        else:
            target = None
        if target is not None and target not in labels:
            return f"jump to unknown label {target!r}"
    return None


def _substitute_symbols(doc: object, symbols) -> object:
    """Replace every ``$Symbol`` with its hex value, before validation.

    Resolving here rather than at run time is the whole point: an unknown
    symbol makes the file fail to load, with the name in the message, instead
    of failing twenty minutes into a run against real hardware. Raises
    SymbolError, which the caller turns into the file's .error.
    """
    if isinstance(doc, dict):
        return {k: _substitute_symbols(v, symbols) for k, v in doc.items()}
    if isinstance(doc, list):
        return [_substitute_symbols(v, symbols) for v in doc]
    if isinstance(doc, str) and doc not in _BUILTINS:
        ref = _SYMBOL_REF.match(doc)
        if ref:
            return f"0x{symbols.value(ref.group(1)):X}"
    return doc


def parse_testcase(text: str, filename: str, require_prefix: bool = True,
                   extensions: dict | None = None, symbols=None) -> TestCase:
    """``extensions`` maps plugin step names ("<plugin>.<key>") to their
    StepType — without it, files using plugin steps are schema errors.
    ``symbols`` resolves ``$Symbol`` references (canopen_bench/symbols.py);
    without it a file using them is a schema error too, for the same reason:
    silently leaving them unresolved would send the literal text to a
    device."""
    tc = TestCase(file=filename)
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        tc.error = f"invalid YAML: {exc}"
        return tc
    if not isinstance(doc, dict):
        tc.error = "not a mapping"
        return tc
    if symbols is not None:
        try:
            doc = _substitute_symbols(doc, symbols)
        except Exception as exc:
            tc.error = str(exc)
            return tc
    if unknown := set(doc) - _HEAD_KEYS:
        tc.error = f"unknown key(s) {sorted(unknown)}"
        return tc

    tc.id = str(doc.get("id") or "")
    tc.name = str(doc.get("name") or "")
    tc.desc = str(doc.get("desc") or "")
    tc.grade = str(doc.get("grade") or "")
    tc.tools = [str(t) for t in doc.get("tools") or []]
    tc.est = str(doc.get("est") or "")
    tc.dut = doc.get("dut") or "selected"
    tc.on_fail = str(doc.get("on_fail") or "continue")
    raw_variants = doc.get("variants") or []
    tc.variants = ([str(v) for v in raw_variants]
                   if isinstance(raw_variants, list) else [])
    tc.preconditions = doc.get("preconditions") or []
    tc.steps = doc.get("steps") or []

    problems: list[str] = []
    if not tc.id:
        problems.append("missing id")
    if not tc.name:
        problems.append("missing name")
    if not tc.steps:
        problems.append("missing steps")
    if require_prefix and tc.id and not filename.startswith(f"TC{tc.id}_"):
        problems.append(f'id "{tc.id}" does not match filename prefix TC{tc.id}_')
    if not (tc.dut == "selected" or (isinstance(tc.dut, dict) and set(tc.dut) == {"code"})):
        problems.append('dut must be "selected" or {code: ...}')
    if tc.grade and tc.grade not in _GRADES:
        problems.append(f'grade must be one of {sorted(_GRADES)}, got "{tc.grade}"')
    if not isinstance(doc.get("variants") or [], list):
        problems.append("variants must be a list of variant names")
    if tc.on_fail not in _ON_FAIL:
        problems.append(f'on_fail must be one of {sorted(_ON_FAIL)}, got "{tc.on_fail}"')
    for group_name, group in (("preconditions", tc.preconditions), ("steps", tc.steps)):
        if not isinstance(group, list):
            problems.append(f"{group_name} must be a list")
            continue
        for step in group:
            if err := _check_step(step, extensions):
                problems.append(err)
        if err := _check_labels(group):
            problems.append(f"{group_name}: {err}")
    tc.error = "; ".join(problems) or None
    return tc


def load_catalog(folder: str | Path, extensions: dict | None = None,
                 symbols=None) -> list[TestCase]:
    """All TC*.yaml files in the folder, sorted by filename. Unreadable or
    invalid files come back as entries with .error set (shown in the
    catalog, not runnable) instead of vanishing silently."""
    out: list[TestCase] = []
    try:
        files = sorted(p for p in Path(folder).iterdir()
                       if p.name.startswith("TC") and p.suffix in (".yaml", ".yml"))
    except OSError:
        return out  # folder missing/unreadable -> empty catalog, caller falls back
    for p in files:
        try:
            tc = parse_testcase(p.read_text(encoding="utf-8"), p.name,
                                symbols=symbols,
                                extensions=extensions)
        except OSError as exc:
            tc = TestCase(file=p.name, error=str(exc))
        if not tc.id:
            tc.id = p.name  # keep broken files addressable in the catalog
        out.append(tc)
    return out
