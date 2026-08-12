"""Hardware abstraction for CAN bus adapters.

The bench talks to the bus only through this interface: `CanopenBus`
(python-can / CPC-USB / IXXAT VCI4 / PCANBasic) for real hardware,
`EdsDemoBus` for the hardware-free demo mode — the service and UI layers
never know the difference.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
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
    def sdo_read(self, node: int, index: str, sub: str) -> SdoResult: ...

    @abstractmethod
    def sdo_write(self, node: int, index: str, sub: str, value: str) -> SdoResult: ...

    @abstractmethod
    def poll_frames(self, max_frames: int = 8) -> list[Frame]:
        """Drain received raw frames for the trace monitor."""
