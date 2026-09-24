"""The plugin seam (canopen_bench/plugin.py) itself: an inline FakePlugin
exercises every hook Bench consumes, independent of any real extension
package. Entry-point discovery (load_plugins()) is covered by each real
plugin package's own tests.
"""
from __future__ import annotations

import asyncio
import base64
import io
import sys
import uuid
import zipfile
from pathlib import Path

import pytest
from conftest import connect_and_scan, write_seed_eds_files

import canopen_bench.testcases as tclib
from canopen_bench.bus.interface import SdoResult
from canopen_bench.core import Bench, _resolve
from canopen_bench.db import Db
from canopen_bench.plugin import (
    AddressingProvider,
    BenchPlugin,
    DemoHook,
    DevicePanel,
    StatsProvider,
    StepType,
    SwdlStrategy,
    TraceDecoder,
    load_plugins,
)
from canopen_bench.testcases import parse_testcase


class FakePlugin(BenchPlugin):
    name = "fake"

    def __init__(self, flow_dir: Path | None = None, tc_dir: Path | None = None):
        self._flow_dir = flow_dir
        self._tc_dir = tc_dir

    def adapters(self) -> list[dict]:
        return [{"key": "fake", "label": "Fake adapter", "sub": "test double",
                 "conn": "Fake connected", "foot": "Fake", "iface": "FAKE",
                 "driver": "driver: none", "full": "Fake adapter"}]

    def adapter_backends(self) -> dict[str, tuple]:
        return {"fake": ("virtual", None)}

    def seed_eds(self) -> list[dict]:
        return [{"file": "fake_dev.eds", "dev": "FAKE_DEV", "ident": "0x1·0x2",
                 "code": "FAK", "enabled": True}]

    def firmware(self) -> list[dict]:
        return [{"ver": "9.9.9", "tag": "latest", "meta": "1 KB"}]

    def flow_dirs(self) -> list[Path]:
        return [self._flow_dir] if self._flow_dir else []

    def testcase_dirs(self) -> list[Path]:
        return [self._tc_dir] if self._tc_dir else []

    def emcy_mec_text(self, mec: int) -> str:
        return "Yarn breakage" if mec == 0x0065 else ""


def test_plugin_adapter_card_listed_first(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[FakePlugin()])
    assert bench.adapter_cards[0]["key"] == "fake"
    keys = [a["key"] for a in bench.adapter_cards]
    assert {"ixxat", "pcan", "demo"} <= set(keys)
    snap_keys = [a["key"] for a in bench.snapshot()["adapters"]]
    assert snap_keys == keys


def test_plugin_adapter_backend_merged_into_bus(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[FakePlugin()])
    assert bench._hw_bus._backends["fake"] == ("virtual", None, {})
    assert "ixxat" in bench._hw_bus._backends
    assert "pcan" in bench._hw_bus._backends


def test_plugin_seeds_eds_once(tmp_path):
    db = Db(tmp_path / "x.db")
    bench = Bench(db, plugins=[FakePlugin()])
    # devices_only: the registry also carries the shipped CiA 301 base,
    # which describes no device and is nobody's contribution but the core's
    assert {e["file"] for e in bench.db.eds_list(devices_only=True)} == {"fake_dev.eds"}

    again = Bench(db, plugins=[FakePlugin()])
    assert {e["file"] for e in again.db.eds_list(devices_only=True)} == {"fake_dev.eds"}
    # and the core's own base EDS is seeded once, not once per start
    assert [e["file"] for e in again.db.eds_list()].count("CiA301Base.eds") == 1


# -- seeded variant detection (Bench.__init__ seed_eds loop, "variant" key) -

class _VariantSeedPlugin(BenchPlugin):
    """Seeds one EDS row carrying an optional ``variant`` key — the plugin
    already knows where its device family keeps its variant number, so the
    operator should not have to configure it by hand in the EDS panel
    afterwards."""

    def __init__(self, name: str, row: dict):
        self.name = name
        self._row = row

    def seed_eds(self) -> list[dict]:
        return [self._row]


def test_seeded_variant_populates_registry_fields(tmp_path):
    row = {"file": "variant_dev.eds", "dev": "VARIANT_DEV",
           "ident": "0x4D2·0x1150", "code": "VAR", "enabled": True,
           "variant": {"index": "0x2050", "sub": "00", "map": {"0x00": "HV"}}}
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_VariantSeedPlugin("variantfake", row)])
    entry = next(e for e in bench.db.eds_list() if e["file"] == "variant_dev.eds")
    assert (entry["variant_index"], entry["variant_sub"], entry["variant_map"]) == \
        ("0x2050", "00", {"0x00": "HV"})


def test_seeded_row_without_variant_key_leaves_variant_fields_empty(tmp_path):
    """Regression: "variant" is optional — a plugin that never mentions it
    must keep seeding exactly as before."""
    row = {"file": "plain_dev.eds", "dev": "PLAIN_DEV",
           "ident": "0x4D2·0x1151", "code": "PLN", "enabled": True}
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_VariantSeedPlugin("novariant", row)])
    entry = next(e for e in bench.db.eds_list() if e["file"] == "plain_dev.eds")
    assert (entry["variant_index"], entry["variant_sub"], entry["variant_map"]) == ("", "", {})


def test_seeded_variant_without_map_key_defaults_to_empty_map(tmp_path):
    """"map" itself is optional on the variant dict — must not crash and
    must store {} rather than None."""
    row = {"file": "nomap_dev.eds", "dev": "NOMAP_DEV",
           "ident": "0x4D2·0x1152", "code": "NOM", "enabled": True,
           "variant": {"index": "0x2050", "sub": "00"}}
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_VariantSeedPlugin("nomap", row)])
    entry = next(e for e in bench.db.eds_list() if e["file"] == "nomap_dev.eds")
    assert (entry["variant_index"], entry["variant_sub"], entry["variant_map"]) == ("0x2050", "00", {})


def test_seeded_variant_fills_device_variant_on_scan(tmp_path):
    """End to end: a plugin-seeded variant config is enough for a scan to
    fill in the device's variant column without any manual EDS-panel setup
    — 0x2050:00 is the seed EDS's "Variant id" object (see conftest.SEED_EDS)
    and reads back as "0x00", which the map here translates to a label."""
    row = {"file": "scan_variant_dev.eds", "dev": "SCAN_VARIANT_DEV",
           "ident": "0x4D2·0x1150", "code": "SVD", "enabled": True,
           "variant": {"index": "0x2050", "sub": "00", "map": {"0x00": "HV"}}}
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_VariantSeedPlugin("scanvariant", row)])
    write_seed_eds_files(bench)
    connect_and_scan(bench)
    dev = next(d for d in bench.devices if d["eds"] == "scan_variant_dev.eds")
    assert dev["variant"] == "HV"


def test_plugin_flow_seeded_and_not_overwritten(tmp_path):
    flow_src = tmp_path / "flow_src"
    flow_src.mkdir()
    (flow_src / "custom.yaml").write_text("steps: []\n", encoding="utf-8")

    db = Db(tmp_path / "x.db")
    bench = Bench(db, plugins=[FakePlugin(flow_src)])
    dst = bench.flows_dir / "custom.yaml"
    assert dst.exists()

    customized = "# customized locally\n"
    dst.write_text(customized, encoding="utf-8")
    again = Bench(db, plugins=[FakePlugin(flow_src)])
    assert (again.flows_dir / "custom.yaml").read_text(encoding="utf-8") == customized


def test_firmware_aggregation(tmp_path):
    """A plugin's own entries have no file behind them, so they answer to
    their version — one name for every row on the page, whether it came
    from the folder, from a plugin or from the demo catalog."""
    bench = Bench(Db(tmp_path / "x.db"), plugins=[FakePlugin()])
    listed = bench.snapshot()["swdl"]["fw"]
    assert listed[0] == {"ver": "9.9.9", "file": "9.9.9", "tag": "latest",
                         "meta": "1 KB", "known": True, "disk": False}
    assert bench.fw_sel == "9.9.9"
    assert [f["file"] for f in listed[1:]] == ["1.1.0", "1.0.0"]  # demo catalog

    neutral = Bench(Db(tmp_path / "y.db"), plugins=[])
    assert neutral.fw_sel == "1.1.0"


def test_unknown_persisted_adapter_falls_back_to_demo(tmp_path):
    db = Db(tmp_path / "x.db")
    db.set("adapter", "cpc")
    bench = Bench(db, plugins=[])
    assert bench.adapter == "demo"


# -- ext hooks (session identity, demo-bus protocol hooks) -------------------

class _FakeAddressingProvider(AddressingProvider):
    name = "fakeaddr"

    def new_session(self, db) -> bytes:
        return b"\x01\x02\x03\x04\x05"


class _OtherAddressingProvider(AddressingProvider):
    name = "otheraddr"

    def new_session(self, db) -> bytes:
        return b"\x00"


class _RecordingDemoHook(DemoHook):
    name = "fake-demo-hook"

    def __init__(self):
        self.pressed = False

    def press_button(self, bus) -> bool:
        self.pressed = True
        return True


class _ExtPlugin(BenchPlugin):
    """Minimal plugin exposing just the ext hooks under test — name,
    addressing provider, demo hooks — independent of FakePlugin's adapter/
    EDS/setup/firmware seeding above."""

    def __init__(self, name, provider=None, hook=None):
        self.name = name
        self._provider = provider
        self._hook = hook

    def addressing_provider(self):
        return self._provider

    def demo_hooks(self) -> list[DemoHook]:
        return [self._hook] if self._hook else []


def test_snapshot_ext_section_empty_without_plugins(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[])
    assert bench.snapshot()["ext"] == {
        "plugins": [], "addressing": None, "canInstall": False, "installed": [],
        "symbols": {"tables": 0, "symbols": 0, "errors": []}}


def test_snapshot_ext_section_lists_plugin_name_and_addressing_provider(tmp_path):
    plugin = _ExtPlugin("fake", provider=_FakeAddressingProvider())
    bench = Bench(Db(tmp_path / "x.db"), plugins=[plugin])
    ext = bench.snapshot()["ext"]
    assert ext["plugins"] == ["fake"]
    assert ext["addressing"] == "fakeaddr"


def test_demo_hook_press_button_is_reached_from_bench(tmp_path):
    hook = _RecordingDemoHook()
    plugin = _ExtPlugin("fake", hook=hook)
    bench = Bench(Db(tmp_path / "x.db"), plugins=[plugin])
    bench._demo_bus.press_button()
    assert hook.pressed is True


class _SdoDemoHook(DemoHook):
    """The shape a bootloader takes on the demo bus: one object that no
    EDS describes, answered by the hook while it is running. Everything
    else is None — not this hook's object, not this hook's business."""

    name = "fake-sdo-hook"
    OWN = "0x5F00"

    def __init__(self):
        self.read = []
        self.written = []
        self.downloaded = b""

    def on_sdo_read(self, bus, node, index, sub):
        if index != self.OWN:
            return None
        self.read.append((node, sub))
        return SdoResult(ok=True, value="0x2A")

    def on_sdo_write(self, bus, node, index, sub, value):
        if index != self.OWN:
            return None
        self.written.append((node, sub, value))
        return SdoResult(ok=True, value=value)

    def on_sdo_download(self, bus, node, index, sub, data):
        if index != self.OWN:
            return None
        self.downloaded = data
        return SdoResult(ok=True, value=str(len(data)))


def _hooked_demo_bench(tmp_path, hook):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_ExtPlugin("fake", hook=hook)])
    write_seed_eds_files(bench)
    connect_and_scan(bench)
    return bench


def test_a_demo_hook_answers_the_sdo_objects_it_owns(tmp_path):
    """An object the hook owns exists nowhere in the EDS — without the
    hook every one of these three would abort."""
    hook = _SdoDemoHook()
    bench = _hooked_demo_bench(tmp_path, hook)

    assert bench.bus.sdo_read(1, hook.OWN, "02").value == "0x2A"
    assert bench.bus.sdo_write(1, hook.OWN, "02", "0x03").ok
    res = bench.bus.sdo_download(1, hook.OWN, "02", b"firmware")

    assert hook.read == [(1, "02")]
    assert hook.written == [(1, "02", "0x03")]
    assert hook.downloaded == b"firmware"
    assert (res.ok, res.value) == (True, "8")


def test_a_demo_hook_is_asked_before_the_eds_store(tmp_path):
    """The hook wins over an object the EDS does describe: a device in its
    bootloader answers for itself, whatever the file says it is."""
    class _Overriding(_SdoDemoHook):
        OWN = "0x2000"  # the seed EDS's writable counter, default 42

    hook = _Overriding()
    bench = _hooked_demo_bench(tmp_path, hook)

    assert bench.bus.sdo_read(1, "0x2000", "00").value == "0x2A"


def test_a_hook_that_answers_none_leaves_the_eds_answering(tmp_path):
    hook = _SdoDemoHook()
    bench = _hooked_demo_bench(tmp_path, hook)

    assert bench.bus.sdo_read(1, "0x2000", "00").value == "0x0000002A"  # EDS default 42
    assert bench.bus.sdo_write(1, "0x2000", "00", "0x63").ok
    assert bench.bus.sdo_read(1, "0x2000", "00").value == "0x63"
    assert hook.read == [] and hook.written == []


def test_a_domain_download_nobody_hooks_is_taken_but_stored_nowhere(tmp_path):
    """The EDS says the object is there and writable, which is as far as
    the demo bus can honestly go — a domain has no single value to read
    back afterwards."""
    bench = _hooked_demo_bench(tmp_path, _SdoDemoHook())

    res = bench.bus.sdo_download(1, "0x2000", "00", b"\x01\x02\x03\x04\x05")

    assert (res.ok, res.value) == (True, "5")
    assert bench.bus.sdo_read(1, "0x2000", "00").value == "0x0000002A"  # untouched


def test_a_domain_download_refuses_like_a_write_does(tmp_path):
    bench = _hooked_demo_bench(tmp_path, _SdoDemoHook())

    missing = bench.bus.sdo_download(1, "0x7777", "00", b"x")
    read_only = bench.bus.sdo_download(1, "0x1000", "00", b"x")

    assert missing.abort.startswith("0x0602")
    assert read_only.abort.startswith("0x0601")


def test_a_domain_download_reaches_the_trace(tmp_path):
    """Like every other transfer on this bus: the row is what says the
    image went out, and where."""
    bench = _hooked_demo_bench(tmp_path, _SdoDemoHook())
    bench.bus.poll_frames(64)  # drop the scan's own traffic

    bench.bus.sdo_download(1, "0x2000", "00", bytes(range(16)))
    frames = bench.bus.poll_frames(64)

    request = next(f for f in frames if f.cob_id == "0x601")
    assert request.data.startswith("23 00 20 00 00 01 02 03")  # first bytes, in order


def test_first_plugins_addressing_provider_wins(tmp_path):
    first = _FakeAddressingProvider()
    second = _OtherAddressingProvider()
    p1 = _ExtPlugin("p1", provider=first)
    p2 = _ExtPlugin("p2", provider=second)
    bench = Bench(Db(tmp_path / "x.db"), plugins=[p1, p2])
    assert bench.addressing is first


# -- plugin actions (dispatch "<plugin>.<action>") ---------------------------

class _ActionsPlugin(BenchPlugin):
    name = "fake"

    def __init__(self):
        self.calls: list[dict] = []

    def actions(self, bench) -> dict:
        def ping(p: dict) -> None:
            self.calls.append(p)
        return {"ping": ping}


def test_plugin_action_dispatched_namespaced(tmp_path):
    plugin = _ActionsPlugin()
    bench = Bench(Db(tmp_path / "x.db"), plugins=[plugin])
    bench.dispatch("fake.ping", {"x": 1})
    assert plugin.calls == [{"x": 1}]


def test_plugin_action_unknown_name_raises(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_ActionsPlugin()])
    with pytest.raises(ValueError):
        bench.dispatch("fake.nope", {})


def test_dispatch_unknown_action_raises(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_ActionsPlugin()])
    with pytest.raises(ValueError):
        bench.dispatch("unknown", {})


def test_core_action_still_dispatches_alongside_plugin_actions(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_ActionsPlugin()])
    bench.dispatch("set_bitrate", {"bitrate": "250"})
    assert bench.bitrate == "250"


# -- trace decoders (Bench._annotate_plugin) ---------------------------------

def _trace_row(cob: str, data: str) -> dict:
    return {"time": "", "dir": "RX", "cob": cob, "len": "8", "data": data,
            "dec": "", "cls": "HAX", "flag": "", "obj": "", "val": ""}


class _FakeDecoder(TraceDecoder):
    name = "fake-decoder"

    def __init__(self, result=None, raises=False):
        self._result = result
        self._raises = raises

    def decode(self, cob: int, data: bytes) -> dict | None:
        if self._raises:
            raise RuntimeError("broken decoder")
        return self._result


class _DecoderPlugin(BenchPlugin):
    name = "fake"

    def __init__(self, decoders):
        self._decoders = decoders

    def trace_decoders(self) -> list:
        return self._decoders


def test_trace_decoder_merges_only_dec_obj_val(tmp_path):
    decoder = _FakeDecoder({"dec": "TEACH offer", "cls": "HAX-changed"})
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_DecoderPlugin([decoder])])
    row = _trace_row("0x780", "01")
    bench._annotate_plugin(row)
    assert row["dec"] == "TEACH offer"
    assert row["cls"] == "HAX"  # only dec/obj/val are merged


def test_trace_decoder_that_raises_is_skipped_and_later_decoder_wins(tmp_path):
    broken = _FakeDecoder(raises=True)
    good = _FakeDecoder({"dec": "TEACH offer"})
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_DecoderPlugin([broken, good])])
    row = _trace_row("0x780", "01")
    bench._annotate_plugin(row)
    assert row["dec"] == "TEACH offer"


def test_no_decoders_leaves_row_unchanged(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[])
    row = _trace_row("0x780", "01")
    before = dict(row)
    bench._annotate_plugin(row)
    assert row == before


# -- plugin step primitives (Bench._step_types, "<plugin>.<key>") -----------

class _SetRegStep(StepType):
    """Writes a resolved value into a register — exercises the extension
    seam end to end without needing bus traffic."""

    key = "setreg"

    def validate(self, val):
        if not isinstance(val, dict) or set(val) != {"to", "value"}:
            return "setreg: needs {to, value}"
        if val["to"] not in tclib.REGISTERS:
            return f"setreg: to must be a register R0-R9, got {val['to']!r}"
        return None

    def label(self, val) -> str:
        return f"setreg {val['to']}"

    async def execute(self, bench, bus, node, val, regs, builtins):
        regs[val["to"]] = _resolve(val["value"], regs, builtins)
        return "ok", ""


class _BoomStep(StepType):
    key = "boom"

    async def execute(self, bench, bus, node, val, regs, builtins):
        raise RuntimeError("kaboom")


class _StepTypesPlugin(BenchPlugin):
    name = "fake"

    def step_types(self) -> list[StepType]:
        return [_SetRegStep(), _BoomStep()]


def _run_steps(bench: Bench, steps: list) -> tuple[tuple, list]:
    """Drive bench._run_program directly with a minimal register/builtins
    set, no real bus needed for the fake steps under test. Returns
    ((status, why), [(step, text), ...]) — the labels on_step recorded."""
    bench.connected = True
    tc = tclib.TestCase(id="1", name="fake step run", steps=steps)
    regs = {f"R{i}": 0 for i in range(10)}
    builtins = {"node": 1, "expected": 0, "session": None}
    seen: list[tuple] = []

    def on_step(step, text):
        seen.append((step, text))

    result = asyncio.run(bench._run_program(tc, steps, 1, regs, builtins, 0,
                                            on_step, lambda: False))
    return result, seen, regs


def test_plugin_step_parses_with_extensions_but_not_without(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StepTypesPlugin()])
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - fake.setreg: {to: R1, value: "0x5"}\n')
    tc = parse_testcase(text, "TC1_x.yaml", extensions=bench._step_types)
    assert tc.error is None

    tc_no_ext = parse_testcase(text, "TC1_x.yaml")
    assert tc_no_ext.error == "unknown step primitive 'fake.setreg'"


def test_plugin_step_validate_error_surfaces(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StepTypesPlugin()])
    text = 'id: "1"\nname: x\nsteps:\n  - fake.setreg: {to: R1}\n'
    tc = parse_testcase(text, "TC1_x.yaml", extensions=bench._step_types)
    assert tc.error == "setreg: needs {to, value}"


def test_bare_step_key_stays_unknown_even_with_extensions(tmp_path):
    """The registry key is namespaced "<plugin>.<key>" — a bare "setreg"
    never resolves to the plugin's step, extensions or not."""
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StepTypesPlugin()])
    text = 'id: "1"\nname: x\nsteps:\n  - setreg: {to: R1, value: "0x5"}\n'
    tc = parse_testcase(text, "TC1_x.yaml", extensions=bench._step_types)
    assert tc.error == "unknown step primitive 'setreg'"


def test_plugin_step_executes_and_labels_via_extension(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StepTypesPlugin()])
    steps = [{"fake.setreg": {"to": "R2", "value": "0x7"}}]
    (status, why), seen, regs = _run_steps(bench, steps)
    assert (status, why) == ("ok", "")
    assert regs["R2"] == 7
    assert seen == [(1, "setreg R2")]  # extension label(), not the raw key


def test_plugin_step_that_raises_becomes_error_without_killing_the_loop(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StepTypesPlugin()])
    steps = [{"fake.boom": None}]
    (status, why), seen, regs = _run_steps(bench, steps)
    assert status == "error"
    assert why.startswith("fake.boom: ")
    assert "kaboom" in why
    assert seen == [(1, "boom")]  # default label (StepType.label() falls back to key)


# -- SWDL strategy seam (Bench._swdl, "<plugin>.swdl_strategy()") -----------

def test_default_swdl_strategy_is_the_core_simulation(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[])
    assert bench._swdl.name == "sim"
    assert bench.snapshot()["swdl"]["strategy"] == "sim"


class _RecordingSwdlStrategy(SwdlStrategy):
    name = "fake-swdl"

    def __init__(self):
        self.started = False
        self.step_calls = 0
        self.stopped = False

    def start(self, bench) -> None:
        self.started = True
        bench.swdl_run = True
        bench.swdl_done = False

    def step(self, bench) -> None:
        self.step_calls += 1
        bench.swdl_run = False
        bench.swdl_done = True

    def stop(self, bench) -> None:
        self.stopped = True


class _SwdlPlugin(BenchPlugin):
    def __init__(self, name, strategy):
        self.name = name
        self.strategy = strategy

    def swdl_strategy(self):
        return self.strategy


def test_plugin_swdl_strategy_selected_and_act_swdl_start_guards_still_hold(tmp_path):
    strategy = _RecordingSwdlStrategy()
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_SwdlPlugin("fake", strategy)])
    write_seed_eds_files(bench)
    assert bench._swdl is strategy
    assert bench.snapshot()["swdl"]["strategy"] == "fake-swdl"

    # guard: no device selected -> act_swdl_start is a no-op
    bench.dispatch("swdl_start", {})
    assert strategy.started is False
    assert bench.swdl_run is False

    connect_and_scan(bench)
    bench.dispatch("dev_toggle", {"node": 1})
    bench.dispatch("swdl_start", {})
    assert strategy.started is True
    assert bench.swdl_run is True

    bench._swdl.step(bench)
    assert strategy.step_calls == 1
    assert bench.swdl_done is True


def test_swdl_stop_reaches_the_strategy_only_while_it_runs(tmp_path):
    """Stop is cooperative — the bench asks the strategy and nothing else.
    A press with no download running asks nobody: there is no state on the
    bench to tidy up, and a strategy would be told to abandon a transfer
    it never started."""
    strategy = _RecordingSwdlStrategy()
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_SwdlPlugin("fake", strategy)])
    write_seed_eds_files(bench)
    connect_and_scan(bench)
    bench.dispatch("dev_toggle", {"node": 1})

    bench.dispatch("swdl_stop", {})
    assert strategy.stopped is False

    bench.dispatch("swdl_start", {})
    bench.dispatch("swdl_stop", {})
    assert strategy.stopped is True


def test_the_swdl_snapshot_carries_phase_and_failure_per_node(tmp_path):
    """Both are the strategy's to write and the page's to draw, keyed by
    node like the progress beside them."""
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_SwdlPlugin("fake", _RecordingSwdlStrategy())])
    bench.swdl_prog = {2: 40, 3: 12}
    bench.swdl_phase = {2: "flashing", 3: "erasing"}
    bench.swdl_err = {3: "no answer after the erase"}

    w = bench.snapshot()["swdl"]

    assert w["prog"] == {"2": 40, "3": 12}
    assert w["phase"] == {"2": "flashing", "3": "erasing"}
    assert w["err"] == {"3": "no answer after the erase"}


def test_first_plugins_swdl_strategy_wins(tmp_path):
    first = _RecordingSwdlStrategy()
    second = _RecordingSwdlStrategy()
    p1 = _SwdlPlugin("p1", first)
    p2 = _SwdlPlugin("p2", second)
    bench = Bench(Db(tmp_path / "x.db"), plugins=[p1, p2])
    assert bench._swdl is first


# -- the firmware folder, described by plugins (Bench._fw_files/_fw_catalog) --

class _FwPlugin(BenchPlugin):
    """An extension that knows one firmware format: a ``.fwpkg`` file
    starting with four magic bytes. Anything else in the folder belongs
    to somebody else — a note, a map file, another vendor's image — and
    is answered with None, which is what makes the file unknown rather
    than this plugin's."""

    MAGIC = b"ACME"

    def __init__(self, name: str = "acme", tag: str = "latest"):
        self.name = name
        self._tag = tag
        self.asked: list[str] = []

    def describe_firmware(self, path: Path) -> dict | None:
        self.asked.append(path.name)
        if path.suffix != ".fwpkg" or path.read_bytes()[:4] != self.MAGIC:
            return None
        return {"ver": path.stem, "tag": self._tag,
                "meta": f"{path.stat().st_size} bytes · {self.name}"}


class _AngryFwPlugin(BenchPlugin):
    """The file was not what it expected, and it says so by raising."""

    name = "angry"

    def __init__(self):
        self.asked = 0

    def describe_firmware(self, path: Path) -> dict | None:
        self.asked += 1
        raise ValueError("that is not a header")


def _fw_bench(tmp_path, plugins, files: dict[str, bytes] | None = None):
    """A bench whose firmware folder is a folder of this test's own —
    configured the way the setup page configures it."""
    bench = Bench(Db(tmp_path / "x.db"), plugins=plugins)
    folder = tmp_path / "build_output"
    folder.mkdir(exist_ok=True)
    for name, content in (files or {}).items():
        (folder / name).write_bytes(content)
    bench.dispatch("set_path", {"which": "fw", "value": str(folder)})
    return bench, folder


def _fw_row(bench, file: str) -> dict:
    return next(f for f in bench.snapshot()["swdl"]["fw"] if f["file"] == file)


def test_a_plugin_says_what_the_files_in_the_firmware_folder_are(tmp_path):
    """The core lists the folder and reads nothing: what a file is comes
    from whoever knows the format. A file nobody knows is listed too —
    and says why it cannot be used."""
    plugin = _FwPlugin()
    bench, _ = _fw_bench(tmp_path, [plugin], {
        "dut_alpha_1.4.0.fwpkg": b"ACME" + b"\x01" * 60,
        "notes.txt": b"flash this one next",
    })

    files = bench._fw_files()

    assert [f["file"] for f in files] == ["dut_alpha_1.4.0.fwpkg", "notes.txt"]
    assert files[0] == {"file": "dut_alpha_1.4.0.fwpkg", "ver": "dut_alpha_1.4.0",
                        "tag": "latest", "meta": "64 bytes · acme", "known": True,
                        "disk": True}
    assert files[1] == {"file": "notes.txt", "ver": "", "tag": "", "known": False,
                        "meta": "no installed extension knows this format", "disk": True}


def test_the_firmware_folder_is_read_again_only_when_it_changed(tmp_path):
    """A build writes into this folder while the page is open, so the
    listing has to follow it — but the snapshot asks ten times a second,
    and reading every image in a build directory that often is a cost
    nobody would find afterwards."""
    plugin = _FwPlugin()
    bench, folder = _fw_bench(tmp_path, [plugin],
                              {"dut_alpha_1.4.0.fwpkg": b"ACME" + b"\x00" * 8})

    bench.snapshot()
    bench.snapshot()
    assert plugin.asked == ["dut_alpha_1.4.0.fwpkg"], "described once, then cached"

    (folder / "dut_alpha_1.5.0.fwpkg").write_bytes(b"ACME" + b"\x00" * 16)
    assert [f["file"] for f in bench._fw_files()] == \
        ["dut_alpha_1.4.0.fwpkg", "dut_alpha_1.5.0.fwpkg"]
    assert plugin.asked == ["dut_alpha_1.4.0.fwpkg", "dut_alpha_1.5.0.fwpkg"]


def test_a_firmware_folder_that_is_not_there_is_empty_and_not_an_error(tmp_path):
    """The path was typed on another machine, or the build has not run
    yet. Neither is a reason for the page not to come up."""
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_FwPlugin()])
    bench.dispatch("set_path", {"which": "fw", "value": str(tmp_path / "nowhere")})

    assert bench._fw_files() == []
    assert bench.snapshot()["swdl"]["folder"] == str(tmp_path / "nowhere")


def test_the_firmware_listing_leaves_hidden_files_alone(tmp_path):
    """An editor's swap file and a half-finished copy are not firmware,
    and the operator never put them there."""
    bench, _ = _fw_bench(tmp_path, [_FwPlugin()], {
        ".dut_alpha_1.4.0.fwpkg.part": b"ACME" + b"\x00" * 8,
        "dut_alpha_1.4.0.fwpkg": b"ACME" + b"\x00" * 8,
    })

    assert [f["file"] for f in bench._fw_files()] == ["dut_alpha_1.4.0.fwpkg"]


def test_the_first_plugin_that_knows_a_firmware_file_describes_it(tmp_path):
    first, second = _FwPlugin("acme", tag="latest"), _FwPlugin("beta", tag="old")
    bench, _ = _fw_bench(tmp_path, [first, second],
                         {"dut_alpha_1.4.0.fwpkg": b"ACME" + b"\x00" * 8})

    row = _fw_row(bench, "dut_alpha_1.4.0.fwpkg")

    assert row["tag"] == "latest" and row["meta"].endswith("acme")
    assert second.asked == [], "the second plugin is not asked about a described file"


def test_a_plugin_that_raises_over_a_firmware_file_is_asked_no_more(tmp_path):
    """Same rule as a panel or a stats block: it is said once, and the
    page comes up. A plugin that raises on one file raises on the next
    one too, and a log filling with the same line is a log nobody reads."""
    angry, good = _AngryFwPlugin(), _FwPlugin()
    bench, _ = _fw_bench(tmp_path, [angry, good], {
        "dut_alpha_1.4.0.fwpkg": b"ACME" + b"\x00" * 8,
        "dut_alpha_1.5.0.fwpkg": b"ACME" + b"\x00" * 8,
    })

    files = bench._fw_files()

    assert [f["known"] for f in files] == [True, True], "the good plugin still answers"
    assert angry.asked == 1, "asked about the first file, then never again"
    assert sum("angry" in row["msg"] for row in bench.logs) == 1
    assert bench.logs[-1]["type"] == "emcy0"


def test_the_selection_falls_back_to_the_first_file_something_knows(tmp_path):
    """A file nobody knows is not a default anybody wants to press Start
    on, and neither is a file that has since been deleted. On a real
    adapter the folder is the whole library, so there is nothing else to
    fall back to."""
    bench, folder = _fw_bench(tmp_path, [_FwPlugin()], {
        "aaa_notes.txt": b"not firmware",
        "dut_alpha_1.4.0.fwpkg": b"ACME" + b"\x00" * 8,
    })
    bench.dispatch("set_adapter", {"adapter": "ixxat"})  # no demo catalog

    assert bench.snapshot()["swdl"]["sel"] == "dut_alpha_1.4.0.fwpkg"

    (folder / "dut_alpha_1.4.0.fwpkg").unlink()
    snap = bench.snapshot()["swdl"]
    assert snap["sel"] == "", "one unknown file is no selection at all"
    assert [f["file"] for f in snap["fw"]] == ["aaa_notes.txt"]


def test_a_firmware_file_nobody_knows_cannot_be_selected(tmp_path):
    """The row is greyed out for the same reason the action refuses it:
    what the bench would do with those bytes is nobody's to say."""
    bench, _ = _fw_bench(tmp_path, [_FwPlugin()], {
        "dut_alpha_1.4.0.fwpkg": b"ACME" + b"\x00" * 8,
        "notes.txt": b"not firmware",
    })
    bench.dispatch("swdl_fw", {"file": "dut_alpha_1.4.0.fwpkg"})

    bench.dispatch("swdl_fw", {"file": "notes.txt"})

    assert bench.fw_sel == "dut_alpha_1.4.0.fwpkg", "the selection is left alone"
    assert "no installed extension knows this format" in bench.logs[-1]["msg"]
    assert bench.logs[-1]["type"] == "emcy0"


def test_selecting_a_firmware_file_names_the_file(tmp_path):
    bench, _ = _fw_bench(tmp_path, [_FwPlugin()], {
        "dut_alpha_1.4.0.fwpkg": b"ACME" + b"\x00" * 8,
        "dut_alpha_1.5.0.fwpkg": b"ACME" + b"\x00" * 8,
    })

    bench.dispatch("swdl_fw", {"file": "dut_alpha_1.5.0.fwpkg"})
    assert bench.fw_sel == "dut_alpha_1.5.0.fwpkg"

    # the page used to send the version, and a demo entry is still one
    bench.dispatch("swdl_fw", {"ver": "1.0.0"})
    assert bench.fw_sel == "1.0.0"


def test_fw_path_is_the_file_and_nothing_for_an_entry_without_one(tmp_path):
    """A strategy reads the bytes itself, so it needs the path — and a
    demo entry has none: there is nothing behind it to open."""
    bench, folder = _fw_bench(tmp_path, [_FwPlugin()],
                              {"dut_alpha_1.4.0.fwpkg": b"ACME" + b"\x00" * 8})

    bench.dispatch("swdl_fw", {"file": "dut_alpha_1.4.0.fwpkg"})
    assert bench.fw_path() == folder / "dut_alpha_1.4.0.fwpkg"

    bench.dispatch("swdl_fw", {"file": "1.1.0"})  # the demo catalog's entry
    assert bench.fw_path() is None


def test_an_uploaded_firmware_file_lands_in_the_folder_byte_for_byte(tmp_path):
    """The bytes are the firmware. Anything that rewrites one of them —
    a text decode, a newline translation — produces a file that flashes
    and a device that does not come back."""
    bench, folder = _fw_bench(tmp_path, [_FwPlugin()])
    raw = b"ACME" + bytes(range(256))

    bench.dispatch("fw_upload", {"filename": "dut_alpha_2.0.0.fwpkg",
                                 "content": base64.b64encode(raw).decode()})

    assert (folder / "dut_alpha_2.0.0.fwpkg").read_bytes() == raw
    assert bench.fw_sel == "dut_alpha_2.0.0.fwpkg", "and is what Start would send"
    assert 'SWDL "dut_alpha_2.0.0.fwpkg" uploaded' in bench.logs[-1]["msg"]
    assert _fw_row(bench, "dut_alpha_2.0.0.fwpkg")["ver"] == "dut_alpha_2.0.0"


def test_an_upload_nobody_knows_is_kept_and_said_so(tmp_path):
    """Keeping it is the point: the operator can see the file arrived,
    and the line says why it is greyed out — which is usually a missing
    extension package rather than a bad file."""
    bench, folder = _fw_bench(tmp_path, [_FwPlugin()])

    bench.dispatch("fw_upload", {"filename": "stranger.dat",
                                 "content": base64.b64encode(b"NOTACME1").decode()})

    assert (folder / "stranger.dat").exists()
    assert bench.fw_sel != "stranger.dat"
    last = bench.logs[-1]["msg"]
    assert "uploaded" in last and "no installed extension knows this format" in last


def test_an_unreadable_upload_is_refused_rather_than_written(tmp_path):
    bench, folder = _fw_bench(tmp_path, [_FwPlugin()])

    bench.dispatch("fw_upload", {"filename": "dut_alpha_2.0.0.fwpkg", "content": "not base64!"})

    assert list(folder.iterdir()) == []
    assert "unreadable upload" in bench.logs[-1]["msg"]


def test_an_upload_keeps_only_the_name_of_what_was_dropped(tmp_path):
    """A browser sends the name the file had on the other machine, and a
    path in it would write outside the folder."""
    bench, folder = _fw_bench(tmp_path, [_FwPlugin()])

    bench.dispatch("fw_upload", {"filename": "../../dut_alpha_2.0.0.fwpkg",
                                 "content": base64.b64encode(b"ACME1234").decode()})

    assert [f.name for f in folder.iterdir()] == ["dut_alpha_2.0.0.fwpkg"]


def test_a_firmware_file_can_be_deleted_and_the_selection_moves_on(tmp_path):
    """Off the disk — the folder is the library, there is no list to
    take it out of — and the selection moves to what is left, the way
    it does when the file goes outside the tool."""
    bench, folder = _fw_bench(tmp_path, [_FwPlugin()], {
        "dut_alpha_1.4.0.fwpkg": b"ACME" + b"\x00" * 8,
        "dut_alpha_1.5.0.fwpkg": b"ACME" + b"\x00" * 8,
    })
    bench.dispatch("swdl_fw", {"file": "dut_alpha_1.5.0.fwpkg"})
    assert all(f["disk"] for f in bench.snapshot()["swdl"]["fw"]
               if f["file"].endswith(".fwpkg"))

    bench.dispatch("fw_delete", {"file": "dut_alpha_1.5.0.fwpkg"})

    assert not (folder / "dut_alpha_1.5.0.fwpkg").exists()
    assert (folder / "dut_alpha_1.4.0.fwpkg").exists(), "only the one named"
    assert bench.snapshot()["swdl"]["sel"] == "dut_alpha_1.4.0.fwpkg"
    assert 'SWDL "dut_alpha_1.5.0.fwpkg" deleted' in bench.logs[-1]["msg"]


def test_a_file_nobody_knows_can_be_deleted_too(tmp_path):
    """It cannot be selected, but it is a file in the folder — a map
    file dropped there by mistake is exactly what the ✕ is for."""
    bench, folder = _fw_bench(tmp_path, [_FwPlugin()], {"notes.txt": b"not firmware"})

    bench.dispatch("fw_delete", {"file": "notes.txt"})

    assert list(folder.iterdir()) == []


def test_only_a_file_in_the_folder_can_be_deleted(tmp_path):
    """The page sends the name it listed. A path, or the name of an
    entry with no file behind it, came from somewhere else."""
    bench, folder = _fw_bench(tmp_path, [_FwPlugin()],
                              {"dut_alpha_1.4.0.fwpkg": b"ACME" + b"\x00" * 8})
    outside = tmp_path / "keep.fwpkg"
    outside.write_bytes(b"ACME1234")

    for name in ("../keep.fwpkg", str(outside), "", ".", "1.1.0", "missing.fwpkg"):
        bench.dispatch("fw_delete", {"file": name})
        assert "not deleted" in bench.logs[-1]["msg"], name
        assert bench.logs[-1]["type"] == "emcy0"

    assert outside.exists()
    assert (folder / "dut_alpha_1.4.0.fwpkg").exists()
    assert not next(f for f in bench.snapshot()["swdl"]["fw"]
                    if f["file"] == "1.1.0")["disk"], "a demo entry has no file"


def test_the_file_a_download_is_reading_stays(tmp_path):
    """Deleting it would move the selection to another file mid-run,
    and the page would show that one as what is being flashed. Every
    other file may go; this one after the run."""
    bench, folder = _fw_bench(tmp_path, [_FwPlugin()], {
        "dut_alpha_1.4.0.fwpkg": b"ACME" + b"\x00" * 8,
        "dut_alpha_1.5.0.fwpkg": b"ACME" + b"\x00" * 8,
    })
    bench.dispatch("swdl_fw", {"file": "dut_alpha_1.5.0.fwpkg"})
    bench.swdl_run = True

    bench.dispatch("fw_delete", {"file": "dut_alpha_1.5.0.fwpkg"})
    assert (folder / "dut_alpha_1.5.0.fwpkg").exists()
    assert "being downloaded" in bench.logs[-1]["msg"]

    bench.dispatch("fw_delete", {"file": "dut_alpha_1.4.0.fwpkg"})
    assert not (folder / "dut_alpha_1.4.0.fwpkg").exists()
    assert bench.fw_sel == "dut_alpha_1.5.0.fwpkg"

    bench.swdl_run = False
    bench.dispatch("fw_delete", {"file": "dut_alpha_1.5.0.fwpkg"})
    assert not (folder / "dut_alpha_1.5.0.fwpkg").exists()


def test_moving_the_firmware_folder_moves_the_library(tmp_path):
    """The folder is the library — there is nowhere else a file could be
    kept, and pointing the bench at the next build directory is the whole
    configuration step."""
    bench, _ = _fw_bench(tmp_path, [_FwPlugin()],
                         {"dut_alpha_1.4.0.fwpkg": b"ACME" + b"\x00" * 8})
    bench.dispatch("swdl_fw", {"file": "dut_alpha_1.4.0.fwpkg"})

    other = tmp_path / "other_build"
    other.mkdir()
    (other / "dut_alpha_9.0.0.fwpkg").write_bytes(b"ACME" + b"\x00" * 8)
    bench.dispatch("set_path", {"which": "fw", "value": str(other)})

    snap = bench.snapshot()["swdl"]
    assert [f["file"] for f in snap["fw"]][0] == "dut_alpha_9.0.0.fwpkg"
    assert snap["sel"] == "dut_alpha_9.0.0.fwpkg", \
        "the file that was selected is not in this folder"
    assert snap["folder"] == str(other)
    assert Bench(Db(bench.db.path), plugins=[_FwPlugin()]).paths["fw"] == str(other)


# -- GUI plugin install (Setup > Extensions) ---------------------------------
# Bench(plugin_dir=..., _install_plugin_wheel, act_plugin_install/remove) —
# see canopen_bench/core.py "GUI plugin install" section. Never touches
# app.py's async reload wiring: that's Starlette infrastructure without an
# existing test pattern in this repo, so coverage stops at the Bench layer
# (on_plugin_reload is a plain callable seam, injected as a spy below).


def _write_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _hand_wheel_bytes(dist_name: str, version: str) -> bytes:
    """A minimal, non-importable wheel archive — enough for
    _install_plugin_wheel/manifest/zip-slip tests, not for a real load."""
    return _write_zip({
        f"{dist_name}/__init__.py": "",
        f"{dist_name}-{version}.dist-info/METADATA":
            f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: {version}\n",
        f"{dist_name}-{version}.dist-info/entry_points.txt":
            f"[canopen_bench.plugins]\n{dist_name} = {dist_name}.plugin:NotReal\n",
    })


def test_plugin_dir_set_up_and_registered_on_sys_path_with_workspaces_root(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[], workspaces_root=tmp_path)
    assert bench.plugin_dir == tmp_path / "plugins"
    assert bench.plugin_dir.is_dir()
    assert str(bench.plugin_dir) in sys.path


def test_plugin_dir_none_without_workspaces_root(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[])
    assert bench.plugin_dir is None


def test_act_plugin_install_rejects_invalid_base64(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[], workspaces_root=tmp_path)
    bench.dispatch("plugin_install", {"filename": "pkg-1.0.0-py3-none-any.whl",
                                       "content": "not base64!!!"})
    assert any("invalid file content" in log["msg"] and log["type"] == "emcy0"
               for log in bench.logs)
    ext = bench.snapshot()["ext"]
    assert ext["canInstall"] is True
    assert ext["installed"] == []


def test_install_plugin_wheel_rejects_non_whl_filename(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[], workspaces_root=tmp_path)
    ok, msg = bench._install_plugin_wheel("pkg-1.0.0.tar.gz", b"whatever")
    assert ok is False
    assert msg.startswith("not a .whl file:")


def test_install_plugin_wheel_rejects_broken_zip(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[], workspaces_root=tmp_path)
    ok, msg = bench._install_plugin_wheel("pkg-1.0.0-py3-none-any.whl", b"not a zip")
    assert (ok, msg) == (False, "not a valid zip/wheel archive")


def test_install_plugin_wheel_rejects_filename_without_dash(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[], workspaces_root=tmp_path)
    content = _write_zip({"foo.py": ""})
    ok, msg = bench._install_plugin_wheel("onlyname.whl", content)
    assert ok is False
    assert msg.startswith("not a valid wheel filename:")


def test_install_plugin_wheel_without_workspaces_root_rejected(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[])
    ok, msg = bench._install_plugin_wheel("pkg-1.0.0-py3-none-any.whl", b"anything")
    assert ok is False
    assert msg == "plugin install needs multi-workspace mode (a data root)"


def test_install_plugin_wheel_rejects_zip_slip_and_extracts_nothing(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[], workspaces_root=tmp_path)
    content = _write_zip({"../../evil.py": "print('evil')\n"})
    ok, msg = bench._install_plugin_wheel("evil-1.0.0-py3-none-any.whl", content)
    assert ok is False
    assert msg.startswith("refusing to extract unsafe path in archive:")
    assert not (tmp_path / "evil.py").exists()
    assert bench._plugin_manifest() == {}


def test_install_plugin_wheel_succeeds_and_records_manifest(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[], workspaces_root=tmp_path)
    content = _hand_wheel_bytes("pkgname", "1.0.0")
    ok, msg = bench._install_plugin_wheel("pkgname-1.0.0-py3-none-any.whl", content)
    assert (ok, msg) == (True, "pkgname-1.0.0")

    manifest = bench._plugin_manifest()
    assert manifest["pkgname"]["version"] == "1.0.0"
    assert manifest["pkgname"]["paths"] == ["pkgname", "pkgname-1.0.0.dist-info"]
    assert bench._installed_plugin_packages() == [{"name": "pkgname", "version": "1.0.0"}]

    plugin_dir = bench.plugin_dir
    assert (plugin_dir / "pkgname-1.0.0.dist-info" / "METADATA").exists()
    assert (plugin_dir / "pkgname" / "__init__.py").exists()


def test_act_plugin_install_end_to_end_success_updates_snapshot_and_calls_reload(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[], workspaces_root=tmp_path)
    calls = []
    bench.on_plugin_reload = lambda: calls.append(True)

    content = _hand_wheel_bytes("pkgname", "1.0.0")
    b64 = base64.b64encode(content).decode("ascii")
    bench.dispatch("plugin_install", {"filename": "pkgname-1.0.0-py3-none-any.whl",
                                       "content": b64})

    assert bench.snapshot()["ext"]["installed"] == [{"name": "pkgname", "version": "1.0.0"}]
    assert calls == [True]
    assert any('"pkgname-1.0.0" installed' in log["msg"] for log in bench.logs)


def test_act_plugin_install_without_reload_hook_logs_restart_needed(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[], workspaces_root=tmp_path)
    assert bench.on_plugin_reload is None

    content = _hand_wheel_bytes("pkgname", "1.0.0")
    b64 = base64.b64encode(content).decode("ascii")
    bench.dispatch("plugin_install", {"filename": "pkgname-1.0.0-py3-none-any.whl",
                                       "content": b64})

    assert any("can't activate without a restart" in log["msg"] and log["type"] == "emcy0"
               for log in bench.logs)


def test_install_plugin_wheel_upgrade_removes_old_version_files(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[], workspaces_root=tmp_path)
    plugin_dir = bench.plugin_dir

    ok1, msg1 = bench._install_plugin_wheel(
        "pkgname-1.0.0-py3-none-any.whl", _hand_wheel_bytes("pkgname", "1.0.0"))
    assert (ok1, msg1) == (True, "pkgname-1.0.0")
    assert (plugin_dir / "pkgname-1.0.0.dist-info").is_dir()

    ok2, msg2 = bench._install_plugin_wheel(
        "pkgname-1.1.0-py3-none-any.whl", _hand_wheel_bytes("pkgname", "1.1.0"))
    assert (ok2, msg2) == (True, "pkgname-1.1.0")

    assert not (plugin_dir / "pkgname-1.0.0.dist-info").exists()
    assert (plugin_dir / "pkgname-1.1.0.dist-info").is_dir()
    manifest = bench._plugin_manifest()
    assert manifest.keys() == {"pkgname"}
    assert manifest["pkgname"]["version"] == "1.1.0"


def test_act_plugin_remove_deletes_files_manifest_entry_and_calls_reload(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[], workspaces_root=tmp_path)
    plugin_dir = bench.plugin_dir
    bench._install_plugin_wheel("pkgname-1.0.0-py3-none-any.whl",
                                 _hand_wheel_bytes("pkgname", "1.0.0"))

    calls = []
    bench.on_plugin_reload = lambda: calls.append(True)
    bench.dispatch("plugin_remove", {"pkg": "pkgname"})

    assert not (plugin_dir / "pkgname-1.0.0.dist-info").exists()
    assert not (plugin_dir / "pkgname").exists()
    assert "pkgname" not in bench._plugin_manifest()
    assert calls == [True]
    assert any('"pkgname" removed' in log["msg"] for log in bench.logs)


def test_act_plugin_remove_unknown_package_logs_and_does_not_crash(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[], workspaces_root=tmp_path)
    calls = []
    bench.on_plugin_reload = lambda: calls.append(True)

    bench.dispatch("plugin_remove", {"pkg": "nosuchpkg"})

    assert any('unknown package "nosuchpkg"' in log["msg"] and log["type"] == "emcy0"
               for log in bench.logs)
    assert calls == []


def test_act_plugin_remove_without_plugin_dir_is_a_noop(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[])
    assert bench.plugin_dir is None
    bench.dispatch("plugin_remove", {"pkg": "anything"})  # must not raise


def test_workspace_create_rejects_reserved_plugins_name(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[], workspaces_root=tmp_path)
    assert (tmp_path / "plugins").is_dir()  # plugin_dir already created it
    bench.on_workspace_switch = lambda name: None  # required for act_workspace_create to run

    bench.dispatch("workspace_create", {"name": "plugins"})

    assert any("invalid workspace name" in log["msg"] and log["type"] == "emcy0"
               for log in bench.logs)
    assert not any("workspace" in log["msg"] and "created" in log["msg"]
                   for log in bench.logs)
    assert "plugins" not in bench._workspace_names()


def test_real_wheel_install_then_reload_loads_the_plugin(tmp_path, monkeypatch):
    """End-to-end: an importable plugin package, installed as a wheel, is
    actually discoverable by a fresh Bench — the same thing on_plugin_reload
    triggers in the real app. Uses the *real* load_plugins() (undoing the
    conftest autouse stub for this test only) and a unique package name so
    sys.modules pollution can't leak into other tests."""
    monkeypatch.setattr("canopen_bench.core.load_plugins", load_plugins)

    pkg = f"benchplug_{uuid.uuid4().hex[:8]}"
    class_name = "RealPlugin"
    plugin_name = f"realplugin-{pkg}"
    content = _write_zip({
        f"{pkg}/__init__.py": "",
        f"{pkg}/plugin.py": (
            "from canopen_bench.plugin import BenchPlugin\n\n"
            f"class {class_name}(BenchPlugin):\n"
            f"    name = {plugin_name!r}\n"
            "    def emcy_codes(self):\n"
            "        return {0x9999: 'synthetic test emcy text'}\n"
        ),
        f"{pkg}-1.0.0.dist-info/METADATA":
            f"Metadata-Version: 2.1\nName: {pkg}\nVersion: 1.0.0\n",
        f"{pkg}-1.0.0.dist-info/entry_points.txt":
            f"[canopen_bench.plugins]\n{pkg} = {pkg}.plugin:{class_name}\n",
    })

    bench1 = Bench(Db(tmp_path / "x.db"), plugins=[], workspaces_root=tmp_path)
    ok, msg = bench1._install_plugin_wheel(f"{pkg}-1.0.0-py3-none-any.whl", content)
    assert (ok, msg) == (True, f"{pkg}-1.0.0")

    try:
        bench2 = Bench(Db(tmp_path / "y.db"), plugins=None, workspaces_root=tmp_path)
        assert plugin_name in [p.name for p in bench2.plugins]
        assert bench2._emcy_text(0x9999) == "synthetic test emcy text"
    finally:
        sys.modules.pop(pkg, None)
        sys.modules.pop(f"{pkg}.plugin", None)


# -- device panels (sidebar boxes contributed by a plugin) -------------------

class _FakePanel(DevicePanel):
    key = "lcd"
    title = "Display"

    def __init__(self, match: bool = True, data: dict | None = None,
                 boom: bool = False):
        self._match, self._data, self._boom = match, data, boom
        self.seen_eds: list = []

    def matches(self, dev: dict, eds: dict | None) -> bool:
        self.seen_eds.append(eds)
        return self._match

    def render(self, bench, dev: dict) -> dict | None:
        if self._boom:
            raise RuntimeError("panel is broken")
        return self._data


class _PanelPlugin(BenchPlugin):
    name = "fake"

    def __init__(self, *panels: DevicePanel):
        self._panels = list(panels)

    def device_panels(self) -> list[DevicePanel]:
        return self._panels


def _panel_bench(tmp_path, *panels, sel: bool = True, eds: str = "—") -> Bench:
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_PanelPlugin(*panels)])
    bench.devices = [{"node": 7, "name": "DUT", "nmt": "Operational", "sel": sel,
                      "cmds": {}, "fw": "", "sn": "", "variant": "",
                      "ident": "0x4D2·0x1150", "eds": eds}]
    return bench


def test_snapshot_has_no_panels_without_plugins(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[])
    assert bench.snapshot()["panels"] == []


def test_panel_is_namespaced_and_carries_title_and_node(tmp_path):
    panel = _FakePanel(data={"leds": [{"c": "red", "on": None}]})
    bench = _panel_bench(tmp_path, panel)
    (got,) = bench.snapshot()["panels"]
    assert got["key"] == "fake.lcd"
    assert got["title"] == "Display"
    assert got["node"] == 7
    assert got["leds"] == [{"c": "red", "on": None}]


def test_panel_canvas_reaches_the_frontend_verbatim(tmp_path):
    """The core does not read the description, it forwards it — including
    a primitive's ``blink``, which says the device is flashing that
    element rather than merely showing it. Filtering keys the core happens
    not to know would silently turn one device state into another."""
    draw = [{"t": "line", "p": [0, 0, 8, 0], "w": 2, "c": "fg", "blink": "slow"}]
    bench = _panel_bench(tmp_path, _FakePanel(data={"canvas": {"w": 20, "h": 10,
                                                               "draw": draw}}))
    (got,) = bench.snapshot()["panels"]
    assert got["canvas"]["draw"] == draw


def test_panel_not_shown_when_it_does_not_match(tmp_path):
    bench = _panel_bench(tmp_path, _FakePanel(match=False, data={"leds": []}))
    assert bench.snapshot()["panels"] == []


def test_panel_not_shown_without_a_selected_device(tmp_path):
    bench = _panel_bench(tmp_path, _FakePanel(data={"leds": []}), sel=False)
    assert bench.snapshot()["panels"] == []


def test_panel_render_returning_none_shows_nothing(tmp_path):
    bench = _panel_bench(tmp_path, _FakePanel(data=None))
    assert bench.snapshot()["panels"] == []


def test_panel_matches_receives_the_eds_registry_row(tmp_path):
    panel = _FakePanel(match=False)
    bench = _panel_bench(tmp_path, panel, eds="DemoDevice.eds")
    bench.db.eds_add("DemoDevice.eds", "DemoDevice", "0x4D2·0x1150", "DMO", True)
    bench.snapshot()
    assert panel.seen_eds[-1] is not None
    assert panel.seen_eds[-1]["file"] == "DemoDevice.eds"


def test_panel_matches_gets_none_when_device_has_no_eds(tmp_path):
    panel = _FakePanel(match=False)
    bench = _panel_bench(tmp_path, panel)
    bench.snapshot()
    assert panel.seen_eds[-1] is None


def test_a_panel_can_tell_the_demo_bus_from_a_real_one(tmp_path):
    """A panel that mirrors hardware has to be able to stay away in demo
    mode: values the tool generated itself are not a picture of a device.
    The core offers the fact and leaves the decision to the panel."""
    bench = _panel_bench(tmp_path, _FakePanel(data={"leds": []}))
    bench.dispatch("set_adapter", {"adapter": "demo"})
    assert bench.demo is True
    bench.dispatch("set_adapter", {"adapter": "ixxat"})
    assert bench.demo is False


def test_broken_panel_is_hidden_for_the_session_and_logged(tmp_path):
    """render() runs on every snapshot, so a raising panel must be dropped
    once — not retried (and re-logged) forever — and must never take the
    snapshot, and with it the whole UI, down."""
    panel = _FakePanel(boom=True)
    bench = _panel_bench(tmp_path, panel)
    assert bench.snapshot()["panels"] == []
    assert sum("fake.lcd" in row["msg"] for row in bench.logs) == 1
    assert bench.snapshot()["panels"] == []          # still up
    assert sum("fake.lcd" in row["msg"] for row in bench.logs) == 1  # not re-logged


def test_two_plugins_panels_stay_distinct(tmp_path):
    class _Other(_PanelPlugin):
        name = "other"

    bench = Bench(Db(tmp_path / "x.db"), plugins=[
        _PanelPlugin(_FakePanel(data={"leds": []})),
        _Other(_FakePanel(data={"leds": []})),
    ])
    bench.devices = [{"node": 7, "name": "DUT", "nmt": "Operational", "sel": True,
                      "cmds": {}, "fw": "", "sn": "", "variant": "",
                      "ident": "0x4D2·0x1150", "eds": "—"}]
    assert [p["key"] for p in bench.snapshot()["panels"]] == ["fake.lcd", "other.lcd"]


def test_panel_caption_reaches_the_snapshot(tmp_path):
    """A one-liner under the canvas — a mode or screen name — so a device's
    own words do not have to be drawn into the picture that mirrors it."""
    panel = _FakePanel(data={"canvas": {"w": 10, "h": 10, "draw": []},
                             "caption": "Working tension"})
    bench = _panel_bench(tmp_path, panel)
    assert bench.snapshot()["panels"][0]["caption"] == "Working tension"


def test_a_plugin_names_the_object_a_step_touches(tmp_path):
    """The firmware's own identifier, where a plugin can derive it: a case
    is written against the headers, and whoever reads the report went
    looking for the same name in the same code."""
    class Naming(BenchPlugin):
        name = "naming"

        def describe_object(self, index: str, sub: str, symbols) -> str:
            return "eObjIdx_LampControl/Mode" if index == "0x2345" else ""

    bench = Bench(Db(tmp_path / "n.db"), plugins=[Naming()])
    text = bench._label_step("sdo_write", {"index": "0x2345", "sub": 1, "value": "0xC001D00D"})
    assert text == "write 0x2345:0x01 = 0xC001D00D  (eObjIdx_LampControl/Mode)"
    # an object the plugin does not know falls back to whatever the EDS says
    assert "(" not in bench._label_step("sdo_read", {"index": "0x6040", "sub": 0})


def test_a_plugin_that_raises_while_naming_does_not_stop_the_run(tmp_path):
    class Broken(BenchPlugin):
        name = "broken"

        def describe_object(self, index: str, sub: str, symbols) -> str:
            raise RuntimeError("headers not loaded")

    bench = Bench(Db(tmp_path / "b.db"), plugins=[Broken()])
    assert bench._label_step("sdo_read", {"index": "0x2345", "sub": 1}) == "read 0x2345:0x01"


# -- the same package installed twice ---------------------------------------

class _FakeDist:
    """Just enough of importlib.metadata.Distribution for _installed_at()."""

    def __init__(self, name: str, version: str, path: str):
        self.name, self.version, self._path = name, version, path

    def locate_file(self, _):
        return self._path


class _FakeEP:
    def __init__(self, name: str, target, dist):
        self.name, self.value, self.dist = name, f"{target.__module__}:x", dist
        self._target = target

    def load(self):
        return self._target


class _Twice(BenchPlugin):
    name = "twice"


def _entry_points(monkeypatch, eps):
    monkeypatch.setattr("canopen_bench.plugin.metadata.entry_points",
                        lambda **kw: list(eps))


def test_one_plugin_installed_twice_is_loaded_once(monkeypatch):
    """A wheel uploaded under Setup > Extensions and the same package
    installed into the environment both register their entry point. Loading
    both gives one bench two of every hook — two sidebar panels, every
    seeded EDS row twice, every trace frame decoded twice."""
    _entry_points(monkeypatch, [
        _FakeEP("acme", _Twice, _FakeDist("cob-acme", "2.0", "/data/plugins")),
        _FakeEP("acme", _Twice, _FakeDist("cob-acme", "1.0", "/site-packages")),
    ])
    notes: list[str] = []

    plugins = load_plugins(note=notes.append)

    assert len(plugins) == 1
    assert len(notes) == 1


def test_the_duplicate_names_both_places_and_both_versions(monkeypatch):
    """Which of the two actually runs is decided by sys.path, not by the
    entry-point list, so a message saying only "installed twice" leaves the
    reader exactly where they were: the version the UI shows belongs to one
    installation and the running code may be the other."""
    _entry_points(monkeypatch, [
        _FakeEP("acme", _Twice, _FakeDist("cob-acme", "2.0", "/data/plugins")),
        _FakeEP("acme", _Twice, _FakeDist("cob-acme", "1.0", "/site-packages")),
    ])
    notes: list[str] = []

    load_plugins(note=notes.append)

    assert "/data/plugins" in notes[0] and "/site-packages" in notes[0]
    assert "2.0" in notes[0] and "1.0" in notes[0]
    assert "sys.path" in notes[0]


def test_two_different_plugins_are_not_a_duplicate(monkeypatch):
    """The check is per entry-point name. Two packages are the normal case
    and must not warn about each other."""
    _entry_points(monkeypatch, [
        _FakeEP("acme", _Twice, _FakeDist("cob-acme", "2.0", "/site-packages")),
        _FakeEP("brox", _Twice, _FakeDist("cob-brox", "1.0", "/site-packages")),
    ])
    notes: list[str] = []

    assert len(load_plugins(note=notes.append)) == 2
    assert notes == []


def test_a_duplicate_survives_a_metadata_backend_that_cannot_say_where(monkeypatch):
    """The message is a diagnostic; a distribution that cannot be asked
    where it lives costs a vaguer sentence, not the startup."""
    class _MuteEP(_FakeEP):
        @property
        def dist(self):
            raise RuntimeError("no metadata here")

        @dist.setter
        def dist(self, _value):
            pass

    _entry_points(monkeypatch, [
        _MuteEP("acme", _Twice, None), _MuteEP("acme", _Twice, None),
    ])
    notes: list[str] = []

    assert len(load_plugins(note=notes.append)) == 1
    assert "installed twice" in notes[0]


def test_the_bench_says_it_in_the_state_log(monkeypatch, tmp_path):
    """Not only through logging: a duplicate reads as "my change did not
    arrive", and nobody debugging that looks at a console the bench was
    never started from."""
    monkeypatch.setattr("canopen_bench.core.load_plugins", load_plugins)
    _entry_points(monkeypatch, [
        _FakeEP("acme", _Twice, _FakeDist("cob-acme", "2.0", "/data/plugins")),
        _FakeEP("acme", _Twice, _FakeDist("cob-acme", "1.0", "/site-packages")),
    ])

    bench = Bench(Db(tmp_path / "x.db"), plugins=None)

    assert [p.name for p in bench.plugins] == ["twice"]
    assert any("installed twice" in entry["msg"] and entry["type"] == "emcy0"
               for entry in bench.logs)


# -- the EDS files a plugin ships -------------------------------------------

def _eds_plugin(folder: Path) -> BenchPlugin:
    class EdsPlugin(BenchPlugin):
        name = "edsy"

        def eds_dirs(self):
            return [folder]

    return EdsPlugin()


def test_a_plugin_brings_the_eds_files_its_rows_name(tmp_path):
    """seed_eds() registers the rows; without the files those rows name,
    a fresh bench has a registry pointing at nothing and somebody carries
    the files from one machine to the next — the thing a plugin exists to
    stop."""
    packaged = tmp_path / "packaged"
    packaged.mkdir()
    (packaged / "acme_feeder.eds").write_text("[FileInfo]\nFileName=acme_feeder.eds\n",
                                           encoding="utf-8")
    (packaged / "notes.txt").write_text("not an EDS", encoding="utf-8")

    bench = Bench(Db(tmp_path / "x.db"), plugins=[_eds_plugin(packaged)])

    assert (bench.db.eds_dir / "acme_feeder.eds").is_file()
    assert not (bench.db.eds_dir / "notes.txt").exists()


def test_the_workspaces_own_eds_outranks_the_packaged_one(tmp_path):
    """The file in the workspace is what the devices on this bench answer
    to, and it is regularly newer than the packaged copy. Editing it there
    has to stick — the same rule flows and headers follow."""
    packaged = tmp_path / "packaged"
    packaged.mkdir()
    (packaged / "acme_feeder.eds").write_text("packaged", encoding="utf-8")

    bench = Bench(Db(tmp_path / "x.db"), plugins=[_eds_plugin(packaged)])
    (bench.db.eds_dir / "acme_feeder.eds").write_text("edited here", encoding="utf-8")
    Bench(Db(tmp_path / "x.db"), plugins=[_eds_plugin(packaged)])   # a later start

    assert (bench.db.eds_dir / "acme_feeder.eds").read_text() == "edited here"


def test_an_eds_folder_that_cannot_be_written_is_said_not_raised(tmp_path):
    """A bench whose EDS folder is read-only still runs; it just cannot
    match identities, and that has to be readable somewhere."""
    packaged = tmp_path / "packaged"
    packaged.mkdir()
    (packaged / "acme_feeder.eds").write_text("packaged", encoding="utf-8")

    class Boom(BenchPlugin):
        name = "boom"

        def eds_dirs(self):
            return [packaged]

    bench = Bench(Db(tmp_path / "x.db"), plugins=[])
    bench.plugins = [Boom()]
    bench.db.eds_dir.mkdir(parents=True, exist_ok=True)
    (bench.db.eds_dir / "acme_feeder.eds").mkdir()      # a directory in the file's place
    bench.logs.clear()
    bench._seed_plugin_eds()                          # must not raise

    assert not any("efs2_920" in entry["msg"] for entry in bench.logs), \
        "an existing name is left alone, whatever it is"


# -- packaged test cases -----------------------------------------------------

def _case(where: Path, name: str, ident: str) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    path = where / name
    path.write_text(f'id: "{ident}"\nname: from the plugin\n'
                    f'steps: [{{nmt: start}}]\n', encoding="utf-8")
    return path


def test_a_plugins_test_cases_reach_the_catalog(tmp_path):
    """A device family's cases belong with the plugin that knows the
    family, the same as its panels and its headers: a bench that has the
    plugin has the cases, without copying a folder or running a
    converter."""
    packaged = _case(tmp_path / "packaged", "TC7_from_plugin.yaml", "7")
    bench = Bench(Db(tmp_path / "x.db"), plugins=[FakePlugin(tc_dir=packaged.parent)])
    assert (Path(bench.paths["tc"]) / "TC7_from_plugin.yaml").exists()
    assert "7" in bench.testcases        # the catalog is keyed by id


def test_a_packaged_case_is_refreshed_and_a_local_one_is_left_alone(tmp_path):
    """Same rule as the headers, and for the same reason: the plugin is
    where an edit to its own file belongs, and a bench whose cases are the
    snapshot of the day the workspace was made is a bench running last
    month's tests."""
    packaged = _case(tmp_path / "packaged", "TC7_from_plugin.yaml", "7")
    bench = Bench(Db(tmp_path / "x.db"), plugins=[FakePlugin(tc_dir=packaged.parent)])
    folder = Path(bench.paths["tc"])
    mine = _case(folder, "TC8_mine.yaml", "8")

    packaged.write_text(packaged.read_text().replace("from the plugin", "changed"),
                        encoding="utf-8")
    again = Bench(Db(tmp_path / "x.db"), plugins=[FakePlugin(tc_dir=packaged.parent)])
    assert "changed" in (folder / "TC7_from_plugin.yaml").read_text()
    assert mine.read_text().endswith("steps: [{nmt: start}]\n"), "a case nobody ships"
    assert {"7", "8"} <= set(again.testcases)


# -- the manufacturer half of an EMCY ----------------------------------------

def _emcy_row(payload: str) -> dict:
    return {"cls": "EMCY", "node": 1, "data": payload, "obj": "", "val": ""}


def test_the_manufacturer_error_code_is_read_and_named(tmp_path):
    """The EEC is CiA 301 and the core knows it. The five bytes after the
    error register are the device's own, and the standard says nothing
    about them — so the frame is read here and named by the plugin, the
    same split as an object's address and its name."""
    bench = Bench(Db(tmp_path / "x.db"), plugins=[FakePlugin()])
    row = _emcy_row("00 10 01 65 00 00 00 00")
    bench._annotate_emcy(row, live=False)
    assert row["obj"] == "0x1000 Generic error · MEC 0x0065 Yarn breakage"


def test_a_manufacturer_code_nobody_names_still_shows_its_number(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[FakePlugin()])
    row = _emcy_row("00 10 01 02 00 00 00 00")
    bench._annotate_emcy(row, live=False)
    assert row["obj"] == "0x1000 Generic error · MEC 0x0002"


def test_an_empty_manufacturer_field_says_nothing(tmp_path):
    """Five zero bytes are what every frame with nothing to say there
    carries, and a "MEC 0x0000" on all of them would be a column of
    noise."""
    bench = Bench(Db(tmp_path / "x.db"), plugins=[FakePlugin()])
    row = _emcy_row("00 10 01 00 00 00 00 00")
    bench._annotate_emcy(row, live=False)
    assert row["obj"] == "0x1000 Generic error"


# -- stats providers (Bench._observe_stats / _stats_blocks) ------------------

class _FakeStats(StatsProvider):
    """Counts what it is given and reports it back, so a test can see both
    halves: which frames reached it, and what the core made of what it
    returned."""

    key, title = "load", "Controller load"

    def __init__(self, data=None, raises="", ):
        self.seen: list[tuple[int, bytes]] = []
        self.forks: list = []
        self.resets = 0
        self._data = data
        self._raises = raises

    def observe(self, cob: int, data: bytes) -> None:
        if self._raises == "observe":
            raise RuntimeError("broken provider")
        self.seen.append((cob, data))

    def render(self, bench) -> dict | None:
        if self._raises == "render":
            raise RuntimeError("broken provider")
        return self._data

    def reset(self) -> None:
        self.resets += 1

    def fresh(self):
        other = _FakeStats(self._data, self._raises)
        self.forks.append(other)
        return other


class _StatsPlugin(BenchPlugin):
    name = "fake"

    def __init__(self, *providers):
        self._providers = list(providers)

    def stats_providers(self) -> list:
        return self._providers


def _one_table(**over) -> dict:
    table = {"title": "Tasks",
             "cols": [{"label": "Task"}, {"label": "Load %", "align": "r"}],
             "rows": [["worker", "12.34"]]}
    table.update(over)
    return {"tables": [table]}


def _blocks(bench) -> list[dict]:
    return bench.snapshot()["trace"]["stats"]["blocks"]


def test_snapshot_has_no_stats_blocks_without_plugins(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[])
    assert _blocks(bench) == []


def test_a_stats_block_is_namespaced_and_carries_its_title(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StatsPlugin(_FakeStats(_one_table()))])
    (got,) = _blocks(bench)
    assert got["key"] == "fake.load"
    assert got["title"] == "Controller load"
    assert got["tables"][0]["rows"] == [["worker", "12.34"]]


def test_a_block_with_nothing_to_say_is_not_drawn(tmp_path):
    """Before the first measurement there is nothing to show, and an empty
    card in the Stats view reads like a device that answered."""
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StatsPlugin(_FakeStats(None))])
    assert _blocks(bench) == []


def test_the_block_carries_only_what_the_core_can_draw(tmp_path):
    """The core lays a block out; what a provider may say is therefore
    exactly as wide as what the core draws. A key it invented would
    otherwise ride along in every snapshot and be drawn by nobody."""
    data = {"note": "3 reports", "colour": "red",
            "fields": [{"label": "Heap free", "value": 12480, "hint": "min 11904"}],
            "tables": [{"title": "Tasks",
                        "cols": [{"label": "Task"}, {"label": "Load %", "align": "r"},
                                 {"label": "Free", "align": "middle"}],
                        "rows": [["worker", 12.34, "512", "spare cell"]]}]}
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StatsPlugin(_FakeStats(data))])
    (got,) = _blocks(bench)
    assert "colour" not in got
    assert got["note"] == "3 reports"
    assert got["fields"] == [{"label": "Heap free", "value": "12480", "hint": "min 11904"}]
    cols = got["tables"][0]["cols"]
    assert [c["align"] for c in cols] == ["l", "r", "l"], "only 'r' is a column of numbers"
    assert got["tables"][0]["rows"] == [["worker", "12.34", "512"]], "a cell per column"


def test_a_block_may_offer_controls(tmp_path):
    """What a measurement is started and stopped by belongs next to the
    measurement. The core forwards the action name and the button's id and
    knows nothing else about it — exactly like a panel's buttons."""
    data = _one_table() | {"action": "fake.set",
                           "controls": [{"id": "off", "label": "Stop", "title": "stop it"},
                                        {"id": "5", "label": "0.5 s"}]}
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StatsPlugin(_FakeStats(data))])
    (got,) = _blocks(bench)
    assert got["action"] == "fake.set"
    assert got["controls"] == [{"id": "off", "label": "Stop", "title": "stop it"},
                               {"id": "5", "label": "0.5 s", "title": ""}]


def test_controls_without_an_action_reach_nobody(tmp_path):
    """A button the core would dispatch nowhere is a button that does
    nothing when pressed, which is worse than one that is not there."""
    data = _one_table() | {"controls": [{"id": "off", "label": "Stop"}]}
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StatsPlugin(_FakeStats(data))])
    assert "controls" not in _blocks(bench)[0]


def test_a_table_without_columns_is_dropped(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"),
                  plugins=[_StatsPlugin(_FakeStats({"tables": [{"rows": [["a"]]}]}))])
    assert _blocks(bench) == []


def test_a_runaway_table_is_capped(tmp_path):
    """The block is rebuilt into every snapshot, which goes out every tick
    to every open browser. A provider that measured ten thousand things
    must not make that push expensive."""
    rows = [[str(i), "1"] for i in range(5000)]
    bench = Bench(Db(tmp_path / "x.db"),
                  plugins=[_StatsPlugin(_FakeStats(_one_table(rows=rows)))])
    (got,) = _blocks(bench)
    assert len(got["tables"][0]["rows"]) == Bench._BLOCK_ROWS


def test_every_recorded_frame_reaches_the_provider_exactly_once(tmp_path):
    """A provider assembles a measurement out of a sequence of frames, so a
    frame counted twice is a wrong number rather than a duplicated row.
    This sits in the drain, where the queue is emptied — whoever drains
    first gets the frames, and the second drain finds nothing."""
    prov = _FakeStats()
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StatsPlugin(prov)])
    write_seed_eds_files(bench)
    connect_and_scan(bench)
    prov.seen.clear()  # the scan itself carried frames
    bench.bus.queue_raw(0x381, bytes.fromhex("0E00C8000000"))
    bench._drain_frames()
    bench._drain_frames()
    # the demo devices go on sending heartbeats around it; this one frame
    # is the one being counted
    assert prov.seen.count((0x381, bytes.fromhex("0E00C8000000"))) == 1


def _import(bench, *lines: str) -> None:
    text = "".join(line + "\n" for line in lines)
    bench.dispatch("trace_import", {"filename": "capture.log", "fmt": "candump",
                                    "data": base64.b64encode(text.encode()).decode()})


def test_a_loaded_capture_is_read_by_a_provider_of_its_own(tmp_path):
    """A capture is a file, the session is the bus, and adding one to the
    other gives a number nobody can see is wrong. So the block that counts
    this session never sees the file: the capture is read by a second
    provider, asked for with fresh()."""
    prov = _FakeStats(_one_table())
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StatsPlugin(prov)])
    _import(bench, "(0.000000) can0 381#0E00C8000000")
    assert len(bench._trace_view()[0]) == 1, "the capture is shown"
    assert prov.seen == [], "and not by the one counting the bus"
    (other,) = prov.forks
    assert other.seen == [(0x381, bytes.fromhex("0E00C8000000"))]
    assert _blocks(bench)[0]["key"] == "fake.load", "the capture's own block"


def test_a_provider_that_hands_back_its_live_self_reads_no_capture(tmp_path):
    """Two measurements added together is worse than one missing block —
    and this is the mistake a plugin makes by returning a singleton."""
    prov = _FakeStats(_one_table())
    prov.fresh = lambda: prov
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StatsPlugin(prov)])
    _import(bench, "(0.000000) can0 381#0E00C8000000")
    assert prov.seen == [], "the file never reached the live counter"
    assert _blocks(bench) == []
    assert sum("fake.load" in row["msg"] for row in bench.logs) == 1


def test_a_capture_is_read_not_driven(tmp_path):
    """The buttons of a block act on the bus. Beside numbers that came out
    of a file they would offer to change something the reading is not
    about — and on a bench with no device at all, nothing."""
    data = _one_table() | {"action": "fake.set", "controls": [{"id": "0", "label": "Stop"}]}
    prov = _FakeStats(data)
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StatsPlugin(prov)])
    assert "controls" in _blocks(bench)[0], "live, they are there"
    _import(bench, "(0.000000) can0 381#0E00C8000000")
    (block,) = _blocks(bench)
    assert "controls" not in block and "action" not in block
    assert block["tables"], "the reading itself is still shown"


def test_the_whole_stats_view_follows_the_open_capture(tmp_path):
    """Half these numbers from a file and half from this session would be
    a reading of neither, so the view switches together — the way the
    trace panel it sits behind does."""
    bench = Bench(Db(tmp_path / "x.db"), plugins=[])
    write_seed_eds_files(bench)
    connect_and_scan(bench)
    bench.bus.queue_raw(0x181, b"\x01\x02")
    bench._drain_frames()
    rows, bench._tick_rows = bench._tick_rows, []
    bench._update_bus_stats(rows)
    live = bench.snapshot()["trace"]["stats"]["total"]

    _import(bench, "(0.000000) can0 700#05", "(2.000000) can0 700#05",
            "(4.000000) can0 285#0102")
    st = bench.snapshot()["trace"]["stats"]
    assert st["of"] == bench.trace_loaded
    assert st["total"] == 3 and st["total"] != live
    assert {c["cob"] for c in st["cobs"]} == {"0x700", "0x285"}
    assert st["span"] == 4.0, "as long as the file, not as long as the session"
    assert st["loadHist"] == [], "a file carries no bitrate to measure load against"

    bench.dispatch("trace_toggle", {})  # back to live
    assert bench.snapshot()["trace"]["stats"]["total"] == live
    assert "of" not in bench.snapshot()["trace"]["stats"]


def test_the_block_starts_over_where_the_counters_do(tmp_path):
    prov = _FakeStats(_one_table())
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StatsPlugin(prov)])
    bench.dispatch("connect_toggle", {})
    assert prov.resets == 1, "connect"
    bench.dispatch("trace_clear", {})
    assert prov.resets == 2, "trace clear"


def test_a_broken_block_is_hidden_for_the_session_and_logged(tmp_path):
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StatsPlugin(_FakeStats(raises="render"))])
    assert _blocks(bench) == []
    assert sum("fake.load" in row["msg"] for row in bench.logs) == 1
    assert _blocks(bench) == []  # snapshot still up
    assert sum("fake.load" in row["msg"] for row in bench.logs) == 1  # not re-logged


def test_a_provider_that_raises_on_a_frame_does_not_stall_the_trace(tmp_path):
    broken, good = _FakeStats(raises="observe"), _FakeStats()
    bench = Bench(Db(tmp_path / "x.db"), plugins=[_StatsPlugin(broken, good)])
    row = _trace_row("0x381", "0E 00 C8 00 00 00")
    bench._observe_stats(row)
    assert good.seen == [(0x381, bytes.fromhex("0E00C8000000"))]
