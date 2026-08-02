"""OWON SP/SPE single-channel laboratory supplies, and the same hardware
sold under other names.

OWON builds these supplies for a number of labels. What changes is the
badge, the colour and the model number; the firmware answers the same
commands and identifies itself the same way, so one driver covers them
all and ``_VENDORS`` is the list of names seen so far:

    KIPRIM,DC605S,23090539,FV:V4.1.0      the unit this was written against
    OWON,P4305,1715040,FV:V1.0.2          the example in OWON's own manual

Matching is on that first field and not on the model, which is what makes
the list short. Models reported compatible elsewhere — the ``owon-psu``
package names OWON SPE6103, SPE3103, P4603 and P4305, and Kiprim DC310S
and DC605S — are all covered by the two vendor names without an entry
each.

Unlike the Töllner, this one is SCPI throughout and there is only one
command language to worry about:

    VOLT 24.000 / VOLT?        set voltage, and the setting in force
    CURR 1.500  / CURR?        set current limit, likewise
    VOLT:LIM?   / CURR:LIM?    the over-voltage and over-current ceilings
    OUTP ON|OFF / OUTP?        output, answered as the word "ON" or "OFF"
    MEAS:ALL:INFO?             what the terminals are doing, in one answer

``MEAS:ALL:INFO?`` is the reason this driver needs one round trip per
refresh where a query per value would need five:

    57.430,0.059,3.390,OFF,OFF,OFF,1

Voltage, current and power, and then four fields the manual lists under
the command without saying what they are. They are carried into ``extra``
as the raw tail and shown as text. Three of them being ``OFF`` makes
protection flags a good guess and the trailing 1 a mode — but a guess is
what it would be, and a wrong label next to right ones cannot be told
apart by whoever reads the box.

Set values and measurements are separate here for a reason worth
recording: the chained SCPI form that the Töllner's manufacturer library
uses, ``:MEAS:VOLT?;CURR?``, is *accepted* by this unit and answers

    57.430
    2.000

where 2.000 is the current **setting**, not the measured 0.059 A. The
second half of the chain is read as a plain ``CURR?``. So the chained
form is never used here; ``MEAS:ALL:INFO?`` is the one that answers about
the terminals.

Everything here has been run against a Kiprim DC605S, reads and writes
both: setting voltage and current is read back from the unit, and the
output switching is confirmed by the measurement rather than by the
unit's own ``OUTP?`` — 0.0 V off, 48.04 V on, which is the difference
between a command the unit acknowledged and one it acted on.

Undocumented but answered by this unit: ``SYST:VERS?`` and ``SYST:ERR?``
(hex, ``0x0000`` when clear). Documented and deliberately not sent:
``SYST:REM`` and ``SYST:LOC``, which take the front panel away from
whoever is standing at the bench and give it back. The driver has no
business doing either on its own.

Not accepted by this unit, though they are ordinary SCPI: ``MEAS?`` on
its own, ``OUTP:MODE?``, ``VOLT:PROT?``, ``CURR:PROT?``, ``*STB?``. They
answer the literal string ``ERR``, which is why an answer is checked for
being a number rather than for being non-empty.
"""
from __future__ import annotations

from . import Channel, PowerSupply, SerialLink, SupplyState

#: first field of *IDN? for every badge this driver recognises
_VENDORS = ("OWON", "KIPRIM")

#: voltage, current, power and four unnamed fields, in one round trip
MEASURE = "MEAS:ALL:INFO?"

#: what this unit says instead of answering a command it does not know
_REFUSED = "ERR"


class OwonSpe(PowerSupply):
    name = "OWON SPE"
    #: these bridge through a CH340. Another badge may use a different chip
    #: — add it here, or pick the port by hand, which skips the hints.
    port_hints = ("CH340", "wch.cn")
    #: OWON's factory default, and not negotiable from this side
    baud = 115200

    def __init__(self, link: SerialLink):
        super().__init__(link)
        self.idn = ""

    @classmethod
    def identify(cls, link: SerialLink) -> str | None:
        answer = link.ask("*IDN?")
        vendor = answer.split(",")[0].strip().upper()
        return answer if vendor in _VENDORS else None

    def state(self) -> SupplyState:
        if not self.idn:
            self.idn = self.link.ask("*IDN?")
        st = SupplyState()
        st.port = self.link.port
        st.model, st.serial, st.firmware = _split_idn(self.idn)

        # one output, always: this is the single-channel series. The base
        # class carries a list because the Töllner has two, not because
        # the count is in doubt here.
        channel = Channel(volt=_number(self.link.ask("VOLT?")) or 0.0,
                          curr=_number(self.link.ask("CURR?")) or 0.0)
        channel.limit = _number(self.link.ask("VOLT:LIM?"))
        if (climit := self.link.ask("CURR:LIM?")) and _number(climit) is not None:
            channel.extra["CURR:LIM"] = climit
        st.channels.append(channel)

        st.output = _on_off(self.link.ask("OUTP?"))
        st.raw = self.link.ask(MEASURE)
        self._read_measured(st.raw, channel)
        return st

    @staticmethod
    def _read_measured(raw: str, channel: Channel) -> None:
        """``57.430,0.059,3.390,OFF,OFF,OFF,1`` into the channel.

        The first three fields are voltage, current and power and are the
        same numbers ``MEAS:VOLT?``/``CURR?``/``POW?`` give one at a time.
        Everything after them is kept as text — see the module docstring
        on why it is not given names.
        """
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) < 2:
            return
        channel.meas_volt = _number(parts[0])
        channel.meas_curr = _number(parts[1])
        if len(parts) > 2 and _number(parts[2]) is not None:
            channel.extra["POWER"] = parts[2]
        if len(parts) > 3:
            channel.extra[MEASURE] = ",".join(parts[3:])

    def set_voltage(self, channel: int, volts: float) -> None:
        self.link.write(f"VOLT {float(volts):.3f}")

    def set_current(self, channel: int, amps: float) -> None:
        self.link.write(f"CURR {float(amps):.3f}")

    def set_output(self, on: bool) -> None:
        # the word, not 0|1: both are documented, and the word is what the
        # unit answers to OUTP?, so a log of what was sent reads the same
        # as a log of what came back
        self.link.write(f"OUTP {'ON' if on else 'OFF'}")


def _split_idn(idn: str) -> tuple[str, str, str]:
    """"KIPRIM,DC605S,23090539,FV:V4.1.0" -> model, serial, firmware."""
    parts = [p.strip() for p in idn.split(",")]
    while len(parts) < 4:
        parts.append("")
    return parts[1], parts[2], parts[3]


def _number(text: str) -> float | None:
    """The value, or None when the unit refused or said something else.

    A refusal is the literal "ERR", not an empty line, so "did it answer"
    is not the same question as "is this a number".
    """
    try:
        return float(text)
    except ValueError:
        return None


def _on_off(answer: str) -> bool | None:
    """``OUTP?`` answers "ON" or "OFF" — anything else means unknown, which
    is not the same as off."""
    word = answer.strip().upper()
    if word in ("ON", "1"):
        return True
    if word in ("OFF", "0"):
        return False
    return None
