"""Bench instruments: the power-supply layer, the Töllner and the OWON driver.

No hardware and no pyserial: a fake port answers the way the real unit
does. The sample strings are the ones a Töllner 8952 and a Kiprim DC605S
actually returned — that is the whole point of having them here, since a
driver written against an imagined answer format is a driver nobody has
tested.
"""
from __future__ import annotations

import pytest

from canopen_bench.instruments import InstrumentError, SerialLink, connect, discover
from canopen_bench.instruments.owon import OwonSpe
from canopen_bench.instruments.toellner import Toellner8952, parse_settings

IDN = "TOELLNER,TOE8952-60,102625,3.63-3.62"
LRN = ("SEL 1;V 005.00;C 00.500;P 0205.0;OVP 0066.0;PV 0;VLIM 061.50;CLIM 07.070;"
       "S 0;EXT 0;VSLEW 0;AVM 0;SEL 2;V 057.00;C 07.000;P 0205.0;OVP 0066.0;PV 0;"
       "VLIM 061.50;CLIM 07.070;S 0;EXT 0;VSLEW 0;AVM 0;TRA 0;EX 1")


class FakePort:
    """Records what was written, answers queries like the unit does."""

    def __init__(self, answers: dict[str, str] | None = None):
        self.written: list[str] = []
        self.answers = answers if answers is not None else {"*IDN?": IDN, "*LRN?": LRN}
        self._pending = ""
        self.closed = False

    def write(self, data: bytes) -> None:
        cmd = data.decode("ascii").strip()
        self.written.append(cmd)
        self._pending = self.answers.get(cmd, "")

    def readline(self) -> bytes:
        out, self._pending = self._pending, ""
        return out.encode("ascii") + b"\n"

    def close(self) -> None:
        self.closed = True


def opener_for(*ports: FakePort):
    """An opener that hands out the given fake ports in order."""
    made = list(ports)

    def opener(device, baud, timeout):
        return made.pop(0)
    return opener


# -- parsing the unit's own answer ------------------------------------------

def test_settings_split_into_channels():
    st = parse_settings(LRN)
    assert len(st.channels) == 2
    assert (st.channels[0].volt, st.channels[0].curr) == (5.0, 0.5)
    assert (st.channels[1].volt, st.channels[1].curr) == (57.0, 7.0)


def test_the_output_flag_is_global_and_comes_from_the_tail():
    assert parse_settings(LRN).output is True
    assert parse_settings(LRN.replace("EX 1", "EX 0")).output is False


def test_an_answer_that_says_nothing_about_the_output_stays_unknown():
    """Tri-state on purpose: "the unit did not say" must not render as
    "the output is off", which is a claim about the bench wiring."""
    assert parse_settings("SEL 1;V 005.00;C 00.500").output is None


def test_keys_this_driver_does_not_interpret_survive_as_text():
    """Rather than guessing what OVP or AVM mean — they stay readable, and
    labelled as coming from the instrument."""
    extra = parse_settings(LRN).channels[0].extra
    assert extra["OVP"] == "0066.0" and extra["VLIM"] == "061.50"


def test_the_raw_answer_is_kept():
    assert parse_settings(LRN).raw == LRN


# -- the driver -------------------------------------------------------------

def test_identify_accepts_only_this_vendor():
    yes = SerialLink("COM6", opener=opener_for(FakePort()))
    assert Toellner8952.identify(yes) == IDN
    other = SerialLink("COM3", opener=opener_for(FakePort({"*IDN?": "ACME,PSU,1,1"})))
    assert Toellner8952.identify(other) is None


def test_state_reports_model_serial_and_firmware():
    psu = Toellner8952(SerialLink("COM6", opener=opener_for(FakePort())))
    st = psu.state()
    assert (st.model, st.serial, st.firmware) == ("TOE8952-60", "102625", "3.63-3.62")
    assert st.port == "COM6"


def test_setting_a_voltage_selects_and_sets_in_one_line():
    """Two writes could be interrupted between select and value, leaving
    the wrong channel armed for whatever the next step does."""
    port = FakePort()
    psu = Toellner8952(SerialLink("COM6", opener=opener_for(port)))
    psu.set_voltage(2, 26)
    assert port.written == ["SEL 2;V 26.00"]


def test_current_and_output_commands():
    port = FakePort()
    psu = Toellner8952(SerialLink("COM6", opener=opener_for(port)))
    psu.set_current(1, 0.5)
    psu.set_output(True)
    psu.set_output(False)
    assert port.written == ["SEL 1;C 0.500", "EX 1", "EX 0"]


def test_the_port_is_opened_once_and_held():
    """Reopening per command is what a one-shot script does; here it would
    pay the open latency on every step of a test case."""
    port = FakePort()
    made = []

    def opener(device, baud, timeout):
        made.append(device)
        return port
    psu = Toellner8952(SerialLink("COM6", opener=opener))
    psu.set_voltage(1, 10)
    psu.set_voltage(1, 20)
    psu.state()
    assert made == ["COM6"]
    psu.close()
    assert port.closed


# -- discovery --------------------------------------------------------------

def test_discovery_only_opens_ports_a_driver_recognises():
    """A serial port might be the CAN adapter. Writing *IDN? to it to see
    what happens is not an acceptable way to find a power supply."""
    opened = []

    def opener(device, baud, timeout):
        opened.append(device)
        return FakePort()
    found = discover(opener=opener, ports=[("COM1", "Silicon Labs CP210x"),
                                           ("COM6", "USB Serial Port FTDI")])
    assert opened == ["COM6"]
    assert found is not None and found[1] == IDN


def test_discovery_returns_nothing_when_no_port_answers():
    ports = [("COM6", "FTDI")]
    got = discover(opener=opener_for(FakePort({"*IDN?": "somebody else"})), ports=ports)
    assert got is None


def test_reconnecting_to_a_known_port_probes_nothing_else():
    opened = []

    def opener(device, baud, timeout):
        opened.append(device)
        return FakePort()
    got = connect("COM6", opener=opener)
    assert got is not None and opened == ["COM6"]


def test_without_pyserial_the_message_says_what_to_install(monkeypatch):
    """The dependency is optional, so the failure has to be a sentence and
    not an ImportError traceback in a log nobody reads."""
    import builtins

    real_import = builtins.__import__

    def no_serial(name, *args, **kwargs):
        if name == "serial":
            raise ImportError("no module named serial")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_serial)
    with pytest.raises(InstrumentError, match="pyserial"):
        SerialLink("COM6").open()


def _without_pyserial(monkeypatch):
    """Make ``import serial`` fail, the way a missing optional extra does."""
    import builtins

    real_import = builtins.__import__

    def no_serial(name, *args, **kwargs):
        if name == "serial" or name.startswith("serial."):
            raise ImportError("no module named serial")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_serial)


def test_searching_without_pyserial_says_so_instead_of_finding_nothing(monkeypatch):
    """An empty port list is exactly what a machine with no serial ports
    looks like. Returning one made "the package is missing" and "there is
    no supply here" the same answer, and only one of them is actionable."""
    _without_pyserial(monkeypatch)
    with pytest.raises(InstrumentError, match="pyserial"):
        discover()


def test_an_injected_port_list_needs_no_pyserial(monkeypatch):
    """Only the enumeration needs the package. Given the ports, the search
    is the caller's own list and an opener — which is how the tests above
    run, and how a caller that knows its port can work without the extra."""
    _without_pyserial(monkeypatch)
    got = discover(opener=opener_for(FakePort()), ports=[("COM6", "FTDI")])
    assert got is not None


def test_a_single_channel_unit_reports_one_channel():
    """Same model line, one output instead of two — nothing here may
    assume the shape of the bench it is standing on."""
    one = "SEL 1;V 012.00;C 01.000;VLIM 030.00;TRA 0;EX 0"
    st = parse_settings(one)
    assert len(st.channels) == 1
    assert st.channels[0].volt == 12.0 and st.output is False


def test_the_channel_carries_the_units_own_voltage_limit():
    """The 8952 comes as a 30 V and a 60 V model. Which one this is gets
    read from the instrument, not guessed from its type plate."""
    assert parse_settings(LRN).channels[0].limit == 61.5


# -- the bench side ---------------------------------------------------------

def _bench_with_psu(tmp_path, port=FakePort):
    from canopen_bench.core import Bench
    from canopen_bench.db import Db
    made = {}

    def opener(device, baud, timeout):
        made[device] = made.get(device) or port()
        return made[device]
    bench = Bench(Db(tmp_path / "psu.db"), plugins=[])
    bench._psu_opener = opener
    bench._psu_ports = [("COM1", "Silicon Labs"), ("COM6", "FTDI")]
    return bench, made


def test_the_box_is_absent_until_something_is_found(tmp_path):
    bench, _ = _bench_with_psu(tmp_path)
    assert bench.snapshot()["psu"] is None


def test_the_search_blames_the_missing_package_not_the_bench(tmp_path, monkeypatch):
    """What this cost once: the supply was connected and answering, the
    extra was not installed, and the box said "no known power supply
    found on the serial ports" — which reads as a verdict on the cable
    and the instrument."""
    from canopen_bench.core import Bench
    from canopen_bench.db import Db
    _without_pyserial(monkeypatch)
    bench = Bench(Db(tmp_path / "psu.db"), plugins=[])   # no ports injected
    bench.dispatch("psu_search", {})
    assert "pyserial" in bench.logs[-1]["msg"]
    assert "no known power supply" not in bench.logs[-1]["msg"]


def test_search_finds_the_supply_and_reports_its_set_values(tmp_path):
    bench, _ = _bench_with_psu(tmp_path)
    bench.dispatch("psu_search", {})
    psu = bench.snapshot()["psu"]
    assert psu["found"] and psu["model"] == "TOE8952-60" and psu["port"] == "COM6"
    assert [c["volt"] for c in psu["channels"]] == [5.0, 57.0]
    assert psu["output"] is True


def test_the_port_that_answered_is_remembered_across_a_restart(tmp_path):
    """A restart must not mean probing every serial port again — that is
    the moment somebody's CAN adapter gets an *IDN? written at it."""
    bench, made = _bench_with_psu(tmp_path)
    bench.dispatch("psu_search", {})
    assert bench.db.get("psu_port") == "COM6"

    from canopen_bench.core import Bench
    again = Bench(bench.db, plugins=[])       # same workspace db
    assert again.snapshot()["psu"] is None    # no opener injected: nothing opens
    again._psu_opener = lambda device, baud, timeout: made[device]
    assert again._psu_connect("COM6") and again.snapshot()["psu"]["found"]


def test_setting_a_voltage_from_the_box_reaches_the_instrument(tmp_path):
    bench, made = _bench_with_psu(tmp_path)
    bench.dispatch("psu_search", {})
    bench.dispatch("psu_set", {"ch": 2, "volt": "26"})
    assert "SEL 2;V 26.00" in made["COM6"].written


def test_a_typo_in_the_field_changes_nothing_and_says_so(tmp_path):
    bench, made = _bench_with_psu(tmp_path)
    bench.dispatch("psu_search", {})
    before = list(made["COM6"].written)
    bench.dispatch("psu_set", {"ch": 1, "volt": "twentysix"})
    assert made["COM6"].written == before
    assert any("is not a number" in row["msg"] for row in bench.logs)


def test_releasing_hands_the_port_back(tmp_path):
    """Holding a serial port open is exactly why another program cannot
    have it — so letting go is a button, not a restart."""
    bench, made = _bench_with_psu(tmp_path)
    bench.dispatch("psu_search", {})
    bench.dispatch("psu_release", {})
    assert made["COM6"].closed
    assert bench.snapshot()["psu"] is None and bench.db.get("psu_port") == ""


# -- measured values, where the unit answers for them ------------------------

MEAS = {"*IDN?": IDN, "*LRN?": LRN, ":MEAS:VOLT?;CURR?": "4.98;0.12"}


def test_both_measurements_come_back_in_one_round_trip():
    """The manufacturer's own library chains them into a single query, and
    it is worth copying: an unanswered query costs the read timeout, and
    this unit is known not to accept every SCPI command."""
    port = FakePort(MEAS)
    Toellner8952(SerialLink("COM6", opener=opener_for(port))).state()
    assert port.written.count(":MEAS:VOLT?;CURR?") == 2      # one per channel


def test_a_half_answer_still_gives_what_it_gave():
    """Nothing here insists on getting both numbers to accept either."""
    port = FakePort({**MEAS, ":MEAS:VOLT?;CURR?": "4.98"})
    st = Toellner8952(SerialLink("COM6", opener=opener_for(port))).state()
    assert st.channels[0].meas_volt == 4.98
    assert st.channels[0].meas_curr is None


def test_measured_values_are_read_when_the_unit_answers():
    """The setting and what the terminals do are two numbers. A supply in
    current limit sits well below its set voltage, and a report that shows
    the setting instead claims something that did not happen."""
    psu = Toellner8952(SerialLink("COM6", opener=opener_for(FakePort(MEAS))))
    st = psu.state()
    assert st.channels[0].volt == 5.0            # the setting
    assert st.channels[0].meas_volt == 4.98      # the measurement
    assert st.channels[0].meas_curr == 0.12


def test_a_unit_that_ignores_the_query_is_asked_once_and_then_left_alone():
    """Every unanswered query costs the read timeout. This unit is
    documented as not supporting the SCPI commands that have a short
    equivalent, so a "no" has to stick."""
    port = FakePort()                            # answers *IDN?/*LRN? only
    psu = Toellner8952(SerialLink("COM6", opener=opener_for(port)))
    st = psu.state()
    assert st.channels[0].meas_volt is None
    assert psu.measures is False
    asked = port.written.count(":MEAS:VOLT?;CURR?")
    psu.state()
    assert port.written.count(":MEAS:VOLT?;CURR?") == asked   # not asked again


def test_the_measurement_never_stands_in_for_the_setting():
    st = Toellner8952(SerialLink("COM6", opener=opener_for(FakePort(MEAS)))).state()
    assert st.channels[1].volt == 57.0           # from *LRN?, unchanged


# -- a unit that speaks the other language ----------------------------------
# These supplies stay in service for decades and the same model number
# covers units with different command sets. The driver settles on the one
# it is answered in rather than assuming from the type plate.

SCPI_ANSWERS = {
    "*IDN?": IDN,
    "*LRN?": "",                       # this unit does not know the short form
    ":VOLT?;CURR?": "12.00;1.500",
    ":OUTP?": "1",
    ":MEAS:VOLT?;CURR?": "11.97;0.31",
}


def test_a_scpi_only_unit_still_reports_its_channels():
    port = FakePort(SCPI_ANSWERS)
    psu = Toellner8952(SerialLink("COM6", opener=opener_for(port)))
    st = psu.state()
    assert psu.dialect == "scpi"
    assert [c.volt for c in st.channels] == [12.0, 12.0]   # both channels answer
    assert st.output is True


def test_writes_follow_the_language_the_unit_answered_in():
    port = FakePort(SCPI_ANSWERS)
    psu = Toellner8952(SerialLink("COM6", opener=opener_for(port)))
    psu.state()
    port.written.clear()
    psu.set_voltage(2, 26)
    psu.set_output(False)
    assert port.written == [":INST OUT2;:VOLT 26.00", ":OUTP 0"]


def test_a_unit_that_answers_the_short_form_is_left_in_it():
    """No language switching: that would change the device for every other
    tool on the bench, including the operator's own scripts."""
    port = FakePort()
    psu = Toellner8952(SerialLink("COM6", opener=opener_for(port)))
    psu.state()
    assert psu.dialect == "short"
    assert not any("SYST:LANG" in w for w in port.written)


# -- the OWON driver --------------------------------------------------------
#
# Every string below is what a Kiprim DC605S answered on COM10.

OWON_IDN = "KIPRIM,DC605S,23090539,FV:V4.1.0"
OWON_ANSWERS = {
    "*IDN?": OWON_IDN,
    "VOLT?": "57.400",
    "CURR?": "2.000",
    "VOLT:LIM?": "62.000",
    "CURR:LIM?": "5.200",
    "OUTP?": "ON",
    "MEAS:ALL:INFO?": "57.430,0.059,3.390,OFF,OFF,OFF,1",
}


def owon_for(answers: dict[str, str] | None = None):
    port = FakePort(answers if answers is not None else dict(OWON_ANSWERS))
    return OwonSpe(SerialLink("COM10", opener=opener_for(port))), port


def test_identify_accepts_every_badge_on_the_same_firmware():
    for idn in (OWON_IDN, "OWON,P4305,1715040,FV:V1.0.2"):
        link = SerialLink("COM10", opener=opener_for(FakePort({"*IDN?": idn})))
        assert OwonSpe.identify(link) == idn


def test_identify_reads_the_vendor_field_not_the_whole_line():
    """A model number containing the vendor name of another make is not
    that make — the first comma-separated field is the badge."""
    link = SerialLink("COM10", opener=opener_for(FakePort({"*IDN?": "ACME,OWON-CLONE,1,1"})))
    assert OwonSpe.identify(link) is None


def test_state_reads_settings_measurements_and_limits():
    psu, _ = owon_for()
    st = psu.state()
    assert (st.model, st.serial, st.firmware) == ("DC605S", "23090539", "FV:V4.1.0")
    assert st.output is True
    assert len(st.channels) == 1
    ch = st.channels[0]
    assert (ch.volt, ch.curr) == (57.4, 2.0)              # the settings
    assert (ch.meas_volt, ch.meas_curr) == (57.43, 0.059)  # what the terminals do
    assert ch.limit == 62.0


def test_the_measurement_is_never_the_setting():
    """The point of MEAS:ALL:INFO? — the chained ":MEAS:VOLT?;CURR?" gives
    2.000 here, which is the current setting wearing the wrong label."""
    psu, _ = owon_for()
    ch = psu.state().channels[0]
    assert ch.meas_curr != ch.curr


def test_the_unnamed_tail_is_kept_as_text_not_given_labels():
    psu, _ = owon_for()
    ch = psu.state().channels[0]
    assert ch.extra["MEAS:ALL:INFO?"] == "OFF,OFF,OFF,1"
    assert ch.extra["POWER"] == "3.390"


def test_a_refused_query_is_not_read_as_a_value():
    """This unit answers the literal "ERR", which float() must not see as
    an answer — "did it reply" and "is that a number" differ here."""
    answers = dict(OWON_ANSWERS, **{"VOLT:LIM?": "ERR", "CURR:LIM?": "ERR"})
    psu, _ = owon_for(answers)
    ch = psu.state().channels[0]
    assert ch.limit is None
    assert "CURR:LIM" not in ch.extra


def test_an_output_the_unit_does_not_name_stays_unknown():
    psu, _ = owon_for(dict(OWON_ANSWERS, **{"OUTP?": "ERR"}))
    assert psu.state().output is None
    psu, _ = owon_for(dict(OWON_ANSWERS, **{"OUTP?": "OFF"}))
    assert psu.state().output is False


def test_the_write_commands_are_the_documented_ones():
    psu, port = owon_for()
    psu.set_voltage(1, 24)
    psu.set_current(1, 1.5)
    psu.set_output(True)
    psu.set_output(False)
    assert port.written == ["VOLT 24.000", "CURR 1.500", "OUTP ON", "OUTP OFF"]


# -- the port is opened at the driver's own baud rate -----------------------

def test_each_driver_opens_its_port_at_its_own_baud():
    """A supply answering at 115200 says nothing at 9600, and discovery
    cannot tell that apart from "not this instrument"."""
    seen: list[int] = []

    def opener(device, baud, timeout):
        seen.append(baud)
        return FakePort(OWON_ANSWERS if baud == OwonSpe.baud else {})

    found = connect("COM10", opener=opener)
    assert found is not None
    assert OwonSpe.baud in seen and OwonSpe.baud == 115200


class DeadPort(FakePort):
    """A handle to a bridge that went away: opened fine, refuses to write.

    What Windows does when the USB serial adapter re-enumerates under an
    open handle — WriteFile answers ERROR_ACCESS_DENIED, which pyserial
    surfaces as PermissionError(13, 'Zugriff verweigert', None, 5).
    """

    def write(self, data: bytes) -> None:
        raise PermissionError(13, "Zugriff verweigert", None, 5)


def test_a_bridge_that_came_back_is_reopened_rather_than_given_up_on():
    """The port is held for the session, so a dead handle stayed dead: the
    supply switched off and on left every write refused until somebody
    found the disconnect button. One fresh handle fixes it."""
    dead, alive = DeadPort(), FakePort()
    psu = Toellner8952(SerialLink("COM6", opener=opener_for(dead, alive)))

    psu.set_voltage(1, 10)

    assert dead.closed, "the dead handle was kept"
    assert alive.written == ["SEL 1;V 10.00"], "the command was not repeated on the new handle"


def test_a_port_that_is_really_gone_says_so_once(monkeypatch):
    """If a new handle does not help, the instrument is gone. Say it —
    hammering a port that is not there helps nobody, and the box shows the
    reason next to the supply."""
    tries = []

    def opener(device, baud, timeout):
        tries.append(device)
        return DeadPort()

    psu = Toellner8952(SerialLink("COM6", opener=opener))
    with pytest.raises(InstrumentError) as caught:
        psu.set_voltage(1, 10)
    assert "COM6" in str(caught.value)
    assert len(tries) == 2, "exactly one retry, not a loop"


def test_a_read_recovers_the_same_way():
    class DeadOnRead(FakePort):
        def readline(self) -> bytes:
            raise PermissionError(13, "Zugriff verweigert", None, 5)

    psu = Toellner8952(SerialLink("COM6", opener=opener_for(DeadOnRead(), FakePort())))
    assert psu.state().model == "TOE8952-60"
