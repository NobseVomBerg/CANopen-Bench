"""Remote-controllable bench equipment beside the bus — power supplies.

A test case that checks under-voltage behaviour has to move the supply,
and doing that by hand turns an automated case into one somebody has to
stand next to. Everything here exists for that: the tool sets the
voltage, the case keeps its verdict.

The split is the same as for the CAN bus. This module knows what a
supply *is* — channels, a set voltage and current per channel, an output
that is on or off — and a driver knows how one particular instrument is
spoken to. Adding a second instrument is a driver plus its entry in
``DRIVERS``; nothing above this line changes.

Two rules, both learned from what a bench does to you:

* **Nothing is probed behind the operator's back.** Opening a serial port
  and writing to it is not a read-only act: the port might be the CAN
  adapter, or a device that takes offence. Discovery runs when asked for,
  on ports whose description a driver recognises, and the port that
  worked is remembered so the next start opens exactly that one.
* **Set values are not measurements.** ``*LRN?`` and its kin report the
  settings in force, which is not what the terminals are doing under
  load. Both are carried separately — ``volt``/``curr`` are the settings,
  ``meas_volt``/``meas_curr`` the measured values where the instrument
  answers for them, and None where it does not. Showing a setting as a
  measurement is how a report ends up claiming a device was at 26 V while
  it was current-limited at 19.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class InstrumentError(Exception):
    """Talking to the instrument failed — no port, no answer, junk back."""


@dataclass
class Channel:
    """One output channel's *set* values (volts, amps).

    How many of these an instrument has is not a property of the driver:
    the same model line comes with one or two outputs and to 30 or 60 V.
    So the channels are whatever the instrument reports, and the UI draws
    what it is given rather than a fixed pair of boxes.
    """
    volt: float = 0.0
    curr: float = 0.0
    #: what the terminals are actually doing, when the instrument can be
    #: asked — None means it cannot, which is not the same as zero
    meas_volt: float | None = None
    meas_curr: float | None = None
    limit: float | None = None    # the instrument's own voltage limit, if it says
    extra: dict[str, str] = field(default_factory=dict)  # driver-specific, shown raw


@dataclass
class SupplyState:
    """What the box shows. ``output`` is tri-state: True/False, or None
    when the instrument does not report it — never guessed as off."""
    model: str = ""
    serial: str = ""
    firmware: str = ""
    port: str = ""
    output: bool | None = None
    channels: list[Channel] = field(default_factory=list)
    raw: str = ""            # the instrument's own answer, kept for the tooltip


class SerialLink:
    """Line-oriented serial port, opened once and held.

    The obvious alternative — open, write, close, per command — is what a
    script does when it runs for a second and exits. Here it would pay
    the port's open latency (tens to hundreds of ms on a USB bridge) on
    every step of a test case, and a case that walks a supply through six
    voltages would spend most of its time opening a file. So the link is
    held while connected and released on disconnect, which also makes
    "who has the port" an operator decision instead of a race.
    """

    def __init__(self, port: str, opener=None, baud: int = 9600, timeout: float = 1.0):
        self.port = port
        self._opener = opener or _pyserial_opener
        self._baud, self._timeout = baud, timeout
        self._io = None

    def open(self) -> None:
        if self._io is None:
            self._io = self._opener(self.port, self._baud, self._timeout)

    def close(self) -> None:
        if self._io is not None:
            try:
                self._io.close()
            finally:
                self._io = None

    def write(self, cmd: str) -> None:
        self.open()
        self._io.write(cmd.encode("ascii") + b"\r\n")

    def ask(self, cmd: str) -> str:
        self.write(cmd)
        line = self._io.readline()
        return line.decode("ascii", "replace").strip()


def _pyserial_opener(port: str, baud: int, timeout: float):
    try:
        import serial
    except ImportError as exc:  # optional dependency: pip install ".[serial]"
        raise InstrumentError(
            "pyserial is not installed — run pip install \".[serial]\"") from exc
    return serial.Serial(port=port, baudrate=baud, timeout=timeout)


def _pyserial_ports() -> list[tuple[str, str]]:
    """(device, description) for every serial port, or [] without pyserial."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [(p.device, f"{p.description or ''} {p.manufacturer or ''}")
            for p in list_ports.comports()]


class PowerSupply:
    """Driver interface. One instance is one connected instrument."""

    #: shown in the UI
    name = "supply"
    #: substrings that make a serial port worth probing at all — a USB
    #: bridge names its chip, and probing everything is how you write to
    #: the CAN adapter by accident
    port_hints: tuple[str, ...] = ()

    def __init__(self, link: SerialLink):
        self.link = link

    @classmethod
    def identify(cls, link: SerialLink) -> str | None:
        """The instrument's identity string if this driver recognises what
        answers on that link, else None."""
        raise NotImplementedError

    def state(self) -> SupplyState:
        raise NotImplementedError

    def set_voltage(self, channel: int, volts: float) -> None:
        raise NotImplementedError

    def set_current(self, channel: int, amps: float) -> None:
        raise NotImplementedError

    def set_output(self, on: bool) -> None:
        raise NotImplementedError

    def close(self) -> None:
        self.link.close()


def drivers() -> list[type[PowerSupply]]:
    from .toellner import Toellner8952
    return [Toellner8952]


def discover(opener=None, ports=None) -> tuple[PowerSupply, str] | None:
    """Find one supply on the serial ports, or None.

    Only ports a driver's ``port_hints`` match are opened — see the rule
    at the top of this module. Returns the connected driver and the
    identity string it answered with.
    """
    available = ports if ports is not None else _pyserial_ports()
    for device, description in available:
        for driver in drivers():
            if driver.port_hints and not any(h.lower() in description.lower()
                                             for h in driver.port_hints):
                continue
            link = SerialLink(device, opener=opener)
            try:
                idn = driver.identify(link)
            except Exception:
                idn = None
            if idn:
                return driver(link), idn
            link.close()
    return None


def connect(device: str, opener=None) -> tuple[PowerSupply, str] | None:
    """Reconnect to a known port — no probing of anything else."""
    for driver in drivers():
        link = SerialLink(device, opener=opener)
        try:
            idn = driver.identify(link)
        except Exception:
            idn = None
        if idn:
            return driver(link), idn
        link.close()
    return None
