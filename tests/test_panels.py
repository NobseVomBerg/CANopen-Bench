"""Panel files: the format, and what the Objects page does with one.

A panel is written by hand against a device's documentation, so the two
things worth testing hardest are the ones a person gets wrong: the
address spelling (a key that differs by a leading zero finds nothing, and
says nothing) and the scaling (a box that shows 16.0 must write 160).
"""
from __future__ import annotations

import asyncio

import pytest
from conftest import connect_and_scan, write_seed_eds_files

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
        working.to_raw("-1")
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
        "rw": True, "val": "",
    }


def test_a_device_the_panel_does_not_match_falls_back_to_the_table(tmp_path):
    bench = _bench_with_panel(tmp_path, SAMPLE.replace("dut_alpha*", "nothing*"))
    bench.dispatch("obj_view", {"view": "panel"})
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
