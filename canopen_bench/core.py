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
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple, TextIO

from canopen.objectdictionary import ODVariable

from . import __version__, data, instruments
from . import report as reportlib
from . import testcases as tclib
from .bus.canopen_bus import CanopenBus, _decode_cob
from .bus.demo import EdsDemoBus
from .bus.interface import NO_SERIAL, BusInterface, SdoResult
from .db import Db
from .eds_od import (
    ObjectInfo,
    OdCache,
    eds_text,
    find_var,
    info_of,
    load_eds,
    object_info,
    pdo_mapping,
)
from .panelspec import load_panels
from .plugin import BenchPlugin, SwdlStrategy, load_plugins
from .symbols import SymbolTables, load_symbols
from .values import (
    BASES,
    Field,
    Quantity,
    ValueError_,
    alternatives,
    base_of,
    describe,
    format_number,
    parse_value,
)

VERSION = __version__  # single source: canopen_bench/__init__.py

# Inside the package on purpose, not at the repository root. Anything the
# running tool reads has to ship in the wheel, and only package data does:
# this file used to live in examples/, which pip does not install, so
# `pip install canopen-bench` produced a demo mode that scanned and found
# nothing at all. Keep it here, and keep seed/*.eds in package-data.
SEED_EDS = Path(__file__).resolve().parent / "seed" / "DemoDevice.eds"

#: The panels the core itself ships (canopen_bench/panelspec.py). Only for
#: devices the core can honestly describe — the demo device it also ships
#: the EDS for. Everything else arrives through a plugin, which is where
#: the knowledge of a real device lives.
PANEL_DIR = Path(__file__).resolve().parent / "panels"

#: Communication objects only, for a device whose own EDS is not to hand —
#: the machine's own controllers, a foreign node that happens to sit on the
#: same bus. Registered *without* an identity, so a scan can never assign it:
#: what it describes is the standard, not this device, and a catalog that
#: claims to know a device nobody has described is worse than an empty one
#: with a reason (see _object_catalog, "no invented placeholder objects").
#: Assigned by hand through the device's ⋮ menu, where the name says what it
#: is. An object the device does not implement answers with an SDO abort,
#: which the bench shows — so the guess corrects itself at the first read.
BASE_EDS = Path(__file__).resolve().parent / "seed" / "CiA301Base.eds"

TICK_S = 0.8
SCAN_DELAY_S = 1.1
#: how far back a `wait_for` on a COB-ID looks in the trace. It looks back
#: at all because a device answers when it is ready, not when a step
#: happens to start listening, and the answer to the step before this one
#: can land while that step is still finishing.
#:
#: Deliberately not the step's timeout: a timeout says how long the case
#: will wait, which is a patience, while this says how old a frame may be
#: and still describe now, which is a property of the traffic. Tying the
#: two together made a `timeout: 0.5` look half a second back, far enough
#: to reach the run before it.
#:
#: Long enough to bridge a device that goes quiet for a moment — a
#: calibration can hold its threads for 150 to 180 ms, and what the PDOs
#: carry stops being updated for that long, so nothing triggers them and
#: the newest frame is simply old without being wrong.
#:
#: It can afford to be generous because it is not what keeps an old frame
#: from answering the wrong question. Two other things do: of a PDO only
#: the newest frame is asked, so a stale one counts exactly when nothing
#: has changed since — which is what "the state is still X" means — and
#: the window never reaches past the start of the case doing the waiting.
FRAME_LOOKBACK_S = 0.4
TRACE_CAP = 200_000  # ring buffer bound: ~120 MB of row dicts, ≈1 h at 55 frames/s
#: Autosave (`_autosave_write`) writes the record to disk as it arrives,
#: because TRACE_CAP is a ring: after an hour the beginning is gone, and an
#: hour is shorter than the runs where something odd happens once.
#:
#: A segment rolls over at this size so no single file grows past what an
#: editor — or this tool's own capture loader — will open, and each one is a
#: complete capture on its own.
AUTOSAVE_SEGMENT_BYTES = 64 * 1024 * 1024
#: How far back the autosaved record reaches. Two weeks, because that is
#: the length of an endurance run — the whole case for autosaving is the
#: fault that shows up once, days in, and a window shorter than the test
#: throws away the part nobody knew to keep. A connected bench writes
#: roughly 40 MB an hour, so a fortnight of one is on the order of 13 GB.
AUTOSAVE_KEEP_DAYS = 14
#: …unless the disk says otherwise. This is the free space autosave will
#: not eat into: below it the oldest segments give way early, before the
#: two weeks are up. A bench tool filling the volume it runs on is a worse
#: outcome than a shorter record. Every removal is logged — a gap in the
#: record must not be silent — and captures saved by hand are never
#: candidates: those are a decision, not a by-product.
AUTOSAVE_FREE_BYTES = 2 * 1024 * 1024 * 1024
#: How much gets written between two looks at the free space. Cheap enough
#: to be frequent, fine enough that the reserve cannot be overshot by more
#: than this before a segment is rolled early.
AUTOSAVE_SPACE_EVERY_BYTES = 4 * 1024 * 1024
#: How long autosave waits before trying again after it could not write.
#:
#: It waits rather than switches off, and this is the whole reason the
#: retry exists. An endurance run can go on for months; a recorder that
#: turned itself off the one night the disk was tight would be off for
#: every week after it, and the fault it was there to catch would arrive
#: to an empty folder. So a full disk, a read-only mount or a reserve that
#: cannot be met is a pause: the chip goes red with the reason, one line
#: goes to the state log, and the moment there is room again it writes on
#: and says so. Nothing here ever clears the switch the operator set.
AUTOSAVE_RETRY_S = 30.0
TRACE_VIEW = 400     # rows per snapshot to the browser — enough scrollback to
                     # follow a multi-step sequence (e.g. addressing) end to end
PLOT_SEL_MAX = 4     # concurrently plotted signals — keeps the chart legible
PLOT_POINTS = 600    # samples retained per plotted signal
TRACE_CLASSES = ("NMT", "SDO", "PDO", "EMCY", "HB")
NMT_LABEL = {"start": "start", "preop": "pre-op", "stop": "stop", "reset": "reset node"}
NMT_STATE = {"start": "Operational", "preop": "Pre-Operational", "stop": "Stopped",
             "reset": "Pre-Operational"}


def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def now_us_str() -> str:
    """Full µs resolution — trace rows carry 6 decimals, the UI decides
    whether to show ms or µs."""
    return datetime.now().strftime("%H:%M:%S.%f")


def _gb(n: int) -> str:
    """Bytes as GB, for the log lines about disk space. One decimal: the
    reserve is a round number of gigabytes and the reader is comparing
    against it, not counting bytes."""
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def _tod_seconds(stamp: str) -> float | None:
    """Trace timestamp "HH:MM:SS.ffffff" -> seconds since midnight, None if
    it does not parse. Rows carry a time of day rather than a date, so age
    is compared within the day and the caller handles the wrap."""
    try:
        h, m, s = stamp.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return None


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




def _subhex(value) -> str:
    """A sub-index, always two digits: 0x01, not 0x1.

    The width is what makes a column of them line up, and a sub-index is a
    byte whichever way the file wrote it — "1", 1 and "0x01" are the same
    address, and a report that renders them three ways invites the reader
    to wonder whether they are."""
    number = _as_int(value)
    return f"0x{number:02X}" if number is not None else str(value)


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


def _addr_int(value) -> int | None:
    """An object address — an index or a sub-index — as a number.

    Hexadecimal without asking, because that is the only way CANopen
    writes one: every EDS section, every catalog row and every test case
    spells sub-index eleven "0B".

    `_as_int` cannot answer this and should not try. It reads *values*,
    where a leading zero followed by a letter names a base — "0b1100" is
    binary and has to be, or a written value quietly means something
    else. Addresses have no such notation, and that rule swallowed 0A
    through 0F whole: none of the six names a base, so each came back as
    None, and a lookup comparing None against None matched the first row
    that also failed to parse. Every favourite past sub-index nine
    answered to the name of 0x0A.
    """
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip(), 16)
    except (ValueError, TypeError):
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


#: The steps a case spends on itself: labels, jumps, register arithmetic,
#: and reading its own registers back out. Nothing of this goes out on the
#: bus, and the report marks them as one kind (report.py, testStepFlow) so
#: that a loop can be read as a loop — which of its lines repeat, and how
#: often.
_FLOW_KEYS = (tclib._ARITH | tclib._COND_JUMPS
              | {"label", "jump", "jump_on_error", "rand", "dump_registers",
                 "loop", "loop_end", "loop_break"})


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
        return f"read {_hexstr(val['index'])}:{_subhex(val['sub'])}"
    if key == "sdo_write":
        return f"write {_hexstr(val['index'])}:{_subhex(val['sub'])} = {val['value']}"
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
    if key == "loop":
        count = val.get("n") if isinstance(val, dict) else val
        return f"LoopBegin {count}"
    if key == "loop_end":
        return "LoopEnd"          # the executor appends how many turns are left
    if key == "loop_break":
        return "LoopBreak"
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
    if key == "dump_registers":
        return "dump registers"
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


class Emcy(NamedTuple):
    """One EMCY as the record kept it. ``at`` is monotonic, and only the
    bench's own bookkeeping reads it — a case asks about the frame."""
    node: int
    code: int
    reg: int
    mfr: bytes
    at: float = 0.0


def _emcy_str(entry: Emcy) -> str:
    """One recorded EMCY, in the terms a case is written in."""
    return (f"0x{entry.code:04X} reg 0x{entry.reg:02X} "
            f"mec 0x{_mec(entry.mfr):04X} from node {entry.node:02d}")


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


def _out_of_range(value: int, lo: object, hi: object) -> bool:
    """Whether a value falls outside the limits the EDS states, which is
    the only place a bench can learn them from. Limits it does not state
    are no limit — most objects state none, and a missing LowLimit must
    not read as zero."""
    try:
        return (lo is not None and value < int(lo)) or (hi is not None and value > int(hi))
    except (TypeError, ValueError):
        return False


def _in_base_of(value: object, like: object) -> str:
    """The answer written in the base the expectation was written in.

    A case asks in the base it wants to read back: `expect: 30` means the
    answer belongs in decimal, `expect: "0x1E"` means hex. The old tool
    worked that way and the cases were written against it — a tension in
    counts is unreadable as hex, and a screen id is unreadable as anything
    else. Without an expectation there is nothing to go on, so the answer
    stays as the device sent it.
    """
    number = _as_int(value)
    if number is None or like is None or isinstance(like, bool):
        return str(value)
    if isinstance(like, int):            # a YAML integer: decimal was meant
        return str(number)
    spelling = str(like).strip()[:2].lower()
    if spelling == "0b":
        return f"0b{number:b}"
    return str(value)                    # hex, and that is how it arrived


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


def _frame_bytes(val: dict, regs: dict, builtins: dict) -> bytes | None:
    """The payload a ``can_send`` puts on the wire, or None when it cannot
    be built yet.

    Same reason as ``_write_value``: the step line and the bus have to
    come from one place. A frame's bytes are the whole content of the
    step — the label only names a COB-ID — so a report that worked them
    out separately could show a frame that was never sent.

    None means the data needs an addressing provider that is not
    installed; the executor turns that into the failure, and the step
    line stays as it was written.
    """
    data = val["data"]
    session = builtins.get("session")
    if data == "$session":
        return bytes(session) if session is not None else None
    buf = bytearray()
    for item in data:
        if item == "$session":       # expands to the session identity bytes
            if session is None:
                return None
            buf += session
        else:
            buf.append(_resolve(item, regs, builtins) & 0xFF)
    return bytes(buf)


def _frame_text(data: bytes) -> str:
    """Payload bytes as the trace writes them — same spacing, same case,
    same order — so a report line and a trace row can be read against
    each other without transcribing either."""
    return " ".join(f"{b:02X}" for b in data)


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
        # the note goes into the state log rather than only into logging:
        # the one thing it reports — the same package installed twice — is
        # read as "my change did not arrive", and nobody debugging that
        # looks at a console the bench was not started from
        self.plugins = (load_plugins(note=lambda msg: self.log(msg, "emcy0"))
                        if plugins is None else list(plugins))
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
        # Object-page panels (panelspec.py): plugin files first, the core's
        # own last, so a vendor panel takes its devices from the general
        # one rather than competing with it. Read from the packages, never
        # copied into the workspace.
        self._obj_panels = load_panels(
            [path for p in self.plugins for path in p.object_panels()] + [PANEL_DIR],
            log=lambda msg: self.log(msg, "emcy0"))
        # how to read an object's value symbolically, keyed "0x2007:09"
        self._object_fields: dict[str, list[Field]] = {}
        for p in self.plugins:
            self._object_fields.update(p.object_fields(self.symbols))
        # and what it means physically — the one thing about an object that
        # no EDS answers, so the only source is the device's documentation
        # by way of a plugin. Same key, and used wherever a value is shown
        self._object_units: dict[str, Quantity] = {}
        for p in self.plugins:
            self._object_units.update(p.object_units(self.symbols))
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
        #: what the plugins' headers call an address (see _symbol_label).
        #: Asked per trace frame and per table row per tick, and answered
        #: from the symbol tables alone — so it is worked out once
        self._sym_labels: dict[tuple[str, str], str] = {}
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
        # which channel of which adapter to open, adapter key -> channel;
        # empty means the backend's default. What the driver reports as
        # attached is fetched on demand (act_detect_channels), never on a
        # snapshot — enumeration talks to the driver.
        self.channels: dict[str, str] = db.get("channels", {})
        self.channel_list: dict = {"adapter": "", "rows": []}
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
        self.emcy_seen: deque[Emcy] = deque(maxlen=200)
        self.obj_vals: dict[str, str] = {}
        #: when each obj_vals entry was learned (monotonic), so a value the
        #: bus carried past can be told from an older one somebody read
        self.obj_vals_at: dict[str, float] = {}
        #: values seen on the wire, (node, "0x2007:01") -> (value, when).
        #: Separate from obj_vals, which also holds what the operator has
        #: typed and not yet written (see _bus_sample)
        self.seen_vals: dict[tuple[int, str], tuple[str, float]] = {}
        # bench instruments beside the bus (canopen_bench/instruments): the
        # port that once answered is remembered, so a restart reconnects to
        # that one instead of writing *IDN? to every serial port it finds
        self.psu: instruments.PowerSupply | None = None
        self.psu_error = ""
        self._psu_state: instruments.SupplyState | None = None
        self._psu_opener = None            # tests inject a fake serial port
        self._psu_ports = None
        #: whether the supply also gets a box in the sidebar. A run that has
        #: to watch the voltage should not have to leave the page it is
        #: watching to do it — but a bench with no supply in play does not
        #: want the room taken, so it is a choice and it is remembered.
        self.psu_sidebar = bool(db.get("psu_sidebar", False))
        self._psu_connect(str(db.get("psu_port") or ""), announce=False)
        self.test_sel: set[str] = set()   # demo seeds below, once the catalog is known
        self.running = False
        self.run_order: list[str] = []
        self.run_idx = 0
        # what the run writes into the results folder at the end
        self._run_cases: list[reportlib.CaseRecord] = []
        self._run_record: reportlib.CaseRecord | None = None
        self._run_started = ""
        #: monotonic start of the case or flow now running — the floor for
        #: how far a `wait_for` looks back (0.0 = nothing running, no floor)
        self._sequence_started_at = 0.0
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
        #: The record of what the bus carried. Fills whenever the interface
        #: is connected and is never emptied or replaced by the trace panel:
        #: test steps read it (`wait_for` with a `cob`), so a frame missing
        #: from it is a step that cannot pass.
        self.trace: list[dict] = []
        self.trace_paused = False  # freezes the *view*; recording continues
        self.trace_hide: set[str] = set()  # classes hidden by the trace filter
        self.trace_dev_filter = False  # True = only frames of the selected devices
        self._trace_counts: dict[tuple[str, int | None], int] = {}  # rows per (class, node)
        #: What stands between the panel and the live record, as
        #: (rows, counts): an opened capture file — a second source, not a
        #: halted first one — or a pause, which is the live rows held still
        #: while the record goes on filling underneath.
        self._trace_import: tuple[list[dict], dict] | None = None
        self._trace_freeze: tuple[list[dict], dict] | None = None
        self._tick_rows: list[dict] = []  # drained since the last tick, for the statistics
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
        # autosave: every recorded frame appended to a capture file as it
        # arrives. The setting persists (a bench that wants its record kept
        # wants it kept tomorrow too); the open segment does not — it starts
        # on the first frame after a start, so a tool left offline writes
        # nothing.
        self.trace_autosave: bool = bool(db.get("trace_autosave", False))
        self._autosave_fh: TextIO | None = None
        self._autosave_name: str | None = None
        self._autosave_bytes = 0
        self._autosave_checked = 0  # bytes written at the last free-space look
        self._autosave_warn = ""    # why it is not writing right now ("" = it is)
        self._autosave_retry_at = 0.0  # monotonic: no attempt before this
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
        # object area: the numeric table, or the panel a plugin describes
        # for this device. Both a habit rather than a per-session choice,
        # so both are remembered — including which boxes are folded away,
        # keyed "<panel>/<group title>"
        self.obj_view: str = db.get("obj_view", "table")
        self.panel_open: dict[str, bool] = db.get("panel_open", {})
        self.panel_busy = False  # a box- or page-wide read is walking the objects
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

        # the files first, then the rows that name them — a row whose EDS is
        # not in the folder matches no identity and reads as a broken install
        self._seed_plugin_eds()
        if not db.eds_count(devices_only=True):
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
        # after the seeding above, which only runs on an empty registry: this
        # row would otherwise make the registry non-empty and swallow it
        self._seed_base_eds()

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
        """Server is stopping: close the bus connection like a disconnect,
        and let go of the supply's serial port.

        A port this process still holds is one the next start cannot have,
        and the search that then finds nothing says "no known power supply
        found" — which reads as a hardware fault and sends the reader to
        the cable. Closed, not released: ``psu_port`` stays in the
        database, so the next start finds the same supply again.
        """
        if self.connected:
            self.connected = False
            self.bus.disconnect()
            self.log("BUS  disconnected — server shutdown")
        self._autosave_close("closed")
        if self.psu is not None:
            try:
                self.psu.close()
            except Exception as exc:  # noqa: BLE001 — nothing left to save it for
                self.log(f"PSU  port not closed cleanly — {exc}", "emcy0")
            self.psu, self._psu_state = None, None

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
        self._autosave_close("bus lost")
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

    def _adapter_info(self, adapter: str = "") -> dict:
        want = adapter or self.adapter
        return next((a for a in self.adapter_cards if a["key"] == want),
                    {"key": want, "full": want, "label": want})

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

    def _drain_frames(self, max_frames: int = 4096) -> None:
        """Move what the interface has received into the trace.

        The tick calls this, and so does any test step waiting for a frame:
        the record has to be current when it is read, not up to one tick
        stale. Draining twice is harmless — whoever gets there first empties
        the queue, and the rows land in the trace exactly once either way.

        Recording does not ask whether the trace panel is paused. A pause
        belongs to the view (`_trace_view`); the record underneath is what
        `wait_for` and the statistics read, and a gap in it cannot be
        recovered afterwards.
        """
        frames = self.bus.poll_frames(max_frames)
        if not frames:
            return
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
        self.trace += rows
        self._tick_rows += rows
        if self.trace_autosave:
            # after annotation, so a row on disk carries the same decode as
            # the one on screen, and before the ring is trimmed
            self._autosave_write(rows)
        cut = len(self.trace) - TRACE_CAP
        if cut > 0:
            for old in self.trace[:cut]:
                self._trace_counts[(old["cls"], old["node"])] -= 1
            del self.trace[:cut]

    async def _tick_once(self) -> None:
        dirty = False
        if self.connected:
            self._drain_frames()
            # also with zero rows: load, rate window and history must
            # decay on an idle bus instead of freezing at the last value.
            # Rows a waiting test step drained are counted here, not there,
            # so who did the draining cannot skew the load figures.
            rows, self._tick_rows = self._tick_rows, []
            self._update_bus_stats(rows)
            self._check_heartbeats()
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
        cases = [c for c in self._run_cases if c.id in order]
        run = reportlib.RunRecord(
            started=self._run_started or datetime.now().isoformat(timespec="seconds"),
            finished=datetime.now().isoformat(timespec="seconds"),
            user=_bench_user(), workspace=self.workspace_name,
            tool=f"canopen-bench {__version__}", cases=cases)
        name = self._write_report(run)
        if cases:
            # counted over the same records the summary counts, so the two
            # cannot disagree. Counting the run's ids against the verdict
            # each of them left behind could: a case repeated 99 times is
            # 99 entries in `order` sharing one entry in `results`, so the
            # last run decided all 99 — 99/99 next to a summary that said
            # 70 pass, 29 fail
            passed, total = sum(1 for c in cases if c.verdict == reportlib.PASS), len(cases)
            # green once the run has something green to show and nothing
            # red: a skipped case did not fail, so it must not turn the
            # entry red — but a run that only skipped has demonstrated
            # nothing and does not get to look like a pass either. The
            # score counts every run, so "48/50" in green says by itself
            # that two of them did not apply.
            ok = run.verdict == reportlib.PASS
        else:
            # the demo catalog runs through the tick loop and leaves no case
            # records behind (data.TESTS, _run_step), so its score is the
            # one thing there is: a verdict per id
            passed, total = sum(1 for tid in order if self.results.get(tid) == "PASS"), len(order)
            ok = passed == total
        # `file` is what the UI links to, and only a run that really wrote
        # one has it — the demo's example entries name files that were
        # never on any disk, and a link to a 404 is worse than plain text
        self.reports = [{"name": name, "file": name,
                         "score": f"{passed}/{total}", "ok": ok}] + self.reports[:4]

    def _write_report(self, run: reportlib.RunRecord) -> str:
        """One file per case, one summary, one JSON beside it. Returns the
        summary's file name — that is what the UI links to."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = Path(self.paths.get("res") or (self.db.path.parent / "results"))
        try:
            folder.mkdir(parents=True, exist_ok=True)
            reportlib.write_stylesheet(folder)
            # a repeated case runs many times and each run is its own file:
            # without the number they all carried one name, so every run
            # after the first overwrote the one before it and all of the
            # summary's rows — the failed ones too — linked to whichever
            # ran last. Numbered only when the case did repeat, so an
            # ordinary run's file names stay what they were.
            runs_of = Counter(c.id for c in run.cases)
            nth: Counter[str] = Counter()
            for case in run.cases:
                name = f"{stamp}__{case.id}__{_slug(case.name)}"
                if runs_of[case.id] > 1:
                    nth[case.id] += 1
                    name += f"__{nth[case.id]:03d}"
                case.file = f"{name}.html"
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

        A raw frame gets its payload for the same reason, and always: the
        label is a COB-ID and nothing else, so without the bytes the step
        line says a frame went out and never which one.
        """
        ext = self._step_types.get(key)
        if ext:
            return ext.label(val)
        text = _step_text(key, val)
        if key == "can_send" and regs is not None and isinstance(val, dict):
            data = _frame_bytes(val, regs, builtins or {})
            if data:
                text += f" = {_frame_text(data)}"
        if key == "loop" and regs is not None:
            # a count the case worked out is a register in the file, and
            # "LoopBegin R11" says nothing about how long this run turned
            count = val.get("n") if isinstance(val, dict) else val
            if isinstance(count, str):
                text += f" = {_resolve(count, regs, builtins or {})}"
        if key == "sdo_write" and regs is not None and isinstance(val, dict):
            actual = _write_value(val, regs, builtins or {})
            # only when it says something the value in the line does not —
            # a literal is already there, in whatever width it was written
            if _as_int(actual) != _as_int(val["value"]):
                text += f" = {actual}"
        if key in ("sdo_read", "sdo_write", "adjust") and isinstance(val, dict):
            idx, sub = _hexstr(val["index"]), _subhex(val["sub"])
            # the firmware's own name wins where a plugin can give one: the
            # case was written against the headers, and its author is who
            # this line is for. The EDS name stands in when none can be
            name = self._label(idx, sub, self._object_label(idx, sub))
            if name:
                text += f"  ({name})"
        return text

    def _symbol_label(self, idx: str, sub: str) -> str:
        """What a plugin's headers call this object, or "" — first answer
        wins, and a plugin that raises is one that does not get to stop a
        run over a label.

        Memoised, because the answer depends on the loaded symbol tables
        and on nothing else: the trace asks it per frame, and the object
        table per row per tick. ``act_symbols_reload`` empties the memo,
        which is the only thing that can change an answer.
        """
        hit = self._sym_labels.get((idx, sub))
        if hit is not None:
            return hit
        found = ""
        for plugin in self.plugins:
            try:
                name = plugin.describe_object(idx, sub, self.symbols)
            except Exception:
                continue
            if name:
                found = name
                break
        self._sym_labels[(idx, sub)] = found
        return found

    def _label(self, idx: str, sub: str, eds_name: str = "") -> str:
        """What to call this object, wherever it is shown.

        The firmware's own name wins where a plugin can give one, and the
        EDS's stands in when none can. Object dictionaries are historical
        documents — a name in one was right when it was written and has
        been carried forward ever since — while the headers the firmware
        is built from are what its authors actually call the thing today.

        One rule, in the report line, the object table, the favourites,
        the signal plot and the trace alike. It used to hold in the
        report only, so the same object answered to two names on one
        screen depending on which box you were looking at.
        """
        return self._symbol_label(idx, sub) or eds_name

    def _value_note(self, idx: str, sub: str, value: object, like: object = None) -> str:
        """A value as the report should show it: what came back, and what
        it means where the device's own headers say so."""
        key = f"{idx}:{sub}"
        fields = self._object_fields.get(key, [])
        number = _as_int(value)
        if fields and number is not None:
            meaning = describe(number, fields, self.symbols)
            if meaning:
                return f"{value} — {meaning}"
        # "160 — 16.0 cN". A report is read by somebody who wants to know
        # what the machine did, and 160 is the number the bus carried
        # rather than the quantity anybody set
        quantity = self._object_units.get(key)
        if quantity is not None and number is not None:
            info = self._sel_info(idx, sub)
            reading = quantity.with_unit(f"0x{number:X}", info.signed_bits if info else 0)
            if reading:
                return f"{_in_base_of(value, like)} — {reading}"
        # an object the EDS calls a string is one somebody wants to read,
        # not decode: 0x0000003332315F4F4D4544 is "DEMO_123" written back
        # to front, and nobody recognises their device name in that
        info = self._sel_info(idx, sub)
        if info is not None and info.is_text:
            text = _hex_to_text(value)
            if text is not None:
                return f'"{text}"'
        return _in_base_of(value, like)

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
            # one segment per connected session: nothing arrives while
            # offline, so an open file would only blur where the gap is
            self._autosave_close("bus disconnected")
            self.log("BUS  disconnected")
            return
        self._capture_loop()
        try:
            self.bus.connect(self.adapter, int(self.bitrate), self.channel_for(self.adapter))
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
        # the channel is in the line because opening the wrong one does not
        # fail: it delivers silence, and a bus that is simply quiet looks
        # exactly the same. Named here, it is one glance instead of an
        # afternoon.
        opened = getattr(self.bus, "channel", None)
        on_device = getattr(self.bus, "serial", None)
        self.log(f"BUS  connected — {self._adapter_info()['full']} @ {self.bitrate} kbit/s"
                 + (f" · port {opened + 1}" if isinstance(opened, int) and on_device
                    else f" · channel {opened}" if opened is not None else "")
                 + (f" · SN {on_device}" if on_device else ""))
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
        # devices_only: the shipped CiA 301 base is enabled and loads, but a
        # demo scan cannot find anything with it — it describes no device
        if any(e["enabled"] and self._ods.load(e["file"]) is not None
               for e in self.db.eds_list(devices_only=True)):
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

    def _bus_sample(self, node, idx: int, sub: int, value: int, width: int) -> None:
        """Remember a value the bus just carried, for whoever shows it.

        The same tap the signal plot uses, and for the same reason: the
        trace already decodes every SDO answer and unpacks every mapped
        PDO signal, so a panel showing one of those objects can have the
        value for nothing. A test case reading a register next door, a
        device publishing its TPDO — the box follows without a single
        frame of its own. Which is the only kind of "live" a bench should
        offer, since the other kind is polling.

        Kept apart from ``obj_vals`` on purpose. That one holds what was
        read *and* what the operator has typed but not yet written; a
        value arriving from the bus must never overwrite the second.
        """
        if node is None:
            return
        self.seen_vals[(int(node), f"0x{idx:04X}:{sub:02X}")] = (
            f"0x{value:0{max(2, width * 2)}X}", time.monotonic())

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
        info = self._object_info(node, f"0x{idx:04X}", f"{sub:02X}")
        name = self._label(f"0x{idx:04X}", f"{sub:02X}", info.name if info else "")
        if name:
            obj += f" {name}"
        row["obj"] = obj
        if cmd == 0x80 and len(data) >= 8:
            row["val"] = f"abort 0x{int.from_bytes(data[4:8], 'little'):08X}"
        elif (n := self._EXPEDITED_LEN.get(cmd)) and len(data) >= 4 + n:
            value = int.from_bytes(data[4:4 + n], "little")
            # what a word means as a number is what the EDS declares it to
            # be, here as everywhere else: -1 rather than 65535. The plot
            # follows, or a signed signal jumps the height of its range
            # every time it crosses zero
            shown = info.signed(value, n * 8) if info is not None else value
            row["val"] = str(shown)
            if live:
                self._plot_sample(idx, sub, shown)
                # not a text object: an expedited frame carries the first
                # four bytes of a device name, and "DemoDevice" would come
                # back as 68 — the first letter read as a number. The
                # rest arrives in segments this decoder does not follow,
                # so there is nothing here worth remembering for one.
                if info is None or info.width:
                    self._bus_sample(node, idx, sub, value, n)

    # predefined connection set: PDO function code -> mapping object
    _PDO_MAPPING_INDEX = {0x180: 0x1A00, 0x280: 0x1A01, 0x380: 0x1A02, 0x480: 0x1A03,
                          0x200: 0x1600, 0x300: 0x1601, 0x400: 0x1602, 0x500: 0x1603}

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
            info = info_of(var) if var is not None else None
            # sign-extended at the *mapped* width, not the declared one: a
            # mapping may carry fewer bits of an object than it has
            if info is not None:
                val = info.signed(val, bits)
            name = self._label(f"0x{idx:04X}", f"{sub:02X}", info.name if info else "") \
                or f"0x{idx:04X}:{sub:02X}"
            decoded.append((name, idx, sub, val))
            if live:
                self._plot_sample(idx, sub, val)
                self._bus_sample(row["node"], idx, sub,
                                 val & ((1 << bits) - 1), -(-bits // 8))
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
        self.emcy_seen.append(
            Emcy(row["node"], code, payload[2], payload[3:8], time.monotonic()))
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
                self.bus.connect(self.adapter, int(self.bitrate), self.channel_for(self.adapter))
                self.log(f"BUS  bitrate applied — reconnected @ {self.bitrate} kbit/s")
            except Exception as exc:
                self.connected = False
                self.devices = []
                self.log(f"BUS  reconnect failed — {exc}", "emcy0")

    def channel_for(self, adapter: str) -> str:
        """The channel picked for this adapter, or "" for its default.

        Per adapter rather than one setting: a bench with an IXXAT in the
        drawer and a Vector on the desk would otherwise carry one card's
        channel number over to the other, where it means something else
        entirely."""
        return str(self.channels.get(adapter, ""))

    def act_set_channel(self, p: dict) -> None:
        """Which channel of the adapter to open. Empty = the backend's
        default. Takes effect on the next connect, like the adapter card
        itself — reconnecting a running bus behind the operator's back is
        what the bitrate does, and that one is a number, not a port."""
        value = str(p.get("channel", "")).strip()
        adapter = str(p.get("adapter") or self.adapter)
        if value:
            self.channels[adapter] = value
        else:
            self.channels.pop(adapter, None)
        self.db.set("channels", self.channels)

    def act_detect_channels(self, p: dict) -> None:
        """Ask the driver what is attached, for the channel dropdown.

        On demand, never on a snapshot: enumeration talks to the driver,
        and a page that polls it would do so for every browser that has
        the Setup page open.
        """
        adapter = str(p.get("adapter") or self.adapter)
        self.channel_list = {"adapter": adapter, "rows": self.bus.channels(adapter)}
        found = len(self.channel_list["rows"])
        self.log(f"BUS  {found} channel{'' if found == 1 else 's'} reported by the "
                 f"{self._adapter_info(adapter)['full']} driver"
                 + ("" if found else " — driver missing, or nothing attached"), "sdo")

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

    def _seed_plugin_eds(self) -> None:
        """The EDS files a plugin ships (``eds_dirs()``), into the workspace
        EDS folder. Never over a file already there: that one is what the
        devices on this bench answer to, and it is regularly newer than the
        packaged copy — the same rule flows and headers follow.

        Every start, not only the first, so a workspace made before a
        plugin shipped its files still gets them. Failing to copy one is
        logged rather than raised: a bench whose EDS folder is read-only
        still runs, it just cannot match identities.
        """
        for packaged in [d for p in self.plugins for d in p.eds_dirs()]:
            if not packaged.is_dir():
                continue
            for src in sorted(packaged.glob("*.eds")):
                dst = self.db.eds_dir / src.name
                if dst.exists():
                    continue
                try:
                    self.db.eds_dir.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(src.read_bytes())
                except OSError as exc:
                    self.log(f"EDS  {src.name} could not be installed — {exc}", "emcy0")

    def _seed_base_eds(self) -> None:
        """Install the generic CiA 301 EDS and register it with no identity.

        On every start, not only the first: a workspace made before this file
        existed should get it too. Only when the registry has no row for it —
        so an operator who disabled it keeps it disabled, and one who edited
        the file keeps their edit. Removing the row brings it back on the
        next start, the same way a deleted default flow comes back; disabling
        is how to put it out of the way for good.

        Registered directly rather than through add_eds_file, which derives
        the identity from the file and would give this one 0x0·0x0. The empty
        identity is the point: _rematch_devices only looks up devices that
        report one, and no reported identity normalizes to empty, so nothing
        can be assigned this file by accident.
        """
        if any(e["file"] == BASE_EDS.name for e in self.db.eds_list()):
            return
        try:
            content = BASE_EDS.read_text(encoding="utf-8")
        except OSError as exc:
            self.log(f"EDS  base file unavailable ({BASE_EDS.name}) — {exc}", "emcy0")
            return
        self.db.eds_write_file(BASE_EDS.name, content)
        self.db.eds_add(BASE_EDS.name, "CiA 301 base (generic)", "", "", True)

    def add_eds_file(self, filename: str, content: str | bytes) -> tuple[bool, str]:
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

        # bytes where the caller has them — an upload does. The encoding an
        # EDS was written in is a property of its bytes, and a str has
        # already had that decided for it, possibly wrongly.
        raw = content.encode("utf-8") if isinstance(content, str) else content
        try:
            od = load_eds(raw)
        except Exception as exc:  # malformed EDS - report, don't crash the bench
            return False, f"could not parse EDS: {exc}"

        dev_name = od.device_information.product_name or safe_name
        vendor = od.device_information.vendor_number
        product = od.device_information.product_number
        if vendor is None or product is None:
            return False, "EDS has no VendorNumber/ProductNumber in [DeviceInfo] — can't match devices on scan"
        ident = f"0x{vendor:X}·0x{product:X}"

        # stored as UTF-8 whatever it arrived as: the characters are the
        # vendor's, the encoding is nobody's, and normalising once here
        # means every later reader gets it right without asking
        self.db.eds_write_file(safe_name, eds_text(raw))
        self.db.eds_add(safe_name, dev_name, ident, code=safe_name[:3].upper())
        self.log(f'EDS  "{safe_name}" added — {dev_name}, identity {ident}')
        self._rematch_devices()
        return True, "ok"

    def act_eds_upload(self, p: dict) -> None:
        """An EDS from the browser, as base64 of the file's own bytes.

        Not as text: a browser reading a file as text decodes it as UTF-8,
        and the EDS files vendors ship are INI files written on Windows —
        an umlaut in a parameter name then arrives as a replacement
        character and is written to disk that way. The original is on the
        far side of that, and there is no getting it back.
        """
        try:
            raw = base64.b64decode(str(p.get("content", "")), validate=True)
        except Exception:
            self.log(f'EDS  "{p.get("filename", "")}" rejected — unreadable upload',
                     "emcy0")
            return
        ok, msg = self.add_eds_file(p["filename"], raw)
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

    def _eds_rows(self) -> list[dict]:
        """Registry rows whose EDS file is really in the folder.

        A plugin seeds the profiles of a whole device family
        (BenchPlugin.seed_eds), so a fresh workspace starts out with rows for
        files nobody has put there yet. Such a row can do nothing — there is
        no object dictionary to load, to match a scanned device against or to
        generate a demo DUT from — and showing it only raises the question
        which of those devices are real. The row is not deleted: it is the
        family's pre-configuration, so dropping the file into the folder
        brings it back with its identity, code and variant already set."""
        return [e for e in self.db.eds_list() if (self.db.eds_dir / e["file"]).is_file()]

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
        for e in sorted((e for e in self._eds_rows() if e["enabled"]), key=mtime):
            out[normalize_identity(e["ident"])] = e
        return out

    def _eds_conflicts(self) -> dict[str, list[str]]:
        """Normalized identity → file names, for identities that more than
        one enabled EDS file claims."""
        groups: dict[str, list[str]] = {}
        for e in self._eds_rows():
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
        # a flow is a sequence like a case is, and its look-backs get the
        # same floor: nothing from before it started — see _match_traced
        self._sequence_started_at = time.monotonic()
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
        base = {"found": True, "name": self.psu.name, "error": self.psu_error,
                "sidebar": self.psu_sidebar}
        if st is None:
            return base | {"port": self.psu.link.port, "channels": []}
        return base | {
            # vendor and model as two fields rather than one string: the
            # box sets them in different weights, and a driver that knows
            # only one of the two must not print a stray space
            "vendor": st.vendor, "model": st.model, "sn": st.serial, "fw": st.firmware,
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

    def act_psu_sidebar_toggle(self, p: dict) -> None:
        """Show the supply in the sidebar, or stop showing it. A view
        preference, so it is remembered and says nothing in the log."""
        self.psu_sidebar = not self.psu_sidebar
        self.db.set("psu_sidebar", self.psu_sidebar)

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
            self.obj_vals_at[key] = time.monotonic()
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
            value = parse_value(text, self._object_fields.get(key, []), self.symbols)
        except ValueError as exc:
            self.log(f"OBJ  {key} ← {text!r} rejected — {exc}", "emcy0")
            return
        # a box that shows a negative has to accept one back. Stored as the
        # two's complement of the object's own width, because that is the
        # only width at which a two's complement is itself — the digits of
        # whatever the last read happened to answer are not it
        info = self._object_info(self._target_node(), p["idx"], p["sub"])
        bits = info.signed_bits if info is not None else 0
        if value < 0:
            if not bits:
                self.log(f"OBJ  {key} ← {text!r} rejected — the EDS declares this "
                         "object unsigned", "emcy0")
                return
            if value < -(1 << (bits - 1)):
                self.log(f"OBJ  {key} ← {text!r} rejected — does not fit in {bits} "
                         "signed bits", "emcy0")
                return
            value += 1 << bits
        # a positive number is taken at face value even where it is above
        # the signed half of the range: with the table in hex, 0xFE0C is
        # what the box *shows* for -500, and a field that will not accept
        # back what it just printed is worse than one that is strict
        self.obj_vals[key] = f"0x{value:0{self._staged_width(key, info, bits)}X}"
        self.obj_vals_at[key] = time.monotonic()

    def _staged_width(self, key: str, info: ObjectInfo | None, bits: int) -> int:
        """How many hex digits a staged value is written with.

        The object's own width wherever it is known, because that is what
        the download will carry and because a two's complement is only
        itself at its own width. What was read last is the fallback and
        was once the only rule, which made the spelling of a staged value
        depend on whether anybody had read the object first.
        """
        if bits:
            return bits // 4
        if info is not None and info.width:
            return info.width * 2
        return len(self.obj_vals.get(key, "").removeprefix("0x")) or 2

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

    def _eds_of(self, node: int) -> str:
        """The EDS file assigned to a node, or "" — "—" is the registry's
        way of writing "none", and loading it would be a missing file."""
        eds = next((d["eds"] for d in self.devices if d["node"] == node), "")
        return "" if eds in ("", "—") else eds

    def _object_info(self, node: int, idx: str, sub: str) -> ObjectInfo | None:
        """What the EDS assigned to this node says about one object.

        The one question the panel, the table, the trace and the write
        path all used to ask separately — each with its own copy of the
        CiA-301 type numbers, and two of those copies disagreed. None
        where there is no EDS or no such object in it; every caller then
        treats the value as the plain unsigned word it always was, which
        is what they all did before anything was asked.
        """
        want_i, want_s = _addr_int(idx), _addr_int(sub)
        if want_i is None:
            return None
        od = self._ods.load(self._eds_of(node)) if self._eds_of(node) else None
        return object_info(od, want_i, want_s or 0)

    def _sel_info(self, idx: str, sub: str) -> ObjectInfo | None:
        """The same for the selected device — what a report line and the
        object table are written about."""
        dev = self.sel_devices[0] if self.sel_devices else None
        if dev is None:
            return None
        want_i, want_s = _addr_int(idx), _addr_int(sub)
        if want_i is None:
            return None
        return object_info(self._ods.load(dev["eds"]), want_i, want_s or 0)

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
        info = self._object_info(node, idx, sub)
        value = self._pad_hex(value, info.width if info else 0)
        res = self.bus.sdo_write(node, idx, sub, value)
        if res.ok:
            self.obj_vals[key] = value
            self.obj_vals_at[key] = time.monotonic()
            self._remember(key, value)
            self.log(f"SDO  write {idx}:{sub} ← {value} (node {node})", "sdo")
        else:
            self.log(f"SDO  write {idx}:{sub} ✗ abort {res.abort} (node {node})", "emcy0")

    # -- object panels (a device's values as named boxes) ---------------------
    def _panel(self):
        """The panel for the selected device, or None.

        A panel that names the devices it is for beats one that takes
        every device: the core ships a general-purpose panel of the
        objects CiA 301 makes mandatory, so the view is never an empty
        promise, and a vendor panel has to be able to replace it rather
        than queue behind it. Among equals the first wins, and plugin
        panels are asked before the core's own (see ``_obj_panels``).
        """
        sel = self.sel_devices
        if not sel:
            return None
        fits = [p for p in self._obj_panels if p.matches(sel[0])]
        return next((p for p in fits if p.match), None) or (fits[0] if fits else None)

    def _panel_open(self, panel, group) -> bool:
        """Whether a box is unfolded. The spec says how it opens the first
        time; what the operator folded away afterwards outranks it."""
        return self.panel_open.get(f"{panel.name}/{group.title}", not group.collapsed)

    def _panel_field(self, idx: str, sub: str, bit=None, flag: bool = False,
                     lane: str = ""):
        """The field a click came from.

        Address alone does not name one: a status word is exactly the case
        where several fields sit on the same object — a mode lane, the
        selection beside it, the flags beside that — so what kind of
        control it was, which bit, and which lane decide between them. A
        click that names no lane takes the first, which is every object
        with only one.
        """
        panel = self._panel()
        if panel is None:
            return None
        key = f"{idx}:{sub}"
        want = [f for g in panel.groups for f in g.fields if f.key == key]
        if flag:
            return next((f for f in want if f.widget == "flag"
                         and (bit is None or f.bit == int(bit))), None)
        rest = [f for f in want if f.widget != "flag"]
        if lane:
            return next((f for f in rest if f.lane == lane), None)
        return next(iter(rest), None)

    def _panel_enum(self, key: str, lane: str = ""):
        """The symbol table behind an enum field: the field a plugin
        declared for this object (``object_fields``), or None.

        The names come from the device's own headers that way, so a panel
        says ``widget: enum`` and gets whatever the firmware calls those
        values — a list written into the panel file instead would be a
        second copy to keep in step with the first.

        ``lane`` picks one of them, for the objects that carry several: a
        status word assembled out of a mode, a selection and a keylock is
        one object with three names in it, and a box that could only ever
        show the first of them showed a third of the word and said nothing
        about the rest. Without a name the first non-flag field wins,
        which is what a single-lane object wants.

        The name is the field's table, or its label where it has one. A
        table name is the firmware's own word for what the lane holds and
        needs nothing invented alongside it; a label is what tells three
        lanes of the same table apart, which is the case a table name
        cannot cover — three colours of one LED enum.
        """
        fields = [f for f in self._object_fields.get(key, []) if not f.flags]
        if lane:
            return next((f for f in fields if lane in (f.label, f.table)), None)
        return next(iter(fields), None)

    def _panel_value(self, key: str, node: int) -> tuple[str | None, str, float]:
        """The freshest thing known about an object: what was read or
        staged, or what the bus carried past since — value, where it came
        from, and how many seconds ago.

        The newer wins, which puts the two in the right order by itself. A
        value the operator typed a moment ago is newer than a PDO from
        before it, so typing is not overwritten; a PDO from a second ago
        is newer than a read from ten minutes back, so the box follows the
        device without asking it anything.
        """
        mine, mine_at = self.obj_vals.get(key), self.obj_vals_at.get(key, 0.0)
        theirs, theirs_at = self.seen_vals.get((node, key), (None, 0.0))
        now = time.monotonic()
        if theirs is not None and theirs_at >= mine_at:
            return theirs, "bus", now - theirs_at
        if mine is None:
            return None, "", 0.0
        return mine, "read", (now - mine_at if mine_at else 0.0)

    def _quantity(self, key: str, own: Quantity | None = None) -> Quantity:
        """What a value at this address means physically.

        What the caller states itself where it states anything — a panel
        field's own ``unit``/``scale`` is written for that box. Otherwise
        what a plugin declared for the address, which is a fact about the
        device rather than about one view of it: a panel that repeated it
        would be the same fact written down twice, kept in step by hand.
        """
        if own is not None and own.stated:
            return own
        return self._object_units.get(key) or own or Quantity()

    def _panel_field_view(self, f, node: int) -> dict:
        raw, src, age = self._panel_value(f.key, node)
        info = self._object_info(node, f.idx, f.sub)
        bits = info.signed_bits if info is not None and f.widget == "number" else 0
        # a text object is a word, whatever the wire carried it as; the
        # widgets below all mean numbers, so this is the whole of it. A
        # value that is already a word — the trace decodes SDO answers and
        # stores one — passes through, since it does not parse as hex
        # what the device itself calls this object, for the hover: a box
        # labels a row the way the file's author reads it, and at a narrow
        # window that label is the first thing the row cuts off. The name
        # rule is the one every other view uses — the firmware's word for
        # it, the EDS's where no plugin has one (_label)
        name = self._label(f.idx, f.sub, info.name if info is not None else "")
        if raw and f.widget == "number" and info is not None and info.is_text:
            return {"idx": f.idx, "sub": f.sub, "label": f.label, "unit": "",
                    "name": name, "wo": info.access == "wo",
                    "rw": f.rw, "widget": "number", "val": _hex_to_text(raw) or str(raw),
                    "src": src, "age": round(age, 1)}
        # a unit and a scale belong to a number; the widgets that mean a
        # name or a bit reject them in the file, and must not pick one up
        # from a plugin's declaration either
        q = self._quantity(f.key, f.quantity) if f.widget == "number" else f.quantity
        # a field the file calls a register is shown as one: the bit
        # pattern written the way its documentation writes it, padded to
        # the object's own width so a byte stays two digits. Display only —
        # what is typed back is read by the one rule the whole bench reads
        # typed numbers by, and 20 is twenty in this box like in every
        # other. The 0x the box prints is what makes that round-trip: type
        # back what you see and it means what it showed
        register = _typed_number(raw or "") if f.base == "hex" else None
        out = {"idx": f.idx, "sub": f.sub, "label": f.label, "unit": q.unit,
               "name": name,
               "rw": f.rw, "widget": f.widget, "base": f.base,
               # an object the EDS says is write-only has nothing to fetch:
               # the SDO could only abort, and a ⟳ that can only fail is a
               # button offering to break something
               "wo": info is not None and info.access == "wo",
               "val": (format_number(register, "hex", (info.width * 2) if info else 0)
                       if register is not None else q.show(raw, bits)),
               # where the number comes from and how old it is: a value a
               # PDO carried past three minutes ago must not look like a
               # reading taken just now
               "src": src, "age": round(age, 1)}
        # every reading of the number for the tooltip — hex, decimal, and
        # the symbolic one where a plugin declared fields. A panel prints
        # decimal because that is what its units are read in, which is
        # exactly why the other readings have to stay one hover away
        # (the same `alternatives` the object table and favourites use)
        shown = _typed_number(raw or "")
        if shown is not None:
            out["alt"] = alternatives(shown, self._object_fields.get(f.key, []),
                                      self.symbols, len((raw or "").removeprefix("0x")) or 2,
                                      info.signed(shown) if info is not None else None)
        value = _typed_number(raw or "") or 0
        if f.widget == "flag":
            out["on"] = bool(value >> f.bit & 1)
            out["bit"] = f.bit
        elif f.widget == "enum":
            field = self._panel_enum(f.key, f.lane)
            # sent back on a pick: several lanes can sit on one object, and
            # the address alone would stage whichever the core listed first
            out["lane"] = f.lane
            table = self.symbols.tables.get(field.table, {}) if field else {}
            shift = field.resolved_shift(self.symbols) if field else 0
            # every choice sheds the prefix it shares with its table: a
            # table name repeated on all four of them says nothing the one
            # open dropdown does not already say, and costs the width the
            # word itself needs — the same reason describe_object drops it
            # from an object's name. removeprefix, so a table whose members
            # are named otherwise keeps them whole.
            prefix = f"{field.table}_" if field else ""
            out["options"] = [[str(sym.value), sym.name.removeprefix(prefix)]
                              for sym in table.values()]
            out["val"] = str(field.extract(value, self.symbols)) if field else out["val"]
            # a value no symbol names is still a fact about the device, so
            # it is shown rather than snapped to the nearest name — as the
            # bare hex it is. It used to carry a "?", which said only that
            # the bench had nothing to add and left the reader wondering
            # whether the number itself was in doubt. Nothing read yet is
            # not such a value: there is no number to offer, and asking for
            # the hex of an empty box used to take the whole snapshot down
            # with a ValueError
            current = _typed_number(out["val"] or "")
            if current is not None and out["val"] not in {o[0] for o in out["options"]}:
                out["options"] = [*out["options"], [out["val"], f"0x{current:X}"]]
            out["shift"] = shift
        return out

    def _panel_view(self) -> dict | None:
        """The panel as the browser draws it: values already formatted,
        because scale and unit belong to the field that declares them and
        formatting them twice is how the two drift apart."""
        panel = self._panel()
        if panel is None:
            return None
        node = self._target_node()
        return {
            "name": panel.name,
            "busy": self.panel_busy,
            "groups": [{
                "title": g.title,
                "cols": g.cols,
                "flow": g.flow,
                "open": self._panel_open(panel, g),
                "fields": [self._panel_field_view(f, node) for f in g.fields],
            } for g in self._panel_groups(panel, node)],
        }

    def _panel_groups(self, panel, node: int) -> list:
        """The boxes this device has. A ``when`` that has been settled and
        says no takes its box away entirely — the machine does not have
        that part, so an empty box to open would be a worse answer than no
        box at all."""
        return [g for g in panel.groups
                if g.when is None or g.when.holds(self._panel_value(g.when.key, node)[0])]

    def act_obj_view(self, p: dict) -> None:
        """Numeric table or panel for the object area."""
        self.obj_view = "panel" if str(p.get("view")) == "panel" else "table"
        self.db.set("obj_view", self.obj_view)

    def act_panel_fold(self, p: dict) -> None:
        panel = self._panel()
        group = next((g for g in panel.groups if g.title == p.get("group")), None) \
            if panel else None
        if panel is None or group is None:
            return
        self.panel_open[f"{panel.name}/{group.title}"] = not self._panel_open(panel, group)
        self.db.set("panel_open", self.panel_open)

    def act_panel_set(self, p: dict) -> None:
        """A value typed into a box: staged like the object table's, the
        next Write sends it — but read through the field's own scale. The
        box says 16.0 cN and the device stores 160; a panel that staged
        the digits as typed would write a sixteenth of what it shows.

        A field without a scale goes through the table's own parsing,
        which knows hex, binary and symbol names — none of which a scaled
        physical quantity has any use for.
        """
        fld = self._panel_field(p.get("idx", ""), p.get("sub", ""), p.get("bit"),
                                flag="on" in p, lane=str(p.get("lane") or ""))
        if fld is not None and fld.widget in ("enum", "flag"):
            self._panel_stage_part(fld, p)
            return
        node = self._target_node()
        info = (self._object_info(node, p.get("idx", ""), p.get("sub", ""))
                if fld is not None else None)
        bits = info.signed_bits if info is not None else 0
        key = f"{p['idx']}:{p['sub']}"
        quantity = self._quantity(key, fld.quantity) if fld is not None else Quantity()
        # an unscaled field goes through the object table's own parsing,
        # which knows hex, binary and symbol names. A signed one does not:
        # that path stages the digits as an unsigned word, and -500 leaves
        # the box as a number no width can hold
        if fld is None or (quantity.scale == 1.0 and not bits):
            self.act_obj_set(p)
            return
        text = str(p.get("val", ""))
        if not text.strip():
            self.obj_vals[key] = ""
            return
        try:
            raw = quantity.to_raw(text, bits)
        except ValueError_ as exc:
            self.log(f"OBJ  {key} ← {text!r} rejected — {exc}", "emcy0")
            return
        # a two's complement is only itself at the object's own width, so
        # the EDS decides it here rather than the digits of whatever the
        # last read happened to answer
        width = self._staged_width(key, info, bits)
        self.obj_vals[key] = f"0x{raw:0{width}X}"
        self.obj_vals_at[key] = time.monotonic()

    def _panel_stage_part(self, fld, p: dict) -> None:
        """Stage a change that touches only part of an object's value: one
        bit for a flag, one masked lane for an enum.

        Read-modify-write against the value last read, because the rest of
        that word belongs to somebody else. A checkbox that wrote a lone
        1 would clear every other flag in the register, which is the kind
        of help nobody asks for twice — so a part that was never read
        refuses rather than guesses.
        """
        key = fld.key
        known = self.obj_vals.get(key, "")
        current = _typed_number(known)
        if current is None:
            self.log(f"OBJ  {key} — read it before writing part of it "
                     f"({'bit' if fld.widget == 'flag' else 'field'} "
                     f"{fld.label}): the other bits of that value are unknown", "emcy0")
            return
        if fld.widget == "flag":
            bit = 1 << fld.bit
            value = current | bit if p.get("on") else current & ~bit
        else:
            field = self._panel_enum(key, fld.lane)
            if field is None:
                return
            shift = field.resolved_shift(self.symbols)
            try:
                chosen = int(str(p.get("val", "")), 0)
            except ValueError:
                return
            value = (current & ~field.mask) | (chosen << shift & field.mask)
        width = len(known.removeprefix("0x")) or 2
        self.obj_vals[key] = f"0x{value:0{width}X}"
        self.obj_vals_at[key] = time.monotonic()

    def act_panel_read(self, p: dict) -> None:
        """Read one box (``group``) or every box that is open.

        Folded boxes are not read: folding one away is the only thing that
        says "not interested right now", and a page-wide read that walks
        them anyway would make it meaningless. Nothing here is periodic —
        a panel that polls turns showing a value into bus load nobody
        asked for.
        """
        if not self.connected or self.panel_busy:
            return
        panel = self._panel()
        if panel is None:
            return
        want = p.get("group")
        node = self._target_node()
        addrs: list[tuple[str, str]] = []
        # what a condition asks about is read too, even though no box shows
        # it: otherwise a box whose device does not have that part only
        # disappears after somebody reads the object by hand, and nothing
        # on the page says which object that is
        if not want:
            addrs += [(g.when.idx, g.when.sub) for g in panel.groups if g.when is not None]
        for g in self._panel_groups(panel, node):
            if (g.title != want) if want else (not self._panel_open(panel, g)):
                continue
            addrs += [(f.idx, f.sub) for f in g.fields]
        seen: set[tuple[str, str]] = set()  # one read per object, in panel order
        addrs = [a for a in addrs if not (a in seen or seen.add(a))]
        # a write-only object cannot be read, and asking anyway turns one
        # Read of a box into a row of aborts in the log that look like a
        # fault and are only the EDS telling the truth (same as fav_read_all)
        catalog, _groups, _hint = self._object_catalog()
        acc = {f"{row[0]}:{row[1]}": row[4] for rows in catalog.values() for row in rows}
        addrs = [a for a in addrs if acc.get(f"{a[0]}:{a[1]}") != "wo"]
        if addrs:
            self._spawn(self._panel_read_async(addrs))

    async def _panel_read_async(self, addrs: list[tuple[str, str]]) -> None:
        """Walk a list of objects, one SDO at a time, off the event loop.

        Values land as they arrive rather than in one lump at the end: a
        box of forty objects takes a noticeable moment, and watching it
        fill is the difference between a slow tool and a hung one. Only
        the failures are logged individually — a line per successful read
        would bury the run log under something nobody reads back.
        """
        self.panel_busy = True
        node = self._target_node()
        done = 0
        try:
            for idx, sub in addrs:
                if not self.connected:
                    break
                res = await asyncio.to_thread(self.bus.sdo_read, node, idx, sub)
                key = f"{idx}:{sub}"
                if res.ok:
                    self.obj_vals[key] = res.value
                    self.obj_vals_at[key] = time.monotonic()
                    self._remember(key, res.value)
                    done += 1
                else:
                    self.log(f"SDO  read {idx}:{sub} ✗ abort {res.abort} (node {node})", "emcy0")
                self._changed()
        finally:
            self.panel_busy = False
            self.log(f"OBJ  read {done} of {len(addrs)} objects (node {node})", "sdo")
            self._changed()

    # -- favorites (named object sets, persisted in the workspace db) --------
    def _fav_rows(self) -> list[dict]:
        """The stored list itself — callers add to it and remove from it."""
        return self.favorites

    def _fav_view(self) -> list[dict]:
        """The same favourites with their names looked up now.

        The name is stored alongside the address when a favourite is added,
        and that copy is the only thing a panel could show while no device
        is selected — so it stays. But it was also the only thing shown
        while one *is*, which made every stored name permanent: favourites
        added while sub-indices 0A…0F resolved to the wrong object kept
        that name after the resolving was fixed, and re-adding each one by
        hand was the only way out. A name the catalog can answer for is
        answered for here, every tick, and the stored one stands in when it
        cannot.
        """
        if not self.favorites:
            return self.favorites
        catalog, _groups, _hint = self._object_catalog()
        names = {(_addr_int(r[0]), _addr_int(r[1])): r[2]
                 for rows in catalog.values() for r in rows}
        return [{**f, "label": names.get((_addr_int(f["idx"]), _addr_int(f["sub"])),
                                         f.get("label", ""))}
                for f in self.favorites]

    def _save_favs(self) -> None:
        self.db.set("favorites", self.favorites)

    def _object_label(self, idx: str, sub: str) -> str:
        """The EDS name of an object, or "".

        Matched numerically. The catalog writes a sub-index as "04" and a
        step writes it "0x04" — comparing the text found nothing, silently,
        for every caller that did not happen to use the catalog's spelling.
        """
        want = (_addr_int(idx), _addr_int(sub))
        if want[0] is None or want[1] is None:
            return ""
        catalog, _groups, _hint = self._object_catalog()
        for rows in catalog.values():
            for r in rows:
                if (_addr_int(r[0]), _addr_int(r[1])) == want:
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

    def _visible(self, p: dict) -> list[str]:
        """The ids on screen, in catalog order.

        Variant, category and the search box are filters the frontend
        applies to the catalog it was sent — the server never hears about
        them, and its own `_shown_tests` knows only the tool filter. So
        anything that acts on "what is shown" has to be told, and both
        callers ask here rather than each keeping their own idea of it.

        What comes back is intersected with what is actually runnable, so
        a stale list cannot name a case that has since gone or broken.
        Without a list at all — any caller that sends none — it means
        everything, which is what it always meant.
        """
        runnable = [t[0] for t in self._shown_tests()]
        asked = p.get("ids")
        if not isinstance(asked, list):
            return runnable
        wanted = set(asked)
        return [tid for tid in runnable if tid in wanted]

    def act_tests_all(self, p: dict) -> None:
        self.test_sel = set(self._visible(p))

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

    def _runnable_ids(self) -> set[str]:
        """The ids that can actually be run: parsed without a schema error."""
        return {tc.id for tc in self.testcases.values() if not tc.error}

    def act_run_start(self, p: dict) -> None:
        # Re-read the folder first, so a case edited since the last scan runs
        # as it stands on disk rather than as it stood at startup. Whatever
        # the re-read costs is said out loud: the selection is narrowed to
        # what is runnable a few lines below, and a case that a fresh typo
        # just made unreadable would otherwise simply not run, quietly, with
        # the count on the button still promising it.
        selected_before = self._runnable_ids() & self.test_sel
        self._load_testcases(log=False)
        lost = sorted(selected_before - self._runnable_ids())
        if lost:
            self.log(f"RUN  {len(lost)} selected test case(s) no longer readable after "
                     f"re-reading the folder: {', '.join(lost)}", "emcy0")
        # what is selected *and* on screen. A filter narrows the list and
        # not the selection, so a case selected before the filter was set
        # stays selected while being invisible — and ran. The button has
        # always counted the other way, "selected among shown", so this is
        # the number it was already promising.
        sel = [tid for tid in self._visible(p) if tid in self.test_sel]
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

    def _emcy_window(self, max_age: float | None = None,
                     through_reset: bool = False) -> list[Emcy]:
        """The EMCYs that describe the device right now, newest last.

        An EMCY is not a state the way a PDO is — it is a report, and a
        device that has three things wrong with it says so three times.
        So unlike a PDO's newest frame, all of them count. What ends the
        window is a *reset*: error code 0x0000 says every error has been
        accepted or cleared, so nothing before it is still true. That is
        the device's own word on the subject, and it beats any duration.

        Two more bounds, both because the record outlives the question:
        nothing from before the case started (a repeat of a case would
        otherwise inherit the errors of its own previous pass), and,
        where the caller asks for one, nothing older than `max_age`.

        Without `max_age` the window is the whole case — which is what
        "nothing has gone wrong" has to mean, or an error early in a case
        would fall out of sight and the check would pass on a device that
        had already failed.

        `through_reset` decides whether the reset frame itself is in the
        window or only ends it, and the two callers genuinely differ. "Is
        anything wrong" must not see it — a device that just cleared its
        error would read as still reporting one. "Did the device tell me
        X" must, because a case acknowledges an error and then waits for
        exactly that frame: `expect_emcy mec 0x0` is a step asking to be
        shown the reset, and a window that always cut in front of it
        answered "nothing arrived at all" about the one frame it was
        waiting for. Either way the window stops there — what came before
        a reset does not stand — the flag only says whether the boundary
        frame is inside or outside.
        """
        out: list[Emcy] = []
        now = time.monotonic()
        for entry in reversed(self.emcy_seen):
            if self._sequence_started_at and entry.at < self._sequence_started_at:
                break                      # belongs to whatever ran before this
            if max_age is not None and now - entry.at > max_age:
                break
            if not entry.code:             # error reset: nothing before it stands
                if through_reset:
                    out.append(entry)
                break
            out.append(entry)
        out.reverse()
        return out

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

        def match(entry: Emcy) -> bool:
            if want_node not in (None, entry.node):
                return False
            if code is not None and entry.code & mask != code & mask:
                return False
            if reg is not None and entry.reg != reg:
                return False
            if mec is not None:
                if _mec(entry.mfr) & mec_mask != mec & mec_mask:
                    return False
            return True
        return match

    async def _exec_case(self, tid: str) -> tuple[str, str]:
        tc = self.testcases.get(tid)
        started = time.time()
        # the floor for every look-back this case does — see _match_traced
        self._sequence_started_at = time.monotonic()
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
                                              builtins, 0, on_step, stop, rec.steps,
                                              lines=tc.precondition_lines)
        if status in ("fail", "skip"):
            return done("SKIP", f"precondition: {why}")
        if status == "error":
            return done("ERROR", why)
        # only the body may carry on after a failure — a precondition that
        # fails means the case does not apply, and there is nothing to undo
        status, why = await self._run_program(tc, tc.steps, node, regs, builtins,
                                              len(tc.preconditions), on_step, stop,
                                              rec.steps, allow_continue=True,
                                              lines=tc.step_lines)
        if status == "ok":
            return done("PASS")
        if status == "skip":
            return done("SKIP", why)
        return done("FAIL" if status == "fail" else "ERROR", why)

    async def _run_program(self, tc: tclib.TestCase, steps: list, node: int,
                           regs: dict, builtins: dict, base: int,
                           on_step, should_stop, record: list | None = None,
                           allow_continue: bool = False,
                           lines: list[int] | None = None) -> tuple[str, str]:
        """Program-counter loop over one step list (format v2: labels, jumps,
        registers). Returns ("ok" | "fail" | "error", reason).

        ``base`` counts steps for the progress line ("step 3/9"), ``lines``
        names them for the report — the file's own line numbers, which a
        reader looks up in the editor. A caller with no file behind it
        (a built-in case, a test) passes none and gets the count."""
        labels = {step["label"]: i for i, step in enumerate(steps)
                  if len(step) == 1 and "label" in step}
        # Where each `loop` finds its `loop_end`. Loops are flat (checked at
        # load, testcases._check_loops), so one pass pairs them and at most
        # one is ever running — which is why the counter can live here in the
        # frame rather than in a register a case could overwrite.
        loop_end_of: dict[int, int] = {}
        opened: int | None = None
        for i, step in enumerate(steps):
            if len(step) != 1:
                continue
            k = next(iter(step))
            if k == "loop":
                opened = i
            elif k == "loop_end" and opened is not None:
                loop_end_of[opened] = i
                opened = None
        loop_at: int | None = None   # the running loop's `loop` step
        loop_left = 0                # turns after the one running now
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
            if key == "loop_end" and loop_at is not None:
                # said here rather than in _step_text, which sees the file and
                # not the run: "how many turns are left" is a fact about this
                # moment, and it is the whole reason the line is worth a row
                text += f", loopsLeft: {loop_left}"
            on_step(base + pc + 1, text)
            # when the step began, not when the runner was done with it.
            # Stamped afterwards, a row carried the moment its logging and
            # its state push had finished — which is about when the *next*
            # request went out, so a report line and the frame it is about
            # sat a step apart in the trace. A step's own duration is still
            # there to read: it is the gap to the row below.
            started_at = datetime.now().strftime("%Y%m%d_%H%M%S.%f")[:-3]
            status, info = await self._exec_one(tc, key, val, node, regs,
                                                builtins, should_stop)
            if record is not None:
                # a note (log step) is neither pass nor fail — it is the
                # sentence somebody wrote to make the report readable
                state = "note" if key == "log" else {
                    "jump": "ok", "end": "ok"}.get(status, status)
                # nor is the case's own bookkeeping: it sets the loop
                # apart from the traffic, so a body that ran seventeen
                # times is visible as seventeen. A step that went wrong
                # keeps saying so — that outweighs what kind it was.
                if state == "ok" and key in _FLOW_KEYS:
                    state = "flow"
                record.append(reportlib.StepRecord(
                    line=lines[pc] if pc < len(lines or ()) else base + pc + 1,
                    text=text, state=state,
                    note=val.get("note", "") if isinstance(val, dict) else "",
                    # on the passing path this is what came back, not a
                    # reason — both belong in the file for the same reason
                    detail=info, ts=started_at))
            if key == "loop":
                count = val.get("n") if isinstance(val, dict) else val
                # resolved once, here: from now on the frame holds the
                # number, so writing to that register does not move a loop
                # that is already turning
                count = _resolve(count, regs, builtins)
                if count:
                    loop_at, loop_left = pc, count - 1
                    pc += 1
                else:
                    # `loop: 0` runs the body no times. Saying so beats making
                    # the author write a jump around it, and a converter that
                    # computes the count does not have to special-case zero.
                    loop_at, loop_left = None, 0
                    pc = loop_end_of.get(pc, pc) + 1
                continue
            if key == "loop_end":
                if loop_at is not None and loop_left > 0:
                    loop_left -= 1
                    pc = loop_at + 1
                else:
                    # also the way out when a jump landed inside a body: with
                    # no loop running this is a step that does nothing, rather
                    # than a jump back into a loop nobody opened
                    loop_at = None
                    pc += 1
                continue
            if key == "loop_break":
                end = loop_end_of.get(loop_at) if loop_at is not None else None
                loop_at, loop_left = None, 0
                pc = (end + 1) if end is not None else pc + 1
                continue
            if status == "jump":
                pc = labels[info]
                continue
            if status == "end":
                break
            if status == "fail" and allow_continue and tc.on_fail == "continue":
                # the case says it wants to reach its own last steps even
                # when something failed — that is where it puts the bench
                # back. The failure is remembered, not forgiven: the first
                # one is the verdict's reason, and jump_on_error can see it.
                #
                # An explicit `fail:` counts as one of those. It used to end
                # the case on the grounds that somebody had written it on
                # purpose — but what they wrote it into is an error branch,
                # and cutting the case off there leaves the device wherever
                # the failure found it. Observed: a case failed while the
                # device was still booting, its closing wait never ran, and
                # the next case failed on a device that had never left
                # startup. A failure that spreads to the following case is
                # worse than a case that runs a few more steps.
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
            data = _frame_bytes(val, regs, builtins)
            if data is None:
                return "fail", ("$session unavailable — no addressing provider "
                                "installed (vendor plugin)")
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
                info = "Response: " + self._value_note(
                    index_s, sub_s, res.value, spec.get("expect"))
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
        if key in ("loop", "loop_end", "loop_break"):
            # nothing to execute: a loop is program counter work, and that
            # lives in _run_program, which is the only place that has one
            return "ok", ""
        if key == "dump_registers":
            # every register, whether the case has touched it or not: the
            # point of asking is to see the state, and "R7 is missing"
            # would be a fact about this list rather than about the run.
            # Hex and decimal together, because a case mixes both — a
            # screen id reads in hex, a count does not.
            cells = [f"{name} = 0x{regs[name] & 0xFFFFFFFF:08X} ({regs[name]})"
                     for name in tclib.REGISTER_ORDER]
            rows = ["   ".join(cells[i:i + 4]) for i in range(0, len(cells), 4)]
            return "ok", "<code>" + "<br>".join(rows) + "</code>"
        if key == "expect_emcy":
            match = self._emcy_matcher(val, regs, builtins)
            timeout = float(val.get("timeout", 1.0))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout

            def seen() -> bool:
                return any(match(e) for e in
                           self._emcy_window(FRAME_LOOKBACK_S, through_reset=True))

            # an EMCY that arrived shortly before this step is a hit too:
            # the device sends it when it feels like it, and a check that
            # only looks forward turns a timing difference into a failure
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
            seen_now = ", ".join(_emcy_str(e) for e in
                                 self._emcy_window(through_reset=True)[-3:])
            return "fail", (f"{_emcy_wanted(val)} — none seen within {timeout:g}s"
                            + (f"; saw {seen_now}" if seen_now
                               else "; nothing arrived at all"))
        if key == "expect_no_emcy":
            # the opposite of expect_emcy, and it cannot wait: no amount of
            # waiting proves nothing will arrive.
            #
            # It asks of the whole case rather than the last fraction of a
            # second. The two questions are not mirror images: a short
            # window makes "did it report X" stricter, and makes "did it
            # report nothing" weaker — an error early in a long case would
            # simply fall out of sight and the step would pass on a device
            # that had already failed. What ends this window is the device
            # saying so itself, with an error reset.
            match = self._emcy_matcher(val, regs, builtins)
            hits = [e for e in self._emcy_window() if match(e)]
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
                # Anchored at the step's start, so the window does not
                # slide while the step waits: FRAME_LOOKBACK_S back from
                # there, and everything that arrives from there on.
                started = loop.time()
                while True:
                    if should_stop():
                        return "error", "aborted"
                    if not self.connected:
                        return "error", "connection lost"
                    # keep the record current instead of waiting out the
                    # tick — at TICK_S the answer could otherwise arrive in
                    # the trace only after this step has already timed out
                    self._drain_frames()
                    idx = self._match_traced(
                        pairs, FRAME_LOOKBACK_S + (loop.time() - started))
                    if idx is not None:
                        if into:
                            regs[into] = idx
                        return "ok", ""
                    left = deadline - loop.time()
                    if left <= 0:
                        break
                    await asyncio.sleep(min(0.01, left))
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
    def _match_traced(self, pairs: list[tuple[int, bytes]], max_age: float) -> int | None:
        """Which of `pairs` is satisfied by the newest frame its COB-ID
        carried inside `max_age`, or None.

        Reads the trace, not the wire. A device answers when it is ready,
        not when a step happens to start listening, and the answer to the
        step before this one can land while that step is still finishing.
        Scans from the newest row and stops at the first one outside the
        window, so the cost is the window, not the buffer.

        **Of a PDO, only the newest frame is asked.** A PDO carries a
        state and the device keeps saying it, so the frame before the
        newest describes a moment that has passed and is not evidence
        about now: reading further back let "the tension is reduced" be
        answered by the pass before the device was switched off. An older
        PDO of a COB-ID whose newest one does not match is therefore
        skipped rather than consulted.

        Everything else keeps its say, because it is an event and not a
        state — and on one COB-ID the two even share the wire. A device
        announces itself once with a boot-up (0x700+id, ``00``) and then
        sends heartbeats on the same COB-ID for as long as it lives. Ask
        only the newest there and the boot-up is gone behind the first
        heartbeat, which is how addressing loses the device it just
        addressed.

        The window is bounded twice over: by ``max_age``, and by the start
        of the case or flow doing the waiting.

        TX rows are skipped — our own frame is not an answer to itself,
        which a `can_send` immediately followed by a `wait_for` on the same
        COB-ID would otherwise make it.
        """
        # Never past the start of the case (or flow) doing the waiting. The
        # record holds the runs before this one too, and their frames
        # describe a device that has since been switched, reset or
        # re-addressed — right down to a repeat of this very case, whose
        # own previous pass would otherwise answer for this one.
        if self._sequence_started_at:
            max_age = min(max_age, time.monotonic() - self._sequence_started_at)
        now_tod = _tod_seconds(now_us_str()) or 0.0
        answered: set[int] = set()   # COB-IDs whose newest frame we have seen
        for row in reversed(self.trace):
            stamp = _tod_seconds(row["time"])
            if stamp is None:
                continue
            age = now_tod - stamp
            if age < 0.0:
                age += 86400.0  # the record crossed midnight
            if age > max_age:
                break
            if row["dir"] != "RX":
                continue
            try:
                cob = int(row["cob"], 16)
                data = bytes.fromhex(row["data"].replace(" ", ""))
            except ValueError:
                continue
            if cob in answered:
                continue         # this PDO's newest frame has had its say
            for i, (want, prefix) in enumerate(pairs):
                if cob != want:
                    continue
                if data.startswith(prefix):
                    return i
                if row.get("cls") == "PDO":
                    answered.add(cob)
        return None

    def _trace_view(self) -> tuple[list[dict], dict]:
        """The (rows, counts) the trace panel shows: an opened capture file
        if there is one, otherwise the live record — held still if paused.
        Either way the record itself keeps filling."""
        if self._trace_import is not None:
            return self._trace_import
        if self._trace_freeze is not None:
            return self._trace_freeze
        return self.trace, self._trace_counts

    def act_trace_toggle(self, p: dict) -> None:
        self.trace_paused = not self.trace_paused
        if self.trace_paused:
            # hold the rows as they stand; the record goes on recording
            self._trace_freeze = (list(self.trace), dict(self._trace_counts))
        else:
            self._trace_freeze = None
            self._trace_import = None  # resuming shows live data, not the capture
            self.trace_loaded = None

    def act_trace_clear(self, p: dict) -> None:
        if self._trace_import is not None:
            # with a capture open, "clear" closes it and goes back to live
            # data — the record is not the view's to erase
            self._trace_import = None
            self.trace_loaded = None
            self.trace_paused = False
            return
        self.trace = []
        self._trace_counts = {}
        self._trace_freeze = None
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

    #: What a capture file may be called. ``.json`` is one object written in
    #: one go; ``.jsonl`` is one row per line, which is what autosave needs —
    #: a file being appended to cannot be a single JSON object, since that
    #: object is only complete once its closing bracket is there, and the
    #: whole point is that a capture cut short by a crash still reads.
    CAPTURE_SUFFIXES = (".json", ".jsonl")

    def _capture_files(self) -> list[Path]:
        """Capture files, newest first — by modification time, not by name.
        The names carry a timestamp but also a prefix (``trace_``,
        ``import_``, ``auto_``), and sorting those as strings groups by
        prefix and only then by time."""
        if not self.trace_dir.is_dir():
            return []
        files = [f for f in self.trace_dir.iterdir()
                 if f.suffix in self.CAPTURE_SUFFIXES and f.is_file()]
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    def _refresh_trace_saved(self) -> None:
        self._trace_saved = [{"file": f.name, "size": f.stat().st_size}
                             for f in self._capture_files()]

    def _saved_listing(self) -> list[dict]:
        """The cached listing, with the open autosave segment flagged
        rather than measured.

        It is the one file that grows between refreshes, so a size here
        would be either the 0 bytes it was created with — a capture that
        reads as empty — or a number that moves every tick. This list
        fills the capture dropdown, and a dropdown whose entries change
        ten times a second is rebuilt under the pointer. So the panel
        writes "recording" for this one instead, and the live figure sits
        on the autosave chip beside it, where nothing depends on it
        holding still.
        """
        if not self._autosave_name:
            return self._trace_saved
        return [{**f, "live": True} if f["file"] == self._autosave_name else f
                for f in self._trace_saved]

    # -- autosave ----------------------------------------------------------
    def act_trace_autosave(self, p: dict) -> None:
        self.trace_autosave = not self.trace_autosave
        self.db.set("trace_autosave", self.trace_autosave)
        if self.trace_autosave:
            # a deliberate switch-on tries at once, whatever the last
            # attempt ran into — the operator may well have just made room
            self._autosave_warn, self._autosave_retry_at = "", 0.0
            self.log("TRACE autosave on — every recorded frame is written to a capture as it arrives")
        else:
            self._autosave_close("off")

    def _autosave_write(self, rows: list[dict]) -> None:
        """Append the just-drained rows to the open autosave segment.

        Unfiltered, like the record it copies: the trace filter is a
        property of the view, and a record that only kept what someone
        happened to be looking at would answer no question asked
        afterwards. Flushed per batch, because the run this protects is
        the one that ends badly — a buffer still in the process is a
        buffer lost with it.
        """
        if not rows:
            return
        if self._autosave_fh is None and time.monotonic() < self._autosave_retry_at:
            return  # waiting out a failed attempt, see AUTOSAVE_RETRY_S
        try:
            if self._autosave_fh is not None and self._autosave_bytes >= AUTOSAVE_SEGMENT_BYTES:
                self._autosave_close("segment full")
            if self._autosave_fh is None:
                self._autosave_open()
                if self._autosave_fh is None:  # no room for one — _autosave_open said so
                    return
            text = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
            self._autosave_fh.write(text)
            self._autosave_fh.flush()
            self._autosave_bytes += len(text.encode("utf-8"))
            self._autosave_watch_space()
        except OSError as exc:
            fh = self._autosave_fh
            self._autosave_forget()
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
            self._autosave_wait("cannot write", str(exc))

    def _autosave_wait(self, kind: str, detail: str) -> None:
        """Autosave cannot write at the moment — hold off and try again.

        Never "switch it off". A run can last months, and a recorder that
        gave up for good the one night the disk was tight would still be
        off weeks later, when the fault it exists for finally happens.
        `kind` is what the chip shows and is deliberately stable, so a
        condition that lasts a fortnight costs one log line and not one
        every retry: the state log is evidence too, and a warning that
        floods it destroys what it was warning about.
        """
        self._autosave_retry_at = time.monotonic() + AUTOSAVE_RETRY_S
        if self._autosave_warn == kind:
            return
        self._autosave_warn = kind
        self.log(f"TRACE autosave paused — {detail}. It stays on and keeps trying; "
                 f"the trace itself is unaffected", "emcy0")

    def _autosave_writing(self) -> None:
        """A segment is open again. Says so if anyone was told otherwise."""
        self._autosave_retry_at = 0.0
        if self._autosave_warn:
            self.log(f"TRACE autosave writing again — {self._autosave_warn} cleared")
            self._autosave_warn = ""

    def _autosave_open(self) -> None:
        """Start a segment — after making room for it, and only if there is
        room. Leaves `_autosave_fh` at None when there is not; the caller
        reads that as "not now", not as "not any more"."""
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self._autosave_prune()
        free = self._free_bytes()
        if free is not None and free < AUTOSAVE_FREE_BYTES:
            # everything droppable is gone and the reserve still is not
            # met, so something other than autosave is filling this disk.
            # Wait for it to let go — and name the number, because "no
            # space" alone sends the reader to the wrong volume.
            self._autosave_wait("low disk space",
                                f"{_gb(free)} free, below the {_gb(AUTOSAVE_FREE_BYTES)} "
                                f"the bench keeps clear")
            return
        stem = datetime.now().strftime("auto_%Y%m%d_%H%M%S")
        path = self.trace_dir / f"{stem}.jsonl"
        n = 0
        while path.exists():  # same second twice (off and on again) — don't append to the old one
            n += 1
            path = self.trace_dir / f"{stem}_{n}.jsonl"
        self._autosave_fh = path.open("w", encoding="utf-8")
        self._autosave_name = path.name
        # a header line, so a reader can tell what it has before parsing
        # rows; the loader skips any line that is not a frame
        head = json.dumps({"v": 1, "kind": "autosave",
                           "started": datetime.now().isoformat(timespec="seconds")},
                          separators=(",", ":")) + "\n"
        self._autosave_fh.write(head)
        self._autosave_bytes = len(head)
        self._autosave_checked = self._autosave_bytes
        self._refresh_trace_saved()
        self.log(f"TRACE autosave → {path.name}")
        self._autosave_writing()

    def _autosave_close(self, why: str = "stopped") -> None:
        fh, name, size = self._autosave_fh, self._autosave_name, self._autosave_bytes
        self._autosave_forget()
        if fh is None:
            return
        try:
            fh.close()
        except OSError:
            pass
        self._refresh_trace_saved()
        self.log(f"TRACE autosave {why} — {name} ({size // 1024} kB)")

    def _autosave_forget(self) -> None:
        self._autosave_fh, self._autosave_name, self._autosave_bytes = None, None, 0
        self._autosave_checked = 0

    def _free_bytes(self) -> int | None:
        """Free space where the captures go, or None if the filesystem will
        not say. Unknown is not the same as full: a guard that cannot read
        the number must not act as though it read a small one."""
        try:
            return shutil.disk_usage(self.trace_dir).free
        except OSError:
            return None

    def _autosave_watch_space(self) -> None:
        """Roll the segment early when the disk is filling up.

        Pruning happens when a segment opens, so on its own the reserve
        could be undercut by up to a whole segment before anything gave
        way. Closing early brings that forward: the next batch opens a
        segment, opening prunes, and the two weeks shorten to what fits.
        """
        if self._autosave_bytes - self._autosave_checked < AUTOSAVE_SPACE_EVERY_BYTES:
            return
        self._autosave_checked = self._autosave_bytes
        free = self._free_bytes()
        if free is not None and free < AUTOSAVE_FREE_BYTES:
            self._autosave_close(f"{_gb(free)} free — rolling early")

    def _autosave_prune(self) -> None:
        """Drop autosaved segments past AUTOSAVE_KEEP_DAYS, and then, while
        the free space is under AUTOSAVE_FREE_BYTES, keep dropping the
        oldest — the disk decides when two weeks is too long to promise.

        The newest segment is never a candidate. If the reserve is still
        not met once everything else is gone, autosave's own files are not
        what is filling the disk, and deleting the last of the record buys
        the volume nothing while costing exactly the hour most likely to
        matter. Only files autosave wrote itself are considered — a capture
        saved by hand is a decision, not a by-product — and each removal is
        logged with the reason, so a hole in the record is never silent.
        """
        try:  # stat once, oldest first — the directory can change underneath
            dated = sorted((f.stat().st_mtime, f) for f in self.trace_dir.glob("auto_*.jsonl"))
        except OSError:
            return
        segments = [f for _, f in dated]
        cutoff = time.time() - AUTOSAVE_KEEP_DAYS * 86400
        for mtime, old in dated:
            if mtime >= cutoff:
                break                       # sorted by age: the rest are younger
            if self._drop_segment(old, f"older than {AUTOSAVE_KEEP_DAYS} days"):
                segments.remove(old)
        while len(segments) > 1:            # never the newest one
            free = self._free_bytes()
            if free is None or free >= AUTOSAVE_FREE_BYTES:
                return
            old = segments.pop(0)
            self._drop_segment(old, f"only {_gb(free)} free")

    def _drop_segment(self, path: Path, why: str) -> bool:
        try:
            path.unlink()
        except OSError:
            return False
        self.log(f"TRACE autosave — capture {path.name} removed, {why}")
        return True

    def act_trace_save(self, p: dict) -> None:
        rows, _ = self._trace_view()  # what is on screen is what gets saved
        if not rows:
            return
        name = datetime.now().strftime("trace_%Y%m%d_%H%M%S") + f"_{len(rows)}f.json"
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        (self.trace_dir / name).write_text(
            json.dumps({"v": 1, "rows": rows}, separators=(",", ":")), encoding="utf-8")
        self._refresh_trace_saved()
        self.log(f"TRACE {len(rows)} frames saved → {name}")

    def _read_capture(self, path: Path) -> list[dict]:
        """The rows of a capture file, in either format — the object one
        `act_trace_save` writes, or the line-per-row one autosave appends
        to. An autosave segment is read the same way whether it was closed
        or is still being written to: every line that is there is complete,
        so "the newest frames so far" needs no special case."""
        if path.suffix == ".jsonl":
            rows = []
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if isinstance(row, dict) and "cob" in row:  # anything else is the header
                        rows.append(row)
            return rows
        rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
        if not isinstance(rows, list):
            raise ValueError("rows is not a list")
        return rows

    def act_trace_load(self, p: dict) -> None:
        name = Path(p["file"]).name  # basename only: no path traversal out of trace_dir
        try:
            rows = self._read_capture(self.trace_dir / name)
        except (OSError, ValueError, KeyError) as exc:
            self.log(f"TRACE load failed — {name}: {exc}", "emcy0")
            return
        self._activate_trace_rows(rows, name)
        self.log(f"TRACE {len(rows)} frames loaded from {name} — view paused, recording continues")

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
        """Common tail of load/import: open `rows` as the shown capture and
        compute its class/node counters. The live record is untouched — it
        keeps recording underneath, so a capture opened during a run cannot
        cost a test step the frame it is waiting for; closing the capture
        returns to it."""
        rows = rows[-TRACE_CAP:]
        counts: dict[tuple[str, int | None], int] = {}
        for row in rows:
            row.setdefault("cls", trace_class(row.get("dec", "")))
            row.setdefault("node", trace_node(row.get("cob", "")))
            key = (row["cls"], row["node"])
            counts[key] = counts.get(key, 0) + 1
        self._trace_import = (rows, counts)
        self._trace_freeze = None
        self.trace_paused = True
        self.trace_loaded = name

    def act_trace_del_saved(self, p: dict) -> None:
        name = Path(p["file"]).name
        if name == self._autosave_name:
            # close before unlinking: Windows refuses to remove an open file,
            # and a handle left pointing at a deleted path writes into
            # nothing. Autosave stays on and starts a fresh segment.
            self._autosave_close("capture deleted")
        try:
            (self.trace_dir / name).unlink()
        except OSError:
            pass
        self._refresh_trace_saved()
        if self.trace_loaded == name:  # the open capture is gone: back to live data
            self._trace_import = None
            self.trace_loaded = None
            self.trace_paused = False
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

    def _trace_match(self) -> tuple[int, int]:
        """(rows matching the filter, rows in the shown source). The match
        count comes from the per-(class, node) counters rather than from
        walking the rows — the buffer holds 200k of them and this is read
        on every snapshot."""
        shown, counts = self._trace_view()
        if not (self.trace_hide or self.trace_dev_filter):
            return len(shown), len(shown)
        passes = self._trace_filter_predicate()
        return (sum(n for (cls, node), n in counts.items() if passes(cls, node)),
                len(shown))

    def _trace_page(self, end: int, n: int) -> dict:
        """A window of the filtered trace, counted back from the newest row:
        skip `end` matching rows from the new end, then take `n`. Newest
        first, the order the panel shows them in.

        This is what makes a scrollback longer than one snapshot possible.
        The snapshot carries the last TRACE_VIEW rows and is pushed ten
        times a second — it cannot also carry the hour behind them. So the
        panel asks for the window it is actually scrolled to, on demand,
        and only once the operator has left the live end; up there the rows
        it already has are the newest ones.

        Reading the same source as the panel (`_trace_view`) is the point:
        a capture that is open answers here too, which is where hours of
        scrollback actually come from — the live record is a ring, an
        autosaved capture is not.
        """
        shown, _ = self._trace_view()
        match, _total = self._trace_match()
        if not (self.trace_hide or self.trace_dev_filter):
            hi = max(0, len(shown) - end)
            rows = shown[max(0, hi - n):hi][::-1]
        else:
            passes = self._trace_filter_predicate()
            rows, skipped = [], 0
            for row in reversed(shown):
                if not passes(row["cls"], row["node"]):
                    continue
                if skipped < end:
                    skipped += 1
                    continue
                rows.append(row)
                if len(rows) >= n:
                    break
        return {"rows": rows, "end": end, "total": match}

    def _trace_snapshot(self) -> dict:
        """Last TRACE_VIEW rows *matching the filters* — scanned from the end
        of the full retained buffer, so hidden classes or devices don't push
        visible frames out of the window. The device filter never hides
        broadcast frames (node None: NMT, SYNC, …)."""
        shown, _counts = self._trace_view()
        passes = self._trace_filter_predicate()
        if self.trace_hide or self.trace_dev_filter:
            rows: list[dict] = []
            for row in reversed(shown):
                if passes(row["cls"], row["node"]):
                    rows.append(row)
                    if len(rows) == TRACE_VIEW:
                        break
            rows.reverse()
        else:
            rows = shown[-TRACE_VIEW:]
        match, _ = self._trace_match()
        return {"rows": rows, "paused": self.trace_paused, "hide": sorted(self.trace_hide),
                "devSel": self.trace_dev_filter,
                "total": len(shown), "match": match,
                "saved": self._saved_listing(), "loaded": self.trace_loaded,
                "auto": {"on": self.trace_autosave, "file": self._autosave_name,
                         "bytes": self._autosave_bytes, "warn": self._autosave_warn},
                "stats": self._trace_stats(),
                "plot": {"sel": self.plot_sel,
                        "series": {k: list(v) for k, v in self.plot_series.items()}}}

    def _export_trace_rows(self) -> list[dict]:
        """The full filtered trace — same predicate as `_trace_snapshot`,
        but not capped to TRACE_VIEW: export formats hand over everything
        that matches the current filter, not just the browser's scrollback."""
        shown, _ = self._trace_view()
        if not (self.trace_hide or self.trace_dev_filter):
            return list(shown)
        passes = self._trace_filter_predicate()
        return [row for row in shown if passes(row["cls"], row["node"])]

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
            od = load_eds(path)
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
                    info = info_of(var)
                    default = var.value if var.value is not None else var.default
                    if default is None:
                        val = "—"
                    elif isinstance(default, str):
                        val = default
                    elif isinstance(default, float):
                        val = str(default)
                    else:
                        width = max(info.bits // 8, 1) * 2
                        val = f"0x{int(default) & ((1 << (info.bits or 8)) - 1):0{width}X}"
                    idx, sub = f"0x{var.index:04X}", f"{var.subindex:02X}"
                    catalog[key].append([
                        idx, sub, self._label(idx, sub, info.name),
                        info.type_name, info.access, val, info.lo, info.hi,
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
        # the names every view shows come from those tables (see _label),
        # so a reload that did not empty this would leave the old firmware
        # naming the objects of the new one
        self._sym_labels.clear()
        self._catalog_cache.clear()
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

        # loaded once for the whole table rather than per row: OdCache
        # stats the file on every call, and a catalog is a thousand rows
        # asked again on every tick
        dev = self.sel_devices[0] if self.sel_devices else None
        od = self._ods.load(dev["eds"]) if dev else None

        out: dict[str, dict] = {}
        for key, (width, default) in keys.items():
            # an object nobody has read yet still shows its EDS default, and
            # that has to follow the chosen base too — otherwise half the
            # table stays hex while the other half switches
            raw = self.obj_vals.get(key) or default
            if raw in (None, "", "—"):
                continue
            idx, _, sub = key.partition(":")
            want_i, want_s = _addr_int(idx), _addr_int(sub)
            info = object_info(od, want_i, want_s or 0) if want_i is not None else None
            # a device name is a word. The table used to read it as the
            # number its bytes happen to spell, in whichever base — and
            # neither reading of nineteen digits is the name
            if info is not None and info.is_text and (text := _hex_to_text(raw)):
                out[key] = {"txt": text, "alt": f"{text} · {raw}", "sym": "", "oor": False}
                continue
            try:
                value = int(str(raw), 16)
            except ValueError:
                continue  # string-typed object: leave it exactly as it is
            fields = self._object_fields.get(key, [])
            # what the word *means* as a number. Hex stays the word as
            # stored — that is what a word is — while decimal is the
            # reading the EDS declares: 0xFE0C is -500 on an INTEGER16 and
            # 65036 on a UNSIGNED16, and only the file can say which
            dec = info.signed(value) if info is not None else value
            # what the number means beside the number itself: the symbolic
            # reading where a plugin declared fields, the physical one
            # where it declared a unit. Never both — a mode word is not
            # measured in anything, and a tension is not an enum
            quantity = self._object_units.get(key)
            meaning = describe(value, fields, self.symbols) if fields else ""
            if not meaning and quantity is not None:
                meaning = quantity.with_unit(raw, info.signed_bits if info else 0)
            out[key] = {
                "txt": format_number(value, "hex", width) if self.num_base == "hex"
                else str(dec),
                "alt": alternatives(value, fields, self.symbols, width, dec)
                + (f" · {meaning}" if quantity is not None and meaning else ""),
                "sym": meaning,
                # the EDS's own limits, checked against that same reading.
                # It used to be checked in the browser, against the text on
                # screen parsed as hex — so with the table in decimal a 500
                # was compared as 0x500, and the warning was about 1280
                "oor": info is not None and _out_of_range(dec, info.lo, info.hi),
            }
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
        eds_files = self._eds_rows()
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
            "channel": self.channel_for(self.adapter),
            "channelList": self.channel_list,
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
                "view": self.obj_view,
                "panel": self._panel_view() if self.obj_view == "panel" else None,
                "hasPanel": self._panel() is not None,
            },
            "mirror": self._mirror_data(),
            "psu": self._psu_data(),
            "panels": self._panel_data(),
            "favorites": {
                "rows": self._fav_view(),
                "lastDb": self.db.last_values_ts(first["sn"]) if first else None,
            },
            "raw": self.raw_rows,
            "sync": {"run": self.sync_run, "ms": self.sync_ms},
            "tests": {
                # the last column is what act_run_start's DUT guard reads:
                # a case addressing its device by code carries its own
                # target, only "selected" needs one picked in the Devices
                # box. The demo rows below are shorter and so read as
                # False, which is right — that catalog runs in sim mode
                # and never touches a device.
                "catalog": ([[tc.id, tc.name, ", ".join(tc.tools) or "—",
                              tc.est or "—", bool(tc.error), tc.grade, tc.variants,
                              tc.file, tc.error or "", tc.dut == "selected"]
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
