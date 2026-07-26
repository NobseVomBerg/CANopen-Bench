# Extending CANopen-Bench with plugins

The core is strictly vendor-neutral CiA-301. Everything a specific
device family, adapter, or machine builder needs — extra hardware
cards, EDS seeds, addressing protocols, firmware download, custom test
steps — arrives through plugins. A plugin is an ordinary pip package;
installing it activates it, there is no configuration step.

(The authoritative API reference is the docstrings in
[`canopen_bench/plugin.py`](../canopen_bench/plugin.py).)

## Mechanics

Register a `BenchPlugin` subclass under the entry-point group
`canopen_bench.plugins`:

```toml
# pyproject.toml of your plugin package
[project]
name = "bench-acme"
dependencies = ["canopen-bench>=1,<2"]

[project.entry-points."canopen_bench.plugins"]
acme = "bench_acme.plugin:AcmePlugin"
```

At startup the bench discovers all installed plugins (sorted by
entry-point name for a stable order) and reads their hooks once. A
plugin that raises during load is logged and skipped — it can never
take the bench down.

Every hook is optional and defaults to "contributes nothing"; override
only what you provide.

## Hooks

| Hook | Returns | What it contributes |
|---|---|---|
| `adapters()` | `list[dict]` | Extra adapter cards on the Setup page (listed before the built-ins) |
| `adapter_backends()` | `dict` | Adapter key → (python-can interface, default channel) for those cards; the python-can driver itself ships via python-can's own `can.interface` group |
| `seed_eds()` | `list[dict]` | EDS registry rows seeded once into an empty workspace |
| `firmware()` | `list[dict]` | Firmware library entries for the SWDL page |
| `flow_dirs()` | `list[Path]` | Directories with packaged flow files (format-v2 YAML); copied into the workspace flows dir, never overwriting local edits |
| `addressing_provider()` | `AddressingProvider \| None` | Session identity for (re-)addressing runs (`$session` in flows); first plugin wins |
| `demo_hooks()` | `list[DemoHook]` | Device-side protocol simulation on the demo bus, so vendor flows run hardware-free |
| `trace_decoders()` | `list[TraceDecoder]` | Decoding for vendor-specific frames in the trace monitor |
| `emcy_codes()` | `dict[int, str]` | Vendor-/profile-specific EMCY error-code texts, merged over the built-in CiA-301 table (plugin wins on conflict) |
| `actions(bench)` | `dict[str, callable]` | Extra API actions, dispatched as `<plugin>.<action>` — collision-free with core actions |
| `step_types()` | `list[StepType]` | Extra flow/test-case step primitives, referenced in YAML as `<plugin>.<key>` |
| `swdl_strategy()` | `SwdlStrategy \| None` | Real firmware-download protocol replacing the core simulation; first plugin wins |

The helper base classes (`AddressingProvider`, `DemoHook`,
`TraceDecoder`, `StepType`, `SwdlStrategy`) are defined next to
`BenchPlugin` in `canopen_bench/plugin.py`, each with docstrings
covering the exact contract.

## Minimal example

A plugin that adds one trace decoder and one custom step type:

```python
# bench_acme/plugin.py
from canopen_bench.plugin import BenchPlugin, StepType, TraceDecoder


class AcmeStatusDecoder(TraceDecoder):
    name = "acme-status"

    def decode(self, cob, data):
        if cob == 0x7A0 and len(data) >= 2:
            return {"dec": "ACME status", "val": f"0x{data[1]:02X}"}
        return None  # not ours — let other decoders try


class BlinkStep(StepType):
    key = "blink"  # YAML: - acme.blink: {times: 3}

    def label(self, val):
        return f"blink x{val.get('times', 1)}"

    async def execute(self, bench, bus, node, val, regs, builtins):
        for _ in range(int(val.get("times", 1))):
            bus.send_raw(0x7A0, bytes([node, 0x01]))
        return "ok", ""


class AcmePlugin(BenchPlugin):
    name = "acme"

    def trace_decoders(self):
        return [AcmeStatusDecoder()]

    def step_types(self):
        return [BlinkStep()]
```

Install it (`pip install -e .`) and restart the bench — the decoder is
active in the trace, and flows can use `- acme.blink: {times: 3}`.

## Testing without hardware

Pair your protocol code with a `DemoHook` that simulates the device
side on the demo bus (`on_raw_frame`, `press_button`). For a public,
worked example of a plugin package — python-can driver plus adapter
card, entry points, tests — see
[`bench-cpcusb`](https://github.com/NobseVomBerg/CANopen-Bench-GPL-Plugins/tree/main/bench-cpcusb) (GPL-2.0).
