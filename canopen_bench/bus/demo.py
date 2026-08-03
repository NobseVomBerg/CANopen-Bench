"""Demo bus: virtual DUTs generated from the *real* uploaded EDS files.

Selected via the "Demo mode" adapter card. On scan, every enabled EDS entry
whose file actually exists under the workspace's eds/ directory produces
demo devices whose identity matches that EDS — so the normal identity-based
auto-assignment kicks in exactly like with hardware. SDO reads/writes are
served from the EDS's own object dictionary (defaults / ParameterValues)
plus a per-node value store, so the Objects page works against the real
device profile without any adapter attached. Every read/write also queues a
synthetic request/response CAN frame pair (same COB-IDs and expedited SDO
command bytes real hardware would put on the wire), so the Trace page shows
matching traffic and out-of-range writes abort like a real device would.
"""
from __future__ import annotations

from canopen.objectdictionary import (
    DOMAIN,
    OCTET_STRING,
    UNICODE_STRING,
    VISIBLE_STRING,
    ObjectDictionary,
    ODVariable,
)

from ..db import Db
from ..eds_od import OdCache, find_var, pdo_mapping
from .canopen_bus import _decode_cob
from .interface import BusInterface, FoundDevice, Frame, SdoResult

_NMT_TO_STATE = {"start": "Operational", "preop": "Pre-Operational",
                 "stop": "Stopped", "reset": "Pre-Operational",
                 "resetcomm": "Pre-Operational"}

# -- synthetic SDO trace frames ----------------------------------------------
# Expedited SDO transfer: command-specifier byte keyed by payload length
# (1-4 bytes) — the same encoding core._annotate_sdo already decodes for
# real-hardware traces (see its _EXPEDITED_LEN table).
_UPLOAD_CMD = {1: 0x4F, 2: 0x4B, 3: 0x47, 4: 0x43}    # read response
_DOWNLOAD_CMD = {1: 0x2F, 2: 0x2B, 3: 0x27, 4: 0x23}  # write request
_TEXT_TYPES = {VISIBLE_STRING, UNICODE_STRING, DOMAIN, OCTET_STRING}


def _parse_int(value: str) -> int | None:
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        return None


def _payload_width(var: ODVariable | None) -> int:
    """Byte width of the expedited SDO payload: the variable's real EDS
    size capped to 4 (expedited transfer's limit), or 4 with no type info
    (identity specials, which have no backing ODVariable)."""
    return min(max((len(var) if var is not None else 32) // 8, 1), 4)


def _sdo_payload(var: ODVariable | None, value: str, width: int) -> bytes:
    """4-byte little-endian expedited payload for a value string. Numeric
    EDS types are masked/packed to `width`, matching the read-side hex
    formatting in `_format`; text types — or a value that isn't hex — fall
    back to ASCII, both padded to the expedited frame's fixed 4-byte data
    field."""
    if var is None or var.data_type not in _TEXT_TYPES:
        num = _parse_int(value)
        if num is not None:
            return (num & ((1 << (width * 8)) - 1)).to_bytes(width, "little").ljust(4, b"\x00")
    return value.encode("ascii", errors="replace")[:4].ljust(4, b"\x00")


def _abort_bytes(abort: str) -> bytes:
    try:
        code = int(abort.split()[0], 16)
    except (IndexError, ValueError):
        code = 0x08000000  # general error — fallback if the text is unparseable
    return code.to_bytes(4, "little")


def _sdo_frame(node: int, is_request: bool, cmd: int, idx: int, sub: int, data: bytes) -> Frame:
    """One synthetic SDO frame — same COB-IDs and expedited-transfer byte
    layout real hardware produces, so core._annotate_sdo decodes it
    identically (object name, value/abort) whether the bus is real or demo.
    """
    cob = (0x600 if is_request else 0x580) + node
    payload = bytes([cmd, idx & 0xFF, idx >> 8, sub]) + data
    return Frame(
        direction="TX" if is_request else "RX",
        cob_id=f"0x{cob:03X}", length="8",
        data=" ".join(f"{b:02X}" for b in payload),
        decoded=f"SDO {'rx' if is_request else 'tx'} node {node:02d}",
    )


class EdsDemoBus(BusInterface):
    simulated = True

    def __init__(self, db: Db) -> None:
        self.db = db
        self.connected = False
        self.adapter = "demo"
        self.bitrate = 500
        self._devices: dict[int, dict] = {}  # node -> {entry, sn, fw}
        self._nmt: dict[int, str] = {}
        self._store: dict[tuple[int, int, int], str] = {}  # (node, idx, sub) -> display value
        self._ods = OdCache(db.eds_dir)
        self._hb_node_idx = 0
        self._sdo_frames: list[Frame] = []  # synthetic frames pending for the trace
        # device-side protocol hooks (plugin.DemoHook) that simulate vendor
        # behaviour, e.g. button-teach
        self._hooks: list = []
        self.session: bytes | None = None  # last stored session identity (set by hooks)

    def retarget_eds(self, eds_dir) -> None:
        """The workspace's EDS folder moved — reload ODs from the new place."""
        self._ods.retarget(eds_dir)

    # -- lifecycle ----------------------------------------------------------
    def connect(self, adapter: str, bitrate: int) -> None:
        self.adapter = adapter
        self.bitrate = bitrate
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False
        self._devices = {}
        self._nmt = {}
        self._sdo_frames = []

    # -- EDS object dictionaries ---------------------------------------------
    def _load_od(self, file: str) -> ObjectDictionary | None:
        return self._ods.load(file)

    # -- discovery ------------------------------------------------------------
    def scan(self, node_from: int = 1, node_to: int = 127) -> list[FoundDevice]:
        if not self.connected:
            return []
        entries = [e for e in self.db.eds_list()
                   if e["enabled"] and self._load_od(e["file"]) is not None]

        self._devices = {}
        found: list[FoundDevice] = []
        node = 1
        for i, entry in enumerate(entries):
            # two DUTs for the first profile, one for each further profile —
            # gives a multi-device demo without flooding the device box
            for _ in range(2 if i == 0 else 1):
                if not (node_from <= node <= node_to):
                    node += 1
                    continue
                sn = f"26{node:04d}"
                fw = "1.0.0-demo"
                self._devices[node] = {"entry": entry, "sn": sn, "fw": fw}
                nmt = self._nmt.setdefault(node, "Pre-Operational")
                found.append(FoundDevice(
                    node=node, name=entry["dev"], nmt=nmt, fw=fw, sn=sn,
                    identity=entry["ident"],
                ))
                node += 1
        return found

    _NMT_CODE = {"start": 0x01, "stop": 0x02, "preop": 0x80, "reset": 0x81, "resetcomm": 0x82}

    # -- commands ----------------------------------------------------------------
    def nmt(self, command: str, node: int | None = None) -> None:
        code = self._NMT_CODE.get(command)
        if code is not None:  # mirror the NMT command into the trace like real TX
            self._sdo_frames.append(Frame(
                direction="TX", cob_id="0x000", length="2",
                data=f"{code:02X} {node or 0:02X}", decoded="NMT"))
        state = _NMT_TO_STATE.get(command)
        if state is None:
            return
        for n in ([node] if node is not None else list(self._devices)):
            self._nmt[n] = state

    _STATE_TOKEN = {"Operational": "operational", "Pre-Operational": "pre-operational",
                    "Stopped": "stopped"}

    def nmt_state(self, node: int) -> str:
        return self._STATE_TOKEN.get(self._nmt.get(node, ""), "?")

    _find_var = staticmethod(find_var)

    @staticmethod
    def _missing_abort(od, idx: int) -> str:
        """CiA-301 distinguishes an unknown index (0x06020000) from a known
        index with an unknown sub-index (0x06090011) — `find_var` collapses
        both into None, so this re-derives which one actually applies."""
        return ("0x06020000 object does not exist" if od.get(idx) is None
                else "0x06090011 sub-index does not exist")

    def _format(self, var: ODVariable, value) -> str:
        if value is None:
            return "—"
        if isinstance(value, str):
            return value
        if isinstance(value, float):
            return str(value)
        width = max(len(var) // 8, 1) * 2
        mask = (1 << (len(var) or 8)) - 1
        return f"0x{int(value) & mask:0{width}X}"

    def sdo_read(self, node: int, index: str, sub: str) -> SdoResult:
        res = self._read(node, index, sub)
        self._trace_sdo(node, index, sub, write=False, written=None, res=res)
        return res

    def _read(self, node: int, index: str, sub: str) -> SdoResult:
        dev = self._devices.get(node)
        if not self.connected or dev is None:
            return SdoResult(ok=False, abort="0x05040000 timeout")
        idx, s = int(index, 16), int(sub or "0", 16)

        hit = self._store.get((node, idx, s))
        if hit is not None:
            return SdoResult(ok=True, value=hit)

        # identity/name specials answer per-device even if the EDS carries
        # no defaults for them
        entry = dev["entry"]
        if idx == 0x1018 and s == 4:
            return SdoResult(ok=True, value=dev["sn"])
        if idx == 0x1008 and s == 0:
            return SdoResult(ok=True, value=entry["dev"])
        if idx == 0x100A and s == 0:
            return SdoResult(ok=True, value=dev["fw"])

        od = self._load_od(entry["file"])
        var = self._find_var(od, idx, s) if od else None
        if var is None:
            return SdoResult(ok=False, abort=self._missing_abort(od, idx) if od
                              else "0x06020000 object does not exist")
        value = var.value if var.value is not None else var.default
        return SdoResult(ok=True, value=self._format(var, value))

    def sdo_write(self, node: int, index: str, sub: str, value: str) -> SdoResult:
        res = self._write(node, index, sub, value)
        self._trace_sdo(node, index, sub, write=True, written=value, res=res)
        return res

    def _write(self, node: int, index: str, sub: str, value: str) -> SdoResult:
        dev = self._devices.get(node)
        if not self.connected or dev is None:
            return SdoResult(ok=False, abort="0x05040000 timeout")
        idx, s = int(index, 16), int(sub or "0", 16)
        od = self._load_od(dev["entry"]["file"])
        var = self._find_var(od, idx, s) if od else None
        if var is None:
            return SdoResult(ok=False, abort=self._missing_abort(od, idx) if od
                              else "0x06020000 object does not exist")
        if not var.writable:
            return SdoResult(ok=False, abort="0x06010002 write to read-only object")
        num = _parse_int(value)
        if num is not None:
            if var.max is not None and num > var.max:
                return SdoResult(ok=False, abort="0x06090031 value above maximum")
            if var.min is not None and num < var.min:
                return SdoResult(ok=False, abort="0x06090032 value below minimum")
        self._store_value(node, idx, s, value)
        return SdoResult(ok=True, value=value)

    def _store_value(self, node: int, idx: int, sub: int, value: str) -> None:
        """One write into the device's value store (SDO write or applied
        RPDO). Simple drive model: 0x606C (velocity actual) follows a
        write to 0x60FF (target velocity), so the demo visibly reacts."""
        self._store[(node, idx, sub)] = value
        if idx == 0x60FF:
            self._store[(node, 0x606C, sub)] = value

    # -- trace generation -----------------------------------------------------
    def _trace_sdo(self, node: int, index: str, sub: str, write: bool,
                    written: str | None, res: SdoResult) -> None:
        """Queue a synthetic request/response frame pair for the trace
        monitor so a demo-mode read/write shows up on the Trace page like it
        would against real hardware. Skipped for the "no such device"
        timeout — there is nothing real hardware would put on the wire
        either."""
        if not self.connected or node not in self._devices:
            return
        dev = self._devices[node]
        idx, s = int(index, 16), int(sub or "0", 16)
        od = self._load_od(dev["entry"]["file"])
        var = self._find_var(od, idx, s) if od else None
        width = _payload_width(var)

        if write:
            req = _sdo_frame(node, True, _DOWNLOAD_CMD[width], idx, s,
                              _sdo_payload(var, written or "0", width))
            resp = (_sdo_frame(node, False, 0x60, idx, s, b"\x00" * 4) if res.ok else
                    _sdo_frame(node, False, 0x80, idx, s, _abort_bytes(res.abort)))
        else:
            req = _sdo_frame(node, True, 0x40, idx, s, b"\x00" * 4)
            resp = (_sdo_frame(node, False, _UPLOAD_CMD[width], idx, s,
                                _sdo_payload(var, res.value, width)) if res.ok else
                    _sdo_frame(node, False, 0x80, idx, s, _abort_bytes(res.abort)))
        self._sdo_frames += [req, resp]

    # -- device-side protocol simulation (plugin.DemoHook) -------------------
    # The demo bus itself only knows generic CANopen; vendor protocols (e.g.
    # the button-teach addressing, A-05) are simulated by hooks that plugins
    # install. Hooks drive the simulation through the small API below.
    def install_hooks(self, hooks: list) -> None:
        self._hooks = list(hooks)

    def queue_raw(self, cob: int, data: bytes) -> None:
        """Emit a device-side raw frame (boot-ups, replies).

        It goes into the trace like any other received frame — that is
        where a `wait_for` step reads, on this bus exactly as on real
        hardware, so a flow behaves the same in demo mode."""
        self._sdo_frames.append(Frame(
            direction="RX", cob_id=f"0x{cob:03X}", length=str(len(data)),
            data=" ".join(f"{b:02X}" for b in data), decoded=_decode_cob(cob)))

    def device_nodes(self) -> list[int]:
        return sorted(self._devices)

    def renumber(self, old: int, new: int) -> None:
        """Move a demo device to a new node-ID (addressing simulation)."""
        if old != new and old in self._devices:
            self._devices[new] = self._devices.pop(old)
            self._nmt.pop(old, None)
        self._nmt[new] = "Pre-Operational"

    def set_nmt(self, node: int, state: str) -> None:
        self._nmt[node] = state

    def emit_emcy(self, node: int, code: int, register: int = 0x00,
                  mfr: bytes = b"") -> None:
        """Simulate a device raising an EMCY: error code u16 LE, error
        register, 5 bytes manufacturer-specific (CiA-301 frame layout)."""
        payload = bytes((code & 0xFF, code >> 8, register)) + (mfr + b"\x00" * 5)[:5]
        self._sdo_frames.append(Frame(
            direction="RX", cob_id=f"0x{0x080 + node:03X}", length="8",
            data=" ".join(f"{b:02X}" for b in payload),
            decoded=_decode_cob(0x080 + node)))

    def send_raw(self, cob: int, data: bytes) -> None:
        # mirror the outgoing frame into the trace (the real bus does this
        # via its TX send hook), then offer it to the device-side hooks
        self._sdo_frames.append(Frame(
            direction="TX", cob_id=f"0x{cob:03X}", length=str(len(data)),
            data=" ".join(f"{b:02X}" for b in data), decoded=_decode_cob(cob)))
        for hook in self._hooks:
            if hook.on_raw_frame(self, cob, data):
                return
        self._apply_rpdo(cob, data)

    _RPDO_MAP_INDEX = {0x200: 0x1600, 0x300: 0x1601, 0x400: 0x1602, 0x500: 0x1603}

    def _apply_rpdo(self, cob: int, data: bytes) -> None:
        """Device side of a received RPDO: unpack the payload per the EDS
        default mapping into the device's value store — like a real device
        applying process data. Subsequent SDO reads and TPDOs show it."""
        mapping_index = self._RPDO_MAP_INDEX.get(cob & 0x780)
        node = cob & 0x7F
        dev = self._devices.get(node)
        if mapping_index is None or dev is None:
            return
        od = self._load_od(dev["entry"]["file"])
        if od is None:
            return
        raw = int.from_bytes(data, "little")
        pos = 0
        for idx, sub, bits in pdo_mapping(od, mapping_index):
            if bits <= 0 or pos + bits > len(data) * 8:
                break
            val = (raw >> pos) & ((1 << bits) - 1)
            pos += bits
            self._store_value(node, idx, sub, f"0x{val:X}")

    def press_button(self) -> None:
        """Operator pressed a demo device button (Setup page, demo mode)."""
        for hook in self._hooks:
            if hook.press_button(self):
                return

    def lss_assign(self, count: int) -> int:
        """Demo: scan enumerates devices contiguously from node 1 anyway, so
        standard-LSS assignment renumbers to 1..count (usually a no-op) and
        confirms — enough to exercise the shipped lss_standard flow without
        hardware."""
        if not self.connected:
            return 0
        nodes = sorted(self._devices)
        for new, old in enumerate(nodes[:count], start=1):
            self.renumber(old, new)
        return min(len(nodes), count)

    def poll_frames(self, max_frames: int = 8) -> list[Frame]:
        if not self.connected:
            return []
        out = self._sdo_frames[:max_frames]
        del self._sdo_frames[:len(out)]
        remaining = max_frames - len(out)
        if self._devices and remaining > 0:
            nodes = sorted(self._devices)
            hb_code = {"Operational": "05", "Pre-Operational": "7F", "Stopped": "04"}
            for _ in range(min(remaining, len(nodes))):
                node = nodes[self._hb_node_idx % len(nodes)]
                self._hb_node_idx += 1
                state = self._nmt.get(node, "Pre-Operational")
                out.append(Frame(
                    direction="RX", cob_id=f"0x{0x700 + node:03X}", length="1",
                    data=hb_code.get(state, "7F"),
                    decoded=f"HB · node {node:02d} {state}",
                ))
            # operational devices additionally publish TPDO1 like real
            # hardware would — payload packed from the EDS default mapping
            for node in nodes:
                if len(out) >= max_frames:
                    break
                if self._nmt.get(node) == "Operational":
                    frame = self._tpdo1_frame(node)
                    if frame:
                        out.append(frame)
        return out

    def _tpdo1_frame(self, node: int) -> Frame | None:
        """TPDO1 of a demo device: signals per the EDS default mapping
        (0x1A00), values from SDO-written state with EDS defaults as
        fallback — so a demo TPDO reflects what SDO reads would report."""
        od = self._load_od(self._devices[node]["entry"]["file"])
        if od is None:
            return None
        entries = pdo_mapping(od, 0x1A00)
        if not entries:
            return None
        raw = 0
        pos = 0
        for idx, sub, bits in entries:
            val = None
            stored = self._store.get((node, idx, sub))
            if stored is not None:
                val = _parse_int(stored)
            if val is None:
                var = self._find_var(od, idx, sub)
                default = var.value if var is not None and var.value is not None \
                    else (var.default if var is not None else None)
                val = int(default) if isinstance(default, (int, float)) else 0
            raw |= (val & ((1 << bits) - 1)) << pos
            pos += bits
        if pos == 0:
            return None
        data = raw.to_bytes((pos + 7) // 8, "little")
        return Frame(
            direction="RX", cob_id=f"0x{0x180 + node:03X}", length=str(len(data)),
            data=" ".join(f"{b:02X}" for b in data),
            decoded=f"TxPDO1 node {node:02d}")
