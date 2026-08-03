"""Real ``BusInterface`` implementation backed by the ``canopen`` library.

Adapter-agnostic: which python-can backend actually moves the bytes is
selected purely by the ``interface`` string passed to ``connect()`` — the
built-in ``virtual`` bus (used in tests, no hardware) or any python-can
interface (``ixxat``, ``pcan``, or one registered by a separately installed
driver package such as ``cob-cpcusb``). This class contains no
adapter-specific logic at all; bench plugins extend the adapter-key mapping
via the ``extra_backends`` constructor argument.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime

import can
import canopen

from .interface import NO_SERIAL, BusInterface, FoundDevice, Frame, SdoResult

# app adapter key (canopen_bench.data.ADAPTERS) -> python-can interface name,
# default channel, and any further keyword arguments that backend needs
_ADAPTER_BACKENDS: dict[str, tuple[str, str | int | None, dict]] = {
    "ixxat": ("ixxat", 0, {}),  # VCI channel index — python-can expects an int here
    "pcan": ("pcan", "PCAN_USBBUS1", {}),
    # Vector VN1600 family. ``app_name=None`` addresses the hardware channel
    # by its global index; python-can's default ("CANalyzer") would instead
    # look the channel up through an application entry in Vector's Hardware
    # Config, which need not exist on a machine carrying only the free XL
    # driver — and that free driver is the whole point of supporting these.
    "vector": ("vector", 0, {"app_name": None}),
}


def _backend_entry(value: tuple) -> tuple[str, str | int | None, dict]:
    """Normalise one backend mapping entry.

    Plugins have always returned ``(interface, channel)`` pairs from
    ``adapter_backends`` and keep working unchanged; the third element
    carrying extra keyword arguments is optional, and was added because
    Vector needs one (``app_name``) that the pair had no room for.
    """
    interface, channel, *rest = value
    return interface, channel, dict(rest[0]) if rest else {}


_NMT_COMMAND_BY_KEY = {
    "start": "OPERATIONAL",
    "preop": "PRE-OPERATIONAL",
    "stop": "STOPPED",
    "reset": "RESET",
    "resetcomm": "RESET COMMUNICATION",
}

_SCAN_SETTLE_S = 0.5  # time to let SDO responses to a broadcast scan trickle in

# canopen heartbeat states -> the tokens used by the test-step format
_NMT_STATE_TOKENS = {
    "OPERATIONAL": "operational",
    "PRE-OPERATIONAL": "pre-operational",
    "STOPPED": "stopped",
    "INITIALISING": "boot",
}


def _hex_to_bytes(value: str) -> bytes:
    """"0x00260001" -> b'\\x01\\x00\\x26\\x00' (little-endian, width from digit count)."""
    digits = value.strip().removeprefix("0x").removeprefix("0X") or "0"
    if len(digits) % 2:
        digits = "0" + digits
    return int(digits, 16).to_bytes(len(digits) // 2, "little")


def _bytes_to_hex(data: bytes) -> str:
    if not data:
        return "0x00"
    return "0x" + format(int.from_bytes(data, "little"), f"0{len(data) * 2}X")


def _abort_text(exc: canopen.SdoAbortedError) -> str:
    text = canopen.SdoAbortedError.CODES.get(exc.code, "")
    return f"0x{exc.code:08X}" + (f" {text}" if text else "")


class _TraceListener(can.Listener):
    """Feeds the trace monitor independently of canopen's own Notifier.

    ``network.bus.recv()`` would race canopen's internal ``can.Notifier``
    thread for the same messages (it already drains the bus to dispatch
    SDO/NMT/heartbeat handling) and would starve most of the time. Being a
    second listener on the same Notifier gets every frame without
    interfering with protocol handling.

    The queue holds ``(direction, message, arrival)`` tuples: the Notifier
    delivers RX only, our own TX frames are injected by the send hook
    installed in ``CanopenBus.connect``. ``arrival`` is wall-clock
    ``time.time()`` at reception — driver timestamps are not trusted to be
    epoch-based (see ``poll_frames``).
    """

    def __init__(self, maxlen: int = 16384) -> None:
        # sized to the tick-loop drain (4096 frames / 0.8 s): absorbs full-bus
        # bursts between polls instead of silently dropping the oldest frames
        self.queue: deque[tuple[str, can.Message, float]] = deque(maxlen=maxlen)

    def on_message_received(self, msg: can.Message) -> None:
        self.queue.append(("RX", msg, time.time()))

    def stop(self) -> None:
        pass


class _ErrorListener(can.Listener):
    """Turns a dying Notifier rx thread into an automatic disconnect.

    python-can calls ``on_error`` when ``bus.recv()`` raised (adapter
    unplugged, driver gone — e.g. IXXAT ``VCIError: … disconnected
    communication port``). Without a handler the rx thread just dies with a
    traceback on stderr and the app keeps believing it is connected; with
    one, the exception counts as handled and ``report`` (→
    ``CanopenBus._connection_lost``) tears the bus down and tells the
    service layer.
    """

    def __init__(self, report: Callable[[Exception], None]) -> None:
        self._report = report

    def on_message_received(self, msg: can.Message) -> None:
        pass

    def on_error(self, exc: Exception) -> None:
        self._report(exc)
        # recv() on a dead port raises instantly, so until the teardown
        # thread stops the Notifier its rx loop would spin at full CPU
        time.sleep(0.05)

    def stop(self) -> None:
        pass


def _shutdown_network(network: canopen.Network) -> None:
    """Best-effort ``Network.disconnect()`` that a dead interface can't derail.

    Once the adapter is gone, every teardown step may raise (PDO/periodic
    stops try to send, ``bus.shutdown()`` talks to the driver), and
    ``Network.check()`` at the end re-raises the rx thread's stored
    exception even after an otherwise clean teardown. Fall back to stopping
    Notifier and bus individually so no rx thread is left running.
    """
    try:
        network.disconnect()
        return
    except Exception:
        pass
    try:
        if network.notifier is not None:
            network.notifier.stop(1.0)
    except Exception:
        pass
    try:
        if network.bus is not None:
            network.bus.shutdown()
    except Exception:
        pass
    network.bus = None


class CanopenBus(BusInterface):
    """One instance per attached adapter, real CANopen protocol via ``canopen.Network``."""

    def __init__(self, extra_backends: dict[str, tuple] | None = None) -> None:
        self.network: canopen.Network | None = None
        self._trace: _TraceListener | None = None
        self._ts_offset: float | None = None  # relative driver clock → epoch, per connection
        self.adapter = ""
        self.bitrate = 500
        self._detach_lock = threading.Lock()  # one winner detaches the network
        # built-in mapping plus adapter keys contributed by bench plugins
        self._backends = {key: _backend_entry(value) for key, value
                          in (_ADAPTER_BACKENDS | (extra_backends or {})).items()}

    # -- lifecycle --------------------------------------------------------
    def connect(self, adapter: str, bitrate: int) -> None:
        if adapter not in self._backends:
            raise ValueError(f"unknown adapter: {adapter}")
        if self.network is not None:
            self.disconnect()

        interface, channel, extra = self._backends[adapter]
        network = canopen.Network()
        self._install_listeners(network)
        kwargs: dict = {"interface": interface, "bitrate": bitrate * 1000, **extra}
        if channel is not None:
            kwargs["channel"] = channel
        network.connect(**kwargs)

        self.network = network
        self.adapter = adapter
        self.bitrate = bitrate
        self._ts_offset = None

    def _install_listeners(self, network: canopen.Network) -> None:
        """All listener wiring, before ``network.connect()`` starts the
        Notifier: the trace feed plus the error listener that turns a dead
        interface into an automatic disconnect."""
        self._install_trace(network)
        network.listeners.append(_ErrorListener(self._connection_lost))

    def _install_trace(self, network: canopen.Network) -> None:
        """Wire the trace monitor into a network: the RX listener (must be
        appended before ``network.connect()`` starts the Notifier) and the
        TX send hook — the Notifier only sees received frames, so our own
        outgoing traffic (SDO requests, NMT commands, ...) is mirrored into
        the trace queue at send time."""
        self._trace = _TraceListener()
        network.listeners.append(self._trace)
        trace, original_send = self._trace, network.send_message

        def send_and_trace(can_id: int, data, remote: bool = False):
            now = time.time()
            trace.queue.append(("TX", can.Message(
                arbitration_id=can_id, data=bytes(data),
                is_extended_id=can_id > 0x7FF, is_remote_frame=remote,
                timestamp=now), now))
            return original_send(can_id, data, remote)

        network.send_message = send_and_trace

    def _detach(self) -> canopen.Network | None:
        """Atomically take ownership of the network for teardown; every
        other caller (deliberate disconnect vs. connection-lost, possibly
        racing from different threads) then sees ``None`` and backs off."""
        with self._detach_lock:
            network, self.network = self.network, None
            self._trace = None
        return network

    def disconnect(self) -> None:
        network = self._detach()
        if network is not None:
            _shutdown_network(network)

    def _connection_lost(self, exc: Exception) -> None:
        """The interface vanished mid-session (adapter unplugged, driver
        gone). Called from the Notifier's rx thread (via ``_ErrorListener``)
        or from a failing send path; safe to call repeatedly — only the
        first caller acts.

        The teardown runs on its own thread: ``Notifier.stop()`` joins the
        rx thread, so it must never run *on* that thread — and the send
        paths get their (failed) result back without waiting on driver
        timeouts. ``on_lost`` fires first so the UI flips immediately.
        """
        network = self._detach()
        if network is None:  # never connected, already lost, or a deliberate disconnect won
            return
        reason = str(exc).strip() or type(exc).__name__
        on_lost = self.on_lost

        def teardown() -> None:
            try:
                if on_lost is not None:
                    on_lost(reason)
            finally:
                _shutdown_network(network)

        threading.Thread(target=teardown, name="can-teardown", daemon=True).start()

    def bus_state(self) -> str:
        net = self.network
        if net is None or net.bus is None:
            return ""
        try:
            return net.bus.state.name.lower()  # can.BusState: ACTIVE/PASSIVE/ERROR
        except Exception:  # not every python-can backend implements .state
            return ""

    # -- discovery --------------------------------------------------------
    def scan(self, node_from: int = 1, node_to: int = 127) -> list[FoundDevice]:
        net = self.network
        if net is None:
            return []
        try:
            net.scanner.reset()
            net.scanner.search(limit=node_to)
            time.sleep(_SCAN_SETTLE_S)

            found: list[FoundDevice] = []
            for node_id in sorted(net.scanner.nodes):
                if not (node_from <= node_id <= node_to):
                    continue
                node = self._node(net, node_id)
                name = self._read_string(node, 0x1008, 0) or f"node {node_id:02d}"
                fw = self._read_string(node, 0x100A, 0) or "?"
                sn_bytes = self._try_upload(node, 0x1018, 4)
                sn = _bytes_to_hex(sn_bytes) if sn_bytes else NO_SERIAL
                vendor = self._try_upload(node, 0x1018, 1)
                product = self._try_upload(node, 0x1018, 2)
                # canonical identity format: minimal hex width (core.normalize_identity),
                # NOT _bytes_to_hex — its fixed width would never match the EDS registry
                identity = (
                    f"0x{int.from_bytes(vendor, 'little'):X}"
                    f"·0x{int.from_bytes(product, 'little'):X}"
                    if vendor and product else "?"
                )
                found.append(FoundDevice(
                    node=node_id, name=name, nmt=node.nmt.state, fw=fw, sn=sn, identity=identity,
                ))
            return found
        except (can.CanError, OSError, RuntimeError) as exc:
            self._connection_lost(exc)
            raise ConnectionError("CAN interface lost") from exc

    def _node(self, net: canopen.Network, node_id: int) -> canopen.RemoteNode:
        """Get (or lazily register) the RemoteNode for a node ID.

        sdo_read/sdo_write/nmt can address any node the UI knows about —
        e.g. from a saved setup — without requiring a scan() to have run
        first in this session. Takes the network as a parameter so callers
        keep using the same instance they null-checked, even if the bus is
        detached concurrently by ``_connection_lost``.
        """
        if node_id not in net:
            net.add_node(node_id)
        return net[node_id]

    def _try_upload(self, node, index: int, sub: int) -> bytes | None:
        try:
            return node.sdo.upload(index, sub)
        except (canopen.SdoAbortedError, canopen.SdoCommunicationError):
            return None

    def _read_string(self, node, index: int, sub: int) -> str:
        data = self._try_upload(node, index, sub)
        if not data:
            return ""
        return data.decode("ascii", errors="replace").rstrip("\x00")

    # -- commands ----------------------------------------------------------
    def nmt(self, command: str, node: int | None = None) -> None:
        net = self.network
        if net is None:
            return
        state = _NMT_COMMAND_BY_KEY[command]
        try:
            target = net.nmt if node is None else self._node(net, node).nmt
            target.state = state
        except (can.CanError, OSError, RuntimeError) as exc:
            self._connection_lost(exc)

    def nmt_state(self, node: int) -> str:
        net = self.network
        if net is None:
            return "?"
        try:
            return _NMT_STATE_TOKENS.get(self._node(net, node).nmt.state, "?")
        except Exception:  # unknown node / no heartbeat seen yet
            return "?"

    def send_raw(self, cob: int, data: bytes) -> None:
        net = self.network
        if net is None:
            return
        try:
            net.send_message(cob, data)  # runs through the TX trace hook
        except (can.CanError, OSError, RuntimeError) as exc:
            self._connection_lost(exc)

    def sdo_read(self, node: int, index: str, sub: str) -> SdoResult:
        net = self.network
        if net is None:
            return SdoResult(ok=False, abort="0x05030000 not connected")
        try:
            data = self._node(net, node).sdo.upload(int(index, 16), int(sub, 16))
        except canopen.SdoAbortedError as exc:
            return SdoResult(ok=False, abort=_abort_text(exc))
        except canopen.SdoCommunicationError:
            return SdoResult(ok=False, abort="0x05040000 timeout")
        except (can.CanError, OSError, RuntimeError) as exc:
            self._connection_lost(exc)
            return SdoResult(ok=False, abort="connection lost")
        return SdoResult(ok=True, value=_bytes_to_hex(data))

    def sdo_write(self, node: int, index: str, sub: str, value: str) -> SdoResult:
        net = self.network
        if net is None:
            return SdoResult(ok=False, abort="0x05030000 not connected")
        try:
            self._node(net, node).sdo.download(int(index, 16), int(sub, 16), _hex_to_bytes(value))
        except canopen.SdoAbortedError as exc:
            return SdoResult(ok=False, abort=_abort_text(exc))
        except canopen.SdoCommunicationError:
            return SdoResult(ok=False, abort="0x05040000 timeout")
        except (can.CanError, OSError, RuntimeError) as exc:
            self._connection_lost(exc)
            return SdoResult(ok=False, abort="connection lost")
        return SdoResult(ok=True, value=value)

    def lss_assign(self, count: int) -> int:
        """Assign node-IDs 1..count via standard LSS (CiA 305).

        Unconfigured slaves (node-ID 0xFF — factory state) are identified
        one at a time via LSS fastscan, configured and stored. If nothing
        answers the fastscan and exactly one device is expected, the single
        device is re-addressed via global state switching instead — the
        global path is only unambiguous with one LSS slave on the bus.
        Returns the number of nodes assigned; a partial count means slaves
        stopped answering mid-run.

        UNTESTED against real LSS hardware — written to CiA 305 and the
        canopen library API, exercised only against the demo bus so far
        (see docs/ablaeufe/A-03-scan-verify.md).
        """
        net = self.network
        if net is None or count < 1:
            return 0
        assigned = 0
        try:
            lss = net.lss
            for node_id in range(1, count + 1):
                found, _identity = lss.fast_scan()
                if not found:
                    break
                lss.configure_node_id(node_id)
                lss.store_configuration()
                lss.send_switch_state_global(lss.WAITING_STATE)
                assigned += 1
            if assigned == 0 and count == 1:
                lss.send_switch_state_global(lss.CONFIGURATION_STATE)
                lss.configure_node_id(1)
                lss.store_configuration()
                lss.send_switch_state_global(lss.WAITING_STATE)
                assigned = 1
        except canopen.lss.LssError:
            return assigned  # slave stopped answering — report what we got
        except (can.CanError, OSError, RuntimeError) as exc:
            self._connection_lost(exc)
            raise ConnectionError("CAN interface lost") from exc
        return assigned

    def poll_frames(self, max_frames: int = 8) -> list[Frame]:
        trace = self._trace
        if trace is None:
            return []
        out: list[Frame] = []
        for _ in range(max_frames):
            try:
                direction, msg, arrival = trace.queue.popleft()
            except IndexError:
                break
            # Driver timestamps are epoch-based only for some backends —
            # IXXAT/PCAN/CPC deliver time since adapter or driver start, which
            # fromtimestamp() would render as a bogus 1970-relative clock.
            # Relative clocks are mapped onto wall time with an offset anchored
            # at the first such frame, so inter-frame deltas keep the
            # hardware's µs precision instead of the Notifier's scheduling
            # jitter; the anchor is refreshed if the mapping drifts (PC vs.
            # adapter clock, or an adapter uptime reset).
            ts = msg.timestamp
            if not ts:
                ts = arrival
            elif abs(ts - arrival) > 300:
                if self._ts_offset is None or abs(ts + self._ts_offset - arrival) > 0.5:
                    self._ts_offset = arrival - ts
                ts += self._ts_offset
            out.append(Frame(
                direction=direction,
                cob_id=f"0x{msg.arbitration_id:03X}",
                length=str(msg.dlc),
                data=" ".join(f"{b:02X}" for b in msg.data),
                decoded=_decode_cob(msg.arbitration_id),
                flag="red" if msg.is_error_frame else "",
                time=datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f"),
            ))
        return out


def _decode_cob(cob_id: int) -> str:
    function = cob_id & 0x780
    node_id = cob_id & 0x7F
    if function == 0x080:  # 0x080 itself is SYNC, 0x081..0x0FF are EMCYs
        return "SYNC" if node_id == 0 else f"EMCY node {node_id:02d}"
    labels = {
        0x000: "NMT", 0x180: "TxPDO1", 0x200: "RxPDO1",
        0x280: "TxPDO2", 0x300: "RxPDO2", 0x380: "TxPDO3", 0x400: "RxPDO3",
        0x480: "TxPDO4", 0x500: "RxPDO4", 0x580: "SDO tx", 0x600: "SDO rx",
        0x700: "Heartbeat",
    }
    label = labels.get(function, f"0x{function:03X}")
    return f"{label} node {node_id:02d}" if function else label
