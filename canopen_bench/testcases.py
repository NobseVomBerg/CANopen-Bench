"""Test-case catalog: YAML files describing declarative step sequences.

Format spec: docs/ablaeufe/testfall-format.md. Parsing is strict — unknown
keys anywhere are schema errors, so a typo like ``expekt`` surfaces in the
catalog instead of silently dropping an expectation mid-run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

MANUAL_TIMEOUT_S = 120.0  # default confirmation window for `manual` steps
MAX_STEPS = 10_000        # executed steps per case — loop runaway guard (v2)

REGISTERS = {f"R{i}" for i in range(10)}  # the 10 predefined variables (v2)
_BUILTINS = {"$node", "$expected"}        # $session: only as can_send data

_HEAD_KEYS = {"id", "name", "tools", "est", "dut", "preconditions", "steps"}
_NMT_COMMANDS = {"start", "preop", "stop", "reset", "resetcomm"}
_HB_STATES = {"boot", "stopped", "operational", "pre-operational"}
_ARITH = {"mov", "add", "sub", "and", "or"}
_COND_JUMPS = {"jump_eq", "jump_ne", "jump_gt", "jump_lt"}
# mapping-valued primitives: required fields, optional fields
_STEP_FIELDS = {
    "sdo_read": ({"index", "sub"}, {"expect", "expect_abort", "mask", "into"}),
    "sdo_write": ({"index", "sub", "value"}, {"size", "expect_abort"}),
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


@dataclass
class TestCase:
    id: str = ""
    name: str = ""
    tools: list[str] = field(default_factory=list)
    est: str = ""
    dut: object = "selected"  # "selected" | {"code": "<DUT code>"}
    preconditions: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    file: str = ""
    error: str | None = None  # parse/schema problem; entry is not runnable


def _check_step(step: object, extensions: dict | None = None) -> str | None:
    if not isinstance(step, dict) or len(step) != 1:
        return f"step must be a single-key mapping: {step!r}"
    key, val = next(iter(step.items()))
    if extensions and key in extensions:  # plugin step "<plugin>.<key>"
        return extensions[key].validate(val)
    if key == "nmt":
        if isinstance(val, dict):
            if unknown := set(val) - {"cmd", "node"}:
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
    if key == "fail":
        return None if isinstance(val, str) and val else "fail: needs a reason text"
    if key == "end":
        return None  # value is ignored ("- end:")
    if key == "label" or key == "jump":
        return None if isinstance(val, str) and val else f"{key}: needs a name"
    if key in _ARITH:
        if not isinstance(val, dict) or set(val) != {"to", "value"}:
            return f"{key}: needs {{to, value}}"
        if val["to"] not in REGISTERS:
            return f"{key}: to must be a register R0–R9, got {val['to']!r}"
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
            return f"lss_assign: into must be a register R0–R9, got {val['into']!r}"
        return None
    if key == "can_send":
        if not isinstance(val, dict) or set(val) != {"cob", "data"}:
            return "can_send: needs {cob, data}"
        if not _is_value(val["cob"]):
            return f"can_send: invalid cob {val['cob']!r}"
        data = val["data"]
        if data == "$session":
            return None
        if not isinstance(data, list) or not data:
            return "can_send: data must be a non-empty byte list or $session"
        for b in data:
            if b != "$session" and not _is_value(b):  # "$session" expands in place
                return f"can_send: invalid data byte {b!r}"
        return None
    if key == "manual":
        if isinstance(val, str) and val:
            return None
        if isinstance(val, dict):
            unknown = set(val) - {"text", "timeout"}
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
            if unknown := set(val) - {"cob", "timeout", "data", "on_timeout", "into"}:
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
        if unknown := set(val) - {"heartbeat", "timeout", "node", "on_timeout"}:
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
        if unknown := set(val) - required - optional:
            return f"{key}: unknown field(s) {sorted(unknown)}"
        if missing := required - set(val):
            return f"{key}: missing field(s) {sorted(missing)}"
        if key == "sdo_read":
            if "expect" in val and "expect_abort" in val:
                return "sdo_read: expect and expect_abort are mutually exclusive"
            if "mask" in val and "expect" not in val:
                return "sdo_read: mask requires expect"
            if "into" in val and val["into"] not in REGISTERS:
                return f"sdo_read: into must be a register R0–R9, got {val['into']!r}"
        if key == "sdo_write":
            if not _is_value(val["value"]):
                return f"sdo_write: invalid value {val['value']!r}"
            if "size" in val and val["size"] not in (1, 2, 4):
                return "sdo_write: size must be 1, 2 or 4"
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
        if key == "jump":
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


def parse_testcase(text: str, filename: str, require_prefix: bool = True,
                   extensions: dict | None = None) -> TestCase:
    """``extensions`` maps plugin step names ("<plugin>.<key>") to their
    StepType — without it, files using plugin steps are schema errors."""
    tc = TestCase(file=filename)
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        tc.error = f"invalid YAML: {exc}"
        return tc
    if not isinstance(doc, dict):
        tc.error = "not a mapping"
        return tc
    if unknown := set(doc) - _HEAD_KEYS:
        tc.error = f"unknown key(s) {sorted(unknown)}"
        return tc

    tc.id = str(doc.get("id") or "")
    tc.name = str(doc.get("name") or "")
    tc.tools = [str(t) for t in doc.get("tools") or []]
    tc.est = str(doc.get("est") or "")
    tc.dut = doc.get("dut") or "selected"
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


def load_catalog(folder: str | Path, extensions: dict | None = None) -> list[TestCase]:
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
                                extensions=extensions)
        except OSError as exc:
            tc = TestCase(file=p.name, error=str(exc))
        if not tc.id:
            tc.id = p.name  # keep broken files addressable in the catalog
        out.append(tc)
    return out
