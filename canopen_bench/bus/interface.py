"""Hardware abstraction for CAN bus adapters.

The bench talks to the bus only through this interface: `CanopenBus`
(python-can / CPC-USB / IXXAT VCI4 / PCANBasic) for real hardware,
`EdsDemoBus` for the hardware-free demo mode — the service and UI layers
never know the difference.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass

#: a device that does not answer 0x1018:04 has no serial number to be told
#: apart by — anything the bench keeps per device has to treat this as "no
#: identity", never as one shared by every such device
NO_SERIAL = "?"


@dataclass
class FoundDevice:
    node: int
    name: str
    nmt: str
    fw: str
    sn: str
    identity: str  # "vendor·product" signature from object 0x1018


@dataclass
class Frame:
    direction: str  # RX / TX
    cob_id: str
    length: str
    data: str
    decoded: str
    flag: str = ""  # "red" for error frames
    time: str = ""  # bus timestamp "HH:MM:SS.ffffff" (µs); "" = stamp at poll time


@dataclass
class SdoResult:
    ok: bool
    value: str = ""
    abort: str = ""


class BusInterface(ABC):
    """One instance per attached adapter."""

    adapter: str = ""
    bitrate: int = 500
    simulated: bool = False  # simulated buses keep the demo scan latency
    # Set by the service layer. A backend calls this — possibly from a
    # background thread — after it detected that the interface vanished
    # mid-session (adapter unplugged, driver gone) and tore itself down.
    on_lost: Callable[[str], None] | None = None

    def bus_state(self) -> str:
        """Controller health: "active" | "passive" | "error"; "" = unknown.

        Distinguishes "bus idle, nobody answered" from "our own frames are
        not being acked" (wrong bitrate / wiring) after a 0-device scan.
        """
        return ""

    def nmt_state(self, node: int) -> str:
        """Last known NMT state of a node, in heartbeat tokens:
        "boot" | "stopped" | "operational" | "pre-operational"; "?" unknown.
        Backs the test-step primitive `wait_for: {heartbeat: ...}`."""
        return "?"

    # -- raw frames (format-v2 primitive can_send) --------------------------
    def send_raw(self, cob: int, data: bytes) -> None:
        """Broadcast a raw CAN frame (e.g. the button-teach 0x780/0x781)."""

    def send_frames(self, cob: int, payloads: Iterable[bytes],
                    stop: Callable[[], bool] | None = None, gap: float = 0.0) -> int:
        """Many frames on one COB-ID, as fast as the interface takes them,
        from the calling thread — a firmware image as PDOs, say, where one
        ``send_raw`` per frame from the event loop costs a thread hop and a
        timer tick every time. Returns how many went out: fewer than given
        when ``stop()`` answered True (asked before every frame) or the
        interface went away.

        ``gap`` is the least time between two frames, kept by the sending
        thread — ``asyncio.sleep`` cannot keep it, its resolution on Windows
        is a timer tick of up to 15 ms. 0 sends back to back, paced only by
        the interface's own transmit queue.

        Default: one ``send_raw`` per frame.
        """
        def one(data: bytes) -> bool:
            self.send_raw(cob, data)
            return True

        return pace_frames(payloads, one, stop, gap)


    # -- standard addressing (format-v2 primitive lss_assign) ----------------
    def lss_assign(self, count: int) -> int:
        """Assign node-IDs 1..count via standard LSS (CiA 305); returns the
        number of nodes actually assigned. Default: not supported."""
        return 0

    def channels(self, adapter: str) -> list[dict]:
        """What this adapter's driver reports as available:
        ``[{value, label}]``, empty where a backend cannot say. Offered to
        the operator so the channel is picked from what is there rather
        than typed from memory."""
        return []

    @abstractmethod
    def connect(self, adapter: str, bitrate: int, channel: str | int | None = None) -> None:
        """Open the interface. ``channel`` overrides the backend's default
        — which adapter counts channels how is the backend's business."""

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def scan(self, node_from: int = 1, node_to: int = 127) -> list[FoundDevice]:
        """Probe the node-id range; read identity object 0x1018 of responders."""

    @abstractmethod
    def nmt(self, command: str, node: int | None = None) -> None:
        """command: start | preop | stop | reset | resetcomm; node None = all."""

    @abstractmethod
    def sdo_read(self, node: int, index: str, sub: str,
                 timeout: float | None = None) -> SdoResult:
        """``timeout`` is how long this one transfer may wait for the
        answer, in seconds; None leaves the backend's default alone. Per
        call, because the waiting time belongs to the operation and not to
        the bus: a flash erase answers after seconds, while an ordinary
        read that takes that long is a fault."""

    @abstractmethod
    def sdo_write(self, node: int, index: str, sub: str, value: str,
                  timeout: float | None = None) -> SdoResult:
        """``timeout``: see ``sdo_read``."""

    def sdo_download(self, node: int, index: str, sub: str, data: bytes,
                     progress: Callable[[int, int], bool] | None = None,
                     timeout: float | None = None) -> SdoResult:
        """Write a block of bytes to one object — segmented CiA-301 domain
        download, the way a firmware image reaches a bootloader. Default:
        not supported.

        Never block transfer. A server may refuse it (the bench has seen
        one answer the block-initiate with an abort), and there is no
        falling back to segmented from inside the library call — the
        transfer is over by then.

        ``progress(sent, total) -> bool`` is called after every chunk;
        returning False cancels the download, which comes back as
        ``SdoResult(ok=False, abort="cancelled")``.

        The bytes go on the wire in the order they are given. That is the
        reason this exists next to ``sdo_write``, which takes a hex
        *string*: ``_hex_to_bytes`` reads it as one little-endian integer,
        and a sequence of bytes comes out of that reversed.
        """
        return SdoResult(ok=False, abort="not supported")

    @abstractmethod
    def poll_frames(self, max_frames: int = 8) -> list[Frame]:
        """Drain received raw frames for the trace monitor."""


def pace_frames(payloads: Iterable[bytes], send_one: Callable[[bytes], bool],
                stop: Callable[[], bool] | None = None, gap: float = 0.0) -> int:
    """The loop behind every ``send_frames``: ``stop`` asked before each
    frame, ``gap`` kept between them, and the count of frames ``send_one``
    took — it answers False when the interface is gone, which ends the
    burst there."""
    sent = 0
    due = time.perf_counter()
    for data in payloads:
        if stop is not None and stop():
            break
        if gap > 0:
            _wait_until(due)
        if not send_one(data):
            break
        sent += 1
        due = time.perf_counter() + gap
    return sent


def _wait_until(deadline: float) -> None:
    """Sleep through the bulk of the wait and spin through the last
    millisecond: ``time.sleep`` overshoots by up to a millisecond, which on
    a gap of a few hundred microseconds is the gap several times over."""
    while True:
        left = deadline - time.perf_counter()
        if left <= 0:
            return
        if left > 0.002:
            time.sleep(left - 0.001)
