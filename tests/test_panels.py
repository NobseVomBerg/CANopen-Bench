"""Panel files: the format, and what the Objects page does with one.

A panel is written by hand against a device's documentation, so the two
things worth testing hardest are the ones a person gets wrong: the
address spelling (a key that differs by a leading zero finds nothing, and
says nothing) and the scaling (a box that shows 16.0 must write 160).
"""
from __future__ import annotations

import asyncio
import time

import pytest
from conftest import SEED_EDS, connect_and_scan, seed_test_registry, write_seed_eds_files

import canopen_bench.core as core_mod
from canopen_bench.core import Bench
from canopen_bench.db import Db
from canopen_bench.panelspec import PanelError, load_panels, parse_panel
from canopen_bench.plugin import BenchPlugin

SAMPLE = """
name: Sample Feeder
match: {eds: "dut_alpha*"}
groups:
  - title: Tension
    fields:
      - {label: Working, obj: "0x2040:01", unit: cN, scale: 0.1, rw: true}
      - {label: Reduced, obj: "2040:2", unit: cN, scale: 0.1}
  - title: Identity
    cols: 2
    collapsed: true
    fields:
      - {obj: "0x1018"}
"""


def test_a_panel_reads_addresses_the_way_the_object_dictionary_is_written():
    """Hex either way, and a bare index means sub 0 — the spelling the
    catalog and obj_vals use, because a panel whose keys are one leading
    zero off shows dashes and no error anywhere."""
    panel = parse_panel(SAMPLE, "sample.panel.yaml")
    tension, identity = panel.groups
    assert [f.key for f in tension.fields] == ["0x2040:01", "0x2040:02"]
    assert identity.fields[0].key == "0x1018:00"
    assert identity.fields[0].label == "0x1018:00"   # no label: the address itself
    assert (identity.cols, identity.collapsed) == (2, True)
    assert (tension.fields[0].rw, tension.fields[1].rw) == (True, False)


def test_the_decimals_follow_the_scale_without_being_written_out():
    panel = parse_panel(SAMPLE)
    working = panel.groups[0].fields[0]
    assert working.digits == 1
    assert working.show("0x00A0") == "16.0"          # 160 tenths of a cN
    assert working.show(None) == ""                  # not read yet
    assert working.show("DemoDevice") == "DemoDevice"  # a name is not a number


def test_what_the_box_shows_is_what_a_write_sends_back():
    """The one arithmetic error a panel can make: showing tenths and
    staging them as units, so a Write puts a tenth of the displayed
    number into the device."""
    working = parse_panel(SAMPLE).groups[0].fields[0]
    assert working.to_raw("16.0") == 160
    assert working.to_raw("16") == 160
    assert working.to_raw("0x10") == 160             # hex is hex, then scaled
    with pytest.raises(PanelError):
        working.to_raw("-1")             # the EDS says this one is unsigned
    with pytest.raises(PanelError):
        working.to_raw("later")


@pytest.mark.parametrize("text, complaint", [
    ("name: x", "groups"),
    ("groups: []", "groups"),
    ("groups: [{fields: []}]", "title"),
    ("groups: [{title: A, fields: [{label: x}]}]", "obj"),
    ("groups: [{title: A, unti: 2, fields: []}]", "unknown"),
    ("groups: [{title: A, fields: [{obj: '0x2000', unti: mA}]}]", "unknown"),
    ("groups: [{title: A, fields: [{obj: 'nonsense'}]}]", "address"),
    ("groups: [{title: A, fields: [{obj: '0x2000', scale: 0}]}]", "zero"),
    ("groups: [{title: A, fields: []}, {title: A, fields: []}]", "share a title"),
    ("[1, 2]", "mapping"),
    ("groups: [{title: A, fields: [{obj: '0x2000'}]}]\nmatch: {edss: x}", "unknown"),
])
def test_a_typo_is_an_error_and_says_where(text, complaint):
    """Silently dropping a misspelled key leaves its author staring at a
    box that did not change — the one failure mode a file format can have
    that costs an afternoon."""
    with pytest.raises(PanelError) as caught:
        parse_panel(text, "broken.panel.yaml")
    assert complaint in str(caught.value)


def test_a_panel_takes_the_devices_it_says_it_takes():
    panel = parse_panel(SAMPLE)
    assert panel.matches({"eds": "dut_alpha_v2.eds", "name": "DUT_ALPHA"})
    assert not panel.matches({"eds": "dut_beta_v7.eds", "name": "DUT_BETA"})
    assert not panel.matches({"eds": "", "name": ""})
    # no match key at all: a general-purpose panel, every device
    assert parse_panel("groups: [{title: A, fields: [{obj: '0x1000'}]}]") \
        .matches({"eds": "anything.eds", "name": "whatever"})


def test_one_unreadable_panel_costs_only_itself(tmp_path):
    (tmp_path / "good.panel.yaml").write_text(SAMPLE, encoding="utf-8")
    (tmp_path / "bad.panel.yaml").write_text("groups: [{title: A, fields: [{}]}]",
                                             encoding="utf-8")
    said: list[str] = []
    panels = load_panels([tmp_path], log=said.append)
    assert [p.name for p in panels] == ["Sample Feeder"]
    assert len(said) == 1 and "bad.panel.yaml" in said[0] and "obj" in said[0]


# -- the Objects page -------------------------------------------------------

def _bench_with_panel(tmp_path, text: str = SAMPLE) -> Bench:
    """A bench whose only plugin ships one panel file — the real path a
    panel takes, package to hook to page, rather than a list poked in."""
    file = tmp_path / "vendor.panel.yaml"
    file.write_text(text, encoding="utf-8")

    class PanelPlugin(BenchPlugin):
        name = "sample"

        def object_panels(self):
            return [file]

    bench = Bench(Db(tmp_path / "test.db"), plugins=[PanelPlugin()])
    write_seed_eds_files(bench)
    connect_and_scan(bench)
    bench.dispatch("dev_toggle", {"node": 1})   # a panel belongs to one device
    return bench


def test_the_panel_reaches_the_page_for_the_device_it_matches(tmp_path):
    bench = _bench_with_panel(tmp_path)
    assert bench.snapshot()["objects"]["hasPanel"] is True
    assert bench.snapshot()["objects"]["panel"] is None   # the table is still the view

    bench.dispatch("obj_view", {"view": "panel"})
    panel = bench.snapshot()["objects"]["panel"]
    assert panel["name"] == "Sample Feeder"
    assert [g["title"] for g in panel["groups"]] == ["Tension", "Identity"]
    assert [g["open"] for g in panel["groups"]] == [True, False]  # collapsed: true
    assert panel["groups"][0]["fields"][0] == {
        "idx": "0x2040", "sub": "01", "label": "Working", "unit": "cN",
        "rw": True, "widget": "number", "val": "", "src": "", "age": 0.0,
    }


def test_a_device_no_plugin_describes_still_gets_the_standard_objects(tmp_path):
    """The core ships one panel for every device — the objects CiA 301
    makes mandatory. Without it the view is invisible until somebody
    writes a file, and "no panel for this device" looks exactly like "the
    update did not install"."""
    bench = _bench_with_panel(tmp_path, SAMPLE.replace("dut_alpha*", "nothing*"))
    bench.dispatch("obj_view", {"view": "panel"})
    objects = bench.snapshot()["objects"]
    assert objects["hasPanel"] is True
    assert objects["panel"]["name"] == "Standard objects"


def test_a_panel_that_names_its_devices_beats_the_general_one(tmp_path):
    """Both match, and the specific one has to win — otherwise a vendor
    panel would queue behind the core's own instead of replacing it."""
    bench = _bench_with_panel(tmp_path)
    bench.dispatch("obj_view", {"view": "panel"})
    assert bench.snapshot()["objects"]["panel"]["name"] == "Sample Feeder"


def test_no_device_selected_leaves_the_panel_out(tmp_path):
    bench = _bench_with_panel(tmp_path)
    bench.dispatch("dev_toggle", {"node": 1})      # deselected again
    objects = bench.snapshot()["objects"]
    assert objects["hasPanel"] is False and objects["panel"] is None


def test_what_the_operator_folds_away_stays_folded(tmp_path):
    bench = _bench_with_panel(tmp_path)
    bench.dispatch("obj_view", {"view": "panel"})
    bench.dispatch("panel_fold", {"group": "Tension"})
    assert [g["open"] for g in bench.snapshot()["objects"]["panel"]["groups"]] == [False, False]

    again = Bench(Db(bench.db.path), plugins=list(bench.plugins))
    write_seed_eds_files(again)
    connect_and_scan(again)
    again.dispatch("dev_toggle", {"node": 1})
    assert [g["open"] for g in again.snapshot()["objects"]["panel"]["groups"]] == [False, False]


def test_a_staged_value_is_scaled_back_to_what_the_device_stores(tmp_path):
    bench = _bench_with_panel(tmp_path)
    bench.dispatch("obj_view", {"view": "panel"})
    bench.dispatch("panel_set", {"idx": "0x2040", "sub": "01", "val": "16.0"})
    assert bench.obj_vals["0x2040:01"] == "0xA0"
    assert bench.snapshot()["objects"]["panel"]["groups"][0]["fields"][0]["val"] == "16.0"

    bench.dispatch("panel_set", {"idx": "0x2040", "sub": "01", "val": "nope"})
    assert bench.obj_vals["0x2040:01"] == "0xA0"      # refused, not silently cleared
    assert "rejected" in bench.logs[-1]["msg"]


def test_a_box_read_asks_only_for_what_is_showing(tmp_path):
    """Folding a box away is the only thing that says "not interested
    right now" — a page-wide read that walked them anyway would make it
    an appearance setting instead of a decision."""
    bench = _bench_with_panel(tmp_path)
    bench.dispatch("obj_view", {"view": "panel"})
    asked: list[tuple[str, str]] = []
    real_read = bench.bus.sdo_read

    def spy(node, idx, sub, *a, **kw):
        asked.append((idx, sub))
        return real_read(node, idx, sub, *a, **kw)

    bench.bus.sdo_read = spy

    async def go(params):
        asked.clear()
        bench.dispatch("panel_read", params)
        if bench._tasks:
            await asyncio.wait(set(bench._tasks), timeout=5)

    asyncio.run(go({}))                       # every open box: Identity is folded
    assert asked == [("0x2040", "01"), ("0x2040", "02")]

    asyncio.run(go({"group": "Identity"}))    # asked for by name: read anyway
    assert asked == [("0x1018", "00")]


# -- what the device calls its own values -----------------------------------

WIDGETS = """
name: Widget Sample
match: {eds: "dut_alpha*"}
groups:
  - title: Modes
    fields:
      - {label: Mode,   obj: "0x2040:01", widget: enum, rw: true}
      - {label: Locked, obj: "0x2040:01", widget: flag, bit: 4, rw: true}
"""


class _FieldPlugin(BenchPlugin):
    """A plugin that says how one object reads, the way a vendor's does:
    a lane of the word is an enum out of the firmware's own header."""

    name = "fieldy"

    def __init__(self, file):
        self._file = file

    def object_panels(self):
        return [self._file]

    def object_fields(self, symbols):
        from canopen_bench.values import Field
        return {"0x2040:01": [Field(table="eMode", mask=0x0F)]}


def _bench_with_widgets(tmp_path) -> Bench:
    file = tmp_path / "widgets.panel.yaml"
    file.write_text(WIDGETS, encoding="utf-8")
    bench = Bench(Db(tmp_path / "test.db"), plugins=[_FieldPlugin(file)])
    # one directory per origin, which is how the workspace keeps two
    # vendors' identically named tables apart
    (bench.symbols_dir / "fieldy").mkdir(parents=True, exist_ok=True)
    (bench.symbols_dir / "fieldy" / "modes.h").write_text(
        "typedef enum eMode { eMode_Off = 0, eMode_Run = 2 } eMode;\n", encoding="utf-8")
    bench.dispatch("symbols_reload", {})
    write_seed_eds_files(bench)
    connect_and_scan(bench)
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("obj_view", {"view": "panel"})
    return bench


def _fields(bench) -> list[dict]:
    return bench.snapshot()["objects"]["panel"]["groups"][0]["fields"]


def test_an_enum_offers_the_names_the_firmware_uses(tmp_path):
    """The choices come from the device's own headers via object_fields —
    a list written into the panel file would be a second copy of the same
    table, kept in step by hand."""
    bench = _bench_with_widgets(tmp_path)
    bench.obj_vals["0x2040:01"] = "0x12"          # lane 0x0F = 2 = eMode_Run
    mode = _fields(bench)[0]
    assert mode["widget"] == "enum"
    # …without the prefix all four of them share with the table: "eMode_"
    # on every line says nothing the one open dropdown does not, and costs
    # the width the word itself needs
    assert ["0", "Off"] in mode["options"] and ["2", "Run"] in mode["options"]
    assert mode["val"] == "2"


def test_a_value_no_symbol_names_is_shown_rather_than_snapped_to_one(tmp_path):
    bench = _bench_with_widgets(tmp_path)
    bench.obj_vals["0x2040:01"] = "0x07"          # lane = 7, named by nothing
    mode = _fields(bench)[0]
    assert mode["val"] == "7"
    assert ["7", "?0x7"] in mode["options"]


def test_picking_a_name_keeps_the_bits_it_does_not_own(tmp_path):
    """The lane is four bits of a byte. Staging the choice alone would
    clear the rest of the word — including the flag right next to it."""
    bench = _bench_with_widgets(tmp_path)
    bench.obj_vals["0x2040:01"] = "0x12"          # bit 4 set, mode 2
    bench.dispatch("panel_set", {"idx": "0x2040", "sub": "01", "val": "0"})
    assert bench.obj_vals["0x2040:01"] == "0x10"  # mode cleared, bit 4 untouched


def test_a_flag_is_one_bit_and_leaves_the_others_alone(tmp_path):
    bench = _bench_with_widgets(tmp_path)
    bench.obj_vals["0x2040:01"] = "0x02"
    assert _fields(bench)[1]["on"] is False

    bench.dispatch("panel_set", {"idx": "0x2040", "sub": "01", "bit": 4, "on": True})
    assert bench.obj_vals["0x2040:01"] == "0x12"  # bit 4 set, mode 2 still there
    assert _fields(bench)[1]["on"] is True

    bench.dispatch("panel_set", {"idx": "0x2040", "sub": "01", "bit": 4, "on": False})
    assert bench.obj_vals["0x2040:01"] == "0x02"


def test_part_of_a_value_nobody_has_read_is_refused_not_guessed(tmp_path):
    """Writing one bit means writing the whole word. With the other bits
    unknown, the honest answer is to say so — a checkbox that assumed
    zeros would clear every flag beside it."""
    bench = _bench_with_widgets(tmp_path)
    bench.obj_vals.pop("0x2040:01", None)
    bench.dispatch("panel_set", {"idx": "0x2040", "sub": "01", "bit": 4, "on": True})
    assert "0x2040:01" not in bench.obj_vals
    assert "read it before writing part of it" in bench.logs[-1]["msg"]


@pytest.mark.parametrize("text, complaint", [
    ("groups: [{title: A, fields: [{obj: '0x2000', widget: dial}]}]", "unknown widget"),
    ("groups: [{title: A, fields: [{obj: '0x2000', widget: flag}]}]", "needs the bit"),
    ("groups: [{title: A, fields: [{obj: '0x2000', widget: flag, bit: 44}]}]", "0…31"),
    ("groups: [{title: A, fields: [{obj: '0x2000', bit: 3}]}]", "belongs to a flag"),
    ("groups: [{title: A, fields: [{obj: '0x2000', widget: enum, unit: mA}]}]", "belong to a number"),
])
def test_a_widget_that_cannot_mean_what_it_says_is_an_error(text, complaint):
    with pytest.raises(PanelError) as caught:
        parse_panel(text, "broken.panel.yaml")
    assert complaint in str(caught.value)


# -- values the bus carried past --------------------------------------------

def test_a_value_the_bus_carried_past_fills_the_box(tmp_path):
    """The trace already decodes every SDO answer and unpacks every mapped
    PDO signal. A box showing one of those objects can have the value for
    nothing — which is the only kind of live a bench should offer, since
    the other kind is polling."""
    bench = _bench_with_panel(tmp_path)
    bench.dispatch("obj_view", {"view": "panel"})
    # somebody else's SDO answer for 0x2040:01 = 0x00A0, node 1
    bench._drain_frames()
    bench._annotate_sdo({"cob": "0x581", "data": "4B 40 20 01 A0 00 00 00",
                         "node": 1, "cls": "SDO", "obj": "", "val": ""})

    field = bench.snapshot()["objects"]["panel"]["groups"][0]["fields"][0]
    assert field["val"] == "16.0"        # tenths of a cN, as the panel says
    assert field["src"] == "bus"
    assert field["age"] < 1


def test_a_pdo_signal_counts_as_much_as_an_sdo_answer(tmp_path):
    """"Perfect would be if the PDO parts could be used too" — they can:
    the trace unpacks them against the EDS default mapping, and a panel
    field on a mapped object follows without a frame of its own."""
    bench = _bench_with_panel(tmp_path)
    bench.dispatch("obj_view", {"view": "panel"})
    bench._bus_sample(1, 0x2040, 0x01, 0x00C8, 2)     # what the annotator calls

    field = bench.snapshot()["objects"]["panel"]["groups"][0]["fields"][0]
    assert (field["val"], field["src"]) == ("20.0", "bus")


def test_what_the_operator_typed_is_not_overwritten_by_the_bus(tmp_path):
    """obj_vals holds what was read *and* what is staged for writing. A
    value arriving from the bus may not push a staged one out from under
    the hand that typed it."""
    bench = _bench_with_panel(tmp_path)
    bench.dispatch("obj_view", {"view": "panel"})
    bench._bus_sample(1, 0x2040, 0x01, 0x00A0, 2)     # bus says 16.0
    bench.dispatch("panel_set", {"idx": "0x2040", "sub": "01", "val": "25.0"})

    field = bench.snapshot()["objects"]["panel"]["groups"][0]["fields"][0]
    assert (field["val"], field["src"]) == ("25.0", "read")

    bench._bus_sample(1, 0x2040, 0x01, 0x0032, 2)     # and now the bus is newer
    assert bench.snapshot()["objects"]["panel"]["groups"][0]["fields"][0]["val"] == "5.0"


def test_a_value_belongs_to_the_device_it_came_from(tmp_path):
    bench = _bench_with_panel(tmp_path)
    bench.dispatch("obj_view", {"view": "panel"})
    bench._bus_sample(9, 0x2040, 0x01, 0x00A0, 2)     # another node entirely
    assert bench.snapshot()["objects"]["panel"]["groups"][0]["fields"][0]["val"] == ""


# -- boxes a machine does not have ------------------------------------------

CONDITIONAL = """
name: Sample Feeder
match: {eds: "dut_alpha*"}
groups:
  - title: Always
    fields: [{obj: "0x2040:01"}]
  - title: Second axis
    when: {obj: "0x2050:00", bit: 3}
    fields: [{obj: "0x2040:02"}]
"""


def _titles(bench) -> list[str]:
    return [g["title"] for g in bench.snapshot()["objects"]["panel"]["groups"]]


def test_a_box_is_there_until_the_device_says_otherwise(tmp_path):
    """Unknown means yes. A condition may take a box away once the device
    has answered; it may not keep one hidden before anything was asked —
    the object that settles it would sit behind the box it is hiding."""
    bench = _bench_with_panel(tmp_path, CONDITIONAL)
    bench.dispatch("obj_view", {"view": "panel"})
    assert _titles(bench) == ["Always", "Second axis"]

    bench.obj_vals["0x2050:00"] = "0x08"          # bit 3: this one has one
    bench.obj_vals_at["0x2050:00"] = time.monotonic()
    assert _titles(bench) == ["Always", "Second axis"]

    bench.obj_vals["0x2050:00"] = "0x04"          # and this one does not
    bench.obj_vals_at["0x2050:00"] = time.monotonic()
    assert _titles(bench) == ["Always"]


def test_a_page_read_asks_what_the_conditions_are_about(tmp_path):
    """Otherwise the box only disappears once somebody reads that object
    by hand, and nothing on the page says which object that is."""
    bench = _bench_with_panel(tmp_path, CONDITIONAL)
    bench.dispatch("obj_view", {"view": "panel"})
    asked: list[tuple[str, str]] = []
    real = bench.bus.sdo_read
    bench.bus.sdo_read = lambda node, idx, sub, *a, **kw: (
        asked.append((idx, sub)) or real(node, idx, sub, *a, **kw))

    async def go():
        bench.dispatch("panel_read", {})
        if bench._tasks:
            await asyncio.wait(set(bench._tasks), timeout=5)

    asyncio.run(go())
    assert ("0x2050", "00") in asked


VARIANTS = """
name: Sample Feeder
match: {eds: "dut_alpha*"}
groups:
  - title: Always
    fields: [{obj: "0x2040:01"}]
  - title: Second axis
    when: {obj: "0x2050:00", value: [3, 4]}
    fields: [{obj: "0x2040:02"}]
"""


def test_a_part_two_variants_carry_is_one_box_not_two(tmp_path):
    """A device family is usually numbered, not flagged: the part is on
    two of the variants and on none of the others, and the variant object
    answers one number or the other. Without a list that box has to be
    written out twice, with every field in it duplicated, to express one
    "or"."""
    bench = _bench_with_panel(tmp_path, VARIANTS)
    bench.dispatch("obj_view", {"view": "panel"})

    for variant, expected in ((3, ["Always", "Second axis"]),
                              (4, ["Always", "Second axis"]),
                              (7, ["Always"])):
        bench.obj_vals["0x2050:00"] = f"0x{variant:04X}"
        bench.obj_vals_at["0x2050:00"] = time.monotonic()
        assert _titles(bench) == expected, variant


@pytest.mark.parametrize("text, complaint", [
    ("groups: [{title: A, when: 3, fields: []}]", "must be a mapping"),
    ("groups: [{title: A, when: {bit: 3}, fields: []}]", "needs an obj"),
    ("groups: [{title: A, when: {obj: '0x2000'}, fields: []}]", "neither"),
    ("groups: [{title: A, when: {obj: '0x2000', bit: 1, value: 2}, fields: []}]", "both"),
    ("groups: [{title: A, when: {obj: '0x2000', bit: 99}, fields: []}]", "0…31"),
    ("groups: [{title: A, when: {obj: '0x2000', bti: 1}, fields: []}]", "unknown"),
    ("groups: [{title: A, when: {obj: '0x2000', value: []}, fields: []}]", "at least one"),
    ("groups: [{title: A, when: {obj: '0x2000', value: [1, x]}, fields: []}]", "list of numbers"),
])
def test_a_condition_that_cannot_be_answered_is_an_error(text, complaint):
    with pytest.raises(PanelError) as caught:
        parse_panel(text, "broken.panel.yaml")
    assert complaint in str(caught.value)


def test_the_tooltip_carries_every_reading_of_the_number(tmp_path):
    """The panel prints decimal because that is what its units are read
    in — which is exactly why hex has to stay one hover away, the same way
    the object table and the favourites panel keep it."""
    bench = _bench_with_panel(tmp_path)
    bench.dispatch("obj_view", {"view": "panel"})
    bench.obj_vals["0x2040:01"] = "0x00A0"
    bench.obj_vals_at["0x2040:01"] = time.monotonic()

    field = bench.snapshot()["objects"]["panel"]["groups"][0]["fields"][0]
    assert field["alt"] == "0x00A0 · 160"      # hex as the device stores it, then decimal


def test_a_text_object_is_not_learned_from_an_expedited_frame(tmp_path):
    """An expedited response carries four bytes. For a device name that is
    the first letter, and "DemoDevice" came back as 68 — 0x44 read as a
    number, overwriting the string somebody had actually read. The rest
    arrives in segments this decoder does not follow, so there is nothing
    here worth remembering for a text object."""
    bench = _bench_with_panel(tmp_path)
    # the demo device's EDS, which declares 0x2004 as a VISIBLE_STRING —
    # without a type there is nothing to go on, and the sample is kept
    bench.db.eds_write_file("dut_alpha_v2.eds",
                            core_mod.SEED_EDS.read_text(encoding="utf-8"))
    bench._ods.retarget(bench.db.eds_dir)
    bench.obj_vals["0x2004:00"] = "DemoDevice"
    bench.obj_vals_at["0x2004:00"] = time.monotonic() - 60

    # node 1's answer for 0x2004:00, one byte, 0x44 = "D"
    bench._annotate_sdo({"cob": "0x581", "data": "4F 04 20 00 44 00 00 00",
                         "node": 1, "cls": "SDO", "obj": "", "val": ""})

    assert (1, "0x2004:00") not in bench.seen_vals
    assert bench._panel_value("0x2004:00", 1)[0] == "DemoDevice"


# -- what the EDS says about an object --------------------------------------

TEXT_EDS = SEED_EDS + """
[1008]
ParameterName=Manufacturer device name
ObjectType=0x7
DataType=0x0009
AccessType=ro
DefaultValue=SEED_DEV

[2060]
ParameterName=Start the motor test
ObjectType=0x7
DataType=0x0007
AccessType=wo
DefaultValue=0

[2061]
ParameterName=Velocity actual value
ObjectType=0x7
DataType=0x0003
AccessType=rw
DefaultValue=0
"""

TEXT_PANEL = """
name: Sample Feeder
match: {eds: "dut_alpha*"}
groups:
  - title: Identity
    fields:
      - {label: Device name, obj: "0x1008:00"}
      - {label: Counter,     obj: "0x2000:00"}
      - {label: Motor test,  obj: "0x2060:00", rw: true}
      - {label: Velocity,    obj: "0x2061:00", unit: rpm, rw: true}
"""


def _bench_with_text_eds(tmp_path) -> Bench:
    file = tmp_path / "vendor.panel.yaml"
    file.write_text(TEXT_PANEL, encoding="utf-8")

    class PanelPlugin(BenchPlugin):
        name = "sample"

        def object_panels(self):
            return [file]

    bench = Bench(Db(tmp_path / "test.db"), plugins=[PanelPlugin()])
    seed_test_registry(bench)
    for entry in bench.db.eds_list():
        if entry["enabled"]:
            bench.db.eds_write_file(entry["file"], TEXT_EDS)
    connect_and_scan(bench)
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("obj_view", {"view": "panel"})
    return bench


def test_the_bytes_of_a_word_are_read_back_the_way_they_were_sent(tmp_path):
    """The bus formats a payload little-endian, which is right for the
    integers that are most of an object dictionary and puts the last byte
    of a string first. Read straight, a name comes out backwards — which
    looks enough like a name that nobody checks it twice."""
    assert Bench._as_text("0x726564656546") == "Feeder"
    assert Bench._as_text("0x00726564656546") == "Feeder"   # trailing NUL
    assert Bench._as_text("DemoDevice") == "DemoDevice"     # already a word
    assert Bench._as_text("0xABC") == "0xABC"               # not whole bytes


def test_a_device_name_is_a_word_not_nineteen_digits(tmp_path):
    """The bus carries a name as bytes like everything else, and a box that
    reads those bytes as a number prints 3472900244173440512 where the
    device said its name. The EDS is what knows the difference."""
    bench = _bench_with_text_eds(tmp_path)
    bench.obj_vals["0x1008:00"] = "0x726564656546"        # "Feeder", as the bus spells it
    bench.obj_vals["0x2000:00"] = "0x2A"
    name, counter, *_ = bench.snapshot()["objects"]["panel"]["groups"][0]["fields"]
    assert name["val"] == "Feeder"
    assert counter["val"] == "42", "a number must not be run through the decoder"


def test_a_unit_on_a_word_is_dropped_rather_than_printed(tmp_path):
    """`unit` and `scale` are a number's, and a panel written against a
    device's documentation may well carry one by accident. "Feeder mV" would
    be the one reading nobody can correct from the screen."""
    bench = _bench_with_text_eds(tmp_path)
    bench.obj_vals["0x1008:00"] = "0x726564656546"
    assert bench.snapshot()["objects"]["panel"]["groups"][0]["fields"][0]["unit"] == ""


def test_a_page_read_leaves_the_write_only_objects_alone(tmp_path):
    """The SDO could only abort, and a row of aborts in the log reads as a
    fault when it is the EDS telling the truth. The ⟳ at the field still
    asks — that one is somebody deciding to."""
    bench = _bench_with_text_eds(tmp_path)
    asked: list[str] = []
    real = bench.bus.sdo_read
    bench.bus.sdo_read = lambda node, idx, sub, *a, **kw: (
        asked.append(f"{idx}:{sub}") or real(node, idx, sub, *a, **kw))

    async def go():
        bench.dispatch("panel_read", {})
        if bench._tasks:
            await asyncio.wait(set(bench._tasks), timeout=5)

    asyncio.run(go())
    assert "0x1008:00" in asked and "0x2000:00" in asked
    assert "0x2060:00" not in asked


# -- numbers that carry a sign ----------------------------------------------

def test_a_word_the_eds_calls_signed_is_read_as_one():
    """A word carries no sign of its own. A motor turning backwards reads
    as 65036 unless something says how wide the object is — the worst kind
    of wrong number, because it is in range and it moves when the device
    moves."""
    field = parse_panel(SAMPLE).groups[0].fields[1]     # scale 0.1, no rw
    assert field.show("0xFE0C", 16) == "-50.0"          # -500 tenths
    assert field.show("0xFE0C") == "6503.6"             # unsigned, as before
    assert field.show("0x7FFF", 16) == "3276.7"         # the top of the range
    assert field.show("0xFFFFFE0C", 32) == "-50.0"


def test_a_box_that_shows_a_negative_can_send_one():
    field = parse_panel(SAMPLE).groups[0].fields[0]     # scale 0.1, rw
    assert field.to_raw("-50.0", 16) == 0xFE0C
    assert field.to_raw("50.0", 16) == 500
    with pytest.raises(PanelError):                     # unsigned, as before
        field.to_raw("-50.0")
    for text in ("-3276.9", "3276.8"):                  # outside 16 signed bits
        with pytest.raises(PanelError):
            field.to_raw(text, 16)


def test_the_panel_takes_the_width_from_the_eds(tmp_path):
    """Both directions, through the page: the EDS says INTEGER16, so a
    device answering 0xFE0C shows -500 and typing -500 stages 0xFE0C —
    at the object's own width, because a two's complement is only itself
    there."""
    bench = _bench_with_text_eds(tmp_path)
    bench.obj_vals["0x2061:00"] = "0xFE0C"
    assert _fields(bench)[3]["val"] == "-500"

    bench.dispatch("panel_set", {"idx": "0x2061", "sub": "00", "val": "-500"})
    assert bench.obj_vals["0x2061:00"] == "0xFE0C"
