"""Two device families on one bench.

Each family is a plugin with headers of its own, and the headers use the same
names for other values: both have an eObjIdx with an eObjIdx_Lamp, both have
an eLamp with an eLamp_On. Tables that merge the two cannot be read by either,
so what a device's family knows is answered per family — ``Bench.symbols_for``
for the tables, ``Bench._owner`` for whose device it is — and this file holds
that to what it was built for: each family reads its own words, a device
nobody owns reads what every plugin says as it always did, and one plugin
alone sees no change at all.

Three plugins, in this order. acme and borg are the families — naming EDS rows
in ``seed_eds()`` is what makes a plugin one. misc names none, owns no
devices, and so speaks for every device.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import NamedTuple

import pytest
from conftest import connect_and_scan
from test_executor import run_selected

from canopen_bench import mcp
from canopen_bench.bus.canopen_bus import _decode_cob
from canopen_bench.core import Bench, trace_class, trace_node
from canopen_bench.db import Db
from canopen_bench.plugin import BenchPlugin
from canopen_bench.symbols import SymbolError
from canopen_bench.values import Field, Quantity

#: the nodes the demo bus puts the devices on: it gives the first profile two
#: DUTs and the others one each, so node 2 is a second acme
ACME, BORG, PLAIN = 1, 3, 4

# -- what the families write -------------------------------------------------

#: the same two names at swapped addresses — eObjIdx_Lamp is 0x2060 in acme's
#: headers and 0x2061 in borg's, eObjIdx_LampControl the other way round — and
#: the same table with other numbers in it. 0x2070 is a name only acme has,
#: and eLamp_Blinking a member only borg's table has. eSpeed is the lane of a
#: register (mask 0xF00): plain 1 and 2 for acme, which the lane moves up to
#: bit 8, and already in place — 0x100, 0x200 — for borg
ACME_H = """\
/// @brief Object indices of the acme family
typedef enum eObjIdx {
    eObjIdx_Lamp        = 0x2060,
    eObjIdx_LampControl = 0x2061,
    eObjIdx_Process     = 0x2070,
} eObjIdx;

/// @brief Lamp states of the acme family
typedef enum eLamp {
    eLamp_Off = 0,
    eLamp_On  = 1,
} eLamp;

typedef enum eSpeed {
    eSpeed_Slow = 1,
    eSpeed_Fast = 2,
} eSpeed;
"""

BORG_H = """\
/// @brief Object indices of the borg family
typedef enum eObjIdx {
    eObjIdx_LampControl = 0x2060,
    eObjIdx_Lamp        = 0x2061,
} eObjIdx;

/// @brief Lamp states of the borg family
typedef enum eLamp {
    eLamp_Off      = 0,
    eLamp_On       = 5,
    eLamp_Blinking = 6,
} eLamp;

typedef enum eSpeed {
    eSpeed_Slow = 0x100,
    eSpeed_Fast = 0x200,
} eSpeed;
"""


def _eds(vendor: int, product: str, number: int, mode: int) -> str:
    """The same small object dictionary for everybody — a mode, a level and a
    mapped TPDO — so that the EDS names no family: what a device is called in
    its own words comes from the headers."""
    return f"""\
[FileInfo]
FileName={product.lower()}.eds
FileVersion=1
FileRevision=1
EDSVersion=4.0

[DeviceInfo]
VendorName=Invented Vendor
VendorNumber={vendor}
ProductName={product}
ProductNumber={number}
RevisionNumber=1

[1000]
ParameterName=Device type
ObjectType=0x7
DataType=0x0007
AccessType=ro
DefaultValue=0x00000000

[2060]
ParameterName=Device mode
ObjectType=0x7
DataType=0x0005
AccessType=rw
DefaultValue={mode}

[2061]
ParameterName=Device level
ObjectType=0x7
DataType=0x0006
AccessType=rw
DefaultValue=160

[1A00]
ParameterName=TPDO1 mapping parameter
ObjectType=0x9
SubNumber=2

[1A00sub0]
ParameterName=Number of mapped objects
ObjectType=0x7
DataType=0x0005
AccessType=ro
DefaultValue=1

[1A00sub1]
ParameterName=Mapping entry 1
ObjectType=0x7
DataType=0x0007
AccessType=ro
DefaultValue=0x20600008
"""


#: a case and a flow as a family's author writes them: the bare name, because
#: they were written against that family's headers and nobody else's
CASE = """\
id: "{ident}"
name: {name} lamp answers
steps:
  - sdo_read: {{index: $eObjIdx_Lamp, sub: "00"}}
"""

FLOW = """\
id: "{name}-lamp"
name: {name} lamp flow
steps:
  - sdo_write: {{index: $eObjIdx_Lamp, sub: "00", value: "0x01"}}
"""

#: the one panel, for every device: which names the enum offers is the
#: device's family's to say
PANEL = """\
name: Lamp
groups:
  - title: Lamp
    fields:
      - {label: Mode,  obj: "0x2060:00", widget: enum, rw: true}
      - {label: Level, obj: "0x2061:00", rw: true}
      - {label: Speed, obj: "0x2066:00", widget: enum, rw: true}
"""


# -- the plugins -------------------------------------------------------------

class _Family(BenchPlugin):
    """One device family. What it says is about the same addresses, codes and
    numbers the other family says something else about — and it keeps the
    tables its hooks were handed, to see which ones those were."""

    header = ""
    vendor = 0
    default_mode = 0
    #: what its own headers say eObjIdx_Lamp and eLamp_On are
    lamp = 0
    lamp_on = 0
    mask = 0
    unit = Quantity()
    #: an address only this family describes — fields for one, a unit for the other
    only_fields: dict[str, list[Field]] = {}
    only_units: dict[str, Quantity] = {}
    emcy: dict[int, str] = {}
    mec: dict[int, str] = {}
    case_id = ""

    def __init__(self, root: Path):
        self.dir = root / self.name
        self.handed: dict[str, list] = {"object_fields": [], "object_units": [],
                                        "describe_object": []}
        for folder, file, text in (
                ("symbols", f"{self.name}.h", self.header),
                ("eds", f"{self.name}_lamp.eds",
                 _eds(self.vendor, f"{self.name.upper()}_LAMP", 1, self.default_mode)),
                ("cases", f"TC{self.case_id}_{self.name}_lamp.yaml",
                 CASE.format(ident=self.case_id, name=self.name)),
                ("flows", f"{self.name}_lamp.yaml", FLOW.format(name=self.name))):
            (self.dir / folder).mkdir(parents=True, exist_ok=True)
            (self.dir / folder / file).write_text(text, encoding="utf-8")

    def symbol_dirs(self):
        return [self.dir / "symbols"]

    def eds_dirs(self):
        return [self.dir / "eds"]

    def testcase_dirs(self):
        return [self.dir / "cases"]

    def flow_dirs(self):
        return [self.dir / "flows"]

    def seed_eds(self):
        return [{"file": f"{self.name}_lamp.eds", "dev": f"{self.name.upper()}_LAMP",
                 "ident": f"0x{self.vendor:X}·0x1", "code": self.name[:3].upper(), "enabled": True}]

    def object_fields(self, symbols):
        self.handed["object_fields"].append(symbols)
        return {"0x2060:00": [Field("eLamp", mask=self.mask)],
                "0x2066:00": [Field("eSpeed", mask=0xF00)]} | self.only_fields

    def object_units(self, symbols):
        self.handed["object_units"].append(symbols)
        return {"0x2061:00": self.unit} | self.only_units

    def describe_object(self, index, sub, symbols):
        self.handed["describe_object"].append(symbols)
        return symbols.name("eObjIdx", int(index, 16))

    def emcy_codes(self):
        return dict(self.emcy)

    def emcy_mec_text(self, mec):
        return self.mec.get(mec, "")


class _Acme(_Family):
    name = "acme"
    header = ACME_H
    vendor = 900001
    default_mode = 1
    lamp, lamp_on, mask = 0x2060, 1, 0x0F
    unit = Quantity("lx", 0.1)
    only_fields = {"0x2064:00": [Field("eLamp", mask=0x0F)]}
    emcy = {0x5001: "Acme lamp burned out", 0x5002: "Acme spare lamp used"}
    mec = {0x0065: "Acme sensor fault", 0x0066: "Acme supply low"}
    case_id = "9001"


class _Borg(_Family):
    name = "borg"
    header = BORG_H
    vendor = 900002
    default_mode = 5
    lamp, lamp_on, mask = 0x2061, 5, 0xFF
    unit = Quantity("mA", 0.01)
    only_units = {"0x2065:00": Quantity("mm", 0.5)}
    emcy = {0x5001: "Borg motor stalled", 0x5003: "Borg spare motor used"}
    mec = {0x0065: "Borg sensor fault"}
    case_id = "9002"


class _Misc(BenchPlugin):
    """A general-purpose plugin: no headers, no EDS rows — nothing of its own
    to be a family of, so what it knows is about every device."""

    name = "misc"

    def __init__(self, root: Path):
        self.dir = root / "misc"
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "lamp.panel.yaml").write_text(PANEL, encoding="utf-8")

    def object_panels(self):
        return [self.dir / "lamp.panel.yaml"]

    def object_units(self, symbols):
        return {"0x2062:00": Quantity("rpm", 10)}

    def object_fields(self, symbols):
        return {"0x2063:00": [Field("eLamp", mask=0x0F)]}

    def describe_object(self, index, sub, symbols):
        return "eMiscName" if index == "0x2090" else ""

    def emcy_codes(self):
        return {0x6001: "Misc watchdog"}

    def emcy_mec_text(self, mec):
        return "Misc fault" if mec == 0x0123 else ""


class World(NamedTuple):
    bench: Bench
    acme: _Acme
    borg: _Borg | None = None
    misc: _Misc | None = None


@pytest.fixture(params=["fresh", "after a symbols reload"])
def fam(request, tmp_path) -> World:
    """acme, borg and misc on one bench, with a device of each family and one
    nobody owns (an EDS uploaded by hand), scanned on the demo bus. Every
    test runs twice: as the bench started, and after the headers were taken
    again — what is true of one has to be true of the other."""
    root = tmp_path / "packages"
    acme, borg, misc = _Acme(root), _Borg(root), _Misc(root)
    workspace = tmp_path / "workspace"
    workspace.mkdir()      # an existing folder: not a first run, no demo EDS
    bench = Bench(Db(workspace / "bench.db"), plugins=[acme, borg, misc])
    ok, why = bench.add_eds_file("plain_lamp.eds", _eds(900003, "PLAIN_LAMP", 3, 7))
    assert ok, why
    connect_and_scan(bench)
    if request.param != "fresh":
        bench.dispatch("symbols_reload", {})
    return World(bench, acme, borg, misc)


# -- helpers -----------------------------------------------------------------

def _pick(bench: Bench, node: int) -> None:
    """Select one device and no other, the way the Devices box does."""
    for dev in [d for d in bench.devices if d["sel"] and d["node"] != node]:
        bench.dispatch("dev_toggle", {"node": dev["node"]})
    if not any(d["sel"] for d in bench.devices):
        bench.dispatch("dev_toggle", {"node": node})


def _frame(cob: str, data: str) -> dict:
    """A trace row as the drain builds it, for the annotators to read."""
    dec = _decode_cob(int(cob, 16))
    return {"time": "", "dir": "RX", "cob": cob, "len": "8", "data": data, "dec": dec,
            "flag": "", "cls": trace_class(dec), "node": trace_node(cob), "obj": "", "val": ""}


def _tool(bench: Bench, name: str, **args):
    """An MCP tool's answer: (parsed JSON or text, isError)."""
    answer = asyncio.run(mcp.handle(bench, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                            "params": {"name": name, "arguments": args}}))
    result = answer["result"]
    text = result["content"][0]["text"]
    try:
        return json.loads(text), result["isError"]
    except ValueError:
        return text, result["isError"]


def _panel_fields(bench: Bench) -> dict[str, dict]:
    """The fields of the object page's one box, by label."""
    bench.dispatch("obj_view", {"view": "panel"})
    box = bench.snapshot()["objects"]["panel"]["groups"][0]
    return {field["label"]: field for field in box["fields"]}


# -- whose device it is ------------------------------------------------------

def test_a_device_belongs_to_the_family_whose_row_names_its_eds(fam):
    bench = fam.bench
    # the premise: the scan gave each device the EDS its identity matched
    assert {d["node"]: d["eds"] for d in bench.devices} == {
        1: "acme_lamp.eds", 2: "acme_lamp.eds", 3: "borg_lamp.eds", 4: "plain_lamp.eds"}
    assert [bench._owner(node) for node in (1, 2, 3, 4)] == ["acme", "acme", "borg", ""]


def test_a_node_nobody_scanned_has_no_owner(fam):
    assert fam.bench._owner(99) == ""


@pytest.mark.parametrize("eds", ["—", ""], ids=["none assigned", "empty"])
def test_a_device_with_no_eds_has_no_owner(fam, eds):
    next(d for d in fam.bench.devices if d["node"] == ACME)["eds"] = eds
    assert fam.bench._owner(ACME) == ""


def test_with_no_node_the_owner_is_the_selected_devices(fam):
    bench = fam.bench
    assert bench._owner() == "", "nothing is selected"
    for node, owner in ((ACME, "acme"), (BORG, "borg"), (PLAIN, ""), (ACME, "acme")):
        _pick(bench, node)
        assert bench._owner() == owner, node


# -- the tables each of them reads -------------------------------------------

@pytest.mark.parametrize("family", ["acme", "borg"])
def test_a_family_plugins_hooks_are_handed_the_tables_as_its_own_headers_read_them(fam, family):
    plugin = getattr(fam, family)
    for hook in ("object_fields", "object_units"):
        assert plugin.handed[hook], hook
        for symbols in plugin.handed[hook]:
            assert symbols.value("eObjIdx_Lamp") == plugin.lamp, hook
            assert symbols.value("eLamp_On") == plugin.lamp_on, hook


def test_describe_object_is_handed_its_own_plugins_view_whoever_is_asking(fam):
    """For a device nobody owns every plugin is asked, in turn — and each of
    them reads the tables as its own headers say, not the merged ones."""
    assert fam.bench._label("0x20A0", "00", "", "") == "", "an address nobody names"
    for plugin in (fam.acme, fam.borg):
        assert plugin.handed["describe_object"], plugin.name
        for symbols in plugin.handed["describe_object"]:
            assert symbols.value("eObjIdx_Lamp") == plugin.lamp, plugin.name


def test_another_familys_plugin_is_not_asked_about_a_device_that_is_not_its_own(fam):
    asked = len(fam.acme.handed["describe_object"])
    fam.bench._label("0x20B0", "00", "", "borg")
    assert len(fam.acme.handed["describe_object"]) == asked
    assert fam.borg.handed["describe_object"], "borg is asked about its own"


def test_symbols_for_is_a_plugins_own_view_of_the_tables(fam):
    bench = fam.bench
    assert bench.symbols_for("acme").value("eObjIdx_Lamp") == 0x2060
    assert bench.symbols_for("borg").value("eObjIdx_Lamp") == 0x2061
    assert bench.symbols_for("acme").value("eObjIdx_LampControl") == 0x2061
    assert bench.symbols_for("borg").value("eObjIdx_LampControl") == 0x2060
    assert bench.symbols_for("acme").value("borg:eObjIdx_Lamp") == 0x2061, \
        "the other's is reachable, by saying whose it is"


def test_a_plugin_without_headers_gets_the_bench_wide_tables(fam):
    assert fam.bench.symbols_for("misc") is fam.bench.symbols
    assert fam.bench.symbols_for("nobody") is fam.bench.symbols


def test_the_bench_wide_tables_still_call_a_shared_name_ambiguous(fam):
    """What a file nobody ships is read against — and the premise of every
    test here that a family's own view is what makes the name resolve."""
    with pytest.raises(SymbolError, match="qualify"):
        fam.bench.symbols.value("eObjIdx_Lamp")


# -- the cases and flows a plugin ships --------------------------------------

def test_each_familys_packaged_case_loads_against_its_own_headers(fam):
    """Before, the second family's headers made the name ambiguous and both
    cases failed to load for want of a prefix they were never written with."""
    for ident, lamp in (("9001", 0x2060), ("9002", 0x2061)):
        case = fam.bench.testcases[ident]
        assert case.error is None, case.error
        assert case.steps == [{"sdo_read": {"index": f"0x{lamp:X}", "sub": "00"}}]


def test_a_case_nobody_ships_has_to_say_whose_headers_it_means(fam):
    bench = fam.bench
    folder = Path(bench.paths["tc"])
    for ident, index in (("9003", "$eObjIdx_Lamp"), ("9004", "$acme:eObjIdx_Lamp")):
        (folder / f"TC{ident}_mine.yaml").write_text(
            f'id: "{ident}"\nname: my own\nsteps:\n  - sdo_read: {{index: {index}, sub: "00"}}\n',
            encoding="utf-8")
    bench.dispatch("tc_rescan", {})

    mine = {tc.file: tc for tc in bench.testcases.values()}
    bare, qualified = mine["TC9003_mine.yaml"], mine["TC9004_mine.yaml"]
    assert bare.error and "qualify" in bare.error and "eObjIdx_Lamp" in bare.error
    assert qualified.error is None
    assert qualified.steps[0]["sdo_read"]["index"] == "0x2060"


def test_each_plugins_flow_is_read_against_its_own_headers(fam):
    for name, lamp in (("acme_lamp.yaml", 0x2060), ("borg_lamp.yaml", 0x2061)):
        flow = fam.bench._load_flow(name)
        assert flow is not None, fam.bench.logs[-1]["msg"]
        assert flow.steps[0]["sdo_write"]["index"] == f"0x{lamp:X}", name


def test_a_flow_nobody_ships_is_read_against_the_bench_wide_tables(fam):
    bench = fam.bench
    (bench.flows_dir / "mine.yaml").write_text(FLOW.format(name="my"), encoding="utf-8")
    assert bench._load_flow("mine.yaml") is None
    assert "qualify" in bench.logs[-1]["msg"] and "mine.yaml" in bench.logs[-1]["msg"]


# -- the words of a device ---------------------------------------------------

@pytest.mark.parametrize("owner, index, want", [
    ("acme", "0x2060", "eObjIdx_Lamp"),
    ("borg", "0x2060", "eObjIdx_LampControl"),   # one address, two things
    ("", "0x2060", "eObjIdx_Lamp"),              # nobody's: the first plugin with a name
    ("acme", "0x2061", "eObjIdx_LampControl"),
    ("borg", "0x2061", "eObjIdx_Lamp"),
    ("", "0x2061", "eObjIdx_LampControl"),
    ("acme", "0x2070", "eObjIdx_Process"),
    ("borg", "0x2070", "the EDS's name"),        # acme's word is for acme's devices
    ("", "0x2070", "eObjIdx_Process"),
])
def test_an_object_is_named_the_way_the_devices_family_names_it(fam, owner, index, want):
    assert fam.bench._label(index, "00", "the EDS's name", owner) == want


def test_a_name_remembered_for_one_owner_is_not_handed_to_the_next(fam):
    bench = fam.bench
    owners = ["acme", "borg", ""]
    want = ["eObjIdx_Lamp", "eObjIdx_LampControl", "eObjIdx_Lamp"]
    assert [bench._label("0x2060", "00", "", owner) for owner in owners] == want
    assert [bench._label("0x2060", "00", "", owner) for owner in reversed(owners)] == want[::-1]


def test_a_report_line_reads_a_value_in_the_words_of_the_device_it_is_about(fam):
    note = fam.bench._value_note
    assert note("0x2060", "00", "0x01", node=ACME) == "0x01 — eLamp_On"
    assert note("0x2060", "00", "0x05", node=BORG) == "0x05 — eLamp_On"
    # not in the other family's: 5 names nothing in acme's table, 1 nothing in borg's
    assert note("0x2060", "00", "0x05", node=ACME) == "0x05 — ?0x5"
    assert note("0x2060", "00", "0x01", node=BORG) == "0x01 — ?0x1"


def test_a_report_line_gives_a_value_the_unit_of_the_device_it_is_about(fam):
    note = fam.bench._value_note
    assert note("0x2061", "00", "0xA0", node=ACME) == "0xA0 — 16.0 lx"
    assert note("0x2061", "00", "0xA0", node=BORG) == "0xA0 — 1.60 mA"


def test_a_report_line_leaves_an_address_only_the_other_family_described_alone(fam):
    note = fam.bench._value_note
    assert note("0x2064", "00", "0x01", node=ACME) == "0x01 — eLamp_On"
    assert note("0x2064", "00", "0x01", node=BORG) == "0x01"
    assert note("0x2065", "00", "0x0A", node=BORG) == "0x0A — 5.0 mm"
    assert note("0x2065", "00", "0x0A", node=ACME) == "0x0A"


def test_a_report_line_without_a_node_is_about_the_selected_device(fam):
    bench = fam.bench
    _pick(bench, BORG)
    assert bench._value_note("0x2060", "00", "0x05") == "0x05 — eLamp_On"
    _pick(bench, ACME)
    assert bench._value_note("0x2060", "00", "0x05") == "0x05 — ?0x5"


def test_a_step_line_names_the_object_the_way_the_steps_device_does(fam):
    """The device a step is about is the one it names itself, else the case's."""
    def line(step: dict, case_device: int) -> str:
        return fam.bench._label_step("sdo_read", step, {}, {"node": case_device})

    step = {"index": "0x2060", "sub": 0}
    assert line(step, BORG).endswith("(eObjIdx_LampControl)")
    assert line(step, ACME).endswith("(eObjIdx_Lamp)")
    assert line(step | {"node": BORG}, ACME).endswith("(eObjIdx_LampControl)"), "its own node wins"
    assert line(step | {"node": ACME}, BORG).endswith("(eObjIdx_Lamp)"), "its own node wins"


@pytest.mark.parametrize("node", ["R5", "$node"], ids=["a register nobody filled", "$node, no case"])
def test_a_step_line_is_written_before_the_steps_node_can_be_known(fam, node):
    """The line comes before the run, which is where a bad node is reported.
    It only wants to know whose names to use, and takes the case's device or,
    without one, the selected."""
    bench = fam.bench
    step = {"index": "0x2060", "sub": 0, "node": node}
    _pick(bench, BORG)
    assert bench._label_step("sdo_read", step).endswith("(eObjIdx_LampControl)")
    assert bench._label_step("sdo_read", step, {}, {"node": ACME}).endswith("(eObjIdx_Lamp)")


def test_fields_are_the_owners_own(fam):
    bench = fam.bench
    assert bench._fields_for("0x2060:00", "acme") == [Field("eLamp", mask=0x0F)]
    assert bench._fields_for("0x2060:00", "borg") == [Field("eLamp", mask=0xFF)]
    assert bench._fields_for("0x9999:00", "acme") == [], "an address nobody described"


def test_units_are_the_owners_own(fam):
    bench = fam.bench
    assert bench._unit_for("0x2061:00", "acme") == Quantity("lx", 0.1)
    assert bench._unit_for("0x2061:00", "borg") == Quantity("mA", 0.01)
    assert bench._unit_for("0x9999:00", "acme") is None, "an address nobody described"


def test_what_only_one_family_describes_is_left_out_of_the_others_devices(fam):
    """The same address regularly means something else on another family, so
    what one family says about an address it alone describes is not offered
    on the other's devices — and a device nobody owns has it, as before."""
    bench = fam.bench
    assert bench._fields_for("0x2064:00", "acme") == [Field("eLamp", mask=0x0F)]
    assert bench._fields_for("0x2064:00", "borg") == []
    assert bench._fields_for("0x2064:00", "") == [Field("eLamp", mask=0x0F)]
    assert bench._unit_for("0x2065:00", "borg") == Quantity("mm", 0.5)
    assert bench._unit_for("0x2065:00", "acme") is None
    assert bench._unit_for("0x2065:00", "") == Quantity("mm", 0.5)


def test_a_panel_enum_is_the_field_the_devices_family_declared(fam):
    bench = fam.bench
    assert bench._panel_enum("0x2060:00", "", "acme") == Field("eLamp", mask=0x0F)
    assert bench._panel_enum("0x2060:00", "", "borg") == Field("eLamp", mask=0xFF)
    assert bench._panel_enum("0x2060:00", "", "") == Field("eLamp", mask=0xFF), "the later plugin's"
    # an address only acme described is no enum on a borg
    assert bench._panel_enum("0x2064:00", "", "acme") == Field("eLamp", mask=0x0F)
    assert bench._panel_enum("0x2064:00", "", "borg") is None


@pytest.mark.parametrize("owner", ["acme", "borg", ""], ids=["acme", "borg", "nobody"])
def test_a_plugin_that_seeds_no_eds_speaks_for_every_device(fam, owner):
    bench = fam.bench
    assert bench._unit_for("0x2062:00", owner) == Quantity("rpm", 10)
    assert bench._fields_for("0x2063:00", owner) == [Field("eLamp", mask=0x0F)]
    assert bench._label("0x2090", "00", "", owner) == "eMiscName"
    assert bench._emcy_text(0x6001, owner) == "Misc watchdog"
    assert bench._mec_text(0x0123, owner) == "Misc fault"


def test_a_device_nobody_owns_gets_every_plugins_answer_as_before(fam):
    """Later plugin over earlier for fields, units and EMCY texts; for a name,
    the first plugin that has one."""
    bench = fam.bench
    assert bench._fields_for("0x2060:00", "") == [Field("eLamp", mask=0xFF)] \
        == bench._object_fields["0x2060:00"]
    assert bench._unit_for("0x2061:00", "") == Quantity("mA", 0.01) \
        == bench._object_units["0x2061:00"]
    assert bench._emcy_text(0x5001, "") == "Borg motor stalled" == bench._emcy_codes[0x5001]
    assert bench._label("0x2060", "00", "", "") == "eObjIdx_Lamp"
    assert bench._mec_text(0x0065, "") == "Acme sensor fault"
    # what only one of the families says is there as well
    assert bench._emcy_text(0x5002, "") == "Acme spare lamp used"
    assert bench._emcy_text(0x5003, "") == "Borg spare motor used"
    assert bench._mec_text(0x0066, "") == "Acme supply low"


def test_an_emcy_code_reads_as_the_owners_text(fam):
    assert fam.bench._emcy_text(0x5001, "acme") == "Acme lamp burned out"
    assert fam.bench._emcy_text(0x5001, "borg") == "Borg motor stalled"


def test_an_emcy_code_only_the_other_family_names_reads_as_the_standard_says(fam):
    bench = fam.bench
    assert bench._emcy_text(0x5003, "acme") == "Device hardware", "borg's 0x5003"
    assert bench._emcy_text(0x5002, "borg") == "Device hardware", "acme's 0x5002"
    assert bench._emcy_text(0x1000, "acme") == "Generic error", "CiA 301 is under both"


def test_a_manufacturer_code_is_named_by_the_owner(fam):
    bench = fam.bench
    assert bench._mec_text(0x0065, "acme") == "Acme sensor fault"
    assert bench._mec_text(0x0065, "borg") == "Borg sensor fault"
    assert bench._mec_text(0x0066, "acme") == "Acme supply low"
    assert bench._mec_text(0x0066, "borg") == "", "a code only acme names says nothing on a borg"


# -- the trace ---------------------------------------------------------------

def test_an_sdo_row_carries_the_name_the_devices_family_gives_the_object(fam):
    """Same frame, same address, three nodes: what the row says is whose
    device it came from."""
    seen = {}
    for node in (ACME, BORG, PLAIN):
        row = _frame(f"0x{0x580 + node:03X}", "4F 60 20 00 01 00 00 00")
        fam.bench._annotate_sdo(row, live=False)
        seen[node] = row["obj"]
    assert seen == {ACME: "0x2060:00 eObjIdx_Lamp", BORG: "0x2060:00 eObjIdx_LampControl",
                    PLAIN: "0x2060:00 eObjIdx_Lamp"}


def test_a_pdo_row_carries_the_name_the_devices_family_gives_the_object(fam):
    seen = {}
    for node in (ACME, BORG, PLAIN):
        row = _frame(f"0x{0x180 + node:03X}", "01")
        fam.bench._annotate_pdo(row, live=False)
        seen[node] = row["obj"]
    assert seen == {ACME: "0x2060:00 eObjIdx_Lamp", BORG: "0x2060:00 eObjIdx_LampControl",
                    PLAIN: "0x2060:00 eObjIdx_Lamp"}


def test_an_emcy_row_carries_the_texts_of_the_senders_family(fam):
    """The same code and the same manufacturer bytes, from three nodes. The
    device nobody owns is the all-plugins merge, which is why its row is half
    one family's and half the other's — exactly as before there were owners."""
    seen = {}
    for node in (ACME, BORG, PLAIN, 99):
        row = _frame(f"0x{0x080 + node:03X}", "01 50 01 65 00 00 00 00")
        fam.bench._annotate_emcy(row, live=False)
        seen[node] = row["obj"]
    assert seen == {
        ACME: "0x5001 Acme lamp burned out · MEC 0x0065 Acme sensor fault",
        BORG: "0x5001 Borg motor stalled · MEC 0x0065 Borg sensor fault",
        PLAIN: "0x5001 Borg motor stalled · MEC 0x0065 Acme sensor fault",
        99: "0x5001 Borg motor stalled · MEC 0x0065 Acme sensor fault",
    }


# -- the object page ---------------------------------------------------------

def test_the_object_table_names_its_rows_the_way_the_selected_devices_family_does(fam):
    bench = fam.bench
    seen = {}
    for node in (ACME, BORG, PLAIN):
        _pick(bench, node)
        rows = [row for group in bench.snapshot()["objects"]["catalog"].values() for row in group]
        seen[node] = {row[0]: row[2] for row in rows if row[0] in ("0x2060", "0x2061")}
    assert seen == {ACME: {"0x2060": "eObjIdx_Lamp", "0x2061": "eObjIdx_LampControl"},
                    BORG: {"0x2060": "eObjIdx_LampControl", "0x2061": "eObjIdx_Lamp"},
                    PLAIN: {"0x2060": "eObjIdx_Lamp", "0x2061": "eObjIdx_LampControl"}}


def _shown(bench: Bench, node: int, key: str, raw: str) -> dict:
    """What the object table shows for one value on one device."""
    _pick(bench, node)
    bench.obj_vals[key] = raw
    return bench.snapshot()["objects"]["fmt"][key]


def test_the_object_table_reads_a_value_in_the_words_of_the_selected_devices_family(fam):
    acme = _shown(fam.bench, ACME, "0x2060:00", "0x01")
    assert acme["sym"] == "eLamp_On" and acme["alt"] == "0x01 · 1 · eLamp_On"
    assert _shown(fam.bench, BORG, "0x2060:00", "0x05")["sym"] == "eLamp_On"
    # the other's number is nobody's name here
    assert _shown(fam.bench, ACME, "0x2060:00", "0x05")["sym"] == "?0x5"
    assert _shown(fam.bench, BORG, "0x2060:00", "0x01")["sym"] == "?0x1"


def test_the_object_table_leaves_an_address_only_the_other_family_described_alone(fam):
    assert _shown(fam.bench, ACME, "0x2064:00", "0x01")["sym"] == "eLamp_On"
    assert _shown(fam.bench, BORG, "0x2064:00", "0x01")["sym"] == ""
    assert _shown(fam.bench, BORG, "0x2065:00", "0x0A")["sym"] == "5.0 mm"
    assert _shown(fam.bench, ACME, "0x2065:00", "0x0A")["sym"] == ""


def test_the_object_table_gives_a_value_the_unit_of_the_selected_devices_family(fam):
    assert _shown(fam.bench, ACME, "0x2061:00", "0x00A0")["sym"] == "16.0 lx"
    assert _shown(fam.bench, BORG, "0x2061:00", "0x00A0")["sym"] == "1.60 mA"


def test_a_panel_enum_offers_the_members_of_the_selected_devices_own_table(fam):
    bench = fam.bench
    seen = {}
    for node, raw in ((ACME, "0x01"), (BORG, "0x05")):
        _pick(bench, node)
        bench.obj_vals["0x2060:00"] = raw
        mode = _panel_fields(bench)["Mode"]
        seen[node] = (mode["options"], mode["val"])
    assert seen == {ACME: ([["0", "Off"], ["1", "On"]], "1"),
                    BORG: ([["0", "Off"], ["5", "On"], ["6", "Blinking"]], "5")}


def test_a_panel_enum_of_a_device_nobody_owns_offers_the_bench_wide_table(fam):
    """Whatever the merge of the two families made of it — not either
    family's own."""
    bench = fam.bench
    _pick(bench, PLAIN)
    bench.obj_vals["0x2060:00"] = "0x05"
    mode = _panel_fields(bench)["Mode"]
    merged = [[str(sym.value), name.removeprefix("eLamp_")]
              for name, sym in bench.symbols.tables["eLamp"].items()]
    assert mode["options"] == merged
    assert merged != [["0", "Off"], ["1", "On"]], "not acme's"


def test_a_panel_number_takes_the_unit_of_the_selected_devices_family(fam):
    bench = fam.bench
    seen = {}
    for node in (ACME, BORG, PLAIN):
        _pick(bench, node)
        bench.obj_vals["0x2061:00"] = "0x00A0"
        level = _panel_fields(bench)["Level"]
        seen[node] = (level["val"], level["unit"])
    assert seen == {ACME: ("16.0", "lx"), BORG: ("1.60", "mA"), PLAIN: ("1.60", "mA")}


def test_a_number_typed_into_a_panel_is_read_in_the_scale_of_the_devices_family(fam):
    """The box says 16.0 and the device stores what its own family's scale
    makes of it: a tenth for one, a hundredth for the other."""
    bench = fam.bench
    for node, stored in ((ACME, "0x00A0"), (BORG, "0x0640")):
        _pick(bench, node)
        bench.dispatch("panel_set", {"idx": "0x2061", "sub": "00", "val": "16.0"})
        assert bench.obj_vals["0x2061:00"] == stored, node


@pytest.mark.parametrize("node, staged", [(ACME, "0x31"), (BORG, "0x01")])
def test_a_choice_in_a_panel_goes_into_the_lane_the_devices_family_gave_it(fam, node, staged):
    """acme's state is the low nibble of its byte and the nibble above it is
    somebody else's, which stays; borg's state is the whole byte."""
    bench = fam.bench
    _pick(bench, node)
    bench.obj_vals["0x2060:00"] = "0x30"
    bench.dispatch("panel_set", {"idx": "0x2060", "sub": "00", "val": "1"})
    assert bench.obj_vals["0x2060:00"] == staged


def test_a_lane_offers_the_choices_where_the_devices_family_keeps_them(fam):
    """The same register and the same two names, and a table that says which
    way it is written: acme's values are plain numbers the lane moves up, borg's
    are already where they belong. Read the one by the other's rule and the
    lane has nothing it can name."""
    bench = fam.bench
    seen = {}
    for node in (ACME, BORG):
        _pick(bench, node)
        bench.obj_vals["0x2066:00"] = "0x0200"
        speed = _panel_fields(bench)["Speed"]
        seen[node] = (speed["options"], speed["val"])
    assert seen == {ACME: ([["1", "Slow"], ["2", "Fast"]], "2"),
                    BORG: ([["256", "Slow"], ["512", "Fast"]], "512")}


@pytest.mark.parametrize("node, picked", [(ACME, "2"), (BORG, "512")])
def test_a_choice_in_a_lane_is_moved_to_where_the_devices_family_keeps_it(fam, node, picked):
    bench = fam.bench
    _pick(bench, node)
    bench.obj_vals["0x2066:00"] = "0x0000"
    bench.dispatch("panel_set", {"idx": "0x2066", "sub": "00", "val": picked})
    assert bench.obj_vals["0x2066:00"] == "0x0200"


@pytest.mark.parametrize("node, typed, staged", [
    (ACME, "eLamp_On", "0x01"),
    (BORG, "eLamp_On", "0x05"),
    (BORG, "eLamp_Blinking", "0x06"),
])
def test_a_name_typed_into_the_table_means_the_number_in_the_selected_devices_table(
        fam, node, typed, staged):
    bench = fam.bench
    _pick(bench, node)
    bench.dispatch("obj_set", {"idx": "0x2060", "sub": "00", "val": typed})
    assert bench.obj_vals["0x2060:00"] == staged


def test_a_name_only_the_other_familys_table_has_is_refused(fam):
    """Staged on an acme device it would write borg's 6 — a number the lamp
    never had a name for."""
    bench = fam.bench
    _pick(bench, ACME)
    bench.obj_vals["0x2060:00"] = "0x01"
    bench.dispatch("obj_set", {"idx": "0x2060", "sub": "00", "val": "eLamp_Blinking"})
    assert bench.obj_vals["0x2060:00"] == "0x01"
    assert "rejected" in bench.logs[-1]["msg"] and "eLamp_Blinking" in bench.logs[-1]["msg"]


def test_a_step_that_reads_another_device_is_worded_by_that_devices_family(fam):
    """A case selected on an acme device that reads a borg: its report line
    says what the borg's 5 is, not what the acme would make of it."""
    bench = fam.bench
    (Path(bench.paths["tc"]) / "TC9005_second_device.yaml").write_text(
        'id: "9005"\nname: reads two\nsteps:\n'
        '  - sdo_read: {index: "0x2060", sub: "00"}\n'
        f'  - sdo_read: {{index: "0x2060", sub: "00", node: {BORG}}}\n', encoding="utf-8")
    _pick(bench, ACME)
    run_selected(bench, {"9005"})
    assert bench.results == {"9005": "PASS"}
    own, other = bench._run_cases[0].steps[:2]
    assert own.detail == "Response: 0x01 — eLamp_On"
    assert other.detail == "Response: 0x05 — eLamp_On"


# -- the MCP endpoint --------------------------------------------------------

def test_mcp_reads_a_symbol_name_as_the_family_of_the_device_does(fam):
    bench = fam.bench
    assert mcp._num(bench, "eObjIdx_Lamp", "index", ACME) == 0x2060
    assert mcp._num(bench, "$eObjIdx_Lamp", "index", BORG) == 0x2061
    assert mcp._num(bench, "eLamp_On", "value", ACME) == 1
    assert mcp._num(bench, "eLamp_On", "value", BORG) == 5
    # the two names at each other's addresses
    assert mcp._num(bench, "eObjIdx_LampControl", "index", ACME) == 0x2061
    assert mcp._num(bench, "eObjIdx_LampControl", "index", BORG) == 0x2060


def test_mcp_needs_the_qualified_name_where_there_is_no_family_to_say_which(fam):
    """No node, or a device nobody owns, is the bench-wide tables — in which
    the name is ambiguous. The way through is the qualified one."""
    bench = fam.bench
    for node in (None, PLAIN, 99):
        with pytest.raises(ValueError, match="eObjIdx_Lamp"):
            mcp._num(bench, "eObjIdx_Lamp", "index", node)
        assert mcp._num(bench, "acme:eObjIdx_Lamp", "index", node) == 0x2060
        assert mcp._num(bench, "$borg:eObjIdx_Lamp", "index", node) == 0x2061


def test_mcp_numbers_stay_numbers(fam):
    assert mcp._num(fam.bench, "0x2060", "index", ACME) == 0x2060
    assert mcp._num(fam.bench, 8288, "index", BORG) == 0x2060


def test_mcp_says_what_a_value_means_in_the_words_of_the_device_it_came_from(fam):
    bench = fam.bench
    assert mcp._reading(bench, ACME, "0x2060", "00", "0x01") == {"decimal": 1, "meaning": "eLamp_On"}
    assert mcp._reading(bench, BORG, "0x2060", "00", "0x05") == {"decimal": 5, "meaning": "eLamp_On"}
    assert mcp._reading(bench, ACME, "0x2060", "00", "0x05") == {"decimal": 5, "meaning": "?0x5"}
    assert mcp._reading(bench, ACME, "0x2061", "00", "0x00A0") == {"decimal": 160, "reading": "16.0 lx"}
    assert mcp._reading(bench, BORG, "0x2061", "00", "0x00A0") == {"decimal": 160, "reading": "1.60 mA"}


def test_mcp_leaves_an_address_only_the_other_family_described_alone(fam):
    bench = fam.bench
    assert mcp._reading(bench, ACME, "0x2064", "00", "0x01") == {"decimal": 1, "meaning": "eLamp_On"}
    assert mcp._reading(bench, BORG, "0x2064", "00", "0x01") == {"decimal": 1}
    assert mcp._reading(bench, BORG, "0x2065", "00", "0x0A") == {"decimal": 10, "reading": "5.0 mm"}
    assert mcp._reading(bench, ACME, "0x2065", "00", "0x0A") == {"decimal": 10}


def test_an_mcp_read_by_name_goes_to_the_address_the_devices_family_gives_it(fam):
    """The same name on two nodes is two different objects: acme's lamp is
    its mode, borg's is its level."""
    acme, failed = _tool(fam.bench, "sdo_read", node=ACME, index="eObjIdx_Lamp")
    assert not failed and acme == {"ok": True, "value": "0x01", "decimal": 1, "meaning": "eLamp_On"}
    borg, failed = _tool(fam.bench, "sdo_read", node=BORG, index="eObjIdx_Lamp")
    assert not failed and borg == {"ok": True, "value": "0x00A0", "decimal": 160,
                                   "reading": "1.60 mA"}


def test_an_mcp_write_by_name_sends_the_number_the_devices_family_gives_it(fam):
    bench = fam.bench
    wrote, failed = _tool(bench, "sdo_write", node=ACME, index="eObjIdx_Lamp", value="eLamp_On")
    assert not failed and wrote == {"ok": True, "written": "0x01"}
    wrote, failed = _tool(bench, "sdo_write", node=BORG, index="eObjIdx_LampControl",
                          value="eLamp_Blinking")
    assert not failed and wrote == {"ok": True, "written": "0x06"}
    # what the device now holds reads back in its own words
    assert _tool(bench, "sdo_read", node=ACME, index="eObjIdx_Lamp")[0]["meaning"] == "eLamp_On"
    assert _tool(bench, "sdo_read", node=BORG, index="eObjIdx_LampControl")[0]["meaning"] \
        == "eLamp_Blinking"


def test_testcase_check_reads_a_shipped_file_against_its_plugins_headers(fam):
    """A file name that is a plugin's says whose case it is; a draft has no
    name, and is read like anything else nobody ships."""
    bench = fam.bench
    text = (fam.acme.dir / "cases" / "TC9001_acme_lamp.yaml").read_text(encoding="utf-8")
    shipped = mcp.testcase_check(bench, {"text": text, "filename": "TC9001_acme_lamp.yaml"})
    assert shipped == {"ok": True, "id": "9001", "name": "acme lamp answers", "steps": 1}

    draft = mcp.testcase_check(bench, {"text": text})
    assert draft["ok"] is False and "qualify" in draft["error"]
    # a name that is nobody's is a draft in every way that matters
    other = mcp.testcase_check(bench, {"text": text, "filename": "TC9001_other.yaml"})
    assert other["ok"] is False and "qualify" in other["error"]


# -- a reload ----------------------------------------------------------------

def test_a_reload_leaves_no_stale_name_behind(fam):
    """The names are remembered per address, since the trace asks per frame —
    and a header the operator dropped in has to reach them."""
    bench = fam.bench
    assert bench._label("0x2075", "00", "", "acme") == "", "remembered as nothing"
    (bench.symbols_dir / "acme" / "under_test.h").write_text(
        "typedef enum eObjIdx { eObjIdx_Foo = 0x2075 } eObjIdx;\n", encoding="utf-8")
    bench.dispatch("symbols_reload", {})
    assert bench._label("0x2075", "00", "", "acme") == "eObjIdx_Foo"
    assert bench._label("0x2075", "00", "", "borg") == "", "and it is acme's, not borg's"
    assert bench._label("0x2075", "00", "", "") == "eObjIdx_Foo"


def test_a_reload_leaves_no_stale_manufacturer_code_behind(fam):
    """Remembered per owner and code, like the names beside it — and, like
    them, not to outlive the reload."""
    bench = fam.bench
    assert bench._mec_text(0x0065, "acme") == "Acme sensor fault"
    fam.acme.mec = {0x0065: "Acme sensor fault, revised"}
    bench.dispatch("symbols_reload", {})
    assert bench._mec_text(0x0065, "acme") == "Acme sensor fault, revised"
    assert bench._mec_text(0x0065, "borg") == "Borg sensor fault"


def test_a_reload_complains_about_the_clashes_of_installed_families_as_plain_ones(fam):
    """Two installed plugins disagreeing is the normal state of a bench with
    two families, not a directory that is somebody's leftover."""
    bench = fam.bench
    bench.logs.clear()
    bench.dispatch("symbols_reload", {})
    said = [row["msg"] for row in bench.logs]
    assert any(msg.startswith("SYM") and "eObjIdx_Lamp" in msg for msg in said)
    assert not any("belongs to no installed plugin" in msg for msg in said)


# -- a plugin that names its rows badly ---------------------------------------

class _BrokenRows(BenchPlugin):
    name = "broken"

    def seed_eds(self):
        raise RuntimeError("no rows today")


def test_a_plugin_whose_rows_cannot_be_read_owns_no_device_and_stops_no_other(tmp_path):
    """The rows are asked for once to see who owns what and once again to
    register them; the second is where a failure is said, and the bench goes
    on either way."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bench = Bench(Db(workspace / "bench.db"), plugins=[_BrokenRows(), _Acme(tmp_path / "packages")])
    connect_and_scan(bench)
    assert [bench._owner(node) for node in (1, 2)] == ["acme", "acme"]
    assert sum("no rows today" in row["msg"] for row in bench.logs) == 1


@pytest.mark.xfail(strict=True, reason="Bench.__init__ builds _family_plugins from the owners of "
                   "the EDS files, so a plugin whose every row another plugin already owns is not "
                   "a family at all — it speaks for every device, the other family's included")
def test_a_plugin_naming_a_file_another_already_owns_is_still_a_family(tmp_path):
    """The first plugin to name a file owns it. A second one names rows too,
    which is what makes a plugin a family — one with no device of its own
    here, and so one the other family's devices hear nothing from."""
    root = tmp_path / "packages"
    acme, borg = _Acme(root), _Borg(root)

    class Twin(BenchPlugin):
        name = "twin"

        def seed_eds(self):
            return acme.seed_eds()

        def emcy_codes(self):
            return {0x5001: "Twin text"}

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bench = Bench(Db(workspace / "bench.db"), plugins=[acme, borg, Twin()])
    assert bench._emcy_text(0x5001, "borg") == "Borg motor stalled"


# -- one plugin alone --------------------------------------------------------

@pytest.fixture()
def alone(tmp_path) -> World:
    """acme and nothing else: the bench before there were families."""
    root = tmp_path / "packages"
    acme = _Acme(root)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bench = Bench(Db(workspace / "bench.db"), plugins=[acme])
    connect_and_scan(bench)
    return World(bench, acme)


def test_a_plugin_alone_reads_the_bench_wide_tables_themselves(alone):
    bench = alone.bench
    assert bench.symbols_for("acme") is bench.symbols
    bench.dispatch("symbols_reload", {})
    assert bench.symbols_for("acme") is bench.symbols


def test_a_device_of_a_plugin_alone_reads_what_the_merged_answers_say(alone):
    bench = alone.bench
    owner = bench._owner(ACME)
    assert owner == "acme"
    assert bench._fields_for("0x2060:00", owner) == bench._object_fields["0x2060:00"]
    assert bench._unit_for("0x2061:00", owner) == bench._object_units["0x2061:00"]
    assert bench._emcy_text(0x5001, owner) == bench._emcy_text(0x5001, "") == "Acme lamp burned out"
    assert bench._label("0x2060", "00", "Device mode", owner) \
        == bench._label("0x2060", "00", "Device mode", "") == "eObjIdx_Lamp"
    assert bench._value_note("0x2060", "00", "0x01", node=ACME) == "0x01 — eLamp_On"


def test_a_plugin_alone_loads_its_case_and_flow_without_a_scope(alone):
    bench = alone.bench
    assert bench.testcases["9001"].error is None
    assert bench._load_flow("acme_lamp.yaml") is not None
    # the bare name is not ambiguous, so a case of one's own needs no prefix either
    (Path(bench.paths["tc"]) / "TC9003_mine.yaml").write_text(
        CASE.format(ident="9003", name="my"), encoding="utf-8")
    bench.dispatch("tc_rescan", {})
    assert bench.testcases["9003"].error is None


# -- a directory nobody owns any more ----------------------------------------

#: what an uninstalled plugin left in the workspace: the shared names, defined
#: another way, and a table of its own
LEGACY_H = """\
typedef enum eObjIdx { eObjIdx_Lamp = 0x3000 } eObjIdx;
typedef enum eLamp { eLamp_On = 7 } eLamp;
typedef enum eKey { eKey_Up = 1 } eKey;
"""

#: the same directory with nothing in it that contradicts acme
LEGACY_QUIET_H = "typedef enum eKey { eKey_Up = 1 } eKey;\n"
LEGACY_ALIKE_H = "typedef enum eObjIdx { eObjIdx_Lamp = 0x2060 } eObjIdx;\n"

ORPHAN = "belongs to no installed plugin"


def _with_leftover(tmp_path, header: str, scan: bool = True) -> Bench:
    """acme alone, in a workspace that already has symbols/legacy/ in it."""
    root = tmp_path / "packages"
    acme = _Acme(root)
    workspace = tmp_path / "workspace"
    (workspace / "symbols" / "legacy").mkdir(parents=True)
    (workspace / "symbols" / "legacy" / "old.h").write_text(header, encoding="utf-8")
    bench = Bench(Db(workspace / "bench.db"), plugins=[acme])
    if scan:
        connect_and_scan(bench)
    return bench


def _orphan_lines(bench: Bench) -> list[str]:
    return [row["msg"] for row in bench.logs if ORPHAN in row["msg"]]


def test_a_directory_no_plugin_owns_which_contradicts_another_is_said_once_per_load(tmp_path):
    bench = _with_leftover(tmp_path, LEGACY_H, scan=False)
    bench.logs.clear()
    bench.dispatch("symbols_reload", {})
    (line,) = _orphan_lines(bench)
    assert line.startswith("SYM") and "symbols/legacy" in line
    assert "2 name(s)" in line, "eObjIdx_Lamp and eLamp_On"
    assert "legacy:NAME" in line, "and the way through"


@pytest.mark.xfail(strict=True, reason="Bench.__init__ loads the symbol tables twice (core.py: "
                   "`self.symbols = self._load_symbols()` at the top and again before the test "
                   "cases), so every SYM line — this one too — is in the log twice after a start")
def test_a_directory_no_plugin_owns_which_contradicts_another_is_said_once_at_the_start(tmp_path):
    assert len(_orphan_lines(_with_leftover(tmp_path, LEGACY_H, scan=False))) == 1


@pytest.mark.parametrize("header", [LEGACY_QUIET_H, LEGACY_ALIKE_H],
                         ids=["other names only", "same values"])
def test_a_directory_no_plugin_owns_which_contradicts_nothing_is_not_mentioned(tmp_path, header):
    bench = _with_leftover(tmp_path, header, scan=False)
    bench.logs.clear()
    bench.dispatch("symbols_reload", {})
    assert [row for row in bench.logs if row["msg"].startswith("SYM")], "the reload did read it"
    assert _orphan_lines(bench) == []


def test_an_installed_plugin_keeps_its_case_and_its_devices_names_beside_a_leftover(tmp_path):
    bench = _with_leftover(tmp_path, LEGACY_H)
    with pytest.raises(SymbolError, match="qualify"):
        bench.symbols.value("eObjIdx_Lamp")       # the premise: the bare name is ambiguous
    case = bench.testcases["9001"]
    assert case.error is None, case.error
    assert case.steps[0]["sdo_read"]["index"] == "0x2060"
    owner = bench._owner(ACME)
    assert owner == "acme"
    assert bench._label("0x2060", "00", "", owner) == "eObjIdx_Lamp"
    assert bench._value_note("0x2060", "00", "0x01", node=ACME) == "0x01 — eLamp_On"
    assert bench._load_flow("acme_lamp.yaml") is not None


def test_any_other_file_needs_the_leftover_directorys_name(tmp_path):
    bench = _with_leftover(tmp_path, LEGACY_H)
    folder = Path(bench.paths["tc"])
    for ident, index in (("9003", "$eObjIdx_Lamp"), ("9004", "$legacy:eObjIdx_Lamp")):
        (folder / f"TC{ident}_mine.yaml").write_text(
            f'id: "{ident}"\nname: my own\nsteps:\n  - sdo_read: {{index: {index}, sub: "00"}}\n',
            encoding="utf-8")
    bench.dispatch("tc_rescan", {})
    mine = {tc.file: tc for tc in bench.testcases.values()}
    assert "qualify" in mine["TC9003_mine.yaml"].error
    assert mine["TC9004_mine.yaml"].steps[0]["sdo_read"]["index"] == "0x3000"
