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

``*LRN?`` reports the settings in force. What the terminals are doing
under load is a different number, and for that the unit speaks SCPI:

    :MEAS:VOLT?;CURR?     measured voltage and current of the selected
                          channel, both in one answer, separated by ";"

That chained form comes from the manufacturer's own LabVIEW library
(TOE8950_LT), which asks ``:MEAS:VOLT?;CURR?;POW?;:SENSE:AVER?`` in a
single write. It is worth copying: a measurement per round trip costs
the read timeout each time it is not answered, and this unit is known
not to accept every SCPI command.

Which is the catch. The 8950 series has **two command languages** — the
short commands above and SCPI — and the library carries a
``SYST:LANG CII`` to switch between them. This driver never switches:
that would change the device under every other tool on the bench,
including the operator's own scripts. It works out which language it is
being answered in instead, and stays in it. These supplies are in
service for decades; what a given unit accepts is not something to
assume from its model number.

So: settings are read with ``*LRN?``, and if that says nothing, the same
question is asked in SCPI. The measurement query is tried once as it
stands. If the unit answers, its values are shown; if not, the driver
stops asking and the box shows settings only. A measured value is never
the setting wearing a different label.

The SCPI path is **untested against hardware** — this bench's unit
speaks the short language, so that is the one with evidence behind it.

Other commands the library uses, none of them needed here yet, but
written down so the next question does not need this file again:
``:INST OUT1|OUT2`` (select), ``:VOLT``/``:CURR``/``:POW`` (set),
``:OUTP 0|1`` and ``:OUTP?``, ``:VOLT?``/``:CURR?``/``:POW?``,
``:VOLT:PROT?`` (over-voltage), ``:VOLT:SLEW:STAT FAST|SLOW``,
``:SENS:AVER ON|OFF``, ``:STAT:QUES:EVEN?``, ``SYST:ERR?``, ``*CLS``,
``*RST``, ``*OPC?``.
"""
from __future__ import annotations

from . import Channel, PowerSupply, SerialLink, SupplyState

#: what the unit calls itself: "TOELLNER,TOE8952-60,102625,3.63-3.62"
_VENDOR = "TOELLNER"

#: measured voltage and current in one answer, values separated by ";"
MEASURE = ":MEAS:VOLT?;CURR?"

#: which language a given unit is answering in
SHORT, SCPI = "short", "scpi"

#: how many channels to look for when the unit only speaks SCPI — there is
#: no "list your channels" query, so the driver asks and keeps what answers
_MAX_CHANNELS = 2


class Toellner8952(PowerSupply):
    name = "Töllner 8952"
    port_hints = ("FTDI",)

    def __init__(self, link: SerialLink):
        super().__init__(link)
        self.idn = ""
        #: None = not tried yet, True/False = this unit answers MEAS: or not
        self.measures: bool | None = None
        #: which language this unit answered in — settled on the first read
        self.dialect = SHORT

    @classmethod
    def identify(cls, link: SerialLink) -> str | None:
        answer = link.ask("*IDN?")
        return answer if _VENDOR in answer.upper() else None

    def state(self) -> SupplyState:
        if not self.idn:
            self.idn = self.link.ask("*IDN?")
        raw = self.link.ask("*LRN?")
        st = parse_settings(raw)
        if not st.channels:
            # this unit does not answer the short language — ask in SCPI
            # instead. Which one a unit speaks is not something to assume:
            # these supplies stay in service for decades and the command
            # set is not the same across that span.
            st = self._scpi_state()
        self.dialect = SHORT if raw and parse_settings(raw).channels else SCPI
        st.port = self.link.port
        st.model, st.serial, st.firmware = _split_idn(self.idn)
        self._read_measured(st)
        return st

    def _scpi_state(self) -> SupplyState:
        """Settings read the SCPI way, one channel at a time.

        There is no query for how many channels a unit has, so the driver
        asks for each in turn and keeps the ones that answer. Untested
        against hardware — this bench's unit speaks the short language.
        """
        st = SupplyState()
        for index in range(1, _MAX_CHANNELS + 1):
            self.link.write(f":INST OUT{index}")
            volt, curr = _measured(self.link.ask(":VOLT?;CURR?"))
            if volt is None and curr is None:
                break
            st.channels.append(Channel(volt=volt or 0.0, curr=curr or 0.0))
        out = _number(self.link.ask(":OUTP?"))
        st.output = None if out is None else bool(out)
        return st

    def _select(self, channel: int) -> str:
        return (f":INST OUT{int(channel)}" if self.dialect == SCPI
                else f"SEL {int(channel)}")

    def _read_measured(self, st: SupplyState) -> None:
        """Ask for the measured values, at most once per session.

        One chained query per channel, the way the manufacturer's own
        library does it. A unit that does not know it answers nothing, and
        every unanswered query costs the read timeout — so the first
        channel decides for the whole session and a "no" is remembered.
        """
        if self.measures is False:
            return
        for index, channel in enumerate(st.channels, start=1):
            self.link.write(self._select(index))
            answer = self.link.ask(MEASURE)
            volt, curr = _measured(answer)
            if volt is None and curr is None:
                self.measures = False
                return
            self.measures = True
            channel.meas_volt, channel.meas_curr = volt, curr

    def set_voltage(self, channel: int, volts: float) -> None:
        # one line, not two writes: the unit takes several commands
        # separated by semicolons, and a select that arrives without its
        # value would leave the wrong channel armed for whatever comes next
        if self.dialect == SCPI:
            self.link.write(f":INST OUT{int(channel)};:VOLT {float(volts):.2f}")
        else:
            self.link.write(f"SEL {int(channel)};V {float(volts):.2f}")

    def set_current(self, channel: int, amps: float) -> None:
        if self.dialect == SCPI:
            self.link.write(f":INST OUT{int(channel)};:CURR {float(amps):.3f}")
        else:
            self.link.write(f"SEL {int(channel)};C {float(amps):.3f}")

    def set_output(self, on: bool) -> None:
        if self.dialect == SCPI:
            self.link.write(f":OUTP {1 if on else 0}")
        else:
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


def _measured(answer: str) -> tuple[float | None, float | None]:
    """The chained measurement answer ("4.98;0.12") into two numbers.

    A unit that does not know the query answers nothing or something that
    is not a number, and both mean the same thing here: no measurement.
    """
    parts = [p.strip() for p in answer.split(";")]
    volt = _number(parts[0]) if parts else None
    curr = _number(parts[1]) if len(parts) > 1 else None
    return volt, curr


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
