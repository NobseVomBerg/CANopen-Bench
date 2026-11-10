"""The bench as an MCP server: tools an AI assistant (Claude Code, GitHub
Copilot, …) calls to drive the bench that is running.

Served by the bench itself at ``/mcp`` — Streamable HTTP with plain JSON
answers and no server-initiated stream. The tools act on the devices, the
bus connection and the supply the page shows, through the code the page
uses, and the page shows what they did. An assistant gets a read's value
back as the answer to the call, instead of working out the HTTP API and
fishing it out of the log.

Hand-written rather than built on the MCP SDK: the protocol part is
JSON-RPC with four methods, and the SDK would bring pydantic and a second
server stack into a tool that already has one.

Extensions add tools and notes with ``BenchPlugin.mcp_tools()`` and
``mcp_instructions()``.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import statistics
import time
from datetime import datetime
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__
from . import testcases as tclib
from .core import NMT_LABEL, NMT_STATE, _hex_to_text
from .plugin import McpTool
from .values import describe, parse_value

#: newest first; a client asking for one of these gets it, any other the first
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LOCAL = {"localhost", "127.0.0.1", "::1"}

INSTRUCTIONS = """\
CANopen Bench is a test bench for CANopen devices on a CAN bus. These tools drive the bench that is running:
the devices, bus connection and power supply its web page shows, and the page shows what you do.

- Start with bench_status: connection, devices (node-ID, name, serial number, firmware, NMT state, device
  commands), power supply, test run, firmware download. No devices: connect on the page, then scan.
- Numbers: "0x..." is hex, bare digits are decimal. Index, sub-index and value may also be symbol names
  from the device family's headers, as on the page; flag symbols can be joined with "+".
- Test cases are YAML files in the bench's TestCases folder. tests_list shows them, testcase_check checks a
  draft against the bench's own parser; the format is docs/ablaeufe/testfall-format.md in the
  CANopen-Bench repository.
- Test runs and firmware downloads start and return; pass wait_s to wait, or ask tests_status /
  bench_status later. A case can stop and ask the person at the bench (tests_status shows the prompt):
  answer only with what the user told you.
- The trace records every frame on the bus; trace lists frames, trace_timing measures how regularly one
  COB-ID arrives (heartbeats, PDOs).
- Writes, NMT commands, supply settings and downloads act on real hardware. Do what the user asked."""


# -- helpers -------------------------------------------------------------------

def _num(b, value, what: str) -> int:
    try:  # "$Name" is how a test case writes a symbol; the same name is meant
        return parse_value(str(value).replace("$", ""), [], b.symbols)
    except ValueError as exc:
        raise ValueError(f"{what} {value!r}: {exc}") from None


def _addr(b, args: dict) -> tuple[str, str]:
    """Index and sub-index the way the page writes them: 0x1017 and 00."""
    return (f"0x{_num(b, args['index'], 'index'):04X}",
            f"{_num(b, args.get('sub', 0), 'sub-index'):02X}")


def _device(b, node) -> dict:
    dev = next((d for d in b.devices if d["node"] == int(node)), None)
    if dev is None:
        have = ", ".join(str(d["node"]) for d in b.devices) or "none — scan first"
        raise ValueError(f"no device on node {node} (devices: {have})")
    return dev


def _connected(b) -> None:
    if not b.connected:
        raise ValueError("the bench is not connected to a CAN interface — connect on the page "
                         "(or bench_action connect_toggle)")


def _select(b, node: int) -> None:
    for d in b.devices:
        d["sel"] = d["node"] == node
    b._load_obj_vals(announce=False)


def _secs(stamp: str) -> float | None:
    """A trace row's time of day ("HH:MM:SS.ffffff") in seconds."""
    try:
        h, m, s = stamp.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (AttributeError, ValueError):
        return None


def _cob(b, value) -> int:
    return _num(b, value, "COB-ID")


def _rows(b, args: dict) -> list[dict]:
    """Trace rows matching the filters, oldest first."""
    cobs = args.get("cob")
    cobs = {_cob(b, c) for c in (cobs if isinstance(cobs, list) else [cobs])} if cobs not in (None, "") else None
    node = int(args["node"]) if args.get("node") not in (None, "") else None
    cls = str(args.get("cls") or "").upper()
    direction = str(args.get("dir") or "").upper()
    text = str(args.get("contains") or "").lower()
    since = None
    if args.get("last_s"):
        now = datetime.now()
        since = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6 \
            - float(args["last_s"])
    rows, _counts = b._trace_view()
    out = []
    for r in reversed(rows):
        if since is not None:
            t = _secs(r.get("time", ""))
            if t is None:
                continue
            if t < since:
                break  # newest first from here on back: everything further is older
        if cobs is not None and int(str(r.get("cob", "0")), 16) not in cobs:
            continue
        if node is not None and r.get("node") != node:
            continue
        if cls and str(r.get("cls", "")).upper() != cls:
            continue
        if direction and r.get("dir") != direction:
            continue
        if text and text not in f"{r.get('data', '')} {r.get('dec', '')} {r.get('obj', '')}".lower():
            continue
        out.append(r)
    return out[::-1]


def _log_since(b, before: list) -> list[str]:
    seen = {id(x) for x in before}
    return [f"{x['t']} {x['msg']}" for x in b.logs if id(x) not in seen]


def _run_status(b) -> dict:
    out = {"running": b.running, "results": b.results}
    if b.running and b.run_order:
        out |= {"done": b.run_idx, "of": len(b.run_order),
                "current": b.run_order[min(b.run_idx, len(b.run_order) - 1)]}
    if b.manual_prompt:
        out["prompt"] = b.manual_prompt
    if b.reports:
        out["last_report"] = {**b.reports[0], "folder": str(b._results_dir())}
    return out


def _preferred(b) -> str:
    """The transfer the installed download runs when nobody names one."""
    return getattr(b._swdl, "preferred_mode", "sdo")


def _swdl_nodes(b, nodes) -> dict:
    return {"nodes": {str(n): {"progress": round(b.swdl_prog.get(n, 0)), "phase": b.swdl_phase.get(n, ""),
                               **({"error": b.swdl_err[n]} if n in b.swdl_err else {})} for n in nodes}}


def _swdl_status(b) -> dict:
    # the page's mode is what its own Start button uses; the tool goes by preferred_mode
    return {"running": b.swdl_run, "file": b.fw_sel, "page_mode": b.swdl_mode,
            "preferred_mode": _preferred(b),
            "progress": {str(k): round(v) for k, v in b.swdl_prog.items()},
            "phase": {str(k): v for k, v in b.swdl_phase.items()},
            "error": {str(k): v for k, v in b.swdl_err.items()}}


async def _wait(cond, wait_s) -> None:
    """Until ``cond()`` is false or ``wait_s`` seconds have passed."""
    deadline = time.monotonic() + max(0.0, min(float(wait_s or 0), 600.0))
    while cond() and time.monotonic() < deadline:
        await asyncio.sleep(0.25)


# -- the tools -----------------------------------------------------------------

def bench_status(b, a):
    psu = b._psu_data()
    return {
        "version": __version__, "workspace": b.workspace_name, "connected": b.connected,
        "adapter": b.adapter, "bitrate": b.bitrate, "scanning": b.scan_busy,
        "devices": [{"node": d["node"], "name": d["name"], "nmt": d["nmt"], "serial": d["sn"],
                     "firmware": d["fw"], "variant": d.get("variant"), "eds": d["eds"],
                     "selected": d["sel"],
                     "commands": [{"key": c["key"], "label": c["label"],
                                   "on": d["cmds"].get(c["key"], False)}
                                  for c in b._device_commands(d["eds"])]}
                    for d in b.devices],
        "psu": psu, "tests": _run_status(b), "swdl": _swdl_status(b),
        "trace": {"rows": len(b.trace), "paused": b.trace_paused, "autosave": b.trace_autosave},
        "plugins": [p.name for p in b.plugins],
    }


def bench_log(b, a):
    text = str(a.get("contains") or "").lower()
    lines = [f"{x['t']} {x['msg']}" for x in b.logs if text in x["msg"].lower()]
    return lines[-int(a.get("lines") or 40):]


def bench_action(b, a):
    before = list(b.logs)
    b.dispatch(str(a["action"]), dict(a.get("params") or {}))
    return {"ok": True, "log": _log_since(b, before)}


async def scan(b, a):
    _connected(b)
    b.dispatch("scan", {})
    await asyncio.sleep(0)
    await _wait(lambda: b.scan_busy, 60)
    return bench_status(b, a)["devices"]


async def sdo_read(b, a):
    _connected(b)
    node = _device(b, a["node"])["node"]
    idx, sub = _addr(b, a)
    res = await asyncio.to_thread(b.bus.sdo_read, node, idx, sub)
    if not res.ok:
        b.log(f"SDO  read {idx}:{sub} ✗ abort {res.abort} (node {node}) · MCP", "emcy0")
        return {"ok": False, "abort": res.abort}
    b.remember(f"{idx}:{sub}", res.value, node)
    b.log(f"SDO  read {idx}:{sub} → {res.value} (node {node}) · MCP", "sdo")
    return {"ok": True, "value": res.value, **_reading(b, node, idx, sub, res.value)}


def _reading(b, node: int, idx: str, sub: str, value) -> dict:
    """What a value means, as a report line says it: a string as its text, a signed object with its
    sign, flag and enum names from the headers, a scaled value with its unit."""
    info = b._object_info(node, idx, sub)
    if info is not None and info.is_text:  # real buses hand a string over as a number, back to front
        text = _hex_to_text(value) if str(value).lower().startswith("0x") else str(value)
        return {"text": text} if text is not None else {}
    if not str(value).lower().startswith("0x"):
        return {}
    raw = int(str(value), 16)
    out = {"decimal": info.signed(raw) if info is not None else raw}
    key = f"{idx}:{sub}"
    if (fields := b._object_fields.get(key)) and (meaning := describe(raw, fields, b.symbols)):
        out["meaning"] = meaning
    if (unit := b._object_units.get(key)) is not None and \
            (reading := unit.with_unit(str(value), info.signed_bits if info else 0)):
        out["reading"] = reading
    return out


async def sdo_write(b, a):
    _connected(b)
    node = _device(b, a["node"])["node"]
    idx, sub = _addr(b, a)
    info = b._object_info(node, idx, sub)
    num = _num(b, a["value"], "value")
    width = int(a.get("size") or (info.width if info else 0))
    if num < 0:
        bits = width * 8 if a.get("size") else (info.signed_bits if info else 0)
        if not bits:
            raise ValueError("a negative value needs a signed object in the EDS, or a size")
        num += 1 << bits
    value = b._pad_hex(f"0x{num:X}", width)
    res = await asyncio.to_thread(b.bus.sdo_write, node, idx, sub, value)
    if not res.ok:
        b.log(f"SDO  write {idx}:{sub} ✗ abort {res.abort} (node {node}) · MCP", "emcy0")
        return {"ok": False, "abort": res.abort}
    b.remember(f"{idx}:{sub}", value, node)
    b.log(f"SDO  write {idx}:{sub} ← {value} (node {node}) · MCP", "sdo")
    return {"ok": True, "written": value}


def nmt(b, a):
    _connected(b)
    cmd = str(a["cmd"])
    if cmd not in ("start", "stop", "preop", "reset", "resetcomm"):
        raise ValueError("cmd is one of start, stop, preop, reset, resetcomm")
    target = a.get("node", "all")
    nodes = [d["node"] for d in b.devices] if target == "all" else [_device(b, target)["node"]]
    b.bus.nmt(cmd, None if target == "all" else nodes[0])
    for d in b.devices:
        if d["node"] in nodes and NMT_STATE.get(cmd):
            d["nmt"] = NMT_STATE[cmd]
    b.log(f"NMT  {NMT_LABEL.get(cmd, cmd)} → {'all' if target == 'all' else f'node {nodes[0]:02d}'} · MCP",
          "nmt")
    if cmd == "reset":
        for n in nodes:
            b._forget_dev_cmds(n, "reset node")
    return {"ok": True}


def can_send(b, a):
    _connected(b)
    data = a.get("data") or []
    if isinstance(data, str):
        data = data.replace(",", " ").split()
    # a frame's bytes are hex wherever one is written down; a JSON number is a number
    payload = bytes((x if isinstance(x, int) else int(str(x), 16)) & 0xFF for x in data)
    b.bus.send_raw(_cob(b, a["cob"]), payload)
    return {"ok": True, "sent": payload.hex(" ").upper()}


def device_command(b, a):
    dev = _device(b, a["node"])
    cmd = next((c for c in b._device_commands(dev["eds"]) if c["key"] == a["key"]), None)
    if cmd is None:
        offered = [c["key"] for c in b._device_commands(dev["eds"])]
        raise ValueError(f"node {dev['node']} offers no command {a['key']!r} (it offers {offered})")
    want = a.get("on")
    if want is None or bool(want) != dev["cmds"].get(cmd["key"], False):
        before = list(b.logs)
        b.dispatch("dev_cmd", {"key": cmd["key"], "node": dev["node"]})
        if want is not None and bool(want) != dev["cmds"].get(cmd["key"], False):
            raise ValueError(f"{cmd['label']} did not switch: {_log_since(b, before)}")
    return {"node": dev["node"], "command": cmd["label"], "on": dev["cmds"].get(cmd["key"], False)}


def psu(b, a):
    if b.psu is None:
        raise ValueError("no power supply connected — search for one on the page "
                         "(or bench_action psu_search)")
    before = list(b.logs)
    ch = int(a.get("ch") or 1)
    if a.get("volt") is not None or a.get("curr") is not None:
        b.dispatch("psu_set", {"ch": ch, "volt": a.get("volt"), "curr": a.get("curr")})
    if a.get("output") is not None:
        b.dispatch("psu_output", {"on": bool(a["output"])})
    if all(a.get(k) is None for k in ("volt", "curr", "output")):
        b.dispatch("psu_refresh", {})
    return {"psu": b._psu_data(), "log": _log_since(b, before)}


def tests_list(b, a):
    text = str(a.get("contains") or "").lower()
    rows = [{"id": tc.id, "name": tc.name, "grade": tc.grade, "tools": tc.tools,
             "variants": tc.variants, "dut": tc.dut, **({"error": tc.error} if tc.error else {})}
            for tc in sorted(b.testcases.values(), key=lambda t: t.id)
            if text in f"{tc.id} {tc.name}".lower()]
    return {"folder": b.paths.get("tc", ""), "count": len(rows), "cases": rows[:500]}


def testcase_check(b, a):
    name = str(a.get("filename") or "")
    tc = tclib.parse_testcase(str(a["text"]), name or "draft.yaml", require_prefix=bool(name),
                              extensions=b._step_types, symbols=b.symbols)
    if tc.error:
        return {"ok": False, "error": tc.error}
    return {"ok": True, "id": tc.id, "name": tc.name,
            "steps": len(tc.preconditions) + len(tc.steps)}


async def tests_run(b, a):
    if b.running:
        raise ValueError("a run is already going — tests_status, or tests_stop")
    ids = [str(i) for i in a["ids"]]
    unknown = [i for i in ids if i not in b.testcases]
    broken = [i for i in ids if i in b.testcases and b.testcases[i].error]
    if unknown or broken:
        raise ValueError(f"unknown: {unknown}, unreadable: {broken} — see tests_list")
    if a.get("node") is not None:
        _select(b, _device(b, a["node"])["node"])
    hidden = [i for i in ids if i not in {t[0] for t in b._shown_tests()}]
    if hidden:
        raise ValueError(f"{hidden} need bench tools ({', '.join(b.testcases[hidden[0]].tools)}) and the "
                         "tool filter hides them — bench_action tool_filter_toggle shows them")
    # the run order is made from these at the start; the page's own choice comes back right after
    kept = b.test_sel, b.repeat_case, b.repeat_run
    b.test_sel, b.repeat_case, b.repeat_run = set(ids), max(1, min(99, int(a.get("repeat") or 1))), 1
    before = list(b.logs)
    try:
        b.dispatch("run_start", {"ids": ids})
    finally:
        b.test_sel, b.repeat_case, b.repeat_run = kept
    if not b.running:
        raise ValueError(f"the run did not start: {_log_since(b, before)}")
    await _wait(lambda: b.running and not b.manual_prompt, a.get("wait_s"))
    return _run_status(b)


async def tests_status(b, a):
    await _wait(lambda: b.running and not b.manual_prompt, a.get("wait_s"))
    return _run_status(b)


def tests_stop(b, a):
    b.dispatch("run_stop", {})
    return _run_status(b)


def tests_answer(b, a):
    if not b.manual_prompt:
        raise ValueError("no test case is waiting for an answer")
    b.dispatch("manual_answer", {"choice": a["choice"], "value": a.get("value", "")})
    return {"ok": True}


def firmware_list(b, a):
    return [{"file": e["file"], "version": e.get("ver"), "tag": e.get("tag"), "known": e["known"]}
            for e in b._fw_catalog()]


async def swdl(b, a):
    _connected(b)
    asked = a.get("nodes") or ([a["node"]] if a.get("node") is not None else [])
    if not asked:
        raise ValueError("which devices? Name them in nodes")
    nodes = list(dict.fromkeys(_device(b, n)["node"] for n in asked))
    file = str(a["file"])
    if not b._fw_file(file):
        raise ValueError(f"{file} is not a firmware file in the library that an installed extension "
                         "knows — see firmware_list")
    if b.swdl_run:
        raise ValueError("a download is already running — bench_status shows it")
    mode = a.get("mode") or _preferred(b)
    if mode not in ("sdo", "pdo"):
        raise ValueError("mode is sdo or pdo")
    task = asyncio.ensure_future(b._swdl_download(file, nodes, mode, lambda: False))
    b._tasks.add(task)

    def said(t: asyncio.Task) -> None:
        b._tasks.discard(t)
        if not t.cancelled() and t.exception() is None:
            status, why = t.result()
            b.log(f"SWDL {status} — {why} · MCP", "info" if status == "ok" else "emcy0")
    task.add_done_callback(said)
    await asyncio.wait({task}, timeout=max(0.5, min(float(a.get("wait_s") or 0), 600.0)))
    if task.done():
        status, why = task.result()
        return {"status": status, "detail": why, "mode": mode, **_swdl_nodes(b, nodes)}
    return {"status": "running", "mode": mode, **_swdl_nodes(b, nodes)}


def trace(b, a):
    rows = _rows(b, a)
    limit = max(1, min(int(a.get("limit") or 100), 2000))
    keep = ("time", "dir", "cob", "data", "dec", "obj", "val")
    return {"source": "capture" if b._trace_import is not None else "live",
            "matched": len(rows),
            "rows": [{k: r[k] for k in keep if r.get(k) not in (None, "")} for r in rows[-limit:]]}


def trace_timing(b, a):
    stamps = [(t, r["time"]) for r in _rows(b, a) if (t := _secs(r.get("time", ""))) is not None]
    if len(stamps) < 2:
        return {"frames": len(stamps), "note": "fewer than two frames match — nothing to measure"}
    gaps = [((t2 - t1) % 86400 * 1000, at) for (t1, _), (t2, at) in zip(stamps, stamps[1:])]
    med = statistics.median(g for g, _ in gaps)
    late = [{"after_ms": round(g, 1), "at": at} for g, at in gaps if g > 1.5 * med][:20]
    gaps = [g for g, _ in gaps]
    return {"frames": len(stamps), "interval_ms": {
                "median": round(med, 2), "mean": round(statistics.fmean(gaps), 2),
                "min": round(min(gaps), 2), "max": round(max(gaps), 2),
                "stdev": round(statistics.pstdev(gaps), 2)},
            "longer_than_1.5x_median": late,
            "shorter_than_half_median": sum(1 for g in gaps if g < 0.5 * med)}


def trace_clear(b, a):
    b.dispatch("trace_clear", {})
    return {"ok": True}


# -- the table ------------------------------------------------------------------

_NODE = {"type": "integer", "description": "node-ID of the device"}
_ADDR = {"index": {"type": ["string", "integer"], "description": 'object index, e.g. "0x1017" or a symbol'},
         "sub": {"type": ["string", "integer"], "description": 'sub-index, e.g. "0x00" (default 0) or a symbol'}}
_WAIT = {"wait_s": {"type": "number", "description": "seconds to wait for it to finish (max 600); "
                                                       "0 returns at once"}}
_FILTER = {
    "cob": {"type": ["string", "integer", "array"], "description": 'COB-ID(s), e.g. "0x702"'},
    "node": _NODE | {"description": "only frames addressed to/from this node"},
    "cls": {"type": "string", "description": "frame class: HB, SDO, PDO, EMCY, NMT, SYNC, LSS, ..."},
    "dir": {"type": "string", "enum": ["RX", "TX"]},
    "contains": {"type": "string", "description": "text in data, decode or object"},
    "last_s": {"type": "number", "description": "only the last N seconds"},
}

TOOLS = [
    McpTool("bench_status", "Connection, devices, power supply, test run and firmware download — "
            "what the bench's page shows, in one answer. Start here.", bench_status, read_only=True),
    McpTool("bench_log", "The bench's log (its last 40 lines), optionally filtered.", bench_log,
            {"lines": {"type": "integer"}, "contains": {"type": "string"}}, read_only=True),
    McpTool("scan", "Scan the bus for devices and wait for the result.", scan),
    McpTool("sdo_read", "Read one object of a device by SDO. Besides the raw value: a string object "
            "as text, a signed one with its sign, flag/enum names and units where the headers say.", sdo_read,
            {"node": _NODE, **_ADDR}, ("node", "index")),
    McpTool("sdo_write", "Write one object of a device by SDO. Sized from the device's EDS "
            "unless size is given.", sdo_write,
            {"node": _NODE, **_ADDR,
             "value": {"type": ["string", "integer"], "description": 'e.g. "0x01F4", 500, or symbols '
                       'joined with "+"'},
             "size": {"type": "integer", "description": "bytes (1, 2, 4) when the EDS does not say"}},
            ("node", "index", "value")),
    McpTool("nmt", "Send an NMT command to one device or to all.", nmt,
            {"cmd": {"type": "string", "enum": ["start", "stop", "preop", "reset", "resetcomm"]},
             "node": {"type": ["integer", "string"], "description": 'node-ID, or "all" (default)'}},
            ("cmd",)),
    McpTool("can_send", "Send one raw CAN frame.", can_send,
            {"cob": {"type": ["string", "integer"], "description": 'COB-ID, e.g. "0x000"'},
             "data": {"type": ["string", "array"], "description": 'bytes in hex, e.g. "01 02"'}},
            ("cob",)),
    McpTool("device_command", "Switch a device command (bench_status lists each device's, e.g. a "
            "vendor's service mode) on or off; without 'on' it toggles.", device_command,
            {"node": _NODE, "key": {"type": "string"}, "on": {"type": "boolean"}}, ("node", "key")),
    McpTool("psu", "The bench's power supply: set voltage/current of a channel, switch the output, "
            "and get its state back. Without arguments it only reads.", psu,
            {"ch": {"type": "integer", "description": "channel, default 1"},
             "volt": {"type": "number"}, "curr": {"type": "number"},
             "output": {"type": "boolean"}}),
    McpTool("tests_list", "The test cases in the bench's TestCases folder.", tests_list,
            {"contains": {"type": "string", "description": "part of id or name"}}, read_only=True),
    McpTool("testcase_check", "Check a test case (YAML text) with the bench's parser without running "
            "it — schema, symbols, jump targets.", testcase_check,
            {"text": {"type": "string"},
             "filename": {"type": "string", "description": 'e.g. "TC1234_name.yaml"'}},
            ("text",), read_only=True),
    McpTool("tests_run", "Run test cases on the bench. Returns the run state; results and the "
            "report path come with tests_status once it is done.", tests_run,
            {"ids": {"type": "array", "items": {"type": "string"}},
             "node": _NODE | {"description": "device under test (selects it)"},
             "repeat": {"type": "integer", "description": "runs per case, default 1"}, **_WAIT},
            ("ids",)),
    McpTool("tests_status", "State of the test run: progress, results, a question waiting for the "
            "person at the bench, the last report.", tests_status, dict(_WAIT), read_only=True),
    McpTool("tests_stop", "Stop the test run.", tests_stop),
    McpTool("tests_answer", "Answer the question a test case is waiting on (tests_status shows it): "
            "ok / no / cancel, and the value for an input prompt.", tests_answer,
            {"choice": {"type": "string", "enum": ["ok", "no", "cancel"]},
             "value": {"type": "string"}}, ("choice",)),
    McpTool("firmware_list", "Firmware files in the bench's library.", firmware_list, read_only=True),
    McpTool("swdl", "Download a firmware file from the library to one or more devices in one run. "
            "pdo sends to all of them at once, sdo to one after another. Leave mode out unless the "
            "user names one: the installed download's preferred transfer is used "
            "(bench_status: swdl.preferred_mode).", swdl,
            {"nodes": {"type": "array", "items": {"type": "integer"}, "description": "node-IDs"},
             "node": _NODE | {"description": "a single node-ID, instead of nodes"},
             "file": {"type": "string", "description": "from firmware_list"},
             "mode": {"type": "string", "enum": ["pdo", "sdo"]}, **_WAIT}, ("file",)),
    McpTool("trace", "Frames from the bus trace (or the capture open on the page), oldest first. "
            'A boot-up is a heartbeat frame (0x700+node) with data "00".',
            trace, {**_FILTER, "limit": {"type": "integer", "description": "newest N, default 100"}},
            read_only=True),
    McpTool("trace_timing", "How regularly matching frames arrived: interval statistics and the "
            "late ones — heartbeats, cyclic PDOs.", trace_timing, dict(_FILTER), read_only=True),
    McpTool("trace_clear", "Empty the live trace.", trace_clear),
    McpTool("bench_action", "Any action of the bench's page by name (core.py act_<name>, plugin "
            "actions as '<plugin>.<name>'), for what no other tool covers — e.g. connect_toggle, "
            "set_adapter, psu_search, trace_autosave. Returns the log lines it produced.", bench_action,
            {"action": {"type": "string"}, "params": {"type": "object"}}, ("action",)),
]


# -- the protocol -----------------------------------------------------------------

def tools_of(b) -> dict[str, McpTool]:
    found = {t.name: t for t in TOOLS}
    for p in b.plugins:
        for t in p.mcp_tools():
            name = f"{p.name}_{t.name}"
            if _TOOL_NAME.match(name) and name not in found:
                found[name] = t
    return found


def _describe(name: str, t: McpTool) -> dict:
    return {"name": name, "description": t.description,
            "inputSchema": {"type": "object", "properties": t.params,
                            **({"required": list(t.required)} if t.required else {})},
            "annotations": {"readOnlyHint": t.read_only, "destructiveHint": False,
                            "openWorldHint": False}}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


async def _call(b, params: dict) -> dict:
    tool = tools_of(b).get(params.get("name"))
    args = params.get("arguments") or {}
    missing = [r for r in tool.required if args.get(r) in (None, "")]
    try:
        if missing:
            raise ValueError(f"missing argument(s): {', '.join(missing)}")
        out = tool.run(b, args)
        if inspect.isawaitable(out):
            out = await out
        text, failed = (out if isinstance(out, str)
                        else json.dumps(out, indent=1, ensure_ascii=False, default=str)), False
    except Exception as exc:  # the assistant reads it and tries again differently
        text, failed = str(exc) or type(exc).__name__, True
    finally:
        b._changed()
    return {"content": [{"type": "text", "text": text}], "isError": failed}


async def handle(b, msg) -> dict | None:
    """One JSON-RPC message in, its answer out — None for a notification."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(None, -32600, "invalid request")
    if "method" not in msg or "id" not in msg:
        return None  # a notification, or an answer to a question this server never asks
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        notes = "\n\n".join(n for p in b.plugins if (n := p.mcp_instructions().strip()))
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "canopen-bench", "title": "CANopen Bench", "version": __version__},
            "instructions": INSTRUCTIONS + (f"\n\n{notes}" if notes else "")}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"tools": [_describe(n, t) for n, t in tools_of(b).items()]}}
    if method == "tools/call":
        if params.get("name") not in tools_of(b):
            return _error(mid, -32602, f"unknown tool: {params.get('name')}")
        return {"jsonrpc": "2.0", "id": mid, "result": await _call(b, params)}
    return _error(mid, -32601, f"method not found: {method}")


def endpoint(bench_of):
    """The Starlette handler for ``/mcp``; ``bench_of()`` is the bench of the
    workspace that is open right now."""
    async def mcp(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).hostname not in _LOCAL | {request.url.hostname}:
            # a web page elsewhere must not drive the bench through the visitor's browser
            return JSONResponse(_error(None, -32600, "origin not allowed"), status_code=403)
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(_error(None, -32700, "parse error"), status_code=400)
        batch = body if isinstance(body, list) else [body]
        answers = [a for m in batch if (a := await handle(bench_of(), m)) is not None]
        if not answers:
            return Response(status_code=202)
        return JSONResponse(answers if isinstance(body, list) else answers[0])
    return mcp
