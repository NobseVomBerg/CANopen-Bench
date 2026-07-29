"""Töllner 8952 laboratory power supply.

Talked to over its serial port (USB models bridge through an FTDI chip,
which has to be in VCP mode). The unit answers ``*IDN?`` but not much
else of SCPI — the manual's own note is that its short commands are the
ones that work, so those are what this driver sends:

    SEL 1        select channel 1 for the commands that follow
    V 26.0       set voltage of the selected channel
    C 1.5        set current limit of the selected channel
    EX 1 / EX 0  output on / off — one switch for both channels
    *LRN?        report all settings, every channel, in one line

``*LRN?`` answers with semicolon-separated ``KEY VALUE`` pairs, where
``SEL n`` starts that channel's block and the tail after the last block
is global:

    SEL 1;V 005.00;C 00.500;P 0205.0;OVP 0066.0;…;SEL 2;V 057.00;…;TRA 0;EX 1

Only V, C and EX are interpreted. The rest is carried along as text and
shown as-is: guessing at what ``PV`` or ``AVM`` mean would put invented
labels on a screen next to real ones, and there is no way for the reader
to tell which is which.

These are **set** values. What the terminals actually do is a different
question, and this instrument's manual does not obviously answer it —
until it does, nothing here claims to be a measurement.
"""
from __future__ import annotations

from . import Channel, PowerSupply, SerialLink, SupplyState

#: what the unit calls itself: "TOELLNER,TOE8952-60,102625,3.63-3.62"
_VENDOR = "TOELLNER"


class Toellner8952(PowerSupply):
    name = "Töllner 8952"
    port_hints = ("FTDI",)

    def __init__(self, link: SerialLink):
        super().__init__(link)
        self.idn = ""

    @classmethod
    def identify(cls, link: SerialLink) -> str | None:
        answer = link.ask("*IDN?")
        return answer if _VENDOR in answer.upper() else None

    def state(self) -> SupplyState:
        if not self.idn:
            self.idn = self.link.ask("*IDN?")
        raw = self.link.ask("*LRN?")
        st = parse_settings(raw)
        st.port = self.link.port
        st.model, st.serial, st.firmware = _split_idn(self.idn)
        return st

    def _select(self, channel: int) -> None:
        self.link.write(f"SEL {int(channel)}")

    def set_voltage(self, channel: int, volts: float) -> None:
        # one line, not two writes: the unit takes several commands
        # separated by semicolons, and a select that arrives without its
        # value would leave the wrong channel armed for whatever comes next
        self.link.write(f"SEL {int(channel)};V {float(volts):.2f}")

    def set_current(self, channel: int, amps: float) -> None:
        self.link.write(f"SEL {int(channel)};C {float(amps):.3f}")

    def set_output(self, on: bool) -> None:
        self.link.write(f"EX {1 if on else 0}")


def _split_idn(idn: str) -> tuple[str, str, str]:
    """"TOELLNER,TOE8952-60,102625,3.63-3.62" -> model, serial, firmware."""
    parts = [p.strip() for p in idn.split(",")]
    while len(parts) < 4:
        parts.append("")
    return parts[1], parts[2], parts[3]


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def parse_settings(raw: str) -> SupplyState:
    """The ``*LRN?`` line into a SupplyState.

    Unknown keys are kept per channel as text rather than dropped — this
    unit reports a dozen of them, and a later question ("was the
    over-voltage protection still at 66 V?") is answerable from the
    tooltip instead of from nothing.

    The number of channels comes out of the answer, not out of a constant:
    this model line exists with one output and with two.
    """
    st = SupplyState(raw=raw)
    current: Channel | None = None
    for token in raw.split(";"):
        token = token.strip()
        if not token:
            continue
        key, _, value = token.partition(" ")
        key, value = key.upper(), value.strip()
        if key == "SEL":
            current = Channel()
            st.channels.append(current)
            continue
        if key == "EX":
            st.output = value.startswith("1")
            continue
        if current is None:      # a global key before the first SEL block
            continue
        if key == "V" and (v := _number(value)) is not None:
            current.volt = v
        elif key == "C" and (v := _number(value)) is not None:
            current.curr = v
        elif key == "VLIM" and (v := _number(value)) is not None:
            # the unit's own ceiling for this channel — the 8952 comes as a
            # 30 V and a 60 V model, so this is read, never assumed
            current.limit = v
            current.extra[key] = value
        else:
            current.extra[key] = value
    return st
