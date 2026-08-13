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
from canopen_bench.core import Bench, _resolve
from canopen_bench.db import Db
from canopen_bench.plugin import (
    AddressingProvider,
    BenchPlugin,
    DemoHook,
    DevicePanel,
    StepType,
    SwdlStrategy,
    TraceDecoder,
    load_plugins,
)
from canopen_bench.testcases import parse_testcase


class FakePlugin(BenchPlugin):
    name = "fake"

    def __init__(self, flow_dir: Path | None = None):
        self._flow_dir = flow_dir

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
        return [{"ver": "9.9.9", "file": "fake_v9.9.9.bin", "tag": "latest", "meta": "1 KB"}]

    def flow_dirs(self) -> list[Path]:
        return [self._flow_dir] if self._flow_dir else []


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
    bench = Bench(Db(tmp_path / "x.db"), plugins=[FakePlugin()])
    assert bench.fw_list[0]["ver"] == "9.9.9"
    assert bench.fw_sel == "9.9.9"

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

    def start(self, bench) -> None:
        self.started = True
        bench.swdl_run = True
        bench.swdl_done = False

    def step(self, bench) -> None:
        self.step_calls += 1
        bench.swdl_run = False
        bench.swdl_done = True


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


def test_first_plugins_swdl_strategy_wins(tmp_path):
    first = _RecordingSwdlStrategy()
    second = _RecordingSwdlStrategy()
    p1 = _SwdlPlugin("p1", first)
    p2 = _SwdlPlugin("p2", second)
    bench = Bench(Db(tmp_path / "x.db"), plugins=[p1, p2])
    assert bench._swdl is first


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
