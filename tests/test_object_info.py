"""One description of an object, and what every view does with it.

The bench used to carry the CiA-301 data type numbers in four places —
the object table, the panel, the trace decoder and the demo bus — and two
of those four disagreed about which types are text. So one device could
answer to two readings on one screen: the panel showed -500 where the
table beside it showed 65036, and the table read a device name as the
nineteen-digit number its bytes happen to spell.

``eds_od.ObjectInfo`` is now the single answer, and these tests hold both
halves of that: the description itself, and the views agreeing.
"""
from __future__ import annotations

from conftest import SEED_EDS, connect_and_scan, seed_test_registry

from canopen_bench.core import Bench
from canopen_bench.db import Db
from canopen_bench.eds_od import load_eds, object_info
from canopen_bench.plugin import BenchPlugin

#: Signed, text and limited objects, plus the record SEED_EDS already has.
#: 0x2070 is deliberately an INTEGER16 with limits stated in decimal, which
#: is how an EDS states a signed range.
INFO_EDS = SEED_EDS + """
[1008]
ParameterName=Manufacturer device name
ObjectType=0x7
DataType=0x0009
AccessType=ro
DefaultValue=SEED_DEV

[2070]
ParameterName=Tilt angle
ObjectType=0x7
DataType=0x0003
AccessType=rw
DefaultValue=0
LowLimit=-900
HighLimit=900

[2071]
ParameterName=Write only trigger
ObjectType=0x7
DataType=0x0005
AccessType=wo
DefaultValue=0
"""


def _od():
    return load_eds(INFO_EDS.encode("utf-8"))


def _bench(tmp_path, plugins=None) -> Bench:
    bench = Bench(Db(tmp_path / "test.db"), plugins=plugins or [])
    seed_test_registry(bench)
    for entry in bench.db.eds_list():
        if entry["enabled"]:
            bench.db.eds_write_file(entry["file"], INFO_EDS)
    connect_and_scan(bench)
    bench.dispatch("dev_toggle", {"node": 1})
    return bench


# -- the description itself --------------------------------------------------

def test_the_three_readings_of_a_type_come_from_one_place():
    """Signedness, textness and whether a value may be padded are three
    readings of the same declared type. Answered apart, they drift: the
    panel's table of signed types held INTEGER8/16/32 and the trace's held
    INTEGER64 as well, so a 64-bit signed value was negative in one view
    and enormous in the other."""
    od = _od()
    angle = object_info(od, 0x2070, 0)
    assert angle.signed_bits == 16 and not angle.is_text and angle.width == 2

    name = object_info(od, 0x1008, 0)
    assert name.is_text and name.signed_bits == 0
    assert name.width == 0, "a word padded to its declared length is a different word"

    counter = object_info(od, 0x2000, 0)
    assert counter.signed_bits == 0 and not counter.is_text and counter.width == 4


def test_an_address_the_eds_does_not_declare_has_no_description():
    """Every caller reads None as "the plain unsigned word it always was".
    A guessed sign bit turns half a range into negatives, and a guessed
    width truncates a write — both silently."""
    od = _od()
    assert object_info(od, 0x9999, 0) is None
    assert object_info(od, 0x2070, 7) is None      # a sub-index of a scalar
    assert object_info(None, 0x2070, 0) is None    # no EDS at all


def test_a_record_member_is_named_with_its_record():
    """canopen joins the two with a dot; this tool separates an index from
    its sub-index with a slash everywhere — in a step's report line, in the
    object table and now in the trace — so the two never read as different
    notions."""
    assert object_info(_od(), 0x2040, 1).name == "Product identification/Product code"


def test_the_limits_are_the_eds_own_and_absent_where_it_states_none():
    """A missing LowLimit is not zero. Most objects state no limits at all,
    and a bench that read the absence as a bound would flag every one of
    them the moment it read a value."""
    od = _od()
    angle = object_info(od, 0x2070, 0)
    assert (angle.lo, angle.hi) == (-900, 900)
    assert object_info(od, 0x2000, 0).lo is None


def test_a_word_is_read_with_the_sign_the_eds_gives_it():
    """0xFE0C is -500 and 65036 at the same time; nothing but the file can
    say which. The width matters as much as the sign — the same bytes are
    a different number read at 32 bits."""
    od = _od()
    angle = object_info(od, 0x2070, 0)
    assert angle.signed(0xFE0C) == -500
    assert angle.signed(0x0064) == 100
    assert angle.signed(0xFE0C, bits=32) == 0xFE0C, "narrower than the word: still positive"
    assert object_info(od, 0x2000, 0).signed(0xFE0C) == 0xFE0C, "unsigned stays unsigned"


# -- and the table reading it the way the panel does -------------------------

def test_the_table_shows_a_signed_value_signed(tmp_path):
    """The panel has read the EDS's sign since it learned to; the table
    beside it went on showing the same object as an unsigned word. Decimal
    is the reading that carries the sign — hex stays the word as stored,
    because that is what a word is."""
    bench = _bench(tmp_path)
    bench.obj_vals["0x2070:00"] = "0xFE0C"
    bench.dispatch("num_base", {})           # hex -> dec
    fmt = bench.snapshot()["objects"]["fmt"]["0x2070:00"]
    assert fmt["txt"] == "-500"
    assert "0xFE0C" in fmt["alt"] and "-500" in fmt["alt"]
    assert "65036" not in fmt["alt"], "the tooltip must not offer a reading the EDS denies"

    bench.dispatch("num_base", {})           # dec -> hex
    assert bench.snapshot()["objects"]["fmt"]["0x2070:00"]["txt"] == "0xFE0C"


def test_the_table_shows_a_device_name_as_the_word_it_is(tmp_path):
    """The bus carries a name as bytes like everything else, and read as a
    number those bytes are nineteen digits of nothing. The panel knew it;
    the table printed the digits, in whichever base."""
    bench = _bench(tmp_path)
    bench.obj_vals["0x1008:00"] = "0x726564656546"     # "Feeder", as the bus spells it
    fmt = bench.snapshot()["objects"]["fmt"]["0x1008:00"]
    assert fmt["txt"] == "Feeder"
    assert "0x726564656546" in fmt["alt"], "the bytes stay one hover away"


def test_the_range_check_is_made_where_the_number_is_known(tmp_path):
    """It used to be made in the browser, by parsing the text on screen as
    hex — so with the table switched to decimal a 500 was compared as
    0x500, and the operator was warned about 1280. Nothing on screen said
    so, because the value shown was right and only the colour was wrong."""
    bench = _bench(tmp_path)
    bench.dispatch("num_base", {})                     # dec, where the bug lived
    bench.obj_vals["0x2070:00"] = "0x01F4"             # 500, inside -900…900
    assert bench.snapshot()["objects"]["fmt"]["0x2070:00"]["oor"] is False

    bench.obj_vals["0x2070:00"] = "0x0BB8"             # 3000, above the limit
    assert bench.snapshot()["objects"]["fmt"]["0x2070:00"]["oor"] is True

    # and the sign is part of it: 0xFC18 is -1000, below the low limit,
    # while as an unsigned word it is 64536 and above the high one. Both
    # are "out of range", and only one of them is the reason
    bench.obj_vals["0x2070:00"] = "0xFC18"
    assert bench.snapshot()["objects"]["fmt"]["0x2070:00"]["txt"] == "-1000"
    assert bench.snapshot()["objects"]["fmt"]["0x2070:00"]["oor"] is True


def test_a_box_that_shows_a_negative_accepts_one_back(tmp_path):
    """Stored as the two's complement of the object's own width, which is
    the only width at which a two's complement is itself. It used to be
    staged as the digits of whatever the last read answered, so a typed
    -500 became the literal string "0x-1F4"."""
    bench = _bench(tmp_path)
    bench.dispatch("obj_set", {"idx": "0x2070", "sub": "00", "val": "-500"})
    assert bench.obj_vals["0x2070:00"] == "0xFE0C"

    bench.dispatch("num_base", {})
    assert bench.snapshot()["objects"]["fmt"]["0x2070:00"]["txt"] == "-500", "round trip"


def test_the_hex_the_box_prints_is_hex_the_box_takes(tmp_path):
    """With the table in hex, 0xFE0C is what -500 looks like. A field that
    refused back what it had just printed — because the number is above
    the signed half of the range — would be strict about the wrong thing."""
    bench = _bench(tmp_path)
    bench.dispatch("obj_set", {"idx": "0x2070", "sub": "00", "val": "0xFE0C"})
    assert bench.obj_vals["0x2070:00"] == "0xFE0C"


def test_a_minus_is_refused_where_the_eds_says_unsigned(tmp_path):
    """Wrapping it would store 4294967096 and show it, which is a number
    the operator neither typed nor meant."""
    bench = _bench(tmp_path)
    bench.dispatch("obj_set", {"idx": "0x2000", "sub": "00", "val": "-200"})
    assert "0x2000:00" not in bench.obj_vals
    assert any("unsigned" in entry["msg"] for entry in bench.logs[-3:])


def test_an_object_with_no_limits_is_never_out_of_range(tmp_path):
    bench = _bench(tmp_path)
    bench.obj_vals["0x2000:00"] = "0xFFFFFFFF"
    assert bench.snapshot()["objects"]["fmt"]["0x2000:00"]["oor"] is False


def test_the_table_carries_the_eds_access_type(tmp_path):
    """Which is what keeps a page read off the write-only objects — the SDO
    could only abort, and a row of aborts reads as a fault when it is the
    EDS telling the truth."""
    bench = _bench(tmp_path)
    rows = {f"{r[0]}:{r[1]}": r[4] for rows in
            bench.snapshot()["objects"]["catalog"].values() for r in rows}
    assert rows["0x2071:00"] == "wo"
    assert rows["0x2070:00"] == "rw"
    assert rows["0x1008:00"] == "ro"


# -- one name for one object -------------------------------------------------

class _NamingPlugin(BenchPlugin):
    """A plugin that names two of the addresses and nothing else, which is
    the normal case: headers name what the firmware has, and an EDS carries
    objects no header mentions."""

    name = "naming"

    def describe_object(self, index: str, sub: str, symbols) -> str:
        return {("0x2070", "00"): "Sensors/TiltAngle",
                ("0x2040", "01"): "Identity/ProductCode"}.get((str(index), str(sub)), "")


def test_the_firmwares_own_name_wins_in_the_object_table(tmp_path):
    """EDS names are historical documents — right when they were written
    and carried forward ever since — while the headers are what the
    firmware's authors call the thing today. The rule held in a step's
    report line only, so one object answered to two names on one screen."""
    bench = _bench(tmp_path, plugins=[_NamingPlugin()])
    names = {f"{r[0]}:{r[1]}": r[2] for rows in
             bench.snapshot()["objects"]["catalog"].values() for r in rows}
    assert names["0x2070:00"] == "Sensors/TiltAngle"
    assert names["0x2000:00"] == "Writable counter", "no header for it: the EDS stands in"


def test_the_same_name_reaches_the_favourites_and_the_plot(tmp_path):
    """Both take their label from the catalog, so both follow by
    themselves — which is the point of having one rule rather than four."""
    bench = _bench(tmp_path, plugins=[_NamingPlugin()])
    bench.dispatch("fav_toggle", {"idx": "0x2070", "sub": "00"})
    bench.dispatch("plot_toggle", {"idx": "0x2070", "sub": "00"})
    state = bench.snapshot()
    assert state["favorites"]["rows"][0]["label"] == "Sensors/TiltAngle"
    assert state["trace"]["plot"]["sel"][0]["label"] == "Sensors/TiltAngle"


def test_the_same_name_reaches_the_trace(tmp_path):
    """The trace named objects straight from the EDS, so a run's log and
    the run's own report called one object two different things."""
    bench = _bench(tmp_path, plugins=[_NamingPlugin()])
    row = {"time": "", "dir": "RX", "cob": "0x601", "len": "8",
           "data": "40 40 20 01 00 00 00 00", "dec": "", "flag": "", "obj": "", "val": ""}
    bench._annotate_sdo(row)
    assert row["obj"] == "0x2040:01 Identity/ProductCode"


def test_a_reload_of_the_headers_renames_what_they_name(tmp_path):
    """The names are memoised — the trace asks per frame and the table per
    row per tick. A memo that outlived a symbol reload would leave the old
    firmware naming the objects of the new one, and the only way out would
    be a restart."""
    bench = _bench(tmp_path, plugins=[_NamingPlugin()])
    assert bench._label("0x2070", "00", "Tilt angle") == "Sensors/TiltAngle"

    bench.plugins = []
    bench.dispatch("symbols_reload", {})
    assert bench._label("0x2070", "00", "Tilt angle") == "Tilt angle"
    names = {f"{r[0]}:{r[1]}": r[2] for rows in
             bench.snapshot()["objects"]["catalog"].values() for r in rows}
    assert names["0x2070:00"] == "Tilt angle"


def test_the_signed_reading_reaches_the_trace(tmp_path):
    """A trace row of 65036 beside a panel box of -500 is one device
    answering two ways, and the row is the one somebody screenshots."""
    bench = _bench(tmp_path)
    row = {"time": "", "dir": "RX", "cob": "0x581", "len": "8",
           "data": "4B 70 20 00 0C FE 00 00", "dec": "", "flag": "", "obj": "", "val": ""}
    bench._annotate_sdo(row)
    assert row["val"] == "-500"
