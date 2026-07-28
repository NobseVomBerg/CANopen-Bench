"""Value display and input (canopen_bench/values.py).

Two rules are load-bearing here and each has its own test: the raw number
is never replaced by a name, and bits no field accounts for are shown
rather than dropped. The rest is about reading typed input without
guessing what the operator meant.
"""
from __future__ import annotations

import pytest
from conftest import connect_and_scan, write_seed_eds_files

from canopen_bench.core import Bench
from canopen_bench.db import Db
from canopen_bench.plugin import BenchPlugin
from canopen_bench.symbols import load_symbols
from canopen_bench.values import Field, alternatives, describe, format_number, parse_value

HEADER = """
typedef enum eMode { eMode_Off = 2, eMode_Run = 4 } eMode;
typedef enum eLamp { eLamp_Off = 0x10, eLamp_Blinking = 0x20 } eLamp;
typedef enum eFlags {
    eFlags_First = 1,
    eFlags_Second = 2,
    eFlags_Fourth = 4,
    eFlags_Mask = 7,
} eFlags;
"""


@pytest.fixture()
def syms(tmp_path):
    (tmp_path / "acme").mkdir()
    (tmp_path / "acme" / "h.h").write_text(HEADER)
    return load_symbols([("acme", tmp_path / "acme")])


# -- describing a value ------------------------------------------------------

def test_whole_value_enum(syms):
    assert describe(4, [Field("eMode")], syms) == "eMode_Run"


def test_two_fields_packed_into_one_byte(syms):
    """0x24 = mode 4 in the low nibble, lamp 0x20 in the high one — the
    shape a status byte with a bit filter per field takes."""
    fields = [Field("eMode", mask=0x0F), Field("eLamp", mask=0x30)]
    assert describe(0x24, fields, syms) == "eMode_Run · eLamp_Blinking"


def test_a_byte_lane_inside_a_wider_word(syms):
    """An enum documented as living in bits 16..23 of a 32-bit value."""
    assert describe(0x04_0000, [Field("eMode", mask=0xFF0000)], syms) == "eMode_Run"


def test_a_logical_table_in_a_lane_is_shifted(syms):
    """eMode holds plain 2/4 — they do not fit inside 0xFF0000 unshifted, so
    the lane has to be shifted down before the lookup."""
    assert Field("eMode", mask=0xFF0000).extract(0x04_0000, syms) == 4


def test_a_pre_positioned_table_is_not_shifted(syms):
    """eLamp holds 0x10/0x20 — already where they belong in the byte.
    Shifting them "into place" would decode into nothing, which is how a
    field silently reads wrong."""
    assert Field("eLamp", mask=0x30).extract(0x24, syms) == 0x20
    # the low nibble is outside this field's mask, so it shows as leftover
    assert describe(0x24, [Field("eLamp", mask=0x30)], syms) == "eLamp_Blinking · +0x4"


def test_an_explicit_shift_overrules_the_table(syms):
    assert Field("eLamp", mask=0x30, shift=4).extract(0x24, syms) == 2


def test_flag_register_names_every_set_bit(syms):
    got = describe(5, [Field("eFlags", flags=True)], syms)
    assert got == "eFlags_First+eFlags_Fourth"


def test_labels_disambiguate_several_fields(syms):
    fields = [Field("eMode", mask=0x0F, label="mode"),
              Field("eLamp", mask=0x30, label="lamp")]
    assert describe(0x24, fields, syms) == "mode eMode_Run · lamp eLamp_Blinking"


def test_bits_no_field_accounts_for_are_shown(syms):
    """The failure worth avoiding: an unknown bit in a status word silently
    disappearing because no table happened to name it."""
    assert describe(0x84, [Field("eMode", mask=0x0F)], syms) == "eMode_Run · +0x80"


def test_a_value_inside_the_mask_that_no_symbol_names_is_still_shown(syms):
    """Not silence: the field is there, we just cannot name its value, and
    that is worth seeing."""
    assert describe(0x07, [Field("eMode", mask=0x0F)], syms) == "?0x7"


def test_flag_bits_no_symbol_names_are_shown_too(syms):
    assert describe(0x09, [Field("eFlags", flags=True)], syms) == "eFlags_First+?0x8"


def test_a_member_covering_the_whole_mask_is_not_a_flag(syms):
    """Headers keep the mask itself in the table — a bit filter, an "all"
    alias. Reporting it as a state would announce "everything is set" at
    the one moment everything is, on top of the names that already say so."""
    got = describe(0x7, [Field("eFlags", mask=0x7, flags=True)], syms)
    assert got == "eFlags_First+eFlags_Second+eFlags_Fourth"


def test_no_fields_means_no_interpretation(syms):
    assert describe(4, [], syms) == ""


# -- formatting --------------------------------------------------------------

@pytest.mark.parametrize(("base", "text"), [("hex", "0x2A"), ("dec", "42")])
def test_format_follows_the_chosen_base(base, text):
    assert format_number(42, base, width=2) == text


def test_hex_keeps_the_objects_width():
    assert format_number(1, "hex", width=4) == "0x0001"


def test_the_tooltip_carries_every_reading(syms):
    """What makes the base switch safe: whichever base is on screen, the
    other one and the symbolic reading are one hover away."""
    got = alternatives(4, [Field("eMode")], syms, width=2)
    assert got == "0x04 · 4 · eMode_Run"


# -- reading typed input -----------------------------------------------------

def test_explicit_prefixes_win_over_the_base(syms):
    assert parse_value("0x10", "dec", [], syms) == 16
    assert parse_value("0b101", "dec", [], syms) == 5


def test_bare_digits_follow_the_chosen_base(syms):
    """"10" is genuinely ambiguous. The tool picks by the base on screen and
    echoes what it resolved to, rather than pretending there is no choice."""
    assert parse_value("10", "dec", [], syms) == 10
    assert parse_value("10", "hex", [], syms) == 16


def test_a_full_symbol_name_resolves(syms):
    assert parse_value("eMode_Run", "dec", [], syms) == 4


def test_a_short_name_resolves_within_this_objects_tables(syms):
    assert parse_value("Run", "dec", [Field("eMode")], syms) == 4


def test_a_short_name_outside_this_objects_tables_is_unknown(syms):
    with pytest.raises(ValueError, match="unknown symbol"):
        parse_value("Run", "dec", [Field("eLamp")], syms)


def test_flags_can_be_typed_as_a_sum(syms):
    assert parse_value("eFlags_First+eFlags_Fourth", "dec", [], syms) == 5
    assert parse_value("First+Fourth", "dec", [Field("eFlags", flags=True)], syms) == 5


def test_nonsense_is_refused_with_the_text_in_the_message(syms):
    with pytest.raises(ValueError, match="wat"):
        parse_value("wat", "dec", [], syms)


def test_a_malformed_hex_literal_is_refused(syms):
    with pytest.raises(ValueError):
        parse_value("0xZZ", "hex", [], syms)


# -- the bench side ----------------------------------------------------------

class _FieldPlugin(BenchPlugin):
    name = "fake"

    def __init__(self, directory):
        self._dir = directory

    def symbol_dirs(self):
        return [self._dir]

    def object_fields(self, symbols):
        return {"0x2007:09": [Field("eMode", mask=0x0F, label="mode"),
                              Field("eLamp", mask=0x30, label="lamp")]}


@pytest.fixture()
def bench(tmp_path):
    packaged = tmp_path / "packaged"
    packaged.mkdir()
    (packaged / "h.h").write_text(HEADER)
    return Bench(Db(tmp_path / "v.db"), plugins=[_FieldPlugin(packaged)])


def test_typed_symbol_is_resolved_when_staged_not_when_written(bench):
    """Resolving on the way in means the field shows what it became before
    anything reaches the device."""
    bench.dispatch("obj_set", {"idx": "0x2007", "sub": "09", "val": "Run"})
    assert bench.obj_vals["0x2007:09"] == "0x04"


def test_unreadable_input_is_refused_and_logged(bench):
    bench.obj_vals["0x2007:09"] = "0x04"
    bench.dispatch("obj_set", {"idx": "0x2007", "sub": "09", "val": "nonsense"})
    assert bench.obj_vals["0x2007:09"] == "0x04"     # the old value stands
    assert any("rejected" in row["msg"] for row in bench.logs)


def test_base_toggles_and_persists(tmp_path):
    db = Db(tmp_path / "b.db")
    bench = Bench(db, plugins=[])
    assert bench.num_base == "hex"
    bench.dispatch("num_base", {})
    assert bench.num_base == "dec"
    assert Bench(db, plugins=[]).num_base == "dec"


def test_snapshot_carries_number_symbol_and_alternatives(bench):
    bench.obj_vals["0x2007:09"] = "0x24"
    view = bench.snapshot()["objects"]["fmt"]["0x2007:09"]
    assert view["txt"] == "0x24"
    assert view["sym"] == "mode eMode_Run · lamp eLamp_Blinking"
    assert view["alt"].startswith("0x24 · 36 · mode")


def test_switching_base_changes_the_number_but_not_the_reading(bench):
    bench.obj_vals["0x2007:09"] = "0x24"
    bench.dispatch("num_base", {})
    view = bench.snapshot()["objects"]["fmt"]["0x2007:09"]
    assert view["txt"] == "36"
    assert "0x24" in view["alt"] and "eMode_Run" in view["sym"]


def test_unread_objects_follow_the_base_too(bench):
    """Half the table switching to decimal while the rest stays hex is worse
    than not switching at all — the EDS default has to follow as well."""
    write_seed_eds_files(bench)
    connect_and_scan(bench)
    bench.dispatch("dev_toggle", {"node": bench.devices[0]["node"]})
    hex_view = bench.snapshot()["objects"]["fmt"]
    bench.dispatch("num_base", {})
    dec_view = bench.snapshot()["objects"]["fmt"]
    numeric = [k for k, v in hex_view.items() if v["txt"].startswith("0x")]
    assert numeric, "expected some numeric objects in the demo EDS"
    assert all(not dec_view[k]["txt"].startswith("0x") for k in numeric)


def test_string_objects_are_never_reformatted(bench):
    """A VISIBLE_STRING is not a number and must survive both bases
    untouched."""
    bench.obj_vals["0x2004:00"] = "SomeDeviceName"
    for _ in range(2):
        assert "0x2004:00" not in bench.snapshot()["objects"]["fmt"]
        bench.dispatch("num_base", {})
