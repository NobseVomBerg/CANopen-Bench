"""What an object's number means physically (`BenchPlugin.object_units`).

The one thing about an object that no EDS can answer. A file says
UNSIGNED16 and stops there; that the sixteen bits are tenths of a
centinewton is in the device's documentation, and until now the only way
to get it into the bench was to write it into a panel file, field by
field — so the object table showed 160 where the box beside it showed
16.0 cN, and a value that appeared in no box had no reading at all.

Declared once per address, used wherever the value is shown.
"""
from __future__ import annotations

import pytest
from conftest import SEED_EDS, connect_and_scan, seed_test_registry

from canopen_bench.core import Bench
from canopen_bench.db import Db
from canopen_bench.plugin import BenchPlugin
from canopen_bench.values import Field, Quantity, ValueError_

UNITS_EDS = SEED_EDS + """
[2007]
ParameterName=Tension parameters
ObjectType=0x9
SubNumber=3

[2007sub0]
ParameterName=Highest sub-index
ObjectType=0x7
DataType=0x0005
AccessType=ro
DefaultValue=2

[2007sub1]
ParameterName=Working tension
ObjectType=0x7
DataType=0x0006
AccessType=rw
DefaultValue=0

[2007sub2]
ParameterName=Drift
ObjectType=0x7
DataType=0x0003
AccessType=rw
DefaultValue=0

[2008]
ParameterName=Operation mode
ObjectType=0x7
DataType=0x0005
AccessType=rw
DefaultValue=0
"""

#: no unit and no scale on 0x2007:01 — the plugin says what it means
PANEL = """
name: Sample Feeder
match: {eds: "dut_alpha*"}
groups:
  - title: Tension
    fields:
      - {label: Working, obj: "0x2007:01", rw: true}
      - {label: Drift,   obj: "0x2007:02", rw: true}
      - {label: Counter, obj: "0x2000:00", unit: "mA", scale: 0.01, rw: true}
"""


class _UnitPlugin(BenchPlugin):
    name = "units"
    panel_file = None

    def object_units(self, symbols) -> dict[str, Quantity]:
        return {"0x2007:01": Quantity("cN", 0.1),
                "0x2007:02": Quantity("cN", 0.1),
                "0x2008:00": Quantity("rpm", 10)}

    def object_panels(self):
        return [self.panel_file] if self.panel_file else []


def _bench(tmp_path, panel: str | None = None) -> Bench:
    plugin = _UnitPlugin()
    if panel is not None:
        file = tmp_path / "vendor.panel.yaml"
        file.write_text(panel, encoding="utf-8")
        plugin.panel_file = file
    bench = Bench(Db(tmp_path / "test.db"), plugins=[plugin])
    seed_test_registry(bench)
    for entry in bench.db.eds_list():
        if entry["enabled"]:
            bench.db.eds_write_file(entry["file"], UNITS_EDS)
    connect_and_scan(bench)
    bench.dispatch("dev_toggle", {"node": 1})
    return bench


# -- the quantity itself -----------------------------------------------------

def test_the_decimals_follow_the_scale_and_the_unit_stays_beside_the_number():
    """scale computes and unit labels: a device storing tenths of a cN
    shows 16.0, and what a write sends is the raw 160 again."""
    cn = Quantity("cN", 0.1)
    assert cn.places == 1
    assert cn.show("0x00A0") == "16.0"
    assert cn.with_unit("0x00A0") == "16.0 cN"
    assert cn.to_raw("16.0") == 160
    assert cn.to_raw("0x10") == 160, "hex is hex, then scaled"


def test_an_address_nobody_described_says_nothing():
    """Which is what lets a caller look elsewhere for a description
    without having to distinguish "no unit" from "no declaration"."""
    assert not Quantity().stated
    assert Quantity("cN", 0.1).stated and Quantity(scale=0.1).stated
    assert Quantity().show("0x00A0") == "160", "unscaled, and no unit to print"
    assert Quantity().with_unit("0x00A0") == "160"


def test_a_negative_needs_a_width_to_be_one():
    """A word carries no sign of its own; the EDS is the only thing that
    can say -500 rather than 65036, and at which width."""
    cn = Quantity("cN", 0.1)
    assert cn.show("0xFE0C", 16) == "-50.0"
    assert cn.show("0xFE0C") == "6503.6"
    assert cn.to_raw("-50.0", 16) == 0xFE0C
    with pytest.raises(ValueError_):
        cn.to_raw("-50.0")


# -- the object table --------------------------------------------------------

def test_the_table_says_what_the_number_means(tmp_path):
    """Beside the raw number, never instead of it — a bench where you
    cannot see the bits you actually read is not a bench. The slot is the
    one the symbolic reading already uses."""
    bench = _bench(tmp_path)
    bench.obj_vals["0x2007:01"] = "0x00A0"
    fmt = bench.snapshot()["objects"]["fmt"]["0x2007:01"]
    assert fmt["txt"] == "0x00A0", "the stored word is still the value"
    assert fmt["sym"] == "16.0 cN"
    assert "16.0 cN" in fmt["alt"]


def test_the_table_reading_carries_the_sign_the_eds_gives_it(tmp_path):
    """0x2007:02 is an INTEGER16 — a drift can be negative, and a box
    reading it as 6503.6 cN would be off by the whole range."""
    bench = _bench(tmp_path)
    bench.obj_vals["0x2007:02"] = "0xFE0C"
    assert bench.snapshot()["objects"]["fmt"]["0x2007:02"]["sym"] == "-50.0 cN"


def test_an_address_with_no_declaration_is_untouched(tmp_path):
    bench = _bench(tmp_path)
    bench.obj_vals["0x2000:00"] = "0x2A"
    assert bench.snapshot()["objects"]["fmt"]["0x2000:00"]["sym"] == ""


def test_a_symbolic_reading_wins_over_a_physical_one(tmp_path):
    """A mode word is not measured in anything and a tension is not an
    enum, so the two never both apply — and where a plugin declares both
    for one address, the names it wrote out are the more specific claim."""
    class Both(_UnitPlugin):
        def object_fields(self, symbols):
            return {"0x2008:00": [Field(table="eMode")]}

    bench = Bench(Db(tmp_path / "test.db"), plugins=[Both()])
    seed_test_registry(bench)
    for entry in bench.db.eds_list():
        if entry["enabled"]:
            bench.db.eds_write_file(entry["file"], UNITS_EDS)
    connect_and_scan(bench)
    bench.dispatch("dev_toggle", {"node": 1})
    bench.obj_vals["0x2008:00"] = "0x03"
    # no such symbol table is loaded, so describe() can only say that no
    # name covers this value — and that is still an answer about a mode.
    # What must not happen is "30 rpm" for a value nobody measures
    assert bench.snapshot()["objects"]["fmt"]["0x2008:00"]["sym"] == "?0x3"


# -- the panel ---------------------------------------------------------------

def _fields(bench):
    bench.dispatch("obj_view", {"view": "panel"})
    return bench.snapshot()["objects"]["panel"]["groups"][0]["fields"]


def test_a_panel_field_need_not_repeat_what_the_plugin_declared(tmp_path):
    """The same fact written down twice is the same fact drifting apart
    twice. A field that says nothing takes the declaration."""
    bench = _bench(tmp_path, PANEL)
    bench.obj_vals["0x2007:01"] = "0x00A0"
    working = _fields(bench)[0]
    assert (working["val"], working["unit"]) == ("16.0", "cN")


def test_a_panel_field_that_says_it_itself_keeps_saying_it(tmp_path):
    """0x2000 has no declaration, and the file's own unit is what a box
    written against one device's documentation is for."""
    bench = _bench(tmp_path, PANEL)
    bench.obj_vals["0x2000:00"] = "0x2A"
    counter = _fields(bench)[2]
    assert (counter["val"], counter["unit"]) == ("0.42", "mA")


def test_what_the_box_shows_is_what_a_write_sends_back(tmp_path):
    """The scale has to run in both directions, whichever side declared
    it — a box showing tenths and staging units writes a tenth of the
    number on screen."""
    bench = _bench(tmp_path, PANEL)
    bench.dispatch("panel_set", {"idx": "0x2007", "sub": "01", "val": "16.0"})
    assert bench.obj_vals["0x2007:01"] == "0x00A0"


def test_a_declared_unit_never_lands_on_a_name_or_a_bit(tmp_path):
    """A panel file rejects scale, digits and unit on an enum or a flag,
    because there is nothing they could mean there. A declaration reaching
    the same widget through the back door would mean the same nothing."""
    panel = PANEL + """
  - title: Mode
    fields:
      - {label: Mode, obj: "0x2008:00", widget: enum, rw: true}
"""
    bench = _bench(tmp_path, panel)
    bench.dispatch("obj_view", {"view": "panel"})
    mode = bench.snapshot()["objects"]["panel"]["groups"][1]["fields"][0]
    assert mode["unit"] == ""


# -- and the report ----------------------------------------------------------

def test_a_report_line_says_what_the_step_read(tmp_path):
    """A report is read by somebody who wants to know what the machine
    did. 160 is the number the bus carried, not the tension anyone set."""
    bench = _bench(tmp_path)
    assert bench._value_note("0x2007", "01", "0x00A0") == "0x00A0 — 16.0 cN"
    assert bench._value_note("0x2000", "00", "0x2A") == "0x2A", "nothing declared"
