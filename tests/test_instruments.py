"""Bench instruments: the power-supply layer and the Töllner driver.

No hardware and no pyserial: a fake port answers the way the real unit
does. The sample strings are the ones a Töllner 8952 actually returned —
that is the whole point of having them here, since a driver written
against an imagined answer format is a driver nobody has tested.
"""
from __future__ import annotations

import pytest

from canopen_bench.instruments import InstrumentError, SerialLink, connect, discover
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
