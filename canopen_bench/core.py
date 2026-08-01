"""Bench service: authoritative application state and all command handling.

The frontend is a thin renderer — every mutation happens here and the full
state snapshot is pushed to connected browsers over a WebSocket.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import importlib
import io
import json
import random
import re
import shutil
import sys
import time
import zipfile
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from canopen import objectdictionary as odlib
from canopen.objectdictionary import ODVariable
from canopen.objectdictionary.eds import import_eds

from . import __version__, data, instruments
from . import report as reportlib
from . import testcases as tclib
from .bus.canopen_bus import CanopenBus, _decode_cob
from .bus.demo import EdsDemoBus
from .bus.interface import NO_SERIAL, BusInterface, SdoResult
from .db import Db
from .eds_od import OdCache, find_var, pdo_mapping
from .plugin import BenchPlugin, SwdlStrategy, load_plugins
from .symbols import SymbolTables, load_symbols
from .values import BASES, Field, alternatives, base_of, describe, format_number, parse_value

VERSION = __version__  # single source: canopen_bench/__init__.py

# Inside the package on purpose, not at the repository root. Anything the
# running tool reads has to ship in the wheel, and only package data does:
# this file used to live in examples/, which pip does not install, so
# `pip install canopen-bench` produced a demo mode that scanned and found
# nothing at all. Keep it here, and keep seed/*.eds in package-data.
SEED_EDS = Path(__file__).resolve().parent / "seed" / "DemoDevice.eds"

TICK_S = 0.8
SCAN_DELAY_S = 1.1
TRACE_CAP = 200_000  # ring buffer bound: ~120 MB of row dicts, ≈1 h at 55 frames/s
TRACE_VIEW = 400     # rows per snapshot to the browser — enough scrollback to
                     # follow a multi-step sequence (e.g. addressing) end to end
PLOT_SEL_MAX = 4     # concurrently plotted signals — keeps the chart legible
PLOT_POINTS = 600    # samples retained per plotted signal
TRACE_CLASSES = ("NMT", "SDO", "PDO", "EMCY", "HB")
NMT_LABEL = {"start": "start", "preop": "pre-op", "stop": "stop", "reset": "reset node"}
NMT_STATE = {"start": "Operational", "preop": "Pre-Operational", "stop": "Stopped",
             "reset": "Pre-Operational"}

_TYPE_NAMES = {
    odlib.BOOLEAN: "BOOL", odlib.INTEGER8: "I8", odlib.INTEGER16: "I16",
    odlib.INTEGER32: "I32", odlib.INTEGER64: "I64", odlib.UNSIGNED8: "U8",
    odlib.UNSIGNED16: "U16", odlib.UNSIGNED32: "U32", odlib.UNSIGNED64: "U64",
    odlib.REAL32: "F32", odlib.REAL64: "F64", odlib.VISIBLE_STRING: "STR",
    odlib.OCTET_STRING: "OCT", odlib.UNICODE_STRING: "USTR", odlib.DOMAIN: "DOM",
}


def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def now_us_str() -> str:
    """Full µs resolution — trace rows carry 6 decimals, the UI decides
    whether to show ms or µs."""
    return datetime.now().strftime("%H:%M:%S.%f")


def trace_node(cob: str) -> int | None:
    """Node-id a COB-ID addresses; None for broadcast/unknown functions
    (NMT, SYNC, teach frames) — those always stay visible under the
    device filter."""
    try:
        cid = int(cob, 16)
    except (TypeError, ValueError):
        return None
    node = cid & 0x7F
    if node == 0 or (cid & 0x780) not in (0x080, 0x180, 0x200, 0x280, 0x300, 0x380,
                                          0x400, 0x480, 0x500, 0x580, 0x600, 0x700):
        return None
    return node


def trace_class(dec: str) -> str:
    """Filter class of a decoded frame; "" = unclassified, never hidden."""
    if dec.startswith("NMT"):
        return "NMT"
    if dec.startswith("SDO"):
        return "SDO"
    if "PDO" in dec:
        return "PDO"
    if dec.startswith("EMCY"):
        return "EMCY"
    if dec.startswith(("HB", "Heartbeat")):
        return "HB"
    return ""


def _trace_time_to_seconds(t: str) -> float | None:
    """Parse a trace row's "HH:MM:SS.ffffff" timestamp into seconds-of-day."""
    try:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return None


def _seconds_to_trace_time(sec: float) -> str:
    """Inverse of `_trace_time_to_seconds` — used to give imported frames
    (which only carry a relative offset, not a real time-of-day) a
    timestamp string in the same "HH:MM:SS.ffffff" shape the rest of the
    UI expects, anchored at 00:00:00."""
    sec %= 86400
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:09.6f}"


# SocketCAN `candump -l` logfile format: "(seconds) interface ID#DATA".
# The live-display format (candump without -l) has no per-frame timestamp
# and isn't recognized; neither are remote frames (id#R) or CAN-FD frames
# (id##flags-data) — this tool doesn't speak CAN-FD.
_CANDUMP_RE = re.compile(
    r"^\(\s*(?P<ts>-?[\d.]+)\s*\)\s+\S+\s+(?P<id>[0-9A-Fa-f]{3,8})\s*#\s*(?P<data>[0-9A-Fa-f]*)\s*$")


def parse_candump(text: str) -> tuple[list[tuple[float, int, bytes]], int]:
    """Parse a SocketCAN `candump -l` log into (seconds-since-first-frame,
    COB-ID, data) tuples, skipping lines that don't match the recognized
    format. Returns (frames, skipped-line-count) — the caller decides how
    to report a partially- or wholly-unrecognized file."""
    frames: list[tuple[float, int, bytes]] = []
    skipped = 0
    t0: float | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _CANDUMP_RE.match(line)
        if not m:
            skipped += 1
            continue
        try:
            ts = float(m["ts"])
            cob_id = int(m["id"], 16)
            frame_data = bytes.fromhex(m["data"])
        except ValueError:
            skipped += 1
            continue
        if t0 is None:
            t0 = ts
        frames.append((ts - t0, cob_id, frame_data))
    return frames, skipped


def normalize_identity(ident: str) -> str:
    """Canonical identity signature: minimal-width hex, e.g. "0x4D2·0x1150".

    Historic entries carry fixed widths (EDS upload "0x00AF", hardware scan
    "0x000000AF"), so every comparison normalizes both sides — stored
    registry rows keep matching without a migration. Unparsable values
    (e.g. "?") pass through unchanged.
    """
    try:
        vendor, product = ident.split("·")
        return f"0x{int(vendor, 16):X}·0x{int(product, 16):X}"
    except ValueError:
        return ident


def _hexstr(value) -> str:
    """Step-file values reach us as str or — because YAML parses bare 0x
    literals — as int; the bus primitives want hex strings."""
    return f"0x{value:X}" if isinstance(value, int) else str(value)




def _as_int(value) -> int | None:
    if isinstance(value, int):
        return value
    text = str(value).strip()
    base = base_of(text)
    if base is None:
        return None
    try:
        return int(text, base)
    except (ValueError, IndexError):
        return None


def _hexstr_width(value: object) -> int:
    """Byte width of a "0x001E"-style answer, or 0 when it is not one.

    The device already told us how wide the object is by how many digits
    it sent back; that is a better default than a constant."""
    text = str(value).strip()
    if not text.lower().startswith("0x"):
        return 0
    digits = len(text) - 2
    return (digits + 1) // 2 if digits else 0


def _variant_matches(declared: list[str], actual: str) -> bool:
    """Does the device's variant satisfy the ones the case names?

    Compared as numbers wherever both sides are numbers, because they
    reach here written differently: a case says ``820``, and the device's
    answer is whatever the SDO read produced — a hex string like
    ``"0x0334"``. Plain string equality meant every case naming a variant
    skipped against real hardware while passing any test that happened to
    spell the value byte for byte.

    A variant that is not a number — an EDS row can map the raw answer to
    a label — falls back to comparing text, case-insensitively.
    """
    got = _typed_number(actual)
    for want in declared:
        number = _typed_number(want)
        if number is not None and got is not None:
            if number == got:
                return True
        elif str(want).strip().lower() == str(actual).strip().lower():
            return True
    return False


def _open_in_editor(path: Path) -> None:
    """Hand a file to whatever the machine opens it with.

    Three platforms, three commands, none of them a shell: the path is
    passed as an argument, so a file name with a space or a quote in it
    is a file name and not a second command.
    """
    import subprocess
    if sys.platform == "win32":
        import os
        os.startfile(str(path))          # noqa: S606 — the platform's own opener
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(path)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)  # never share this server's stdin


def _typed_number(text: str) -> int | None:
    """A number the way a person types it: hex only when it says ``0x``.

    Everywhere a *file* supplies a value, a bare string is hex — it was
    written against a datasheet and the format says so. Everywhere a
    person types one, it is not: "30" is thirty. Guessing hex from the
    digits means somebody watching a meter types 30 and the device gets
    forty-eight, and nothing on screen admits it.

    So the rule for typed input is one rule, in one place, and it is the
    explicit one: write 0x or it is decimal.
    """
    text = str(text).strip()
    try:
        return int(text, 16) if text.lower().startswith(("0x", "-0x")) else int(text, 10)
    except ValueError:
        return None


def _judge_read(spec: dict, res: SdoResult) -> tuple[str, str]:
    """Verdict for an sdo_read step: values compare numerically (with
    optional mask) so "0x2A" matches "0x0000002A". An expectation that is
    not a number is text: the answer is decoded back into characters and
    the two are compared as such."""
    where = f"sdo_read {_hexstr(spec['index'])}:{_hexstr(spec['sub'])}"
    if "expect_abort" in spec:
        if res.ok:
            return "fail", f"{where} expected abort {_hexstr(spec['expect_abort'])}, got {res.value}"
        code = _as_int(res.abort.split()[0]) if res.abort else None
        if code is not None and code == _as_int(spec["expect_abort"]):
            return "ok", f"Response: abort {res.abort} — expected"
        return "fail", f"{where} expected abort {_hexstr(spec['expect_abort'])}, got {res.abort}"
    if not res.ok:
        return "fail", f"{where} abort {res.abort}"
    if "expect" not in spec:
        return "ok", ""
    got, exp = _as_int(res.value), _as_int(spec["expect"])
    if got is None or exp is None:
        # One side is not a number, so this is a text comparison — and the
        # answer has to be decoded for it. Comparing the raw hex against
        # the wanted characters could only ever fail: a device name comes
        # back as 0x0000003332315F4F4D4544, and no amount of it looks like
        # "DEMO_123". The quotes are the CSV's way of saying "these are
        # characters", and are not part of the value.
        want = str(spec["expect"]).strip()
        if len(want) >= 2 and want[0] == want[-1] == '"':
            want = want[1:-1]
        text = _hex_to_text(res.value)
        if text == want or str(res.value) == str(spec["expect"]):
            return "ok", ""
        shown = f'"{text}"' if text is not None else repr(res.value)
        return "fail", f"{where} = {shown}, expected {want!r}"
    mask = _as_int(spec["mask"]) if "mask" in spec else None
    ok = (got & mask) == (exp & mask) if mask is not None else got == exp
    if ok:
        return "ok", ""
    detail = f" (mask {_hexstr(spec['mask'])})" if mask is not None else ""
    return "fail", f"{where} = {res.value}, expected {_hexstr(spec['expect'])}{detail}"


def _with_registers(spec: dict, regs: dict) -> dict:
    """``expect``/``mask`` named as a register, replaced by its value.

    A case that works out what it expects — read the variant, derive the
    screen code from it, then compare — writes ``expect: R11``, which the
    format has always allowed. Without this the name reached the
    comparison as the literal string "R11", matched nothing, and the step
    could only fail.
    """
    if not any(spec.get(k) in regs for k in ("expect", "mask")):
        return spec
    out = dict(spec)
    for key in ("expect", "mask"):
        if out.get(key) in regs:
            out[key] = regs[out[key]]
    return out


def _step_text(key: str, val) -> str:
    """Human-readable step line for the run progress ("step 3/9 <text>")."""
    if key in ("manual", "ask"):
        return val if isinstance(val, str) else str(val.get("text", ""))
    if key == "adjust":
        where = f"{_hexstr(val['index'])}:{_hexstr(val['sub'])}"
        return f"adjust {where}" + (f" — {val['text']}" if val.get("text") else "")
    if key == "rand":
        return f"rand {val['to']} in {val.get('min', 0)}..{val.get('max', '2^32-1')}"
    if key == "jump_on_error":
        return f"jump to {val} if the case already failed"
    if key == "nmt":
        if isinstance(val, dict):
            return f"NMT {val['cmd']}" + (" (all)" if val.get("node") == "all" else "")
        return f"NMT {val}"
    if key == "sdo_read":
        return f"read {_hexstr(val['index'])}:{_hexstr(val['sub'])}"
    if key == "sdo_write":
        return f"write {_hexstr(val['index'])}:{_hexstr(val['sub'])} = {val['value']}"
    if key == "wait":
        secs = val.get("s") if isinstance(val, dict) else val
        return f"wait {secs:g}s" if isinstance(secs, (int, float)) else f"wait {secs}s"
    if key == "wait_for":
        if "cob" in val:
            cobs = val["cob"] if isinstance(val["cob"], list) else [val["cob"]]
            return f"wait for frame {' or '.join(str(c) for c in cobs)}"
        return f"wait for heartbeat {val['heartbeat']}"
    if key == "can_send":
        return f"send frame {val['cob']}"
    if key == "lss_assign":
        return f"LSS assign 1..{val['count']}"
    if key in tclib._ARITH:
        return f"{key} {val['to']}, {val['value']}"
    if key in ("jump", "label"):
        return f"{key} {val}"
    if key in tclib._COND_JUMPS:
        return f"{key} {val['a']} {val['b']} → {val['to']}"
    if key == "psu":
        bits = [f"channel {val['ch']}"] if "ch" in val else []
        bits += [f"{val[k]} {u}" for k, u in (("volt", "V"), ("curr", "A")) if k in val]
        if "output" in val:
            bits += ["output " + ("on" if val["output"] in (True, "on") else "off")]
        return "supply " + ", ".join(bits)
    if key == "expect_emcy":
        # mask 0 compares nothing, which is how "any EMCY at all" is
        # written — saying "expect EMCY 0x00" for that would be a lie
        if "mask" in val and _as_int(val["mask"]) == 0:
            return "expect any EMCY"
        return _emcy_wanted(val).replace("expect_emcy ", "expect EMCY ")
    if key == "expect_no_emcy":
        return ("expect no EMCY" if not (val.keys() & {"code", "mec", "reg"})
                else _emcy_wanted(val).replace("expect_emcy ", "expect no EMCY "))
    if key == "emcy_clear":
        return "clear EMCY list"
    if key in ("fail", "skip"):
        return f"{key}: {val}"
    if key == "end":
        return "end"
    return str(val)


def _resolve(value, regs: dict, builtins: dict) -> int:
    """Format-v2 value: int literal, hex string, register, or builtin."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    s = str(value)
    if s in regs:
        return regs[s]
    if s == "$node":
        return int(builtins["node"])
    if s == "$expected":
        return int(builtins["expected"])
    base = base_of(s)
    if base is None:
        raise ValueError(f"{s!r} names a base that does not exist "
                         f"(known: {', '.join(sorted(BASES))})")
    return int(s, base)


def _resolve_num(value, regs: dict, builtins: dict) -> float:
    """Like _resolve, but volts and amps are not integers — a supply set to
    26.5 V is an ordinary thing to ask for. Registers stay whole numbers;
    they hold values read from a device."""
    if isinstance(value, float):
        return value
    return float(_resolve(value, regs, builtins))


def _slug(text: str) -> str:
    """A file-name-safe version of a case name, in the shape the previous
    tool used: words joined by underscores, nothing exotic left."""
    keep = [ch if ch.isalnum() else " " for ch in text]
    return "_".join("".join(keep).split()) or "case"


def _mec(mfr: bytes) -> int:
    """The manufacturer error code: the first two of the five manufacturer
    bytes, little-endian like every other multi-byte field in the frame.

    Reading only the first byte works right up to the first code above
    0xFF, and then compares a low byte against a whole number and says
    nothing arrived.
    """
    return (mfr[0] if mfr else 0) | ((mfr[1] << 8) if len(mfr) > 1 else 0)


def _emcy_str(entry: tuple[int, int, int, bytes]) -> str:
    """One recorded EMCY, in the terms a case is written in."""
    node, code, reg, mfr = entry
    return (f"0x{code:04X} reg 0x{reg:02X} mec 0x{_mec(mfr):04X} "
            f"from node {node:02d}")


def _emcy_wanted(val: dict) -> str:
    """What the step asked for, for the failure line."""
    parts = []
    if "code" in val:
        parts.append(f"code {_hexstr(val['code'])}"
                     + (f" (mask {_hexstr(val['mask'])})" if "mask" in val else ""))
    if "mec" in val:
        parts.append(f"mec {_hexstr(val['mec'])}"
                     + (f" (mask {_hexstr(val['mec_mask'])})" if "mec_mask" in val else ""))
    if "reg" in val:
        parts.append(f"reg {_hexstr(val['reg'])}")
    return "expect_emcy " + (", ".join(parts) if parts else "any")


#: CiA 301 data types whose content is characters, not a number
#: (VISIBLE_STRING, OCTET_STRING, UNICODE_STRING)
_TEXT_TYPES = (0x09, 0x0A, 0x0B)


def _hex_to_text(value: object) -> str | None:
    """A device answer read back as the characters it is, or None.

    The bus builds the answer with int.from_bytes(data, "little"), so the
    bytes stand in the hex string back to front: "DEMO_123" arrives as
    0x0000003332315F4F4D4544. Reversing puts them right, and the leading
    zeros of the number are the trailing padding of the string.

    Anything outside printable ASCII means this was not text after all,
    and saying so beats printing control characters at somebody.
    """
    digits = str(value).strip()
    if digits[:2].lower() != "0x":
        return None
    digits = digits[2:]
    if not digits or len(digits) % 2:
        return None
    try:
        raw = bytes.fromhex(digits)[::-1].rstrip(b"\x00")
    except ValueError:
        return None
    if not raw or any(b < 0x20 or b > 0x7E for b in raw):
        return None
    return raw.decode("ascii")


def _write_value(val: dict, regs: dict, builtins: dict) -> str:
    """The hex literal an sdo_write actually puts on the wire.

    One function, because the step line shows this and the bus gets it: if
    the two were worked out separately, a report could name a value the
    device never saw, which is the kind of difference nobody catches by
    reading.

    `size` wins where it is given. Without one, a literal's own digits are
    the width — that is how a two-byte object is written as "0x001E".
    """
    raw = val["value"]
    declared = val.get("size")
    if (declared is None and isinstance(raw, str)
            and raw not in regs and not raw.startswith("$")):
        return raw
    size = int(declared or 4)
    v = _resolve(raw, regs, builtins) & ((1 << (size * 8)) - 1)
    return f"0x{v:0{size * 2}X}"


def _duplicate_ids(catalog: list) -> dict[str, list[str]]:
    """Ids that more than one file in the folder claims.

    The catalog is keyed by case id, so two files claiming 4602 means one
    of them is simply not in it — and until now nothing said so: the
    folder held 85 files, the list showed 81, and the four that went
    missing were whichever the directory order dropped. The id cannot
    just be made unique here, because it is what the run order, the
    results and the report are all written against. So the collision is
    reported instead, against the file that survived it.
    """
    seen: dict[str, list[str]] = {}
    for tc in catalog:
        seen.setdefault(tc.id, []).append(Path(tc.file).name if tc.file else "?")
    return {tid: files for tid, files in seen.items() if len(files) > 1}


def _bench_user() -> str:
    """Who ran it, for the report header. Best effort: on a bench machine
    this is a login name, and where the environment does not say, an empty
    field beats a made-up one."""
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return ""


def _bytes_str(data: bytes) -> str:
    return "0x" + data.hex().upper()


def _session_bytes(session_str: str) -> bytes:
    """mc.session display string back to raw bytes ($session builtin)."""
    digits = str(session_str).removeprefix("0x").removeprefix("0X")
    if len(digits) % 2:
        digits = "0" + digits
    try:
        return bytes.fromhex(digits) if digits else b"\x00"
    except ValueError:
        return b"\x00"


class SimSwdlStrategy(SwdlStrategy):
    """The shipped SWDL simulation: advances per-device progress each tick
    until all selected devices reach 100 %, then stamps the chosen
    firmware version. Real vendor download protocols replace this via
    BenchPlugin.swdl_strategy()."""

    name = "sim"

    def start(self, bench) -> None:
        bench.swdl_run = True
        bench.swdl_done = False
        bench.swdl_prog = {}
        mode = bench.swdl_mode.upper() + (" parallel" if bench.swdl_mode == "pdo" else " serial")
        bench.log(f"SWDL v{bench.fw_sel} → {bench._sel_names()} ({mode})")

    def step(self, bench) -> None:
        targets = bench.sel_devices
        if bench.swdl_mode == "sdo":
            cur = next((d for d in targets if bench.swdl_prog.get(d["node"], 0) < 100), None)
            if cur:
                n = cur["node"]
                bench.swdl_prog[n] = min(100, bench.swdl_prog.get(n, 0) + 9 + random.random() * 8)
        else:
            for d in targets:
                n = d["node"]
                bench.swdl_prog[n] = min(100, bench.swdl_prog.get(n, 0) + 6 + random.random() * 6)
        if targets and all(bench.swdl_prog.get(d["node"], 0) >= 100 for d in targets):
            bench.swdl_run = False
            bench.swdl_done = True
            for d in bench.devices:
                if d["sel"]:
                    d["fw"] = bench.fw_sel
            bench.log(f"SWDL complete — {len(targets)} device(s) now on v{bench.fw_sel}")


class Bench:
    def __init__(self, db: Db, bus: BusInterface | None = None,
                 plugins: list[BenchPlugin] | None = None,
                 workspaces_root: Path | None = None):
        self.db = db
        # multi-workspace mode: every subfolder of the root is one workspace
        # (db + eds/traces/flows inside); the app layer swaps the Bench on
        # switch and registers the callback below
        self.workspaces_root = workspaces_root
        self.on_workspace_switch: Callable[[str], None] | None = None
        # GUI plugin install (Setup > Extensions): one directory next to the
        # workspaces, shared across all of them since plugins are a process
        # concept, not a per-workspace one — same reasoning as why it's tied
        # to workspaces_root rather than the current workspace's own folder.
        # Registered on sys.path before plugin discovery below, so packages
        # installed in an earlier run are found on every startup, not just
        # right after an install.
        self.plugin_dir: Path | None = None
        if workspaces_root is not None:
            self.plugin_dir = workspaces_root / "plugins"
            self.plugin_dir.mkdir(parents=True, exist_ok=True)
            if str(self.plugin_dir) not in sys.path:
                sys.path.insert(0, str(self.plugin_dir))
        self.on_plugin_reload: Callable[[], None] | None = None
        # None -> entry-point discovery; a list (possibly empty) -> injected,
        # same pattern as the bus parameter (tests, embedding).
        self.logs: list[dict] = []  # before the plugin hooks: symbol loading logs
        self.plugins = load_plugins() if plugins is None else list(plugins)
        # symbol tables from the device's own C headers, parsed before the
        # hooks below because object_fields() is written against them — the
        # workspace copy is the firmware actually under test, and the field
        # descriptions have to follow it rather than a packaged snapshot
        self.symbols_dir = db.path.parent / "symbols"
        self.symbols: SymbolTables = self._load_symbols()
        self.adapter_cards = ([c for p in self.plugins for c in p.adapters()]
                              + list(data.ADAPTERS))
        extra_backends = {key: backend for p in self.plugins
                          for key, backend in p.adapter_backends().items()}
        self._plugin_fw = [f for p in self.plugins for f in p.firmware()]
        self.fw_list = self._plugin_fw + list(data.FIRMWARE)
        # first plugin (entry-point order) providing addressing support wins
        self.addressing = next((ap for p in self.plugins
                                if (ap := p.addressing_provider()) is not None), None)
        self._trace_decoders = [d for p in self.plugins for d in p.trace_decoders()]
        # sidebar panels, namespaced like actions and step types; a panel
        # that raises is dropped into _panels_broken and never retried,
        # since render() runs on every snapshot
        self._device_panels = [(f"{p.name}.{panel.key}", panel)
                               for p in self.plugins for panel in p.device_panels()]
        self._panels_broken: set[str] = set()
        # how to read an object's value symbolically, keyed "0x2007:09"
        self._object_fields: dict[str, list[Field]] = {}
        for p in self.plugins:
            self._object_fields.update(p.object_fields(self.symbols))
        # CiA-301 EMCY texts with vendor codes merged over them (plugin wins)
        self._emcy_codes = dict(data.EMCY_CODES)
        for p in self.plugins:
            self._emcy_codes.update(p.emcy_codes())
        # plugin actions are namespaced "<plugin>.<name>" — they can never
        # shadow a core act_* handler
        self._plugin_actions = {f"{p.name}.{name}": fn
                                for p in self.plugins
                                for name, fn in p.actions(self).items()}
        # plugin step primitives, referenced in YAML as "<plugin>.<key>"
        self._step_types = {f"{p.name}.{st.key}": st
                            for p in self.plugins for st in p.step_types()}
        # firmware download: first plugin strategy wins, else the simulation
        self._swdl = next((s for p in self.plugins
                           if (s := p.swdl_strategy()) is not None), None) \
            or SimSwdlStrategy()
        self._hw_bus = bus or CanopenBus(extra_backends=extra_backends)
        self._demo_bus = EdsDemoBus(db)
        self._demo_bus.install_hooks([h for p in self.plugins for h in p.demo_hooks()])
        self._notify: Callable[[], Awaitable[None]] | None = None
        self._tasks: set[asyncio.Task] = set()
        # one state push at a time, plus a note that another was asked for
        # while it ran (see _changed)
        self._push_task: asyncio.Task | None = None
        self._push_again = False
        self._push_error = ""
        self._catalog_cache: dict[str, tuple[float, tuple[dict, list] | str]] = {}
        self._ods = OdCache(db.eds_dir)  # object names for the trace interpreter
        # bus backends report a vanished interface (adapter unplugged) from
        # their worker threads; the captured loop gets us back on the loop
        self._loop: asyncio.AbstractEventLoop | None = None
        self._hw_bus.on_lost = self._on_bus_lost
        self._demo_bus.on_lost = self._on_bus_lost

        # --- live state ---------------------------------------------------
        # The tool starts offline: connect explicitly, then scan populates
        # the device list. Disconnecting (or shutdown) drops the devices.
        self.connected = False
        self.scan_busy = False
        adapter = db.get("adapter", "demo")  # out of the box: no hardware needed
        if not any(a["key"] == adapter for a in self.adapter_cards):
            adapter = "demo"  # persisted adapter's plugin is no longer installed
        self.adapter = adapter
        self.bitrate = db.get("bitrate", "500")
        self.own_node_id: int = int(db.get("own_node_id", 127))
        sr = db.get("scan_range", [1, 127])
        self.scan_range: tuple[int, int] = (int(sr[0]), int(sr[1]))
        self.browse: dict | None = None  # directory-picker state while the modal is open
        self.devices: list[dict] = []
        self.emcy_new = 0
        #: every EMCY seen since the last emcy_clear, as
        #: (node, error code, error register, manufacturer bytes) — what a
        #: test case checks against. A device sends an EMCY when it is
        #: ready, not when a step happens to be waiting, so the check reads
        #: a record rather than a live window (deque: a run that never
        #: clears must not grow without bound).
        #:
        #: The manufacturer bytes are kept because that is where a device
        #: family puts its own error code, and that is what a case is
        #: usually about — CiA 301 says nothing about their content, so
        #: only the frame carries it.
        self.emcy_seen: deque[tuple[int, int, int, bytes]] = deque(maxlen=200)
        self.obj_vals: dict[str, str] = {}
        # bench instruments beside the bus (canopen_bench/instruments): the
        # port that once answered is remembered, so a restart reconnects to
        # that one instead of writing *IDN? to every serial port it finds
        self.psu: instruments.PowerSupply | None = None
        self.psu_error = ""
        self._psu_state: instruments.SupplyState | None = None
        self._psu_opener = None            # tests inject a fake serial port
        self._psu_ports = None
        self._psu_connect(str(db.get("psu_port") or ""), announce=False)
        self.test_sel: set[str] = set()   # demo seeds below, once the catalog is known
        self.running = False
        self.run_order: list[str] = []
        self.run_idx = 0
        # what the run writes into the results folder at the end
        self._run_cases: list[reportlib.CaseRecord] = []
        self._run_record: reportlib.CaseRecord | None = None
        self._run_started = ""
        self.results: dict[str, str] = {}
        self.run_prog: dict | None = None       # {tid, step, of, text} while executing
        self.manual_prompt: dict | None = None  # {tid, text} while waiting for the operator
        self._manual_event: asyncio.Event | None = None
        self._manual_result = "cancel"   # "ok" | "no" | "cancel" | "abort"
        self._manual_value = ""          # what an `adjust` prompt was given
        self._run_stop_requested = False
        self._run_mode = "sim"  # "exec" once a run uses real test-case files
        # off by default: one failing case is a result, not a reason to
        # stop finding out about the rest. The option is on the Tests page
        # for the runs where the first failure invalidates what follows.
        self.stop_on_err = False
        self.tool_filter = True
        self.repeat_case = 1
        self.repeat_run = 1
        self.reports: list[dict] = []  # demo seeds are injected per snapshot, demo adapter only
        #: the last overview across runs, once one was asked for
        self.overview: dict | None = None
        # expected/found/last/result start empty — they only carry values
        # once a state was adopted and a verify actually ran
        self.mc: dict = {"enabled": False, "session": "", "expected": 0, "found": 0,
                         "last": "", "result": "", "busy": False}
        self.mc.update({"autoStart": True, "autoReaddr": True, "scanStart": True,
                        "teachFlow": "teach_addressing.yaml", "hbTimeoutMs": 3000}
                       | db.get("mc_opts", {}))
        # heartbeat-loss monitoring (Machine Control only — see
        # _check_heartbeats): last-seen time per node, nodes currently past
        # the timeout, and the grace-window anchor after a (re)start
        self._hb_seen: dict[int, float] = {}
        self._hb_lost: set[int] = set()
        self._hb_monitor_since = 0.0
        self.fw_sel = self.fw_list[0]["ver"] if self.fw_list else ""
        self.swdl_mode = "sdo"
        self.swdl_run = False
        self.swdl_done = False
        self.swdl_prog: dict[int, float] = {}
        self.trace: list[dict] = []  # fills once connected
        self.trace_paused = False
        self.trace_hide: set[str] = set()  # classes hidden by the trace filter
        self.trace_dev_filter = False  # True = only frames of the selected devices
        self._trace_counts: dict[tuple[str, int | None], int] = {}  # rows per (class, node)
        self.bus_load = 0.0  # rolling %, estimated from traced frames
        self.err_frames = 0  # error frames seen since connect
        self._load_win: deque[tuple[float, int]] = deque()  # (monotonic, bits) per tick
        # trace statistics (Stats view): cumulative per-COB counters since
        # connect/clear, a short window for frames/s, and a bus-load history
        self._cob_stats: dict[str, dict] = {}  # cob -> {"n", "dec", "cls"}
        self._rate_win: deque[tuple[float, dict[str, int]]] = deque()  # (monotonic, cob->n) per tick
        self._load_hist: deque[float] = deque(maxlen=75)  # ~60 s of bus-load %, one point per tick
        self._stats_t0 = 0.0  # monotonic start of the current observation
        self.trace_dir = db.path.parent / "traces"  # saved capture files
        self.trace_loaded: str | None = None  # capture file currently shown instead of live data
        self._trace_saved: list[dict] = []  # cached dir listing: refreshed on save/delete, not per tick
        self._refresh_trace_saved()
        self.raw_rows: list[dict] = db.get("raw_sdo", [{"i": "0x2040", "s": "01", "l": "4", "v": "0x00260001"}])
        self._cyc_seq = 0  # id source for raw rows (cyclic scheduler key)
        for r in self.raw_rows:  # rows saved before typed rows / per-row nodes existed
            r.setdefault("type", "sdo")
            r.setdefault("node", "")
            r.setdefault("cyc", "100")  # cycle time ms — persisted with the row
            r["run"] = False  # cyclic senders never resume on their own after a restart
            self._cyc_seq += 1
            r["id"] = self._cyc_seq
        self._cyc_next: dict[int, float] = {}  # row id -> monotonic next-due
        self.sync_ms = int(db.get("sync", {"ms": 100}).get("ms", 100))
        self.sync_run = False  # like row runs: no surprise traffic on startup
        # favorites: one auto-saved list per workspace ({idx, sub, label});
        # old named-set workspaces carry over their last active set
        stored_favs = db.get("favorites")
        if stored_favs is None:
            stored_favs = (db.get("fav_sets") or {}).get(db.get("fav_set", "Default"), [])
        self.favorites: list[dict] = stored_favs
        # signal plot selection ({idx, sub, label}, same shape/keys as
        # favorites — separate list since "watch this on a chart" and
        # "quick-write this" are different intents); series are in-memory
        # only (time-windowed, not meaningful to persist across restarts)
        self.plot_sel: list[dict] = db.get("plot_sel", [])
        self.plot_series: dict[str, deque[tuple[float, float]]] = {}
        self._plot_keys: set[str] = {f"{r['idx']}:{r['sub']}" for r in self.plot_sel}
        # symbol tables from the device's own C headers, seeded per plugin
        # like flows and parsed before the test-case catalog — cases may
        # reference $Symbol and must fail to load, not mid-run, on a typo
        # hex or dec for every object value shown; the other reading stays
        # one hover away, so switching can never hide anything
        self.num_base: str = db.get("num_base", "hex")
        self.symbols_dir = db.path.parent / "symbols"
        self.symbols: SymbolTables = self._load_symbols()
        # test-config paths default into the workspace folder; the default is
        # computed (not persisted) so a copied workspace keeps pointing at its
        # own copy — act_set_path persists once the user configures something
        stored_paths = db.get("paths")
        if stored_paths is None:
            stored_paths = {"tc": str(db.path.parent / "testcases"),
                            "res": str(db.path.parent / "results")}
            for p in stored_paths.values():
                Path(p).mkdir(parents=True, exist_ok=True)
        self.paths = stored_paths
        self.testcases: dict[str, tclib.TestCase] = {}
        self._load_testcases(log=False)
        # The demo catalog comes with a few cases ticked, so that the shipped
        # demo shows what a selection looks like rather than an empty list.
        # A real TestCases folder must not inherit it: the demo ids are
        # ordinary numbers and overlap with real ones, so cases nobody had
        # chosen sat ticked at every start — one press of Start away from
        # running against the hardware on the bench. Gated exactly like the
        # demo catalog itself (see _catalog_rows).
        if not self.testcases and self.adapter == "demo":
            self.test_sel = set(data.DEFAULT_TEST_SEL)
        # test suites: name -> {sel, repeat_case, repeat_run, stop_on_err}
        self.suites: dict[str, dict] = db.get("suites", {})
        self.active_suite: str = db.get("active_suite", "")
        # button-teach addressing (A-05): flow files + live progress
        self.flows_dir = self.db.path.parent / "flows"
        self._seed_default_flows()
        # normalize the persisted procedure choice: heal names that no longer
        # exist (e.g. plugin uninstalled) and prefer a vendor procedure over
        # the shipped standard-LSS flow when one is available
        flow_files = self._flow_files()
        if self.mc.get("teachFlow") not in flow_files:
            if "teach_addressing.yaml" in flow_files:
                self.mc["teachFlow"] = "teach_addressing.yaml"
            else:
                self.mc["teachFlow"] = flow_files[0] if flow_files else ""
        self.teach: dict | None = None  # {step, of, text} while teaching
        self._teach_abort = False

        if not db.eds_count():
            seed_eds = list(data.SEED_EDS_FILES) + [e for p in self.plugins
                                                    for e in p.seed_eds()]
            for e in seed_eds:
                db.eds_add(e["file"], e["dev"], e["ident"], e["code"], e["enabled"])
                if e.get("commands"):
                    db.eds_set_commands(e["file"], e["commands"])
                if variant := e.get("variant"):
                    # where this device family keeps its variant number. The
                    # panel lets an operator configure it per row; a family
                    # whose plugin already knows should not make them.
                    db.eds_set_variant(e["file"], variant.get("index", ""),
                                       variant.get("sub", ""),
                                       variant.get("map") or {})

        if db.is_first_run:
            self._seed_demo_eds()

        # machine control's expected state (device count, session, node→EDS)
        # lives in the workspace kv — adopted deliberately via the MC card
        self.mc_ref: dict | None = db.get("mc_ref")
        self._adopt_ref()

    @property
    def bus(self) -> BusInterface:
        """The demo adapter routes to the EDS-driven demo bus (no hardware);
        every other adapter key goes to the real backend (CanopenBus via
        python-can, or whatever was injected for tests)."""
        return self._demo_bus if self.adapter == "demo" else self._hw_bus

    @property
    def demo(self) -> bool:
        """No hardware behind the bus: devices are generated from the EDS
        files, and every value read is something this process made up.

        Plugins need this. Anything that mirrors a device — a panel
        showing what the front of it looks like, a readout of its state —
        is a measurement, and a measurement invented by the tool itself is
        worse than none: it shows a device that is not there. Such a
        feature belongs to the real bus, and the demo keeps whatever
        generic stand-in the core provides.
        """
        return self.adapter == "demo"

    def shutdown(self) -> None:
        """Server is stopping: close the bus connection like a disconnect."""
        if self.connected:
            self.connected = False
            self.bus.disconnect()
            self.log("BUS  disconnected — server shutdown")

    # ------------------------------------------------------------------
    def set_notifier(self, notify: Callable[[], Awaitable[None]]) -> None:
        self._notify = notify

    # -- connection loss (auto-disconnect) --------------------------------
    def _capture_loop(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:  # sync context (tests): lost-callbacks apply directly
            pass

    def _on_bus_lost(self, reason: str) -> None:
        """A bus backend detected that the interface vanished mid-session
        (adapter unplugged, driver gone) and already tore itself down.
        Arrives from a backend worker thread — hop into the event loop
        before touching state."""
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._apply_bus_lost, reason)
                return
            except RuntimeError:  # loop closed between check and call (shutdown race)
                pass
        self._apply_bus_lost(reason)

    def _apply_bus_lost(self, reason: str) -> None:
        if not self.connected:  # already disconnected — nothing to do, no duplicate log
            return
        self._stop_cyclic()
        self.connected = False
        self.devices = []
        self.log(f"BUS  connection lost — {reason} — auto-disconnected", "emcy0")
        self._changed()

    def _changed(self) -> None:
        """Ask for a state push, with at most one of them in flight.

        A push is a full snapshot of everything on screen, and the executor
        asks for one after every step — including the register arithmetic
        and the jumps that a case spends most of its steps on. Measured on
        three short cases against a browser: 3627 pushes, 41 MB through the
        socket in five seconds. The page spends all of it parsing JSON, so
        it never repaints between the first case and the end of the run.
        That does not look like a slow screen, it looks like a frozen one —
        the run is over and the panel still names the first case.

        A request that arrives while a push is running becomes a single
        trailing push once that one lands. The trailing part is the whole
        point: plain throttling drops the last request, and the last
        request is the one carrying "the run has finished".
        """
        if self._notify is None:
            return
        if self._push_task is not None and not self._push_task.done():
            self._push_again = True
            return
        self._push_again = False
        self._push_task = asyncio.ensure_future(self._push_state())
        self._tasks.add(self._push_task)
        self._push_task.add_done_callback(self._tasks.discard)

    async def _push_state(self) -> None:
        """Push, then push once more for everything that asked while it ran.

        The loop can only exit with no request outstanding: nothing awaits
        between the check and the return, so no request can slip in behind
        it and be forgotten.

        A notifier that raises is caught here. It used to be caught by the
        tick loop, which awaited it directly; now that every push comes
        through this one task, a raise would otherwise be an unretrieved
        exception on the console while the screen quietly stopped updating
        — which is the failure this whole path exists to make visible. The
        same message is logged once, not once per push.
        """
        notify = self._notify
        if notify is None:
            return
        while True:
            try:
                await notify()
            except Exception as exc:      # noqa: BLE001 — a push must not take the run with it
                text = f"{type(exc).__name__}: {exc}"
                if text != self._push_error:
                    self._push_error = text
                    self.log(f"APP  state push failed — {text}", "emcy0")
            else:
                self._push_error = ""
            if not self._push_again:
                return
            self._push_again = False

    def log(self, msg: str, type_: str = "info") -> None:
        self.logs = self.logs[-40:] + [{"t": now_str(), "type": type_, "msg": msg}]
        if type_ == "emcy":
            self.emcy_new += 1

    # -- helpers ---------------------------------------------------------
    @property
    def sel_devices(self) -> list[dict]:
        return [d for d in self.devices if d["sel"]]

    def _sel_names(self) -> str:
        return ", ".join(f"node {d['node']:02d}" for d in self.sel_devices) or "no selection"

    def _adapter_info(self) -> dict:
        return next(a for a in self.adapter_cards if a["key"] == self.adapter)

    @property
    def eds_enabled(self) -> set[str]:
        return {e["file"] for e in self.db.eds_list() if e["enabled"]}

    @property
    def eds_codes(self) -> dict[str, str]:
        return {e["file"]: e["code"] for e in self.db.eds_list()}

    def _dut_code(self, eds: str) -> str:
        return self.eds_codes.get(eds) or eds[:3].upper()

    # ------------------------------------------------------------------
    # periodic tick — drives run/swdl/trace simulation
    async def tick_loop(self) -> None:
        self._capture_loop()
        # the cyclic transmit engine lives and dies with the tick loop —
        # cancelling the ticker (workspace switch) tears it down too
        cyclic = asyncio.create_task(self._cyclic_loop())
        try:
            await self._tick_loop_body()
        finally:
            cyclic.cancel()

    async def _tick_loop_body(self) -> None:
        """The loop everything on screen comes from — trace, bus load,
        heartbeats, every state push.

        So one bad tick must not end it. It used to have no guard at all:
        an exception anywhere inside, including out of the notifier that
        pushes state to the browsers, killed the loop for good. Nothing
        closed the WebSocket, so the screen simply stopped changing while
        looking connected, and a run that had long finished still read
        "Running…". A repeat of the same failure is logged once, so a
        persistent fault says so without filling the log every 0.8 s.
        """
        last_error = ""
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self._tick_once()
            except Exception as exc:
                text = f"{type(exc).__name__}: {exc}"
                if text != last_error:
                    last_error = text
                    self.log(f"APP  tick failed — {text}", "emcy0")
                continue
            last_error = ""

    async def _tick_once(self) -> None:
        dirty = False
        if self.connected and not self.trace_paused:
            frames = self.bus.poll_frames(4096)
            rows = [{"time": f.time or now_us_str(), "dir": f.direction,
                     "cob": f.cob_id, "len": f.length, "data": f.data,
                     "dec": f.decoded, "flag": f.flag,
                     "cls": trace_class(f.decoded),
                     "node": trace_node(f.cob_id),
                     "obj": "", "val": ""} for f in frames]
            for row in rows:
                self._annotate_sdo(row)
                self._annotate_pdo(row)
                self._annotate_emcy(row)
                # last: a matching vendor decoder overrides generic decode
                self._annotate_plugin(row)
                if row["cls"] == "HB" and row["node"] is not None:
                    self._hb_seen[row["node"]] = time.monotonic()
                key = (row["cls"], row["node"])
                self._trace_counts[key] = self._trace_counts.get(key, 0) + 1
            # also with zero rows: load, rate window and history must
            # decay on an idle bus instead of freezing at the last value
            self._update_bus_stats(rows)
            self._check_heartbeats()
            if rows:
                self.trace += rows
                cut = len(self.trace) - TRACE_CAP
                if cut > 0:
                    for old in self.trace[:cut]:
                        self._trace_counts[(old["cls"], old["node"])] -= 1
                    del self.trace[:cut]
            dirty = True  # stats/load move every tick while draining
        if self.running and self._run_mode == "sim":
            self._run_step()
            dirty = True
        if self.swdl_run:
            self._swdl.step(self)
            dirty = True
        if dirty:
            # through the same gate as every other push, so a tick and a
            # running case cannot end up writing to one socket at once
            self._changed()

    # -- test runner -------------------------------------------------------
    def _run_step(self) -> None:
        if self.run_idx >= len(self.run_order):
            self.running = False
            return
        tid = self.run_order[self.run_idx]
        fail = tid in data.FAILING_TESTS
        self.results[tid] = "FAIL" if fail else "PASS"
        self.log(f"TEST {tid} {'FAILED' if fail else 'passed'}", "emcy0" if fail else "test")
        if fail and self.stop_on_err:
            self.running = False
            self.log(f"RUN  aborted — stop on error (after {self.run_idx + 1} of {len(self.run_order)})")
            self._push_report(self.run_order[: self.run_idx + 1])
            return
        self.run_idx += 1
        if self.run_idx >= len(self.run_order):
            self.running = False
            self.log("RUN  finished — report created")
            self._push_report(self.run_order)

    def _push_report(self, order: list[str]) -> None:
        """End of a run: write the files, then show the run in the list.

        The list used to be the whole thing — a name that looked like a
        file and a score, with nothing on disk behind it. A run nobody can
        open a week later is not a record of anything.
        """
        passed = sum(1 for tid in order if self.results.get(tid) == "PASS")
        cases = [c for c in self._run_cases if c.id in order]
        run = reportlib.RunRecord(
            started=self._run_started or datetime.now().isoformat(timespec="seconds"),
            finished=datetime.now().isoformat(timespec="seconds"),
            user=_bench_user(), workspace=self.workspace_name,
            tool=f"canopen-bench {__version__}", cases=cases)
        name = self._write_report(run)
        # `file` is what the UI links to, and only a run that really wrote
        # one has it — the demo's example entries name files that were
        # never on any disk, and a link to a 404 is worse than plain text
        self.reports = [{"name": name, "file": name,
                         "score": f"{passed}/{len(order)}",
                         "ok": passed == len(order)}] + self.reports[:4]

    def _write_report(self, run: reportlib.RunRecord) -> str:
        """One file per case, one summary, one JSON beside it. Returns the
        summary's file name — that is what the UI links to."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = Path(self.paths.get("res") or (self.db.path.parent / "results"))
        try:
            folder.mkdir(parents=True, exist_ok=True)
            reportlib.write_stylesheet(folder)
            for case in run.cases:
                case.file = f"{stamp}__{case.id}__{_slug(case.name)}.html"
                (folder / case.file).write_text(reportlib.case_html(case), encoding="utf-8")
            summary = f"{stamp}__summary.html"
            (folder / summary).write_text(reportlib.summary_html(run), encoding="utf-8")
            (folder / f"{stamp}__summary.json").write_text(
                reportlib.summary_json(run), encoding="utf-8")
        except OSError as exc:
            # never let a full disk or a bad path swallow the run itself —
            # the verdicts are already in the log and on screen
            self.log(f"RUN  report not written — {exc}", "emcy0")
            return f"{stamp}__summary.html"
        self.log(f"RUN  report {summary} ({len(run.cases)} case(s)) in {folder}")
        return summary

    def _results_dir(self) -> Path:
        return Path(self.paths.get("res") or (self.db.path.parent / "results"))

    def act_report_overview(self, p: dict) -> None:
        """Fold the last so many days of runs into one page per hardware
        variant. Written on request rather than after every run: it reads
        the whole results folder, and most runs are one more data point in
        a picture nobody is looking at right now.
        """
        days = max(1, min(90, int(p.get("days") or 7)))
        folder = self._results_dir()
        try:
            runs = reportlib.load_runs(folder, days)
            variants = reportlib.collect_overview(runs)
            reportlib.write_stylesheet(folder)
            generated = datetime.now().isoformat(timespec="seconds")
            (folder / reportlib.OVERVIEW).write_text(
                reportlib.overview_html(variants, days, generated), encoding="utf-8")
        except OSError as exc:
            self.log(f"RUN  overview not written — {exc}", "emcy0")
            return
        self.overview = {"name": reportlib.OVERVIEW, "days": days, "runs": len(runs),
                         "variants": [{"key": v.key, "runs": v.runs,
                                       "passed": v.passed, "of": v.executions,
                                       "verdict": v.verdict} for v in variants]}
        what = (f"{len(runs)} run(s), {len(variants)} variant(s)" if runs
                else f"no runs in the last {days} day(s)")
        self.log(f"RUN  overview {reportlib.OVERVIEW} — {what} in {folder}")

    # ------------------------------------------------------------------
    # actions (dispatched from the API layer)
    def _label_step(self, key: str, val, regs: dict | None = None,
                    builtins: dict | None = None) -> str:
        """Progress text for one step — plugin step types label themselves.

        Object steps get the EDS name of what they touch appended. A line
        reading "write 0x1F51:0x02 = 2" is a line somebody has to go and
        look up before they can say whether the run did the right thing;
        the name is already in the registry, so it belongs in the report.

        With the registers to hand, a write whose value is a register or a
        builtin also shows what that came out as: "= R12 = 0x00007211".
        Otherwise the number that went on the wire appears nowhere on the
        step line, and a run a week old cannot say what it wrote.
        """
        ext = self._step_types.get(key)
        if ext:
            return ext.label(val)
        text = _step_text(key, val)
        if key == "sdo_write" and regs is not None and isinstance(val, dict):
            actual = _write_value(val, regs, builtins or {})
            # only when it says something the value in the line does not —
            # a literal is already there, in whatever width it was written
            if _as_int(actual) != _as_int(val["value"]):
                text += f" = {actual}"
        if key in ("sdo_read", "sdo_write", "adjust") and isinstance(val, dict):
            name = self._object_label(_hexstr(val["index"]), _hexstr(val["sub"]))
            if name:
                text += f"  ({name})"
        return text

    def _value_note(self, idx: str, sub: str, value: object) -> str:
        """A value as the report should show it: what came back, and what
        it means where the device's own headers say so."""
        fields = self._object_fields.get(f"{idx}:{sub}", [])
        number = _as_int(value)
        if fields and number is not None:
            meaning = describe(number, fields, self.symbols)
            if meaning:
                return f"{value} — {meaning}"
        # an object the EDS calls a string is one somebody wants to read,
        # not decode: 0x0000003332315F4F4D4544 is "DEMO_123" written back
        # to front, and nobody recognises their device name in that
        if self._is_text_object(idx, sub):
            text = _hex_to_text(value)
            if text is not None:
                return f'"{text}"'
        return str(value)

    def _is_text_object(self, idx: str, sub: str) -> bool:
        """Whether the EDS declares this object as one of the string types."""
        dev = self.sel_devices[0] if self.sel_devices else None
        od = self._ods.load(dev["eds"]) if dev else None
        if od is None:
            return False
        want_i, want_s = _as_int(idx), _as_int(sub)
        if want_i is None:
            return False
        var = find_var(od, want_i, want_s or 0)
        return getattr(var, "data_type", None) in _TEXT_TYPES

    def dispatch(self, action: str, p: dict[str, Any]) -> None:
        fn = self._plugin_actions.get(action)  # namespaced "<plugin>.<name>"
        if fn is None:
            fn = getattr(self, "act_" + action, None)
        if fn is None:
            raise ValueError(f"unknown action: {action}")
        fn(p)
        self._changed()

    # -- connection / devices --------------------------------------------
    def act_connect_toggle(self, p: dict) -> None:
        if self.connected:
            self._stop_cyclic()
            self.connected = False
            self.bus.disconnect()
            self.devices = []
            self.bus_load = 0.0
            self.log("BUS  disconnected")
            return
        self._capture_loop()
        try:
            self.bus.connect(self.adapter, int(self.bitrate))
        except Exception as exc:  # driver missing, adapter unplugged, channel busy
            self.log(f"BUS  connect failed — {exc}", "emcy0")
            return
        self.bus_load = 0.0
        self.err_frames = 0
        self._load_win.clear()
        self._cob_stats = {}
        self._rate_win.clear()
        self._load_hist.clear()
        self._stats_t0 = 0.0
        self.connected = True
        self._reset_hb_monitor()
        self.log(f"BUS  connected — {self._adapter_info()['full']} @ {self.bitrate} kbit/s")
        # machine-control startup validation: the tool starts offline, so
        # "validate on start" (A-05) fires on connect
        if self.mc["enabled"] and self.mc.get("scanStart"):
            self.act_mc_verify({})

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def act_scan(self, p: dict) -> None:
        if not self.connected or self.scan_busy:
            return
        self._spawn(self._scan_async())

    def _ensure_demo_eds_available(self) -> None:
        """Demo mode needs at least one enabled EDS with a real, loadable
        file to ever find a device. The one-shot first-run seed
        (Db.is_first_run, see _seed_demo_eds) only fires when the whole
        workspace directory didn't exist yet — it misses the case where
        that directory survives but its database was reset (e.g. the .db
        file was deleted by hand and the app restarted), leaving a demo
        scan permanently empty. Self-heal here instead, right before a
        scan actually needs the registry to have something in it.
        """
        if self.adapter != "demo":
            return
        if any(e["enabled"] and self._ods.load(e["file"]) is not None for e in self.db.eds_list()):
            return
        if any(e["file"] == "DemoDevice.eds" for e in self.db.eds_list()):
            return  # already registered (disabled) — an explicit choice, not ours to override
        self._seed_demo_eds()

    async def _scan_async(self) -> None:
        """The full scan flow (A-02); also the first step of scan & verify."""
        self.scan_busy = True
        self._ensure_demo_eds_available()
        node_from, node_to = self.scan_range
        self.log(f"SCAN node-id {node_from}…{node_to}")
        bus = self.bus
        eds_by_ident = self._eds_by_identity()
        for ident, files in self._eds_conflicts().items():
            winner = eds_by_ident[ident]["file"]
            self.log(f"EDS  identity conflict {ident}: {', '.join(files)} — newest file wins ({winner})")

        def probe() -> list[tuple]:
            """All bus traffic — probe, identity and variant reads — off the
            event loop: on real hardware this holds settle waits and SDO
            timeouts that would otherwise stall WebSocket, trace and UI."""
            results = []
            for f in bus.scan(node_from, node_to):
                entry = eds_by_ident.get(normalize_identity(f.identity))
                variant = self._read_variant(f.node, entry) if entry else ""
                results.append((f, entry, variant))
            return results

        try:
            if bus.simulated:
                await asyncio.sleep(SCAN_DELAY_S)
            results = await asyncio.to_thread(probe)
            self._apply_scan(bus, results)
        except Exception as exc:
            self.log(f"SCAN failed — {exc}", "emcy0")
        finally:
            self.scan_busy = False
            self._changed()

    def _apply_scan(self, bus: BusInterface, results: list[tuple]) -> None:
        prev = {d["node"]: d for d in self.devices}
        devices: list[dict] = []
        for f, entry, variant in results:
            devices.append({"node": f.node, "name": f.name, "nmt": f.nmt,
                            "sel": prev.get(f.node, {}).get("sel", False),
                            "cmds": prev.get(f.node, {}).get("cmds", {}),
                            "fw": f.fw, "sn": f.sn, "variant": variant,
                            "ident": f.identity,
                            "eds": entry["file"] if entry else "—"})
            if entry:
                self.log(f"SCAN identity 0x1018 node {f.node:02d} → {f.identity} ⇒ {entry['file']}", "sdo")
            else:
                self.log(f"SCAN identity 0x1018 node {f.node:02d} → {f.identity} — no active EDS match", "sdo")
        self.devices = devices
        # selection survives a scan by node-id, but the device sitting at
        # that node may be a different unit than before — so the object
        # table is rebuilt from whoever is there now, quietly: the scan
        # says enough already
        self._load_obj_vals(announce=False)
        if not results and self.adapter == "demo":
            self.log("SCAN demo mode found nothing — upload and enable at least one real EDS file first", "emcy0")
        elif not results and bus.bus_state() in ("passive", "error"):
            self.log("SCAN 0 found — bus errors detected, check bitrate/wiring", "emcy0")
        else:
            self.log(f"SCAN done — {len(results)} devices found, EDS auto-assigned")

    def _update_bus_stats(self, rows: list[dict]) -> None:
        """Rolling bus-load estimate from the traced frames (~47 framing bits
        plus payload per standard frame) over the last ~5 s, a cumulative
        error-frame counter, and the per-COB statistics behind the trace
        Stats view. Called every tick while draining — also with zero rows,
        so load and history decay on an idle bus. Values freeze while the
        trace is paused — no frames are drained then, so there is nothing
        to measure."""
        bits = 0
        tick_counts: dict[str, int] = {}
        for r in rows:
            try:
                dlc = int(r["len"])
            except (TypeError, ValueError):
                dlc = 8
            bits += 47 + 8 * dlc
            if r["flag"] == "red":
                self.err_frames += 1
            st = self._cob_stats.get(r["cob"])
            if st is None:
                st = self._cob_stats[r["cob"]] = {"n": 0, "dec": "", "cls": r["cls"]}
            st["n"] += 1
            st["dec"] = r["dec"]  # latest label (HB labels carry the NMT state)
            tick_counts[r["cob"]] = tick_counts.get(r["cob"], 0) + 1
        now = time.monotonic()
        if not self._stats_t0:
            self._stats_t0 = now
        self._load_win.append((now, bits))
        self._rate_win.append((now, tick_counts))
        while self._load_win and now - self._load_win[0][0] > 5.0:
            self._load_win.popleft()
        while self._rate_win and now - self._rate_win[0][0] > 5.0:
            self._rate_win.popleft()
        span = now - self._load_win[0][0] if len(self._load_win) > 1 else TICK_S
        try:
            bitrate = int(self.bitrate) * 1000
        except (TypeError, ValueError):
            bitrate = 500_000
        total = sum(b for _, b in self._load_win)
        self.bus_load = min(100.0, 100.0 * total / (max(span, TICK_S) * bitrate))
        self._load_hist.append(round(self.bus_load, 2))

    def _reset_hb_monitor(self) -> None:
        """(Re)anchor the heartbeat-loss grace window and drop stale
        alerts — called whenever the monitored device set or the
        connection changes (connect, adopt, MC activation), so a fresh
        start never inherits alerts from a different configuration."""
        self._hb_monitor_since = time.monotonic()
        self._hb_lost.clear()

    def _check_heartbeats(self) -> None:
        """Heartbeat-loss monitoring — deliberately Machine Control only:
        the generic Devices box never flags this on its own. Only nodes in
        the adopted expected state's assignments are watched, and only
        while MC is active; a device outside that responsibility going
        quiet is not this tool's business. A grace window after (re)start
        avoids flagging a node before its first heartbeat had a chance to
        arrive. Freezes while the trace is paused, like the other
        tick-driven bus health metrics (nothing is being drained then)."""
        if not (self.mc["enabled"] and self.mc_ref and self.connected):
            if self._hb_lost:
                self._hb_lost.clear()
            return
        monitored = {int(n) for n in (self.mc_ref.get("assignments") or {})}
        if not monitored:
            return
        timeout = max(0.5, self.mc.get("hbTimeoutMs", 3000) / 1000)
        now = time.monotonic()
        grace = now - self._hb_monitor_since < timeout
        stale = set()
        for node in monitored:
            last = self._hb_seen.get(node)
            if last is None:
                if not grace:
                    stale.add(node)
            elif now - last > timeout:
                stale.add(node)
        for node in sorted(stale - self._hb_lost):
            self.log(f"MC   node {node:02d} — heartbeat lost (no HB for >{timeout:g}s)", "emcy0")
        for node in sorted(self._hb_lost - stale):
            self.log(f"MC   node {node:02d} — heartbeat resumed")
        self._hb_lost = stale

    def _plot_sample(self, idx: int, sub: int, value) -> None:
        """Record a decoded numeric value for the signal plot, if that
        object is currently selected. Called from the SDO/PDO annotators
        that already decode a value — the plot needs no bus access of its
        own, it just taps values the trace already computes."""
        key = f"0x{idx:04X}:{sub:02X}"
        if key not in self._plot_keys:
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        self.plot_series.setdefault(key, deque(maxlen=PLOT_POINTS)).append((time.monotonic(), v))

    # -- trace interpretation ---------------------------------------------
    _EXPEDITED_LEN = {0x4F: 1, 0x4B: 2, 0x47: 3, 0x43: 4,   # upload response
                      0x2F: 1, 0x2B: 2, 0x27: 3, 0x23: 4}   # download request

    def _annotate_sdo(self, row: dict, live: bool = True) -> None:
        """Interpret SDO frames for the trace: `obj` = index:sub plus the
        object name from the EDS assigned to that node, `val` = expedited
        payload in decimal (or the abort code). `live=False` (imported/
        replayed rows) skips feeding the signal plot — those samples
        aren't "now", and would otherwise inject stale points into it."""
        try:
            cob = int(row["cob"], 16)
            data = bytes.fromhex(row["data"].replace(" ", ""))
        except ValueError:
            return
        if (cob & 0x780) not in (0x580, 0x600) or len(data) < 4:
            return
        cmd, idx, sub = data[0], data[1] | (data[2] << 8), data[3]
        obj = f"0x{idx:04X}:{sub:02X}"
        node = cob & 0x7F
        eds = next((d["eds"] for d in self.devices if d["node"] == node), "")
        if eds and eds != "—":
            od = self._ods.load(eds)
            var = find_var(od, idx, sub) if od else None
            if var is not None and var.name:
                obj += f" {var.name}"
        row["obj"] = obj
        if cmd == 0x80 and len(data) >= 8:
            row["val"] = f"abort 0x{int.from_bytes(data[4:8], 'little'):08X}"
        elif (n := self._EXPEDITED_LEN.get(cmd)) and len(data) >= 4 + n:
            value = int.from_bytes(data[4:4 + n], "little")
            row["val"] = str(value)
            if live:
                self._plot_sample(idx, sub, value)

    # predefined connection set: PDO function code -> mapping object
    _PDO_MAPPING_INDEX = {0x180: 0x1A00, 0x280: 0x1A01, 0x380: 0x1A02, 0x480: 0x1A03,
                          0x200: 0x1600, 0x300: 0x1601, 0x400: 0x1602, 0x500: 0x1603}
    _SIGNED_TYPES = {0x02, 0x03, 0x04, 0x15}  # INTEGER8/16/32/64 (CiA-301 data types)

    def _annotate_pdo(self, row: dict, live: bool = True) -> None:
        """Decode PDO payloads into named signals via the default mapping
        (0x1600-/0x1A00-series) in the EDS assigned to that node: `obj` =
        "name=value" pairs (single-signal PDOs use the SDO column style).
        Signals are unpacked LSB-first from the little-endian payload;
        INTEGER types are sign-extended. `live=False`: see `_annotate_sdo`.
        Assumes the predefined connection set (function code -> PDO
        number) and the EDS *default* mapping —
        a live remapped device may pack differently; reading the actual
        mapping from the device is commissioning territory."""
        if row["cls"] != "PDO" or row["node"] is None:
            return
        try:
            cob = int(row["cob"], 16)
            payload = bytes.fromhex(row["data"].replace(" ", ""))
        except ValueError:
            return
        mapping_index = self._PDO_MAPPING_INDEX.get(cob & 0x780)
        if mapping_index is None:
            return
        eds = next((d["eds"] for d in self.devices if d["node"] == row["node"]), "")
        if not eds or eds == "—":
            return
        od = self._ods.load(eds)
        if od is None:
            return
        entries = pdo_mapping(od, mapping_index)
        raw = int.from_bytes(payload, "little")
        pos = 0
        decoded: list[tuple[str, int, int, int]] = []  # (name, idx, sub, value)
        for idx, sub, bits in entries:
            if bits <= 0 or pos + bits > len(payload) * 8:
                break
            val = (raw >> pos) & ((1 << bits) - 1)
            pos += bits
            var = find_var(od, idx, sub)
            if var is not None and var.data_type in self._SIGNED_TYPES \
                    and val >= 1 << (bits - 1):
                val -= 1 << bits
            name = var.name if var is not None and var.name else f"0x{idx:04X}:{sub:02X}"
            decoded.append((name, idx, sub, val))
            if live:
                self._plot_sample(idx, sub, val)
        if not decoded:
            return
        if len(decoded) == 1:
            name, idx, sub, val = decoded[0]
            row["obj"] = f"0x{idx:04X}:{sub:02X} {name}"
            row["val"] = str(val)
        else:
            row["obj"] = " · ".join(f"{name}={val}" for name, _, _, val in decoded)

    def _emcy_text(self, code: int) -> str:
        """Resolve an EMCY error code against the merged code table:
        exact match, then the 0xXX00 class, then the 0xX000 group."""
        for key in (code, code & 0xFF00, code & 0xF000):
            if key in self._emcy_codes:
                return self._emcy_codes[key]
        return "Unknown error code"

    def _annotate_emcy(self, row: dict, live: bool = True) -> None:
        """Interpret EMCY frames for the trace: `obj` = error code with its
        CiA-301 (or plugin-supplied vendor) text, `val` = error-register
        bits — and mirror the event into the state log, which drives the
        EMCY badge. Frame layout (CiA 301): error code u16 LE, error
        register u8, 5 bytes manufacturer-specific. `live=False` (imported/
        replayed rows) skips the state-log mirror — a historical EMCY
        isn't a fresh alarm and shouldn't move the "since connect" badge."""
        if row["cls"] != "EMCY":
            return
        try:
            payload = bytes.fromhex(row["data"].replace(" ", ""))
        except ValueError:
            return
        if len(payload) < 3:
            return
        code = payload[0] | (payload[1] << 8)
        text = self._emcy_text(code)
        row["obj"] = f"0x{code:04X} {text}"
        reg = f"reg 0x{payload[2]:02X}"
        bits = [name for i, name in enumerate(data.ERROR_REGISTER_BITS)
                if payload[2] & (1 << i)]
        if bits:
            reg += " " + "+".join(bits)
        row["val"] = reg
        if not live:
            return
        self.emcy_seen.append((row["node"], code, payload[2], payload[3:8]))
        node = f"node {row['node']:02d}" if row["node"] else "node ?"
        # an error reset clears, it doesn't alarm — log it without the badge
        self.log(f"EMCY {node}  0x{code:04X}  {text}", "emcy" if code else "info")

    def _annotate_plugin(self, row: dict) -> None:
        """Let plugin trace decoders interpret vendor-specific frames the
        generic CANopen decoding leaves blank (e.g. teach telegrams)."""
        if not self._trace_decoders:
            return
        try:
            cob = int(row["cob"], 16)
            data = bytes.fromhex(row["data"].replace(" ", ""))
        except ValueError:
            return
        for decoder in self._trace_decoders:
            try:
                res = decoder.decode(cob, data)
            except Exception:  # a broken decoder must not stall the trace
                continue
            if res:
                row.update({k: v for k, v in res.items()
                            if k in ("dec", "obj", "val")})
                return

    def _read_variant(self, node: int, eds_entry: dict) -> str:
        """Auto-detect the device variant from the object configured on its
        EDS entry, instead of a hardcoded global toggle — every manufacturer
        places this somewhere different, so it's per-EDS config, not a fixed
        app-wide concept.
        """
        index, sub = eds_entry["variant_index"], eds_entry["variant_sub"]
        if not index:
            return ""
        res = self.bus.sdo_read(node, index, sub or "00")
        if not res.ok:
            return ""
        return eds_entry["variant_map"].get(res.value, res.value)

    def _load_obj_vals(self, announce: bool = True) -> None:
        """Fill the object table from the device it belongs to — the first
        selected one — and from nothing else.

        Replaces the cache rather than merging into it. Merging left the
        previous device's values standing for every object the new one has
        no stored value for, so the table showed numbers under a device
        they were never read from, and Write would have sent them there.

        Values are kept per serial number (db.last_values), which is the
        only identity that survives re-addressing. A device that answers no
        serial number reports "?" — every such device would share one set
        of values, which is the very thing this method exists to prevent,
        so those get an empty table and a line saying why.
        """
        sel = self.sel_devices
        first = sel[0] if sel else None
        self.obj_vals = ({} if first is None or first["sn"] == NO_SERIAL
                         else dict(self.db.last_values(first["sn"])))
        if not announce:
            return
        if first is None:
            self.log("DB   no device selected — object values cleared")
        elif first["sn"] == NO_SERIAL:
            self.log(f"DB   node {first['node']:02d} · no serial number — "
                     "values are not remembered for this device", "emcy0")
        else:
            self.log(f"DB   node {first['node']:02d} · SN {first['sn']} "
                     "→ last known values restored")

    def act_dev_toggle(self, p: dict) -> None:
        node = int(p["node"])
        for d in self.devices:
            if d["node"] == node:
                d["sel"] = not d["sel"]
        self._load_obj_vals()

    def act_nmt(self, p: dict) -> None:
        cmd = p["cmd"]
        if not self.sel_devices or not self.connected:
            return
        state = NMT_STATE[cmd]
        for d in self.devices:
            if d["sel"]:
                d["nmt"] = state
                self.bus.nmt(cmd, d["node"])
        self.log(f"NMT  {NMT_LABEL[cmd]} → {self._sel_names()}", "nmt")

    def _device_commands(self, eds_file: str) -> list[dict]:
        """Device commands declared by the EDS registry entry this device is
        assigned to — special functions like a vendor's SuperUser mode are
        per-EDS data (db.eds_set_commands), not an app-wide concept."""
        entry = next((e for e in self.db.eds_list() if e["file"] == eds_file), None)
        return entry["device_commands"] if entry else []

    def _apply_dev_cmd(self, dev: dict, cmd: dict) -> bool | None:
        """Toggle one device command on one device; SDO-write the on/off
        value when the command declares one. Returns the new state, or
        None when the device refused the write (state unchanged)."""
        on = not dev["cmds"].get(cmd["key"], False)
        write = cmd.get("write")
        if write and self.connected:
            value = write["on"] if on else write["off"]
            res = self.bus.sdo_write(dev["node"], str(write["index"]),
                                     str(write.get("sub", "00")), str(value))
            if not res.ok:
                self.log(f'CMD  {cmd["label"]} node {dev["node"]:02d} — '
                         f'write {write["index"]} abort {res.abort}', "emcy0")
                return None
        dev["cmds"][cmd["key"]] = on
        return on

    def act_dev_cmd(self, p: dict) -> None:
        """Toggle a per-EDS device command ({key}), on one device ({node})
        or on the whole selection."""
        key = p["key"]
        if "node" in p:
            targets = [d for d in self.devices if d["node"] == int(p["node"])]
        else:
            targets = self.sel_devices
        hit = []
        for d in targets:
            cmd = next((c for c in self._device_commands(d["eds"])
                        if c["key"] == key), None)
            if cmd is None:  # this device's profile doesn't offer the command
                continue
            on = self._apply_dev_cmd(d, cmd)
            if on is None:  # refused — the abort is already logged
                continue
            hit.append((d, cmd, on))
        if hit:
            label = hit[0][1]["label"]
            state = "on" if hit[0][2] else "off"
            nodes = ", ".join(f"node {d['node']:02d}" for d, _, _ in hit)
            self.log(f"CMD  {label} {state} → {nodes}")

    def act_dev_menu(self, p: dict) -> None:
        node = int(p["node"])
        what = p["what"]
        dev = next((d for d in self.devices if d["node"] == node), None)
        if dev is None:
            return
        if what in ("restart", "op", "preop", "resetcomm"):
            # actually send the NMT command — this used to only flip the
            # displayed state and log, without ever touching the bus
            if not self.connected:
                return
            cmd = {"restart": "reset", "op": "start", "preop": "preop",
                   "resetcomm": "resetcomm"}[what]
            self.bus.nmt(cmd, node)
            state = NMT_STATE.get(cmd)
            if state:
                dev["nmt"] = state
            self.log(f"NMT  {NMT_LABEL.get(cmd, 'reset comm')} → node {node:02d}", "nmt")
        elif what == "eds_next":
            enabled = sorted(self.eds_enabled)
            if enabled:
                try:
                    nxt = enabled[(enabled.index(dev["eds"]) + 1) % len(enabled)]
                except ValueError:
                    nxt = enabled[0]
                dev["eds"] = nxt
                self.log(f"EDS  node {node} manually assigned → {nxt}")

    def act_mirror_refresh(self, p: dict) -> None:
        sel = self.sel_devices
        slots = self._mirror_slots(sel[0]["eds"]) if sel else []
        if slots:
            node = sel[0]["node"]
            for slot in slots:
                res = self.bus.sdo_read(node, slot["idx"], slot["sub"])
                if res.ok:
                    self.obj_vals[f"{slot['idx']}:{slot['sub']}"] = res.value
        self.log("LCD  refresh display mirror")

    # -- setup: interface ---------------------------------------------------
    def act_set_adapter(self, p: dict) -> None:
        if p["adapter"] == self.adapter:
            return
        if self.connected:
            # the bus property routes by adapter key, so disconnect the old
            # backend before the key flips underneath it
            self.bus.disconnect()
            self.connected = False
            self.devices = []
            self.log("BUS  disconnected — adapter changed")
        self.adapter = p["adapter"]
        self.db.set("adapter", self.adapter)

    def act_set_bitrate(self, p: dict) -> None:
        self.bitrate = p["bitrate"]
        self.db.set("bitrate", self.bitrate)
        if self.connected:
            # applied immediately — reconnect the running interface
            self._capture_loop()
            try:
                self.bus.connect(self.adapter, int(self.bitrate))
                self.log(f"BUS  bitrate applied — reconnected @ {self.bitrate} kbit/s")
            except Exception as exc:
                self.connected = False
                self.devices = []
                self.log(f"BUS  reconnect failed — {exc}", "emcy0")

    def act_set_own_node_id(self, p: dict) -> None:
        try:
            node_id = int(str(p["node_id"]), 0)  # hex accepted, like the RAW rows
        except (KeyError, ValueError):
            self.log(f'CFG  own node-ID unchanged — "{p.get("node_id")}" is not a number', "emcy0")
            return
        if not 1 <= node_id <= 127:
            self.log(f"CFG  own node-ID unchanged — {node_id} outside 1..127", "emcy0")
            return
        self.own_node_id = node_id
        self.db.set("own_node_id", node_id)
        self.log(f"CFG  tool's own node-ID set to {node_id:02d}")

    def act_set_scan_range(self, p: dict) -> None:
        try:
            lo, hi = int(str(p["from"]), 0), int(str(p["to"]), 0)
        except (KeyError, TypeError, ValueError):
            return
        lo, hi = max(1, min(127, lo)), max(1, min(127, hi))
        if lo > hi:
            lo, hi = hi, lo
        self.scan_range = (lo, hi)
        self.db.set("scan_range", [lo, hi])
        self.log(f"CFG  scan range set to node-id {lo}…{hi}")

    def act_set_path(self, p: dict) -> None:
        which, value = p["which"], p["value"]
        if which == "eds":
            self._set_eds_dir(str(value).strip())
            return
        if which in self.paths:
            self.paths[which] = value
            self.db.set("paths", self.paths)
            if which == "tc":
                self._load_testcases()

    def _set_eds_dir(self, value: str) -> None:
        """Move the EDS folder (empty = back to the workspace default) and
        refresh everything that cached the old location."""
        self.db.set_eds_dir(value)
        self._ods.retarget(self.db.eds_dir)
        self._demo_bus.retarget_eds(self.db.eds_dir)
        self._catalog_cache.clear()
        self._rematch_devices()
        self.log(f"CFG  EDS folder → {self.db.eds_dir}" + ("" if value else " (workspace default)"))

    def _load_testcases(self, log: bool = True) -> None:
        catalog = tclib.load_catalog(self.paths.get("tc", ""),
                                     extensions=self._step_types,
                                     symbols=self.symbols)
        self.testcases = {tc.id: tc for tc in catalog}
        clashes = _duplicate_ids(catalog)
        for tid, files in clashes.items():
            kept = self.testcases.get(tid)
            if kept is None:
                continue
            lost = [f for f in files if f != Path(kept.file).name]
            note = (f"duplicate id {tid} — also claimed by {', '.join(lost)}; "
                    "only this file is loaded")
            kept.error = f"{kept.error}; {note}" if kept.error else note
        if log:
            broken = sum(1 for tc in catalog if tc.error)
            msg = f"CFG  testcases folder scanned — {len(catalog)} test cases discovered"
            if broken:
                msg += f", {broken} with schema errors"
            self.log(msg, "emcy0" if broken else "info")
            for tid, files in clashes.items():
                self.log(f"CFG  test case id {tid} is claimed by {len(files)} files "
                         f"({', '.join(files)}) — only one of them can be run", "emcy0")

    def act_tc_rescan(self, p: dict) -> None:
        self._load_testcases()

    def act_tc_open(self, p: dict) -> None:
        """Open a test case in whatever the machine opens .yaml with.

        The bench does not edit test cases — they are files, and people
        already have an editor they like. This only hands one to the
        system, which is also why it is the *server's* system: the browser
        cannot, and this tool runs on the bench it is looking at.

        Only files inside the configured TestCases folder are opened, and
        only ones the catalog knows. A path arriving from the outside is
        not a path this resolves.
        """
        tc = self.testcases.get(str(p.get("id") or ""))
        if tc is None or not tc.file:
            self.log("CFG  open — no such test case", "emcy0")
            return
        folder = Path(self.paths.get("tc") or "").resolve()
        try:
            target = (folder / tc.file).resolve()
            target.relative_to(folder)          # no escaping the folder
            if not target.is_file():
                raise FileNotFoundError(target)
        except (OSError, ValueError) as exc:
            self.log(f"CFG  open {tc.file} — {exc}", "emcy0")
            return
        try:
            _open_in_editor(target)
        except OSError as exc:
            self.log(f"CFG  open {tc.file} — {exc}", "emcy0")
            return
        self.log(f"CFG  opening {tc.file} in the system editor")

    # -- workspaces --------------------------------------------------------
    _WS_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,49}$")
    _RESERVED_WORKSPACE_NAMES = {"plugins"}  # plugin_dir lives beside the workspaces

    @property
    def workspace_name(self) -> str:
        return self.db.path.parent.name if self.workspaces_root else self.db.path.stem

    def _workspace_names(self) -> list[str]:
        if not self.workspaces_root or not self.workspaces_root.is_dir():
            return []
        return sorted((d.name for d in self.workspaces_root.iterdir()
                       if d.is_dir() and not d.name.startswith(".")
                       and d.name not in self._RESERVED_WORKSPACE_NAMES), key=str.lower)

    def act_workspace_create(self, p: dict) -> None:
        if not (self.workspaces_root and self.on_workspace_switch):
            self.log("WS   workspace switching disabled (started with an explicit --db)", "emcy0")
            return
        name = str(p.get("name", "")).strip()
        if not self._WS_NAME.match(name) or name.lower() in self._RESERVED_WORKSPACE_NAMES:
            self.log(f'WS   invalid workspace name "{name}" — letters, digits, space, ._- only', "emcy0")
            return
        path = self.workspaces_root / name
        if not path.exists():
            path.mkdir(parents=True)
            self.log(f'WS   workspace "{name}" created — switching')
        self.on_workspace_switch(name)

    def act_workspace_switch(self, p: dict) -> None:
        if not (self.workspaces_root and self.on_workspace_switch):
            self.log("WS   workspace switching disabled (started with an explicit --db)", "emcy0")
            return
        name = Path(str(p.get("name", ""))).name
        if name == self.workspace_name:
            return
        if name not in self._workspace_names():
            self.log(f'WS   unknown workspace "{name}"', "emcy0")
            return
        self.log(f'WS   switching to workspace "{name}"')
        self.on_workspace_switch(name)

    # -- GUI plugin install (Setup > Extensions) -----------------------------
    # importlib.metadata's standard finder only recognizes a `*.dist-info`
    # directory as a *direct child* of a scanned sys.path entry — exactly
    # how site-packages itself is laid out (package dir and its dist-info
    # side by side). A wheel must therefore be extracted flat into
    # plugin_dir, not into its own per-package subfolder (verified
    # empirically — the nested layout silently finds nothing, no error).
    # That flat layout mixes files from every installed plugin together,
    # so a small self-maintained manifest (not RECORD, which lives inside
    # the now-scattered dist-info and is awkward to re-locate later) is
    # what makes a clean removal possible: distribution name -> {version,
    # the top-level paths that came from its wheel}.
    def _plugin_manifest_path(self) -> Path:
        return self.plugin_dir / ".manifest.json"

    def _plugin_manifest(self) -> dict:
        try:
            return json.loads(self._plugin_manifest_path().read_text())
        except (OSError, ValueError):
            return {}

    def _save_plugin_manifest(self, manifest: dict) -> None:
        self._plugin_manifest_path().write_text(json.dumps(manifest))

    def _installed_plugin_packages(self) -> list[dict]:
        if self.plugin_dir is None:
            return []
        manifest = self._plugin_manifest()
        return sorted(({"name": name, "version": info["version"]}
                       for name, info in manifest.items()), key=lambda r: r["name"])

    def _remove_plugin_files(self, dist_name: str) -> None:
        manifest = self._plugin_manifest()
        info = manifest.pop(dist_name, None)
        if info is None:
            return
        for rel in info["paths"]:
            target = self.plugin_dir / rel
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
        self._save_plugin_manifest(manifest)

    def _install_plugin_wheel(self, filename: str, content: bytes) -> tuple[bool, str]:
        """Extract an uploaded wheel flat into plugin_dir (see class of
        comments above) so importlib.metadata's standard finder discovers
        its entry points without a real `pip install`. Pure-Python wheels
        need no build step; that's the whole point of the wheel format.
        Returns (ok, "name-version" or an error text)."""
        if self.plugin_dir is None:
            return False, "plugin install needs multi-workspace mode (a data root)"
        safe_name = Path(filename).name
        if not safe_name.lower().endswith(".whl"):
            return False, f"not a .whl file: {filename!r}"
        parts = safe_name[:-4].split("-")  # PEP 427: {dist}-{version}-...
        if len(parts) < 2:
            return False, f"not a valid wheel filename: {safe_name!r}"
        dist_name, version = parts[0], parts[1]
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile:
            return False, "not a valid zip/wheel archive"
        root = self.plugin_dir.resolve()
        top_level: set[str] = set()
        for name in zf.namelist():
            # zip-slip guard: no archive member may resolve outside
            # plugin_dir, regardless of what the standard library's own
            # extraction already blocks — this is untrusted, uploaded input
            if not (root / name).resolve().is_relative_to(root):
                return False, f"refusing to extract unsafe path in archive: {name!r}"
            top_level.add(name.split("/", 1)[0])
        self._remove_plugin_files(dist_name)  # re-upload = clean reinstall/upgrade
        zf.extractall(self.plugin_dir)
        manifest = self._plugin_manifest()
        manifest[dist_name] = {"version": version, "paths": sorted(top_level)}
        self._save_plugin_manifest(manifest)
        importlib.invalidate_caches()
        return True, f"{dist_name}-{version}"

    def act_plugin_install(self, p: dict) -> None:
        try:
            content = base64.b64decode(p["content"])
        except Exception:
            self.log("PLUGIN install rejected — invalid file content", "emcy0")
            return
        ok, msg = self._install_plugin_wheel(p.get("filename", ""), content)
        if not ok:
            self.log(f"PLUGIN install rejected — {msg}", "emcy0")
            return
        self.log(f'PLUGIN "{msg}" installed — reloading to activate it')
        if self.on_plugin_reload:
            self.on_plugin_reload()
        else:
            self.log("PLUGIN installed but can't activate without a restart "
                     "(no reload hook — embedded/test setup)", "emcy0")

    def act_plugin_remove(self, p: dict) -> None:
        if self.plugin_dir is None:
            return
        dist_name = str(p.get("pkg", "")).strip()
        if dist_name not in self._plugin_manifest():
            self.log(f'PLUGIN remove rejected — unknown package "{dist_name}"', "emcy0")
            return
        self._remove_plugin_files(dist_name)
        importlib.invalidate_caches()
        self.log(f'PLUGIN "{dist_name}" removed — reloading')
        if self.on_plugin_reload:
            self.on_plugin_reload()

    # -- directory picker (Browse… buttons) --------------------------------
    # The browser cannot open a native dialog for *server-side* paths, so the
    # picker is a modal fed from here: one listing per navigation step.
    def _browse_listing(self, path: Path) -> dict:
        try:
            dirs = sorted((d.name for d in path.iterdir() if d.is_dir()), key=str.lower)
            error = ""
        except OSError as exc:
            dirs, error = [], str(exc)
        return {"path": str(path), "dirs": dirs, "error": error,
                "hasParent": path.parent != path}

    def act_browse_open(self, p: dict) -> None:
        which = p.get("which")
        if which != "eds" and which not in self.paths:
            return
        configured = str(self.db.eds_dir) if which == "eds" else self.paths[which]
        start = Path(configured or "~").expanduser()
        while not start.is_dir() and start.parent != start:
            start = start.parent  # configured path may not exist on this machine
        if not start.is_dir():
            start = Path.home()
        self.browse = {"which": which} | self._browse_listing(start.resolve())

    def act_browse_nav(self, p: dict) -> None:
        if not self.browse:
            return
        base = Path(self.browse["path"])
        name = str(p.get("dir", ""))
        target = base.parent if name == ".." else base / Path(name).name
        self.browse = {"which": self.browse["which"]} | self._browse_listing(target.resolve())

    def act_browse_select(self, p: dict) -> None:
        if not self.browse:
            return
        which, path = self.browse["which"], self.browse["path"]
        self.browse = None
        self.act_set_path({"which": which, "value": path})

    def act_browse_close(self, p: dict) -> None:
        self.browse = None

    # -- setup: EDS -----------------------------------------------------------
    def _seed_demo_eds(self) -> None:
        """First-ever run: install the bundled DemoDevice.eds like a manual upload, so Demo mode works out of the box."""
        src = SEED_EDS
        try:
            content = src.read_text(encoding="utf-8")
        except OSError as e:
            # Never silent: without this file a demo scan finds nothing, and
            # an empty device list with no explanation is the worst way to
            # learn that. It went missing once already — see SEED_EDS.
            self.log(f'EDS  demo seed unavailable ({src.name}) — {e}', "emcy0")
            return
        ok, msg = self.add_eds_file(src.name, content)
        if not ok:
            self.log(f'EDS  demo seed "{src.name}" rejected — {msg}', "emcy0")
            return
        # the sidebar display mirror is specific to this device profile —
        # velocity + board temperature are the only two live gauges
        # DemoDevice.eds actually has (see canopen_bench/seed/DemoDevice.eds)
        self.db.eds_set_display(src.name, [
            {"label": "m/min", "idx": "0x606C", "sub": "00"},
            {"label": "°C", "idx": "0x2002", "sub": "00"},
        ])
        # a neutral example device command, so the OSS demo shows the
        # mechanism vendor profiles use for special functions (SuperUser & co)
        self.db.eds_set_commands(src.name, [
            {"key": "svc", "label": "Service mode", "badge": "SVC"},
        ])

    def add_eds_file(self, filename: str, content: str) -> tuple[bool, str]:
        """Parse and register a real EDS file, stored as a plain file under
        db.eds_dir (not a DB blob) so it stays individually browsable and
        copyable outside the app — the sqlite row only holds metadata keyed
        by this filename.

        Kept separate from dispatch() (called directly from the 'eds_upload'
        action, see act_eds_upload) since it returns a message the caller
        needs synchronously, unlike the fire-and-forget act_* actions.
        """
        safe_name = Path(filename).name
        if not safe_name or safe_name in (".", ".."):
            return False, f"invalid filename: {filename!r}"

        try:
            od = import_eds(io.StringIO(content), None)
        except Exception as exc:  # malformed EDS - report, don't crash the bench
            return False, f"could not parse EDS: {exc}"

        dev_name = od.device_information.product_name or safe_name
        vendor = od.device_information.vendor_number
        product = od.device_information.product_number
        if vendor is None or product is None:
            return False, "EDS has no VendorNumber/ProductNumber in [DeviceInfo] — can't match devices on scan"
        ident = f"0x{vendor:X}·0x{product:X}"

        self.db.eds_write_file(safe_name, content)
        self.db.eds_add(safe_name, dev_name, ident, code=safe_name[:3].upper())
        self.log(f'EDS  "{safe_name}" added — {dev_name}, identity {ident}')
        self._rematch_devices()
        return True, "ok"

    def act_eds_upload(self, p: dict) -> None:
        ok, msg = self.add_eds_file(p["filename"], p["content"])
        if not ok:
            self.log(f'EDS  "{p["filename"]}" rejected — {msg}', "emcy0")

    def act_eds_remove(self, p: dict) -> None:
        f = p["file"]
        self.db.eds_remove(f)
        self.log(f'EDS  "{f}" removed from registry')

    def act_eds_toggle(self, p: dict) -> None:
        f = p["file"]
        enable = f not in self.eds_enabled
        self.db.eds_set_enabled(f, enable)
        if enable:
            self._rematch_devices()

    def _eds_by_identity(self) -> dict[str, dict]:
        """Enabled EDS entries keyed by normalized identity. When several
        enabled files claim the same identity, the newest file on disk wins —
        the EDS list marks all of them with a conflict warning."""
        def mtime(e: dict) -> float:
            try:
                return (self.db.eds_dir / e["file"]).stat().st_mtime
            except OSError:
                return 0.0
        out: dict[str, dict] = {}
        for e in sorted((e for e in self.db.eds_list() if e["enabled"]), key=mtime):
            out[normalize_identity(e["ident"])] = e
        return out

    def _eds_conflicts(self) -> dict[str, list[str]]:
        """Normalized identity → file names, for identities that more than
        one enabled EDS file claims."""
        groups: dict[str, list[str]] = {}
        for e in self.db.eds_list():
            if e["enabled"]:
                groups.setdefault(normalize_identity(e["ident"]), []).append(e["file"])
        return {ident: files for ident, files in groups.items() if len(files) > 1}

    def _rematch_devices(self) -> None:
        """A newly uploaded or enabled EDS may match devices that are already
        on the list — assign it right away instead of demanding a re-scan."""
        eds_by_ident = self._eds_by_identity()
        for d in self.devices:
            if d["eds"] in ("", "—") and d.get("ident"):
                entry = eds_by_ident.get(normalize_identity(d["ident"]))
                if entry:
                    d["eds"] = entry["file"]
                    self.log(f"EDS  node {d['node']:02d} identity {d['ident']} ⇒ {entry['file']}")

    def act_eds_code(self, p: dict) -> None:
        self.db.eds_set_code(p["file"], p["code"])

    def act_eds_variant(self, p: dict) -> None:
        value_map = p.get("map") or {}
        self.db.eds_set_variant(p["file"], p.get("index", ""), p.get("sub", ""), value_map)

    # -- machine control ---------------------------------------------------------
    # There is no separate "setup" entity: the workspace IS the configuration.
    # Everything else (adapter, bitrate, EDS registry, paths) persists on
    # change; only machine control's expected state is adopted deliberately —
    # auto-updating it on every scan would make verification meaningless.
    def _adopt_ref(self) -> None:
        """Machine control expects what the workspace's stored reference
        recorded (F-5)."""
        ref = self.mc_ref or {}
        if "expected" in ref:
            self.mc["expected"] = int(ref["expected"])
        if ref.get("session"):
            self.mc["session"] = ref["session"]

    def act_mc_adopt(self, p: dict) -> None:
        """Store the current bus state as the expected state — device count,
        session-ID and the node→EDS assignments."""
        if not self.devices:
            self.log("MC   nothing to adopt — connect and scan first", "emcy0")
            return
        self.mc_ref = {
            "expected": len(self.devices),
            "session": self.mc["session"],
            "assignments": {str(d["node"]): d["eds"] for d in self.devices},
            "adopted": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        self.db.set("mc_ref", self.mc_ref)
        self.mc["expected"] = len(self.devices)
        self._reset_hb_monitor()  # newly adopted device set — drop stale alerts
        assigns = ", ".join(f"{d['node']:02d}→{d['eds'].removesuffix('.eds')}" for d in self.devices)
        self.log(f'MC   expected state adopted — {len(self.devices)} device(s), '
                 f'session-ID {self.mc["session"]}, EDS assignments: {assigns}')
    def _save_mc_opts(self) -> None:
        self.db.set("mc_opts", {k: self.mc[k]
                                for k in ("enabled", "autoStart", "autoReaddr", "scanStart",
                                          "teachFlow", "hbTimeoutMs")})

    def act_mc_toggle(self, p: dict) -> None:
        self.mc["enabled"] = not self.mc["enabled"]
        self._save_mc_opts()
        if self.mc["enabled"]:
            self._reset_hb_monitor()
            hint = "" if self.mc_ref else " — no expected state yet, adopt the current state first"
            self.log("MC   machine control activated" + hint)
        else:
            self.log("MC   machine control deactivated")

    def act_mc_opt(self, p: dict) -> None:
        key = p["key"]
        if key in ("autoStart", "autoReaddr", "scanStart"):
            self.mc[key] = not self.mc[key]
            self._save_mc_opts()

    def act_mc_set_hb_timeout(self, p: dict) -> None:
        try:
            ms = int(str(p["ms"]).strip())
        except (KeyError, ValueError):
            return
        self.mc["hbTimeoutMs"] = min(600_000, max(500, ms))
        self._save_mc_opts()

    def act_mc_verify(self, p: dict) -> None:
        if self.mc["busy"] or self.scan_busy:
            return
        if not self.connected:
            self.log("MC   scan & verify skipped — interface not connected", "emcy0")
            return
        if not self.mc_ref:
            self.log("MC   scan & verify skipped — no expected state adopted", "emcy0")
            return
        self.mc["busy"] = True
        self.log("MC   scan & verify against the expected state")
        self._spawn(self._mc_verify_task(self.mc_ref))

    async def _mc_verify_task(self, ref: dict, allow_teach: bool = True) -> None:
        try:
            await self._scan_async()  # F-4: a real scan, not the stale device list
            expected = int(ref.get("expected", self.mc["expected"]))
            assignments = ref.get("assignments")
            mismatches: list[str] = []
            if assignments is not None:
                mismatches = [
                    f"node {d['node']:02d} {assignments.get(str(d['node'])) or '—'} ≠ {d['eds']}"
                    for d in self.devices if assignments.get(str(d["node"])) != d["eds"]]
            found = len(self.devices)
            ok = found == expected and not mismatches
            self.mc.update(found=found, expected=expected,
                           result="ok" if ok else "mismatch", last=now_str()[:8])
            if ok:
                self.log(f'MC   {found}/{expected} devices · session-ID {self.mc["session"]} ✓ — expected state valid')
            else:
                detail = f" · {mismatches[0]}" if mismatches and found == expected else ""
                self.log(f'MC   {found}/{expected} devices — mismatch{detail}', "emcy0")
                # auto-addressing only while machine control is active (A-05);
                # never from a teach's own final verify (no loop)
                if allow_teach and self.mc["enabled"] and self.mc["autoReaddr"] \
                        and self.teach is None:
                    self.log("MC   validation failed — starting button-teach addressing")
                    self._start_teach(auto=True)
        finally:
            self.mc["busy"] = False
            self._changed()

    # -- button-teach addressing (A-05) -------------------------------------
    def _seed_default_flows(self) -> None:
        """Workspace flows dir with the shipped default procedures — from the
        core package and from every plugin's flow_dirs(); existing (possibly
        locally customized) files are never overwritten."""
        self.flows_dir.mkdir(parents=True, exist_ok=True)
        sources = [Path(__file__).parent / "flows"]
        sources += [d for p in self.plugins for d in p.flow_dirs()]
        for packaged in sources:
            if not packaged.is_dir():
                continue
            for src in sorted(packaged.glob("*.yaml")):
                dst = self.flows_dir / src.name
                if not dst.exists():
                    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    def _load_flow(self, name: str) -> tclib.TestCase | None:
        path = self.flows_dir / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            self.log(f'MC   flow "{name}" not found in {self.flows_dir}', "emcy0")
            return None
        flow = tclib.parse_testcase(text, name, require_prefix=False,
                                    extensions=self._step_types,
                                    symbols=self.symbols)
        if flow.error:
            self.log(f'MC   flow "{name}" invalid — {flow.error}', "emcy0")
            return None
        return flow

    def _flow_files(self) -> list[str]:
        try:
            return sorted(p.name for p in self.flows_dir.glob("*.yaml"))
        except OSError:
            return []

    def act_mc_flow(self, p: dict) -> None:
        """Select which addressing procedure (flow file) machine control runs."""
        name = p.get("file", "")
        if name in self._flow_files():
            self.mc["teachFlow"] = name
            self._save_mc_opts()
            self.log(f'MC   addressing procedure set to "{name}"')

    def act_mc_readdress(self, p: dict) -> None:
        """Operator-requested re-addressing — runs the selected teach flow."""
        self._start_teach()

    def _start_teach(self, auto: bool = False) -> None:
        """Teach in two modes. Operator-initiated (auto=False): open-ended
        across the address range — the flow's device bound is the range's
        upper node-id, the procedure ends via Addr-End/assignment running
        dry, and the freshly addressed bus is adopted as the new expected
        state. That is the escape hatch when the machine grew or shrank:
        bounded by a stale expected count, teach could never address more
        devices, and adopt needs the devices addressed first.
        Auto re-address after a failed verification (auto=True) stays
        bounded by the adopted count and verifies against it — it restores
        the expected state and must never silently adopt a shrunken bus,
        else a dead device would vanish from verification."""
        if self.teach is not None or self.scan_busy:
            return
        if not self.connected:
            self.log("MC   teach skipped — interface not connected", "emcy0")
            return
        if auto:
            expected = int(self.mc.get("expected") or 0)
            if expected < 1:
                self.log("MC   teach skipped — expected device count unknown "
                         "(adopt the current state first)", "emcy0")
                return
        else:
            expected = int(self.scan_range[1])
        flow = self._load_flow(self.mc.get("teachFlow") or "teach_addressing.yaml")
        if flow is None:
            return
        self._teach_abort = False
        self.teach = {"step": 0, "of": len(flow.steps), "text": "starting…"}
        self._spawn(self._teach_task(flow, expected, adopt_after=not auto))

    def act_mc_teach_abort(self, p: dict) -> None:
        if self.teach is not None:
            self._teach_abort = True

    def act_demo_press(self, p: dict) -> None:
        """Demo mode only: simulate the operator pressing a device button."""
        if self.adapter == "demo":
            self._demo_bus.press_button()

    async def _teach_task(self, flow: tclib.TestCase, expected: int,
                          adopt_after: bool = False) -> None:
        # session identity is vendor-specific — without an addressing
        # provider (plugin) the run has none, and flows that reference
        # $session fail with a clear message instead
        session = self.addressing.new_session(self.db) if self.addressing else None
        via = f", new session {_bytes_str(session)}" if session is not None else ""
        how = f"up to {expected} device(s) (address range)" if adopt_after \
            else f"{expected} device(s)"
        self.log(f'MC   teach started — {how}, flow "{flow.name}"{via}')
        try:
            regs = {name: 0 for name in tclib.REGISTERS}
            builtins = {"node": 0, "expected": expected, "session": session}

            def on_step(idx: int, text: str) -> None:
                self.teach = {"step": idx, "of": len(flow.steps), "text": text}
                self._changed()

            status, why = await self._run_program(
                flow, flow.steps, 0, regs, builtins, 0, on_step,
                lambda: self._teach_abort)
            if status != "ok":
                label = "aborted" if self._teach_abort else status.upper()
                reason = "by operator" if self._teach_abort else why
                self.log(f"MC   teach {label} — {reason}", "emcy0")
                return
            if session is not None:
                self.mc["session"] = _bytes_str(session)
                if self.mc_ref:  # the expected state follows the re-addressing
                    self.mc_ref["session"] = self.mc["session"]
                    self.db.set("mc_ref", self.mc_ref)
                self.log(f'MC   teach complete — session {self.mc["session"]} distributed')
            else:
                self.log("MC   teach complete")
            if adopt_after:
                # operator-declared count: the freshly addressed bus IS the
                # machine now — scan and adopt it as the new expected state
                await self._scan_async()
                if self.devices:
                    self.act_mc_adopt({})
                else:
                    self.log("MC   teach done but scan found nothing — expected state unchanged", "emcy0")
            elif self.mc_ref:
                self.mc["busy"] = True
                self.log("MC   scan & verify against the expected state")
                await self._mc_verify_task(self.mc_ref, allow_teach=False)
        finally:
            self.teach = None
            self._teach_abort = False
            self._changed()

    def startup(self) -> None:
        """Machine-control behaviour on server start: restore the remembered
        on/off state instead of force-activating — autoStart off always
        starts deactivated."""
        if not (self.mc.get("autoStart") and self.mc["enabled"]):
            self.mc["enabled"] = False
            return
        if not self.mc_ref:
            self.mc["enabled"] = False
            self.log("MC   startup — machine control stays off, no expected state adopted")
            return
        self.log("MC   startup — machine control restored "
                 f"({int(self.mc_ref.get('expected', 0))} device(s) expected)")
        if self.mc.get("scanStart"):
            if self.connected:
                self.act_mc_verify({})
            else:
                self.log("MC   startup scan skipped — connect the interface, then scan & verify")

    # -- bench instruments (power supply) ------------------------------------
    # A supply the tool can set is what keeps an under-voltage case
    # automated: without it the case degrades to "operator, turn the knob".
    # Nothing here polls — the box reads when the operator asks and after
    # it has changed something itself.
    def _psu_connect(self, port: str, announce: bool = True) -> bool:
        """Reconnect to a remembered port. No probing of other ports —
        writing to a serial port that might be the CAN adapter is not a
        way to look around."""
        if not port:
            return False
        try:
            found = instruments.connect(port, opener=self._psu_opener)
        except Exception as exc:
            self.psu_error = str(exc)
            found = None
        if found is None:
            if announce:
                self.log(f"PSU  nothing answered on {port}", "emcy0")
            return False
        self.psu, idn = found
        self.psu_error = ""
        self.db.set("psu_port", port)
        self._psu_read()
        if announce:
            self.log(f"PSU  {self.psu.name} on {port} — {idn}")
        return True

    def _psu_read(self) -> None:
        if self.psu is None:
            return
        try:
            self._psu_state = self.psu.state()
            self.psu_error = ""
        except Exception as exc:      # the port can vanish mid-session
            self.psu_error = str(exc)
            self._psu_state = None

    def _psu_data(self) -> dict | None:
        """Snapshot form. Values are the instrument's *set* values, which
        is why the UI labels them that way — a set voltage is not a
        measurement of what the terminals are doing."""
        if self.psu is None:
            return {"found": False, "error": self.psu_error} if self.psu_error else None
        st = self._psu_state
        if st is None:
            return {"found": True, "name": self.psu.name, "error": self.psu_error,
                    "port": self.psu.link.port, "channels": []}
        return {"found": True, "name": self.psu.name, "error": self.psu_error,
                "model": st.model, "sn": st.serial, "fw": st.firmware,
                "port": st.port, "output": st.output, "raw": st.raw,
                "channels": [{"volt": ch.volt, "curr": ch.curr, "limit": ch.limit,
                              "mvolt": ch.meas_volt, "mcurr": ch.meas_curr,
                              "extra": ch.extra} for ch in st.channels]}

    def act_psu_search(self, p: dict) -> None:
        try:
            found = instruments.discover(opener=self._psu_opener, ports=self._psu_ports)
        except Exception as exc:
            self.psu_error = str(exc)
            self.log(f"PSU  search failed — {exc}", "emcy0")
            return
        if found is None:
            self.psu_error = "no known power supply found"
            self.log("PSU  no known power supply found on the serial ports", "emcy0")
            return
        psu, idn = found
        self.psu, self.psu_error = psu, ""
        self.db.set("psu_port", psu.link.port)
        self._psu_read()
        self.log(f"PSU  {psu.name} on {psu.link.port} — {idn}")

    def act_psu_refresh(self, p: dict) -> None:
        self._psu_read()

    def act_psu_release(self, p: dict) -> None:
        """Hand the port back — another program on this machine may need
        it, and holding it open would be the reason it cannot have it."""
        if self.psu is not None:
            self.psu.close()
            self.log(f"PSU  released {self.psu.link.port}")
        self.psu, self._psu_state, self.psu_error = None, None, ""
        self.db.set("psu_port", "")

    def act_psu_output(self, p: dict) -> None:
        if self.psu is None:
            return
        on = bool(p.get("on"))
        try:
            self.psu.set_output(on)
        except Exception as exc:
            self.psu_error = str(exc)
            self.log(f"PSU  output {'on' if on else 'off'} failed — {exc}", "emcy0")
            return
        self.log(f"PSU  output {'on' if on else 'off'}")
        self._psu_read()

    def act_psu_set(self, p: dict) -> None:
        if self.psu is None:
            return
        ch = int(p.get("ch") or 1)
        try:
            if p.get("volt") not in (None, ""):
                volts = float(p["volt"])
                self.psu.set_voltage(ch, volts)
                self.log(f"PSU  channel {ch} → {volts:g} V")
            if p.get("curr") not in (None, ""):
                amps = float(p["curr"])
                self.psu.set_current(ch, amps)
                self.log(f"PSU  channel {ch} → {amps:g} A")
        except ValueError:
            self.log(f'PSU  channel {ch} unchanged — "{p.get("volt") or p.get("curr")}"'
                     " is not a number", "emcy0")
            return
        except Exception as exc:
            self.psu_error = str(exc)
            self.log(f"PSU  channel {ch} unchanged — {exc}", "emcy0")
            return
        self._psu_read()

    # -- objects -------------------------------------------------------------
    def _target_node(self) -> int:
        sel = self.sel_devices
        return sel[0]["node"] if sel else 1

    def _remember(self, key: str, value: str) -> None:
        sel = self.sel_devices
        if sel:
            self.db.remember_value(sel[0]["sn"], key, value, datetime.now().strftime("%Y-%m-%d %H:%M"))

    def act_obj_read(self, p: dict) -> None:
        idx, sub = p["idx"], p["sub"]
        node = self._target_node()
        res = self.bus.sdo_read(node, idx, sub)
        key = f"{idx}:{sub}"
        if res.ok:
            self.obj_vals[key] = res.value
            self._remember(key, res.value)
            self.log(f"SDO  read {idx}:{sub} → {res.value} (node {node})", "sdo")
        else:
            self.log(f"SDO  read {idx}:{sub} ✗ abort {res.abort} (node {node})", "emcy0")

    def act_obj_set(self, p: dict) -> None:
        """Value typed into the object table / favorites: staged in
        obj_vals — the next Write sends it. No bus traffic by itself.

        Input is resolved here rather than on write, so the field shows
        what it became before anything reaches the device: "0x2A", "42",
        a symbol name from the device's own headers, or several joined
        with "+" for a flag register. Unreadable input is refused and
        logged — staging text that only fails later, mid-write, is the
        one outcome worth avoiding.
        """
        key = f"{p['idx']}:{p['sub']}"
        text = str(p.get("val", ""))
        if not text.strip():
            self.obj_vals[key] = ""
            return
        try:
            value = parse_value(text, self.num_base, self._object_fields.get(key, []),
                                self.symbols)
        except ValueError as exc:
            self.log(f"OBJ  {key} ← {text!r} rejected — {exc}", "emcy0")
            return
        width = len(self.obj_vals.get(key, "").removeprefix("0x")) or 2
        self.obj_vals[key] = f"0x{value:0{width}X}"

    # value strings are hex by convention (with or without 0x); string-,
    # octet- and domain-typed objects must never be reformatted
    _NO_PAD_TYPES = {0x09, 0x0A, 0x0B, 0x0F}  # VISIBLE/OCTET/UNICODE_STRING, DOMAIN

    @staticmethod
    def _pad_hex(value: str, width_bytes: int) -> str:
        """Normalize a typed value to the object's byte width, so the SDO
        download carries the length the EDS declares ("0x42" as U16 ->
        "0x0042" -> 2 bytes on the wire; the bus layer sizes the transfer
        from the digit count). Longer values are never truncated — the
        device's abort is more honest than silently dropped bytes.

        The value is read the way a person writes one: hex only with an
        explicit ``0x`` (see ``_typed_number``). This box used to read
        everything as hex, so typing 30 wrote forty-eight — and a field
        that quietly means something else than it says is worse on a bench
        than one that refuses.
        """
        num = _typed_number(value)
        if num is None:
            return value              # a string value (device name) — untouched
        if num < 0 or width_bytes <= 0:
            return value
        digits = max(width_bytes * 2, (num.bit_length() + 3) // 4)
        return f"0x{num:0{digits}X}"

    def _eds_write_width(self, node: int, idx: str, sub: str) -> int:
        """Byte width of an object per the EDS assigned to the node;
        0 = unknown or a type that must not be padded."""
        eds = next((d["eds"] for d in self.devices if d["node"] == node), "")
        od = self._ods.load(eds) if eds and eds != "—" else None
        var = find_var(od, int(idx, 16), int(sub or "0", 16)) if od else None
        if var is None or var.data_type in self._NO_PAD_TYPES:
            return 0
        return max(len(var) // 8, 1)

    def act_obj_write(self, p: dict) -> None:
        idx, sub = p["idx"], p["sub"]
        node = self._target_node()
        key = f"{idx}:{sub}"
        value = p.get("val") or self.obj_vals.get(key) or ""
        if not value:
            catalog, _groups, _hint = self._object_catalog()
            for rows in catalog.values():
                for r in rows:
                    if r[0] == idx and r[1] == sub:
                        value = r[5]
        value = self._pad_hex(value, self._eds_write_width(node, idx, sub))
        res = self.bus.sdo_write(node, idx, sub, value)
        if res.ok:
            self.obj_vals[key] = value
            self._remember(key, value)
            self.log(f"SDO  write {idx}:{sub} ← {value} (node {node})", "sdo")
        else:
            self.log(f"SDO  write {idx}:{sub} ✗ abort {res.abort} (node {node})", "emcy0")

    # -- favorites (named object sets, persisted in the workspace db) --------
    def _fav_rows(self) -> list[dict]:
        return self.favorites

    def _save_favs(self) -> None:
        self.db.set("favorites", self.favorites)

    def _object_label(self, idx: str, sub: str) -> str:
        """The EDS name of an object, or "".

        Matched numerically. The catalog writes a sub-index as "04" and a
        step writes it "0x04" — comparing the text found nothing, silently,
        for every caller that did not happen to use the catalog's spelling.
        """
        want = (_as_int(idx), _as_int(sub))
        if want[0] is None:
            return ""
        catalog, _groups, _hint = self._object_catalog()
        for rows in catalog.values():
            for r in rows:
                if (_as_int(r[0]), _as_int(r[1])) == want:
                    return r[2]
        return ""

    def act_fav_toggle(self, p: dict) -> None:
        idx, sub = p["idx"], p["sub"]
        rows = self._fav_rows()
        for r in rows:
            if r["idx"] == idx and r["sub"] == sub:
                rows.remove(r)
                self._save_favs()
                self.log(f"FAV  {idx}:{sub} removed")
                return
        label = self._object_label(idx, sub)
        rows.append({"idx": idx, "sub": sub, "label": label})
        self._save_favs()
        self.log(f'FAV  {idx}:{sub} {label or "?"} added')

    def act_fav_read(self, p: dict) -> None:
        self.act_obj_read({"idx": p["idx"], "sub": p["sub"]})

    def act_fav_read_all(self, p: dict) -> None:
        catalog, _groups, _hint = self._object_catalog()
        acc = {f"{row[0]}:{row[1]}": row[4] for rows in catalog.values() for row in rows}
        for r in list(self._fav_rows()):
            # write-only objects can't be read — the SDO would only abort
            if acc.get(f"{r['idx']}:{r['sub']}") == "wo":
                continue
            self.act_obj_read({"idx": r["idx"], "sub": r["sub"]})

    # -- signal plot (Trace page) ---------------------------------------------------
    def _save_plot_sel(self) -> None:
        self.db.set("plot_sel", self.plot_sel)
        self._plot_keys = {f"{r['idx']}:{r['sub']}" for r in self.plot_sel}

    def act_plot_toggle(self, p: dict) -> None:
        idx, sub = p["idx"], p["sub"]
        for r in self.plot_sel:
            if r["idx"] == idx and r["sub"] == sub:
                self.plot_sel.remove(r)
                self.plot_series.pop(f"{idx}:{sub}", None)
                self._save_plot_sel()
                self.log(f"PLOT {idx}:{sub} removed")
                return
        if len(self.plot_sel) >= PLOT_SEL_MAX:
            self.log(f"PLOT  can't add {idx}:{sub} — at most {PLOT_SEL_MAX} signals plotted "
                     "at once, remove one first", "emcy0")
            return
        label = self._object_label(idx, sub)
        self.plot_sel.append({"idx": idx, "sub": sub, "label": label})
        self._save_plot_sel()
        self.log(f'PLOT {idx}:{sub} {label or "?"} added')

    def act_plot_clear(self, p: dict) -> None:
        self.plot_sel = []
        self.plot_series = {}
        self._save_plot_sel()

    # -- raw rows: SDO / PDO / NMT (autosaved) -------------------------------------
    _RAW_FIELDS = ("type", "node", "i", "s", "l", "v", "pdo", "data", "cmd", "cyc")
    _PDO_BASES = {"RxPDO1": 0x200, "RxPDO2": 0x300, "RxPDO3": 0x400, "RxPDO4": 0x500,
                  "TxPDO1": 0x180, "TxPDO2": 0x280, "TxPDO3": 0x380, "TxPDO4": 0x480}
    _RAW_NMT_CMDS = ("start", "preop", "stop", "reset", "resetcomm")

    def _save_raw(self) -> None:
        self.db.set("raw_sdo", self.raw_rows)

    def _raw_node(self, r: dict) -> int | None:
        """The row's own node-id (hex accepted); empty falls back to the
        selected device. None = unparsable or outside 1..127."""
        raw = str(r.get("node", "")).strip()
        if not raw:
            return self._target_node()
        try:
            node = int(raw, 0)
        except ValueError:
            return None
        return node if 1 <= node <= 127 else None

    @staticmethod
    def _raw_sdo_addr_ok(r: dict) -> bool:
        try:
            int(str(r.get("i", "")), 16)
            int(str(r.get("s") or "0"), 16)
            return True
        except ValueError:
            return False

    def act_raw_update(self, p: dict) -> None:
        i, field, value = int(p["row"]), p["field"], p["value"]
        if 0 <= i < len(self.raw_rows) and field in self._RAW_FIELDS:
            self.raw_rows[i][field] = value
            self._save_raw()

    def act_raw_add(self, p: dict) -> None:
        if len(self.raw_rows) < 8:
            self._cyc_seq += 1
            self.raw_rows.append({"type": "sdo", "node": "", "i": "0x", "s": "00", "l": "1",
                                  "v": "", "cyc": "100", "run": False, "id": self._cyc_seq})
            self._save_raw()

    def act_raw_remove(self, p: dict) -> None:
        if len(self.raw_rows) > 1:
            gone = self.raw_rows.pop()
            self._cyc_next.pop(gone.get("id"), None)
            self._save_raw()

    def act_raw_read(self, p: dict) -> None:
        r = self.raw_rows[int(p["row"])]
        node = self._raw_node(r)
        if node is None or not self._raw_sdo_addr_ok(r):
            self.log(f'RAW  SDO read skipped — invalid index/sub/node '
                     f'("{r.get("i")}:{r.get("s")}", node "{r.get("node")}")', "emcy0")
            return
        res = self.bus.sdo_read(node, r["i"], r["s"])
        if res.ok:
            r["v"] = res.value
            self._save_raw()
            self.log(f"SDO  read {r['i']}:{r['s']} → {res.value} (node {node})", "sdo")
        else:
            self.log(f"SDO  read {r['i']}:{r['s']} ✗ abort {res.abort} (node {node})", "emcy0")

    def act_raw_write(self, p: dict) -> None:
        r = self.raw_rows[int(p["row"])]
        node = self._raw_node(r)
        if node is None or not self._raw_sdo_addr_ok(r):
            self.log(f'RAW  SDO write skipped — invalid index/sub/node '
                     f'("{r.get("i")}:{r.get("s")}", node "{r.get("node")}")', "emcy0")
            return
        try:
            width = int(str(r.get("l") or "0"))
        except ValueError:
            width = 0
        value = self._pad_hex(r["v"] or "0x00", width)
        res = self.bus.sdo_write(node, r["i"], r["s"], value)
        if res.ok:
            self.log(f"SDO  write {r['i']}:{r['s']} ← {value} (node {node})", "sdo")
        else:
            self.log(f"SDO  write {r['i']}:{r['s']} ✗ abort {res.abort} (node {node})", "emcy0")

    def _raw_pdo_frame(self, r: dict) -> tuple[int, bytes] | str:
        """(cob, data) a PDO row would put on the wire, or an error text."""
        base = self._PDO_BASES.get(r.get("pdo") or "RxPDO1")
        node = self._raw_node(r)
        if base is None or node is None:
            return f'invalid PDO/node ({r.get("pdo")}, "{r.get("node")}")'
        try:
            data = bytes.fromhex(str(r.get("data") or "").replace("0x", "").replace(",", " "))
        except ValueError:
            return f'data must be hex bytes, got "{r.get("data", "")}"'
        if len(data) > 8:
            return f"{len(data)} bytes > 8"
        return base + node, data

    def act_raw_send(self, p: dict) -> None:
        """Send button of PDO and NMT rows."""
        r = self.raw_rows[int(p["row"])]
        if not self.connected:
            self.log("RAW  send skipped — interface not connected", "emcy0")
            return
        kind = r.get("type")
        if kind == "pdo":
            frame = self._raw_pdo_frame(r)
            if isinstance(frame, str):
                self.log(f"RAW  PDO send skipped — {frame}", "emcy0")
                return
            cob, data = frame
            self.bus.send_raw(cob, data)
            self.log(f"PDO  {r.get('pdo') or 'RxPDO1'} → node {cob & 0x7F:02d}  [{data.hex(' ').upper() or '—'}]")
        elif kind == "nmt":
            cmd = r.get("cmd") or "start"
            if cmd not in self._RAW_NMT_CMDS:
                self.log(f'RAW  NMT send skipped — unknown command "{cmd}"', "emcy0")
                return
            raw_node = str(r.get("node", "")).strip()
            node: int | None = None  # empty/0 = broadcast to all nodes
            if raw_node and raw_node != "0":
                try:
                    node = int(raw_node, 0)
                except ValueError:
                    node = -1
                if not 1 <= node <= 127:
                    self.log(f'RAW  NMT send skipped — invalid node "{raw_node}"', "emcy0")
                    return
            self.bus.nmt(cmd, node)
            state = NMT_STATE.get(cmd)
            if state:
                for d in self.devices:
                    if node is None or d["node"] == node:
                        d["nmt"] = state
            label = NMT_LABEL.get(cmd, "reset comm")
            self.log(f"NMT  {label} → {'all nodes' if node is None else f'node {node:02d}'}", "nmt")

    # -- cyclic transmit (Sendeliste + SYNC producer) -----------------------
    # Runs beside tick_loop with ms granularity — TICK_S is far too coarse
    # for PDO cycle times. asyncio/Python jitter means this feeds devices
    # and generates load; it is not a hardware-timed frame generator.
    _SYNC_KEY = -1  # scheduler key of the SYNC producer among the row ids

    def act_raw_cycle(self, p: dict) -> None:
        """⟳ toggle of a PDO row: start/stop cyclic sending."""
        r = self.raw_rows[int(p["row"])]
        if r.get("type") != "pdo":
            return
        if r.get("run"):
            r["run"] = False
            return
        if not self.connected:
            self.log("CYC  start skipped — interface not connected", "emcy0")
            return
        frame = self._raw_pdo_frame(r)
        if isinstance(frame, str):
            self.log(f"CYC  start skipped — {frame}", "emcy0")
            return
        r["run"] = True
        self._cyc_next[r["id"]] = 0.0  # first frame goes out immediately

    def act_sync_toggle(self, p: dict) -> None:
        if self.sync_run:
            self.sync_run = False
            self.log("SYNC producer stopped")
            return
        if not self.connected:
            self.log("SYNC start skipped — interface not connected", "emcy0")
            return
        self.sync_run = True
        self._cyc_next[self._SYNC_KEY] = 0.0
        self.log(f"SYNC producer started — every {self.sync_ms} ms")

    def act_set_sync_ms(self, p: dict) -> None:
        try:
            ms = int(str(p["ms"]).strip())
        except (KeyError, ValueError):
            return
        self.sync_ms = min(60000, max(5, ms))
        self.db.set("sync", {"ms": self.sync_ms})

    def _stop_cyclic(self) -> None:
        """Disconnect/bus-lost: nothing keeps transmitting into the void."""
        self.sync_run = False
        for r in self.raw_rows:
            r["run"] = False
        self._cyc_next.clear()

    def _cyc_period(self, r: dict) -> float:
        try:
            return max(0.005, float(str(r.get("cyc") or "100")) / 1000)
        except ValueError:
            return 0.1

    def _cyc_fire(self, r: dict) -> None:
        frame = self._raw_pdo_frame(r)
        if isinstance(frame, str):  # row edited into an invalid state mid-run
            r["run"] = False
            self.log(f"CYC  row stopped — {frame}", "emcy0")
            return
        cob, data = frame
        self.bus.send_raw(cob, data)

    async def _cyclic_loop(self) -> None:
        while True:
            if not self.connected or not (self.sync_run or any(r.get("run") for r in self.raw_rows)):
                await asyncio.sleep(0.2)
                continue
            now = time.monotonic()
            wake = now + 0.25
            senders: list[tuple[int, float, dict | None]] = [
                (r["id"], self._cyc_period(r), r) for r in self.raw_rows if r.get("run")]
            if self.sync_run:
                senders.append((self._SYNC_KEY, max(0.005, self.sync_ms / 1000), None))
            for key, period, row in senders:
                due = self._cyc_next.get(key, 0.0)
                if now >= due:
                    try:
                        if row is None:
                            self.bus.send_raw(0x080, b"")
                        else:
                            self._cyc_fire(row)
                    except Exception as exc:  # bus torn down mid-send
                        self._stop_cyclic()
                        self.log(f"CYC  stopped — send failed ({exc})", "emcy0")
                        break
                    # keep the cadence when we're on time; re-anchor after a stall
                    due = due + period if now - due < period else now + period
                    self._cyc_next[key] = due
                wake = min(wake, due)
            await asyncio.sleep(max(0.002, wake - time.monotonic()))

    # -- tests ---------------------------------------------------------------------
    def act_test_toggle(self, p: dict) -> None:
        tid = p["id"]
        if tid in self.test_sel:
            self.test_sel.discard(tid)
        else:
            self.test_sel.add(tid)

    def _catalog_rows(self) -> list[tuple]:
        """Selectable catalog rows (id, name, tools, est): the real test-case
        files when the TestCases folder has any, else the demo catalog.
        Files with schema errors are listed in the snapshot but not here —
        they cannot be selected or run."""
        if self.testcases:
            return [(tc.id, tc.name, ", ".join(tc.tools) or "—", tc.est or "—")
                    for tc in sorted(self.testcases.values(), key=lambda t: t.id)
                    if not tc.error]
        # demo catalog only in demo mode — with real hardware an empty
        # TestCases folder means an empty list, not invented tests
        return data.TESTS if self.adapter == "demo" else []

    def _shown_tests(self) -> list[tuple]:
        return [t for t in self._catalog_rows() if self.tool_filter or t[2] == "—"]

    def act_tests_all(self, p: dict) -> None:
        """Select what the list is showing — which the client has to say.

        Variant, category and the search box are filters the frontend
        applies to the catalog it was sent; the server never hears about
        them. Selecting from its own idea of "shown" therefore picked up
        cases that were not on screen: with the category set to
        `automated`, a semi-automated case went into the selection and
        the run stopped at a question nobody expected.

        The ids are still intersected with what is actually runnable, so
        a stale list cannot select a case that has since gone or broken.
        """
        runnable = {t[0] for t in self._shown_tests()}
        asked = p.get("ids")
        self.test_sel = (runnable & set(asked)) if isinstance(asked, list) else runnable

    def act_tests_none(self, p: dict) -> None:
        self.test_sel = set()

    def act_tool_filter_toggle(self, p: dict) -> None:
        self.tool_filter = not self.tool_filter

    def act_stop_err_toggle(self, p: dict) -> None:
        self.stop_on_err = not self.stop_on_err

    def act_set_repeat(self, p: dict) -> None:
        n = max(1, min(99, int(p["n"] or 1)))
        if p["which"] == "case":
            self.repeat_case = n
        else:
            self.repeat_run = n

    def act_run_start(self, p: dict) -> None:
        sel = [t[0] for t in self._shown_tests() if t[0] in self.test_sel]
        if self.running or not sel:
            return
        if self.testcases:
            if not self.connected:
                self.log("RUN  cannot start — interface not connected", "emcy0")
                return
            needs_selection = any(self.testcases[t].dut == "selected"
                                  for t in sel if t in self.testcases)
            if needs_selection and not self.sel_devices:
                self.log("RUN  no target device — select a DUT in the Devices box", "emcy0")
                return
            self._run_mode = "exec"
        else:
            self._run_mode = "sim"  # demo catalog (data.TESTS) has no step files
        order: list[str] = []
        for _ in range(self.repeat_run):
            for tid in sel:
                order.extend([tid] * self.repeat_case)
        self.running = True
        self.run_order = order
        self.run_idx = 0
        self.results = {}
        self._run_stop_requested = False
        extra = "" if len(order) == len(sel) else f" ({len(sel)} selected × repeats)"
        self.log(f"RUN  started — {len(order)} test cases{extra}")
        if self._run_mode == "exec":
            task = asyncio.ensure_future(self._run_task())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # -- suites (named selection + run configuration) ------------------------
    def act_suite_save(self, p: dict) -> None:
        name = (p.get("name") or "").strip()
        if not name:
            return
        self.suites[name] = {"sel": sorted(self.test_sel), "repeat_case": self.repeat_case,
                             "repeat_run": self.repeat_run, "stop_on_err": self.stop_on_err}
        self.active_suite = name
        self.db.set("suites", self.suites)
        self.db.set("active_suite", name)
        self.log(f'SUITE "{name}" saved — {len(self.test_sel)} tests, '
                 f'case×{self.repeat_case}, run×{self.repeat_run}')

    def act_suite_load(self, p: dict) -> None:
        s = self.suites.get(p["name"])
        if not s:
            return
        self.test_sel = set(s.get("sel", []))
        self.repeat_case = s.get("repeat_case", 1)
        self.repeat_run = s.get("repeat_run", 1)
        self.stop_on_err = s.get("stop_on_err", True)
        self.active_suite = p["name"]
        self.db.set("active_suite", self.active_suite)
        self.log(f'SUITE "{p["name"]}" loaded — {len(self.test_sel)} tests selected')

    def act_suite_delete(self, p: dict) -> None:
        name = p["name"]
        if name in self.suites:
            del self.suites[name]
            if self.active_suite == name:
                self.active_suite = ""
                self.db.set("active_suite", "")
            self.db.set("suites", self.suites)
            self.log(f'SUITE "{name}" deleted')

    def act_run_stop(self, p: dict) -> None:
        if not self.running:
            return
        if self._run_mode == "exec":
            self._run_stop_requested = True
            if self._manual_event is not None:  # unblock a waiting manual step
                self._manual_result = False
                self._manual_event.set()
        else:
            self.running = False
            self.log("RUN  stopped by user")

    async def _prompt(self, prompt: dict, timeout: float) -> str | None:
        """Put a question on the screen and wait for the person at the bench.

        Returns their answer ("ok" | "no" | "cancel"), or None on timeout.
        One waiting mechanism for all three prompt kinds — a second one
        would be a second way to leave the run stuck with no dialog.
        """
        self.manual_prompt = prompt
        self._manual_event = asyncio.Event()
        self._manual_result, self._manual_value = "cancel", ""
        self._changed()
        try:
            await asyncio.wait_for(self._manual_event.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.manual_prompt = None
            self._manual_event = None
            self._changed()
        return self._manual_result

    def act_manual_confirm(self, p: dict) -> None:
        self._answer("ok", p)

    def act_manual_abort(self, p: dict) -> None:
        self._answer("abort", p)

    def act_manual_answer(self, p: dict) -> None:
        """The three-way answer to `ask`, and the value typed into `adjust`."""
        choice = str(p.get("choice") or "cancel")
        self._answer(choice if choice in ("ok", "no", "cancel") else "cancel", p)

    def _answer(self, choice: str, p: dict) -> None:
        if self.manual_prompt and self._manual_event is not None:
            self._manual_result = choice
            self._manual_value = str(p.get("value", ""))
            self._manual_event.set()

    # -- step executor (real test-case files, see docs/ablaeufe/A-04) --------
    async def _run_task(self) -> None:
        self._run_cases = []
        self._run_started = datetime.now().isoformat(timespec="seconds")
        try:
            for i, tid in enumerate(self.run_order):
                self.run_idx = i
                if self._run_stop_requested:
                    self.log("RUN  stopped by user")
                    self._push_report(self.run_order[:i])
                    return
                verdict, reason = await self._exec_case(tid)
                if self._run_record is not None:
                    self._run_cases.append(self._run_record)
                self.results[tid] = verdict
                suffix = f" ({reason})" if reason else ""
                if verdict == "PASS":
                    self.log(f"TEST {tid} passed", "test")
                elif verdict == "SKIP":
                    self.log(f"TEST {tid} SKIPPED{suffix}", "test")
                else:
                    self.log(f"TEST {tid} {verdict}{suffix}", "emcy0")
                self._changed()
                if self._run_stop_requested:
                    self.log("RUN  stopped by user")
                    self._push_report(self.run_order[: i + 1])
                    return
                if reason == "connection lost":
                    self.log("RUN  aborted — connection lost", "emcy0")
                    self._push_report(self.run_order[: i + 1])
                    return
                if verdict in ("FAIL", "ERROR") and self.stop_on_err:
                    self.log(f"RUN  aborted — stop on error (after {i + 1} of {len(self.run_order)})")
                    self._push_report(self.run_order[: i + 1])
                    return
            self.run_idx = len(self.run_order)
            self.log("RUN  finished — report created")
            self._push_report(self.run_order)
        finally:
            self.running = False
            self.run_prog = None
            self.manual_prompt = None
            self._manual_event = None
            self._changed()

    def _emcy_matcher(self, val: dict, regs: dict, builtins: dict):
        """One predicate over a recorded EMCY, from the fields a step gave.

        The three parts of the frame are asked about separately, and every
        part a step names has to agree. CiA 301 fixes only the first
        three bytes — error code u16, error register u8 — and leaves the
        remaining five to the manufacturer, which is exactly where a
        device family puts the code its own test cases are written
        against. Folding those into `code` would mean one number standing
        for two unrelated things; asking about them by name keeps a case
        readable and lets it check a manufacturer code and the error
        register in the same step.

        A step that names none of them matches any EMCY at all, which is
        what "an EMCY, never mind which" looks like.
        """
        def num(key):
            return _resolve(val[key], regs, builtins)
        code = num("code") if "code" in val else None
        mask = num("mask") if "mask" in val else 0xFFFF
        mec = num("mec") if "mec" in val else None
        mec_mask = num("mec_mask") if "mec_mask" in val else 0xFFFF
        reg = num("reg") if "reg" in val else None
        want_node = num("node") if "node" in val else None

        def match(entry: tuple[int, int, int, bytes]) -> bool:
            node, got_code, got_reg, mfr = entry
            if want_node not in (None, node):
                return False
            if code is not None and got_code & mask != code & mask:
                return False
            if reg is not None and got_reg != reg:
                return False
            if mec is not None:
                if _mec(mfr) & mec_mask != mec & mec_mask:
                    return False
            return True
        return match

    async def _exec_case(self, tid: str) -> tuple[str, str]:
        tc = self.testcases.get(tid)
        started = time.time()
        rec = reportlib.CaseRecord(id=tid, started=datetime.now().isoformat(timespec="seconds"),
                                   user=_bench_user())
        self._run_record = rec
        if tc is None:
            rec.verdict, rec.reason = "ERROR", "unknown test case"
            rec.seconds = time.time() - started
            return rec.verdict, rec.reason
        rec.name, rec.desc, rec.grade, rec.tools = tc.name, tc.desc, tc.grade, list(tc.tools)
        if tc.error:
            rec.verdict, rec.reason = "ERROR", f"invalid test file: {tc.error}"
            rec.seconds = time.time() - started
            return rec.verdict, rec.reason
        node = self._resolve_dut(tc)
        if node is None:
            rec.verdict, rec.reason = "ERROR", "no target device"
            rec.seconds = time.time() - started
            return rec.verdict, rec.reason
        dev = next((d for d in self.devices if d["node"] == node), None)
        if dev is not None:
            rec.device, rec.variant = dev.get("name", ""), dev.get("variant", "")
            rec.sn, rec.node = dev.get("sn", ""), node
        # a case that says which variants it is for, against a device that
        # says which one it is: only a real mismatch skips. An unknown
        # variant runs the case — refusing to run because the bench could
        # not read a number is how coverage disappears without a trace
        if tc.variants and rec.variant and not _variant_matches(tc.variants, rec.variant):
            rec.seconds = time.time() - started
            rec.verdict = "SKIP"
            rec.reason = f"for variant {', '.join(tc.variants)}, this is {rec.variant}"
            return rec.verdict, rec.reason
        regs = {name: 0 for name in tclib.REGISTERS}
        sess = self.mc.get("session") or ""  # "" until an addressing run distributed one
        builtins = {"node": node, "expected": int(self.mc.get("expected") or 0),
                    "session": _session_bytes(sess) if sess else None,
                    "failed": ""}   # set by a failure the case chose to survive
        total = len(tc.preconditions) + len(tc.steps)

        def on_step(idx: int, text: str) -> None:
            self.run_prog = {"tid": tc.id, "step": idx, "of": total, "text": text}
            self._changed()

        def stop() -> bool:
            return self._run_stop_requested

        def done(verdict: str, why: str = "") -> tuple[str, str]:
            rec.verdict, rec.reason = verdict, why
            rec.seconds = time.time() - started
            return verdict, why

        status, why = await self._run_program(tc, tc.preconditions, node, regs,
                                              builtins, 0, on_step, stop, rec.steps)
        if status in ("fail", "skip"):
            return done("SKIP", f"precondition: {why}")
        if status == "error":
            return done("ERROR", why)
        # only the body may carry on after a failure — a precondition that
        # fails means the case does not apply, and there is nothing to undo
        status, why = await self._run_program(tc, tc.steps, node, regs, builtins,
                                              len(tc.preconditions), on_step, stop,
                                              rec.steps, allow_continue=True)
        if status == "ok":
            return done("PASS")
        if status == "skip":
            return done("SKIP", why)
        return done("FAIL" if status == "fail" else "ERROR", why)

    async def _run_program(self, tc: tclib.TestCase, steps: list, node: int,
                           regs: dict, builtins: dict, base: int,
                           on_step, should_stop, record: list | None = None,
                           allow_continue: bool = False) -> tuple[str, str]:
        """Program-counter loop over one step list (format v2: labels, jumps,
        registers). Returns ("ok" | "fail" | "error", reason)."""
        labels = {step["label"]: i for i, step in enumerate(steps)
                  if len(step) == 1 and "label" in step}
        pc = 0
        executed = 0
        while pc < len(steps):
            executed += 1
            if executed > tclib.MAX_STEPS:
                return "error", "step limit exceeded"
            if not self.connected:
                return "error", "connection lost"
            if should_stop():
                return "error", "aborted"
            key, val = next(iter(steps[pc].items()))
            text = self._label_step(key, val, regs, builtins)
            on_step(base + pc + 1, text)
            status, info = await self._exec_one(tc, key, val, node, regs,
                                                builtins, should_stop)
            if record is not None:
                # a note (log step) is neither pass nor fail — it is the
                # sentence somebody wrote to make the report readable
                state = "note" if key == "log" else {
                    "jump": "ok", "end": "ok"}.get(status, status)
                record.append(reportlib.StepRecord(
                    line=base + pc + 1, text=text, state=state,
                    note=val.get("note", "") if isinstance(val, dict) else "",
                    # on the passing path this is what came back, not a
                    # reason — both belong in the file for the same reason
                    detail=info,
                    ts=datetime.now().strftime("%Y%m%d_%H%M%S.%f")[:-3]))
            if status == "jump":
                pc = labels[info]
                continue
            if status == "end":
                break
            if (status == "fail" and allow_continue and tc.on_fail == "continue"
                    and key != "fail"):
                # the case says it wants to reach its own last steps even
                # when something failed — that is where it puts the bench
                # back. The failure is remembered, not forgiven: the first
                # one is the verdict's reason, and jump_on_error can see it.
                if not builtins.get("failed"):
                    builtins["failed"] = info or "step failed"
                pc += 1
                continue
            if status != "ok":
                # a case that already failed does not get to end as SKIP
                # because a later step was cancelled — the device failed,
                # and that is the verdict the report has to carry
                if status == "skip" and builtins.get("failed"):
                    return "fail", builtins["failed"]
                return status, info
            pc += 1
        return ("fail", builtins["failed"]) if builtins.get("failed") else ("ok", "")

    def _resolve_dut(self, tc: tclib.TestCase) -> int | None:
        if isinstance(tc.dut, dict):
            code = str(tc.dut.get("code", ""))
            for d in self.devices:
                if self._dut_code(d["eds"]) == code:
                    return d["node"]
            return None
        sel = self.sel_devices
        return sel[0]["node"] if sel else None

    async def _exec_one(self, tc: tclib.TestCase, key: str, val, node: int,
                        regs: dict, builtins: dict, should_stop) -> tuple[str, str]:
        """One step. Returns ("ok" | "fail" | "error" | "jump" | "end", info)."""
        bus = self.bus
        # -- local: registers, arithmetic, control flow (no bus traffic) -----
        if key == "label":
            return "ok", ""
        if key == "jump":
            return "jump", val
        if key == "jump_on_error":
            # only ever true in a case with on_fail: continue — anywhere
            # else the run would already have ended at the failure
            return ("jump", val) if builtins.get("failed") else ("ok", "")
        if key == "rand":
            lo = _resolve(val.get("min", 0), regs, builtins)
            hi = _resolve(val.get("max", 0xFFFFFFFF), regs, builtins)
            if lo > hi:
                return "error", f"rand {val['to']}: min {lo} above max {hi}"
            regs[val["to"]] = random.randint(lo, hi) & 0xFFFFFFFF
            return "ok", ""
        if key in tclib._COND_JUMPS:
            a = _resolve(val["a"], regs, builtins)
            b = _resolve(val["b"], regs, builtins)
            hit = {"jump_eq": a == b, "jump_ne": a != b,
                   "jump_gt": a > b, "jump_lt": a < b,
                   "jump_ge": a >= b, "jump_le": a <= b}[key]
            return ("jump", val["to"]) if hit else ("ok", "")
        if key in tclib._ARITH:
            v = _resolve(val["value"], regs, builtins)
            cur = regs[val["to"]]
            if key == "div" and v == 0:
                return "error", f"div {val['to']} by zero"
            # a mapping of every result would divide even when the step is
            # an add — the operands come from the device, so that is a real
            # crash waiting for a zero
            ops = {"mov": lambda: v, "add": lambda: cur + v, "sub": lambda: cur - v,
                   "mul": lambda: cur * v, "div": lambda: cur // v,
                   "and": lambda: cur & v, "or": lambda: cur | v,
                   "xor": lambda: cur ^ v}
            regs[val["to"]] = ops[key]() & 0xFFFFFFFF
            return "ok", ""
        if key == "fail":
            return "fail", val
        if key == "skip":
            return "skip", val
        if key == "end":
            return "end", ""
        if key == "log":
            self.log(f"TEST {tc.id} · {val}", "test")
            return "ok", ""
        if key == "wait":
            await asyncio.sleep(float(val["s"] if isinstance(val, dict) else val))
            return "ok", ""
        # -- bus -------------------------------------------------------------
        if key == "nmt":
            if isinstance(val, dict):
                cmd, n = val["cmd"], val.get("node")
                target = None if n == "all" else (
                    _resolve(n, regs, builtins) if n is not None else node)
            else:
                cmd, target = val, node
            await asyncio.to_thread(bus.nmt, cmd, target)
            return "ok", ""
        if key == "can_send":
            cob = _resolve(val["cob"], regs, builtins)
            uses_session = val["data"] == "$session" or (
                isinstance(val["data"], list) and "$session" in val["data"])
            if uses_session and builtins.get("session") is None:
                return "fail", ("$session unavailable — no addressing provider "
                                "installed (vendor plugin)")
            if val["data"] == "$session":
                data = bytes(builtins["session"])
            else:
                buf = bytearray()
                for item in val["data"]:
                    if item == "$session":  # expands to the session identity bytes
                        buf += builtins["session"]
                    else:
                        buf.append(_resolve(item, regs, builtins) & 0xFF)
                data = bytes(buf)
            await asyncio.to_thread(bus.send_raw, cob, data)
            return "ok", ""
        if key == "lss_assign":
            count = _resolve(val["count"], regs, builtins)
            try:
                assigned = await asyncio.to_thread(bus.lss_assign, count)
            except ConnectionError:
                return "error", "connection lost"
            regs[val.get("into", "R0")] = assigned & 0xFFFFFFFF
            return "ok", ""
        if key in ("sdo_read", "sdo_write") and "node" in val:
            # a case may talk to more than the one device it is about — a
            # second feeder consuming yarn, a gateway. Default stays the
            # DUT resolved from the file's `dut`.
            node = _resolve(val["node"], regs, builtins)
        if key == "sdo_write":
            value_str = _write_value(val, regs, builtins)
            res = await asyncio.to_thread(
                bus.sdo_write, node, _hexstr(val["index"]), _hexstr(val["sub"]), value_str)
            where = f"sdo_write {_hexstr(val['index'])}:{_hexstr(val['sub'])}"
            if "expect_abort" in val:
                if res.ok:
                    return "fail", f"{where} expected abort {_hexstr(val['expect_abort'])}, wrote ok"
                code = _as_int(res.abort.split()[0]) if res.abort else None
                if code is not None and code == _as_int(val["expect_abort"]):
                    return "ok", f"Response: abort {res.abort} — expected"
                return "fail", f"{where} expected abort {_hexstr(val['expect_abort'])}, got {res.abort}"
            # nothing on the passing path: the step line already carries
            # the value, resolved, so a "wrote …" underneath it is the same
            # number a second time
            return ("ok", "") if res.ok else ("fail", f"{where} abort {res.abort}")
        if key == "sdo_read":
            index_s, sub_s = _hexstr(val["index"]), _hexstr(val["sub"])
            res = await asyncio.to_thread(bus.sdo_read, node, index_s, sub_s)
            # resolved *before* the result is stored: `into` defaults to R0,
            # so an `expect: R0` resolved afterwards would compare the value
            # against itself and pass no matter what the device answered
            spec = _with_registers(val, regs)
            if res.ok:  # the result is always available for further processing
                regs[val.get("into", "R0")] = (_as_int(res.value) or 0) & 0xFFFFFFFF
            status, info = _judge_read(spec, res)
            if status == "ok" and res.ok:
                # what came back, on the passing path too: a report that
                # only records failures cannot answer "what did it read
                # last Tuesday", which is most of why anybody keeps them
                info = f"Response: {self._value_note(index_s, sub_s, res.value)}"
            return status, info
        if key == "psu":
            if self.psu is None:
                # equipment the case needs is not there — that is not the
                # device failing, and calling it FAIL would blame the DUT
                return "error", "no power supply connected"
            ch = int(val.get("ch", 1))
            try:
                if "volt" in val:
                    await asyncio.to_thread(self.psu.set_voltage, ch,
                                            _resolve_num(val["volt"], regs, builtins))
                if "curr" in val:
                    await asyncio.to_thread(self.psu.set_current, ch,
                                            _resolve_num(val["curr"], regs, builtins))
                if "output" in val:      # YAML turns bare on/off into a bool
                    await asyncio.to_thread(self.psu.set_output,
                                            val["output"] in (True, "on"))
            except Exception as exc:
                return "error", f"power supply: {exc}"
            self._psu_read()
            return "ok", ""
        if key == "emcy_clear":
            self.emcy_seen.clear()
            return "ok", ""
        if key == "expect_emcy":
            match = self._emcy_matcher(val, regs, builtins)
            timeout = float(val.get("timeout", 1.0))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout

            def seen() -> bool:
                return any(match(e) for e in self.emcy_seen)

            # an EMCY that arrived before this step is a hit too: the device
            # sends it when it feels like it, and a check that only looks
            # forward turns a timing difference into a test failure
            while True:
                if seen():
                    return "ok", ""
                if loop.time() >= deadline:
                    break
                if should_stop():
                    return "error", "aborted"
                if not self.connected:
                    return "error", "connection lost"
                await asyncio.sleep(0.05)
            seen_now = ", ".join(_emcy_str(e) for e in list(self.emcy_seen)[-3:])
            return "fail", (f"{_emcy_wanted(val)} — none seen within {timeout:g}s"
                            + (f"; saw {seen_now}" if seen_now
                               else "; nothing arrived at all"))
        if key == "expect_no_emcy":
            # the opposite of expect_emcy, and it cannot wait: no amount of
            # waiting proves nothing will arrive. It asks what expect_emcy
            # asks — of the same window, everything since the last
            # emcy_clear — and fails if anything in it matches.
            match = self._emcy_matcher(val, regs, builtins)
            hits = [e for e in self.emcy_seen if match(e)]
            if not hits:
                return "ok", ""
            return "fail", ("expected no EMCY, saw "
                            + ", ".join(_emcy_str(e) for e in hits[-3:]))
        if key == "wait_for":
            timeout = float(val["timeout"])
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            on_timeout = val.get("on_timeout")  # jump target instead of FAIL
            if "cob" in val:  # frame form (v2)
                # cob/data may each be a single value or a list — a list
                # races every (cob, prefix) pair concurrently in the same
                # wait, so a step can't miss an out-of-band signal (e.g.
                # addressing's Addr-End) while blocked waiting on the
                # primary one; `into` reports which pair matched (index)
                cob_val = val["cob"]
                cob_list = cob_val if isinstance(cob_val, list) else [cob_val]
                data_val = val.get("data")
                data_list = data_val if isinstance(data_val, list) else [data_val] * len(cob_list)
                pairs = [(_resolve(c, regs, builtins),
                         bytes.fromhex(str(d).replace(" ", "")) if d else b"")
                        for c, d in zip(cob_list, data_list, strict=True)]
                into = val.get("into")
                while loop.time() < deadline:
                    if should_stop():
                        return "error", "aborted"
                    if not self.connected:
                        return "error", "connection lost"
                    slice_s = max(0.05, min(0.5, deadline - loop.time()))
                    idx = await asyncio.to_thread(bus.wait_frame, pairs, slice_s)
                    if idx is not None:
                        if into:
                            regs[into] = idx
                        return "ok", ""
                if on_timeout:
                    return "jump", on_timeout
                cobs_str = " / ".join(f"0x{c:03X}" for c, _ in pairs)
                return "fail", f"wait_for {cobs_str} — timeout after {timeout:g}s"
            target = val["heartbeat"]
            n = _resolve(val["node"], regs, builtins) if "node" in val else node
            while loop.time() < deadline:
                if bus.nmt_state(n) == target:
                    return "ok", ""
                if should_stop():
                    return "error", "aborted"
                if not self.connected:
                    return "error", "connection lost"
                await asyncio.sleep(0.1)
            if on_timeout:
                return "jump", on_timeout
            return "fail", f"wait_for heartbeat {target} — timeout after {timeout:g}s"
        if key in self._step_types:  # plugin step "<plugin>.<key>"
            try:
                return await self._step_types[key].execute(
                    self, bus, node, val, regs, builtins)
            except Exception as exc:  # a broken plugin step must not kill the run
                return "error", f"{key}: {exc}"
        if key == "manual":
            text = val if isinstance(val, str) else val["text"]
            timeout = tclib.MANUAL_TIMEOUT_S if isinstance(val, str) \
                else float(val.get("timeout", tclib.MANUAL_TIMEOUT_S))
            answer = await self._prompt(
                {"tid": tc.id, "text": text, "kind": "confirm"}, timeout)
            if answer is None:
                return "error", "manual step timed out"
            self._manual_result = answer
            if should_stop():
                return "error", "aborted"
            return ("ok", "") if self._manual_result == "ok" \
                else ("error", "manual step aborted")
        if key == "ask":
            text = val if isinstance(val, str) else val["text"]
            title = "" if isinstance(val, str) else str(val.get("title", ""))
            timeout = tclib.MANUAL_TIMEOUT_S if isinstance(val, str) \
                else float(val.get("timeout", tclib.MANUAL_TIMEOUT_S))
            answer = await self._prompt({"tid": tc.id, "text": text, "title": title,
                                         "kind": "ask"}, timeout)
            if answer is None:
                return "error", "question timed out"
            if should_stop():
                return "error", "aborted"
            # the operator looked and said no: that is a verdict about the
            # device, not an aborted run, so it is a FAIL and the question
            # is the reason — nobody has to guess what was answered
            if answer == "no":
                return "fail", text
            return ("ok", "") if answer == "ok" else ("skip", f"cancelled: {text}")
        if key == "adjust":
            index, sub = _hexstr(val["index"]), _hexstr(val["sub"])
            node_ = _resolve(val["node"], regs, builtins) if "node" in val else node
            res = await asyncio.to_thread(bus.sdo_read, node_, index, sub)
            if not res.ok:
                return "fail", f"adjust {index}:{sub} abort {res.abort}"
            answer = await self._prompt(
                {"tid": tc.id, "kind": "adjust", "text": str(val.get("text", "")),
                 "index": index, "sub": sub, "value": str(res.value)},
                float(val.get("timeout", tclib.MANUAL_TIMEOUT_S)))
            if answer is None:
                return "error", "adjust step timed out"
            if should_stop():
                return "error", "aborted"
            if answer != "ok":
                return "skip", f"cancelled: {val.get('text') or index}"
            number = _typed_number(self._manual_value)
            if number is None:
                return "error", (f"adjust {index}:{sub}: "
                                 f"{self._manual_value!r} is not a number")
            # width travels in the literal, the way the sdo_write step does
            # it. Default it from the value the device just answered rather
            # than from 4: this step read the object a moment ago, so its
            # width is known, and widening a U16 to four bytes is an abort
            # the operator would have to guess the cause of.
            size = int(val.get("size", 0)) or _hexstr_width(res.value) or 4
            if number.bit_length() > size * 8 or number < 0:
                # truncating what somebody typed while watching a meter
                # writes a different value than they read back
                return "error", (f"adjust {index}:{sub}: {number} does not fit "
                                 f"in {size} byte(s)")
            wres = await asyncio.to_thread(bus.sdo_write, node_, index, sub,
                                           f"0x{number:0{size * 2}X}")
            if not wres.ok:
                return "fail", f"adjust {index}:{sub} write abort {wres.abort}"
            regs["R0"] = number
            return "ok", ""
        return "error", f"unknown step {key!r}"

    # -- SWDL ---------------------------------------------------------------------
    def act_swdl_fw(self, p: dict) -> None:
        self.fw_sel = p["ver"]

    def act_swdl_mode(self, p: dict) -> None:
        self.swdl_mode = p["mode"]

    def act_swdl_start(self, p: dict) -> None:
        if self.swdl_run or not self.sel_devices:
            return
        if self.adapter != "demo" and isinstance(self._swdl, SimSwdlStrategy):
            # never fake-flash real hardware: the simulation is demo-only
            self.log("SWDL no vendor download protocol installed — firmware "
                     "update needs a vendor extension package", "emcy0")
            return
        self._swdl.start(self)

    # -- trace --------------------------------------------------------------------
    def act_trace_toggle(self, p: dict) -> None:
        self.trace_paused = not self.trace_paused
        if not self.trace_paused:
            self.trace_loaded = None  # resuming: live frames append, no longer "the capture"

    def act_trace_clear(self, p: dict) -> None:
        self.trace = []
        self._trace_counts = {}
        self.trace_loaded = None
        # statistics restart with the cleared buffer; err_frames stays on
        # its connect lifecycle — it tracks bus health, not the buffer
        self._cob_stats = {}
        self._rate_win.clear()
        self._stats_t0 = 0.0

    def act_trace_filter(self, p: dict) -> None:
        self.trace_hide = set(p.get("hide", [])) & set(TRACE_CLASSES)

    def act_trace_devfilter(self, p: dict) -> None:
        self.trace_dev_filter = not self.trace_dev_filter

    def _refresh_trace_saved(self) -> None:
        if not self.trace_dir.is_dir():
            self._trace_saved = []
            return
        files = sorted(self.trace_dir.glob("*.json"), reverse=True)  # name = timestamp → newest first
        self._trace_saved = [{"file": f.name, "size": f.stat().st_size} for f in files]

    def act_trace_save(self, p: dict) -> None:
        if not self.trace:
            return
        name = datetime.now().strftime("trace_%Y%m%d_%H%M%S") + f"_{len(self.trace)}f.json"
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        (self.trace_dir / name).write_text(
            json.dumps({"v": 1, "rows": self.trace}, separators=(",", ":")), encoding="utf-8")
        self._refresh_trace_saved()
        self.log(f"TRACE {len(self.trace)} frames saved → {name}")

    def act_trace_load(self, p: dict) -> None:
        name = Path(p["file"]).name  # basename only: no path traversal out of trace_dir
        try:
            rows = json.loads((self.trace_dir / name).read_text(encoding="utf-8"))["rows"]
            if not isinstance(rows, list):
                raise ValueError("rows is not a list")
        except (OSError, ValueError, KeyError) as exc:
            self.log(f"TRACE load failed — {name}: {exc}", "emcy0")
            return
        self._activate_trace_rows(rows, name)
        self.log(f"TRACE {len(self.trace)} frames loaded from {name} — paused")

    _IMPORT_FORMATS = ("candump",)

    def act_trace_import(self, p: dict) -> None:
        """Import a foreign capture file (currently: SocketCAN `candump -l`
        logs) — parsed frames run through the normal decode pipeline
        (non-live: no state-log/plot side effects for historical data,
        see `_annotate_*`), then get saved as an ordinary capture and
        loaded live, exactly like `act_trace_load` — an import behaves
        like any other saved capture from that point on."""
        fmt = p.get("fmt", "candump")
        filename = Path(str(p.get("filename") or "capture")).name
        if fmt not in self._IMPORT_FORMATS:
            self.log(f"TRACE import failed — unsupported format {fmt!r}", "emcy0")
            return
        try:
            text = base64.b64decode(p["data"]).decode("utf-8", errors="replace")
        except Exception as exc:
            self.log(f"TRACE import failed — {filename}: {exc}", "emcy0")
            return
        frames, skipped = parse_candump(text)
        if not frames:
            self.log(f"TRACE import failed — no recognized candump frames in {filename}", "emcy0")
            return
        rows = []
        for rel, cob_id, frame_data in frames:
            dec = _decode_cob(cob_id)
            cob = f"0x{cob_id:03X}"
            row = {"time": _seconds_to_trace_time(rel), "dir": "RX", "cob": cob,
                   "len": str(len(frame_data)),
                   "data": " ".join(f"{b:02X}" for b in frame_data),
                   "dec": dec, "flag": "", "cls": trace_class(dec), "node": trace_node(cob),
                   "obj": "", "val": ""}
            self._annotate_sdo(row, live=False)
            self._annotate_pdo(row, live=False)
            self._annotate_emcy(row, live=False)
            self._annotate_plugin(row)
            rows.append(row)
        name = datetime.now().strftime("import_%Y%m%d_%H%M%S") + f"_{len(rows)}f.json"
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        (self.trace_dir / name).write_text(
            json.dumps({"v": 1, "rows": rows}, separators=(",", ":")), encoding="utf-8")
        self._refresh_trace_saved()
        self._activate_trace_rows(rows, name)
        skip_note = f", {skipped} unrecognized line(s) skipped" if skipped else ""
        self.log(f"TRACE {len(rows)} frames imported from {filename} ({fmt}) → saved as {name}{skip_note}")

    def _activate_trace_rows(self, rows: list[dict], name: str) -> None:
        """Common tail of load/import: install `rows` as the live (paused)
        trace view and recompute the class/node counters."""
        self.trace = rows[-TRACE_CAP:]
        self._trace_counts = {}
        for row in self.trace:
            row.setdefault("cls", trace_class(row.get("dec", "")))
            row.setdefault("node", trace_node(row.get("cob", "")))
            key = (row["cls"], row["node"])
            self._trace_counts[key] = self._trace_counts.get(key, 0) + 1
        self.trace_paused = True
        self.trace_loaded = name

    def act_trace_del_saved(self, p: dict) -> None:
        name = Path(p["file"]).name
        try:
            (self.trace_dir / name).unlink()
        except OSError:
            pass
        self._refresh_trace_saved()
        if self.trace_loaded == name:
            self.trace_loaded = None
        self.log(f"TRACE capture {name} deleted")

    def _trace_filter_predicate(self) -> Callable[[str, int | None], bool]:
        """The trace/device filter as a (cls, node) -> bool predicate,
        shared between the live snapshot (capped scrollback) and export
        (the full matching set) so the two can never drift apart."""
        hide = self.trace_hide
        sel_nodes = ({d["node"] for d in self.sel_devices}
                     if self.trace_dev_filter else None)

        def passes(cls: str, node: int | None) -> bool:
            if cls in hide:
                return False
            return sel_nodes is None or node is None or node in sel_nodes

        return passes

    def _trace_snapshot(self) -> dict:
        """Last TRACE_VIEW rows *matching the filters* — scanned from the end
        of the full retained buffer, so hidden classes or devices don't push
        visible frames out of the window. The device filter never hides
        broadcast frames (node None: NMT, SYNC, …)."""
        passes = self._trace_filter_predicate()
        if self.trace_hide or self.trace_dev_filter:
            rows: list[dict] = []
            for row in reversed(self.trace):
                if passes(row["cls"], row["node"]):
                    rows.append(row)
                    if len(rows) == TRACE_VIEW:
                        break
            rows.reverse()
            match = sum(n for (cls, node), n in self._trace_counts.items()
                        if passes(cls, node))
        else:
            rows = self.trace[-TRACE_VIEW:]
            match = len(self.trace)
        return {"rows": rows, "paused": self.trace_paused, "hide": sorted(self.trace_hide),
                "devSel": self.trace_dev_filter,
                "total": len(self.trace), "match": match,
                "saved": self._trace_saved, "loaded": self.trace_loaded,
                "stats": self._trace_stats(),
                "plot": {"sel": self.plot_sel,
                        "series": {k: list(v) for k, v in self.plot_series.items()}}}

    def _export_trace_rows(self) -> list[dict]:
        """The full filtered trace — same predicate as `_trace_snapshot`,
        but not capped to TRACE_VIEW: export formats hand over everything
        that matches the current filter, not just the browser's scrollback."""
        if not (self.trace_hide or self.trace_dev_filter):
            return list(self.trace)
        passes = self._trace_filter_predicate()
        return [row for row in self.trace if passes(row["cls"], row["node"])]

    def _trace_csv(self) -> str:
        """The filtered trace as CSV — one row per frame, same columns the
        Trace table shows plus the raw node-id."""
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time", "dir", "cob", "len", "data", "dec", "flag", "node", "obj", "val"])
        for row in self._export_trace_rows():
            w.writerow([row["time"], row["dir"], row["cob"], row["len"], row["data"],
                        row["dec"], row["flag"], row["node"] if row["node"] is not None else "",
                        row["obj"], row["val"]])
        return buf.getvalue()

    def _trace_candump(self) -> str:
        """The filtered trace as a SocketCAN `candump -l` log
        ("(seconds) interface ID#DATA"). Timestamps are relative to the
        first exported frame — trace rows only keep time-of-day, not the
        original capture date, so a real epoch can't be reconstructed for
        a reloaded or imported capture."""
        lines: list[str] = []
        t0: float | None = None
        for row in self._export_trace_rows():
            t = _trace_time_to_seconds(row["time"])
            if t0 is None:
                t0 = t if t is not None else 0.0
            rel = (t - t0) if t is not None else 0.0
            if rel < 0:
                rel += 86400  # crossed midnight
            cob = row["cob"].removeprefix("0x") or "0"
            frame_data = row["data"].replace(" ", "")
            lines.append(f"({rel:.6f}) can0 {cob}#{frame_data}")
        return "\n".join(lines) + ("\n" if lines else "")

    _STATS_TOP = 40  # COB rows shipped per snapshot; the rest is aggregated

    def _trace_stats(self) -> dict:
        """Statistics view: cumulative per-COB counters (since connect or
        trace clear), frames/s over the last ~5 s, per-class totals, the
        bus-load history and the error-frame counter."""
        now = time.monotonic()
        rate_span = (now - self._rate_win[0][0]
                     if len(self._rate_win) > 1 else TICK_S)
        rates: dict[str, int] = {}
        for _, counts in self._rate_win:
            for cob, n in counts.items():
                rates[cob] = rates.get(cob, 0) + n
        ordered = sorted(self._cob_stats.items(),
                         key=lambda kv: (-kv[1]["n"], kv[0]))
        top = [{"cob": cob, "dec": st["dec"], "cls": st["cls"], "n": st["n"],
                "rate": round(rates.get(cob, 0) / max(rate_span, TICK_S), 1)}
               for cob, st in ordered[:self._STATS_TOP]]
        rest = ordered[self._STATS_TOP:]
        classes: dict[str, int] = {}
        for _, st in ordered:
            key = st["cls"] or "other"
            classes[key] = classes.get(key, 0) + st["n"]
        return {"cobs": top,
                "restN": sum(st["n"] for _, st in rest), "restCobs": len(rest),
                "classes": classes,
                "total": sum(st["n"] for _, st in ordered),
                "rate": round(sum(rates.values()) / max(rate_span, TICK_S), 1),
                "span": round(now - self._stats_t0, 1) if self._stats_t0 else 0.0,
                "loadHist": list(self._load_hist), "err": self.err_frames}

    def act_emcy_ack(self, p: dict) -> None:
        self.emcy_new = 0

    # -- object catalog ---------------------------------------------------
    def _catalog_from_eds(self, file: str) -> tuple[dict, list] | str:
        """Build the Objects-page catalog from a real EDS file on disk.

        Returns (catalog, groups), or an error text when the file is missing
        or unparseable — the Objects page shows that instead of staying
        silently empty. Cached per file mtime, parse failures included:
        snapshot() runs every tick.
        """
        path = self.db.eds_dir / file
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return f'EDS file "{file}" is missing from the workspace — upload it again in Setup'
        cached = self._catalog_cache.get(file)
        if cached and cached[0] == mtime:
            return cached[1]

        try:
            od = import_eds(str(path), None)
        except Exception as exc:
            err = f'EDS "{file}" could not be parsed — {exc}'
            self._catalog_cache[file] = (mtime, err)
            return err

        group_defs = [("comm", "Communication", 0x1000, 0x1FFF),
                      ("manu", "Manufacturer", 0x2000, 0x5FFF),
                      ("profile", "Device profile", 0x6000, 0x9FFF)]
        catalog: dict[str, list] = {k: [] for k, _, _, _ in group_defs}

        def add_row(var: ODVariable) -> None:
            for key, _, lo, hi in group_defs:
                if lo <= var.index <= hi:
                    default = var.value if var.value is not None else var.default
                    if default is None:
                        val = "—"
                    elif isinstance(default, str):
                        val = default
                    elif isinstance(default, float):
                        val = str(default)
                    else:
                        width = max(len(var) // 8, 1) * 2
                        val = f"0x{int(default) & ((1 << (len(var) or 8)) - 1):0{width}X}"
                    acc = var.access_type if var.access_type in ("ro", "rw", "wo") else \
                        ("ro" if not var.writable else "rw")
                    catalog[key].append([
                        f"0x{var.index:04X}", f"{var.subindex:02X}", var.qualname,
                        _TYPE_NAMES.get(var.data_type, f"0x{var.data_type:02X}"), acc, val,
                        var.min, var.max,
                    ])
                    return

        for obj in od.values():
            if isinstance(obj, ODVariable):
                add_row(obj)
            else:  # record/array: emit each member
                for sub in sorted(getattr(obj, "subindices", {})):
                    member = obj.subindices[sub]
                    if isinstance(member, ODVariable):
                        add_row(member)

        groups = [{"key": k, "label": lbl, "range": f"0x{lo:04X}–0x{hi:04X}",
                   "count": len(catalog[k])} for k, lbl, lo, hi in group_defs]
        self._catalog_cache[file] = (mtime, (catalog, groups))
        return catalog, groups

    def _object_catalog(self) -> tuple[dict, list, str]:
        """Catalog of the selected device's EDS plus a hint that explains an
        empty catalog — no invented placeholder objects."""
        sel = self.sel_devices
        if not sel:
            return {}, [], ("select a device in the Devices box and a matching "
                            "EDS file in Setup to browse its object dictionary")
        eds = sel[0]["eds"]
        if eds in ("", "—"):
            return {}, [], (f"node {sel[0]['node']:02d} has no EDS file assigned — enable a "
                            "matching EDS file in Setup (it is assigned automatically once "
                            "its identity matches), or pick one via the device's ⋮ menu")
        built = self._catalog_from_eds(eds)
        if isinstance(built, str):
            return {}, [], built
        return built[0], built[1], ""

    def _load_symbols(self) -> SymbolTables:
        """Seed each plugin's packaged headers into ``<workspace>/symbols/
        <plugin>/`` (never overwriting — the operator's copy is the firmware
        under test), then parse everything found there.

        Origins are per plugin directory, so two vendors' identically
        named tables stay apart instead of the winner depending on file
        order.
        """
        for plugin in self.plugins:
            for src_dir in plugin.symbol_dirs():
                dst_dir = self.symbols_dir / plugin.name
                dst_dir.mkdir(parents=True, exist_ok=True)
                for src in sorted(Path(src_dir).glob("*.h")):
                    dst = dst_dir / src.name
                    if not dst.exists():
                        shutil.copy2(src, dst)
        origins = [(d.name, d) for d in sorted(self.symbols_dir.glob("*"))
                   if d.is_dir()] if self.symbols_dir.is_dir() else []
        tables = load_symbols(origins)
        if tables.by_name:
            self.log(f"SYM  {len(tables.by_name)} symbols in {len(tables.tables)} tables "
                     f"from {', '.join(o for o, _ in origins)}")
        for err in tables.errors:
            self.log(f"SYM  {err}", "emcy0")
        return tables

    def act_num_base(self, p: dict) -> None:
        """Hex or dec for every value shown. Persisted, because it is a
        reading habit rather than a per-session choice."""
        self.num_base = "dec" if self.num_base == "hex" else "hex"
        self.db.set("num_base", self.num_base)

    def act_symbols_reload(self, p: dict) -> None:
        """Re-parse the workspace symbol directory, so dropping in the
        headers of a newer firmware does not need a restart."""
        self.symbols = self._load_symbols()
        self._load_testcases()

    def _value_view(self, catalog: dict) -> dict[str, dict]:
        """Per object key: the number in the chosen base, every reading of
        it for the tooltip, and the symbolic one where a plugin declared
        fields for it. Built here rather than in the frontend so parsing
        and formatting have exactly one home."""
        keys: dict[str, tuple[int, str]] = {}
        for rows in catalog.values():
            for row in rows:
                default = str(row[5])
                keys[f"{row[0]}:{row[1]}"] = (max(2, len(default.removeprefix("0x"))),
                                              default)
        for key in self.obj_vals:
            keys.setdefault(key, (2, ""))

        out: dict[str, dict] = {}
        for key, (width, default) in keys.items():
            # an object nobody has read yet still shows its EDS default, and
            # that has to follow the chosen base too — otherwise half the
            # table stays hex while the other half switches
            raw = self.obj_vals.get(key) or default
            if raw in (None, "", "—"):
                continue
            try:
                value = int(str(raw), 16)
            except ValueError:
                continue  # string-typed object: leave it exactly as it is
            fields = self._object_fields.get(key, [])
            out[key] = {"txt": format_number(value, self.num_base, width),
                        "alt": alternatives(value, fields, self.symbols, width),
                        "sym": describe(value, fields, self.symbols) if fields else ""}
        return out

    def _mirror_slots(self, eds: str) -> list[dict]:
        entry = next((e for e in self.db.eds_list() if e["file"] == eds), None)
        return entry["display_slots"] if entry else []

    def _mirror_data(self) -> dict | None:
        """Sidebar display-mirror panel for the selected device — only when
        its EDS declares slots (db.eds_set_display); no generic readout
        invented for devices that don't define one, since the panel mimics
        one specific machine family's own front-panel LCD.

        Values come from the same obj_vals cache Objects-page reads/writes
        already populate (falling back to the EDS default when never
        read), so writing the backing object anywhere updates the mirror
        too. act_mirror_refresh performs the actual SDO read into that
        cache — there is no periodic polling here.
        """
        sel = self.sel_devices
        if not sel:
            return None
        slots = self._mirror_slots(sel[0]["eds"])
        if not slots:
            return None
        od = self._ods.load(sel[0]["eds"])
        values = []
        for slot in slots:
            hexval = self.obj_vals.get(f"{slot['idx']}:{slot['sub']}")
            if hexval is None:
                var = find_var(od, int(slot["idx"], 16), int(slot["sub"] or "0", 16)) if od else None
                default = (var.value if var and var.value is not None else var.default) if var else None
                num = default if isinstance(default, (int, float)) else None
            else:
                try:
                    num = int(hexval, 16)
                except ValueError:
                    num = None
            values.append({"label": slot["label"], "value": "—" if num is None else str(num)})
        return {"node": sel[0]["node"], "values": values}

    def _panel_data(self) -> list[dict]:
        """Plugin-contributed sidebar panels for the selected device.

        The core knows nothing about which devices these apply to — that
        is the panel's own matches(). Same on-demand rule as the display
        mirror above: render() reads caches, and the bus is only touched
        by the plugin actions the panel's buttons dispatch.
        """
        sel = self.sel_devices
        if not sel or not self._device_panels:
            return []
        dev = sel[0]
        eds = next((e for e in self.db.eds_list() if e["file"] == dev["eds"]), None)
        panels = []
        for key, panel in self._device_panels:
            if key in self._panels_broken:
                continue
            try:
                data = panel.render(self, dev) if panel.matches(dev, eds) else None
            except Exception as exc:  # a broken panel must not break the snapshot
                self._panels_broken.add(key)
                self.log(f'PLG  panel "{key}" failed — hidden for this session ({exc})',
                         "emcy0")
                continue
            if data:
                panels.append({"key": key, "title": panel.title, "node": dev["node"]} | data)
        return panels

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        sel = self.sel_devices
        first = sel[0] if sel else None
        demo = self.adapter == "demo"
        catalog, obj_groups, obj_hint = self._object_catalog()
        eds_files = self.db.eds_list()
        conflicts = self._eds_conflicts()
        winners = {ident: e["file"] for ident, e in self._eds_by_identity().items()}
        for e in eds_files:
            ident = normalize_identity(e["ident"])
            group = conflicts.get(ident, []) if e["enabled"] else []
            e["conflict"] = [f for f in group if f != e["file"]]
            e["conflictWin"] = bool(e["conflict"]) and winners.get(ident) == e["file"]
        return {
            "connected": self.connected,
            "version": VERSION,
            "workspace": self.workspace_name,
            "workspaces": {"list": self._workspace_names(),
                           "canSwitch": bool(self.workspaces_root and self.on_workspace_switch)},
            "busLoad": round(self.bus_load, 1),
            "errFrames": self.err_frames,
            "scanBusy": self.scan_busy,
            "adapter": self.adapter,
            "bitrate": self.bitrate,
            "ownNodeId": self.own_node_id,
            "scanRange": list(self.scan_range),
            "browse": self.browse,
            "adapters": self.adapter_cards,
            "ext": {
                "plugins": [p.name for p in self.plugins],
                "addressing": self.addressing.name if self.addressing else None,
                "canInstall": self.plugin_dir is not None,
                "installed": self._installed_plugin_packages(),
                "symbols": self.symbols.summary(),
            },
            "devices": self.devices,
            "logs": self.logs[-30:],
            "emcyNew": self.emcy_new,
            "eds": {"files": eds_files},
            "mc": self.mc | {"ref": self.mc_ref, "hbLost": sorted(self._hb_lost),
                             "teach": self.teach, "flows": self._flow_files()},
            "paths": self.paths | {"eds": str(self.db.eds_dir)},
            "objects": {
                "catalog": catalog,
                "groups": obj_groups,
                "vals": self.obj_vals,
                "hint": obj_hint,
                "base": self.num_base,
                "fmt": self._value_view(catalog),
            },
            "mirror": self._mirror_data(),
            "psu": self._psu_data(),
            "panels": self._panel_data(),
            "favorites": {
                "rows": self._fav_rows(),
                "lastDb": self.db.last_values_ts(first["sn"]) if first else None,
            },
            "raw": self.raw_rows,
            "sync": {"run": self.sync_run, "ms": self.sync_ms},
            "tests": {
                "catalog": ([[tc.id, tc.name, ", ".join(tc.tools) or "—",
                              tc.est or "—", bool(tc.error), tc.grade, tc.variants,
                              tc.file, tc.error or ""]
                             for tc in sorted(self.testcases.values(), key=lambda t: t.id)]
                            if self.testcases
                            else [list(t) + ["", [], "", ""] for t in data.TESTS]
                            if demo else []),
                #: what the two catalog filters can offer, from what is
                #: actually in the folder — an empty dropdown is better
                #: than one listing grades no case has
                "grades": sorted({tc.grade for tc in self.testcases.values() if tc.grade}),
                "variants": sorted({v for tc in self.testcases.values()
                                    for v in tc.variants}),
                "lastRes": data.LAST_RESULTS if not self.testcases and demo else {},
                "runProg": self.run_prog,
                "manual": self.manual_prompt,
                "sel": sorted(self.test_sel),
                "running": self.running,
                "runOrder": self.run_order,
                "runIdx": self.run_idx,
                "results": self.results,
                "stopOnErr": self.stop_on_err,
                "toolFilter": self.tool_filter,
                "repeatCase": self.repeat_case,
                "repeatRun": self.repeat_run,
                "fileCount": len(self.testcases),
                "reports": self.reports or (list(data.SEED_REPORTS) if demo else []),
                "overview": self.overview,
                "suites": sorted(self.suites),
                "activeSuite": self.active_suite,
            },
            "swdl": {
                "fw": self._plugin_fw + (list(data.FIRMWARE) if demo else []),
                "strategy": self._swdl.name,
                "vendor": not isinstance(self._swdl, SimSwdlStrategy),
                "sel": self.fw_sel,
                "mode": self.swdl_mode,
                "run": self.swdl_run,
                "done": self.swdl_done,
                "prog": {str(k): round(v) for k, v in self.swdl_prog.items()},
            },
            "trace": self._trace_snapshot(),
        }
