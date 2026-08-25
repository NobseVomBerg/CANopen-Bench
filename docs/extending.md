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
name = "cob-acme"
dependencies = ["canopen-bench>=1,<2"]

[project.entry-points."canopen_bench.plugins"]
acme = "cob_acme.plugin:AcmePlugin"
```

At startup the bench discovers all installed plugins (sorted by
entry-point name for a stable order) and reads their hooks once. A
plugin that raises during load is logged and skipped — it can never
take the bench down.

An entry-point name that appears twice is loaded once, and the state log
says so. Two installations of the same package both register it — a wheel
uploaded under Setup > Extensions plus the same package installed into
the environment is how it happens — and reading both would give one bench
two of every hook. Which of the two the import resolves to is decided by
`sys.path`, so the message names both places and the file the code came
from: the version shown under Extensions belongs to one of them, and the
running code may be the other. That looks exactly like a change that did
not arrive.

Every hook is optional and defaults to "contributes nothing"; override
only what you provide.

## Hooks

| Hook | Returns | What it contributes |
|---|---|---|
| `adapters()` | `list[dict]` | Extra adapter cards on the Setup page (listed before the built-ins) |
| `adapter_backends()` | `dict` | Adapter key → (python-can interface, default channel) for those cards; the python-can driver itself ships via python-can's own `can.interface` group |
| `seed_eds()` | `list[dict]` | EDS registry rows seeded once into an empty workspace; a row may bring its `commands` and its `variant` (`{index, sub, map?}` — where this family keeps its variant number) instead of the operator configuring both by hand |
| `firmware()` | `list[dict]` | Firmware library entries for the SWDL page |
| `flow_dirs()` | `list[Path]` | Directories with packaged flow files (format-v2 YAML); copied into the workspace flows dir, never overwriting local edits |
| `symbol_dirs()` | `list[Path]` | Directories with the device's C headers, parsed into symbol tables. Copied into the workspace on every start, over whatever is there: a header belongs to its plugin the way its panels do, and the plugin is where an edit to one belongs. A header no plugin ships — the firmware actually under test, under a name of its own — is left where it was put |
| `eds_dirs()` | `list[Path]` | Directories with the family's own EDS files, copied into the workspace EDS folder like flows and headers. `seed_eds()` registers the rows; this brings the files those rows name |
| `describe_object(index, sub, symbols)` | `str` | What the device's own firmware calls this object, from the symbol tables. This name wins wherever the object is shown — object table, favourites, signal plot, trace and the report's step line — and the EDS name stands in where a plugin gives none. An object dictionary is a historical document, right when it was written and carried forward ever since; the headers are what the firmware's authors call the thing today, and they are what somebody greps. Return `""` for an address the headers do not name |
| `object_fields(symbols)` | `dict[str, list[Field]]` | How to read an object's value symbolically — a whole-value enum, fields packed into one word, or a flag register |
| `object_units(symbols)` | `dict[str, Quantity]` | What an object's number means physically: `"0x2007:02" -> Quantity("cN", 0.1)`. The one thing about an object no EDS can answer — a file says UNSIGNED16 and stops there. Used wherever the value is shown: beside the raw number in the object table, in a report line, and as the default for a panel field that gives no unit of its own |
| `addressing_provider()` | `AddressingProvider \| None` | Session identity for (re-)addressing runs (`$session` in flows); first plugin wins |
| `demo_hooks()` | `list[DemoHook]` | Device-side protocol simulation on the demo bus, so vendor flows run hardware-free |
| `trace_decoders()` | `list[TraceDecoder]` | Decoding for vendor-specific frames in the trace monitor |
| `device_panels()` | `list[DevicePanel]` | Sidebar boxes for a device family — front-panel mirror, virtual buttons, status LEDs |
| `object_panels()` | `list[Path]` | Packaged `*.panel.yaml` files: the Objects page's panel view, where a device's values appear as named boxes with the unit and scaling no EDS carries ([panel-format.md](panel-format.md)). Read from the package, never copied into a workspace |
| `emcy_codes()` | `dict[int, str]` | Vendor-/profile-specific EMCY error-code texts, merged over the built-in CiA-301 table (plugin wins on conflict) |
| `actions(bench)` | `dict[str, callable]` | Extra API actions, dispatched as `<plugin>.<action>` — collision-free with core actions |
| `step_types()` | `list[StepType]` | Extra flow/test-case step primitives, referenced in YAML as `<plugin>.<key>` |
| `swdl_strategy()` | `SwdlStrategy \| None` | Real firmware-download protocol replacing the core simulation; first plugin wins |

The helper base classes (`AddressingProvider`, `DemoHook`,
`TraceDecoder`, `DevicePanel`, `StepType`, `SwdlStrategy`) are defined
next to `BenchPlugin` in `canopen_bench/plugin.py`, each with docstrings
covering the exact contract.

### Symbol tables

Object indices, sub-indices and enum values are whatever the firmware
calls them. Keeping a second copy of those numbers in the tool is how a
bench ends up writing to the wrong object after a firmware change, so
`symbol_dirs()` points at the device's own C headers instead and the core
parses them (`canopen_bench/symbols.py`).

They are used in both directions: `$eObjIdx_LampControl` in a
test case or flow resolves **at load time**, so a typo makes the file
invalid in the catalog rather than failing mid-run; and a value read back
can be rendered as `Running (4)` rather than `0x04`.

Headers are seeded into `<workspace>/symbols/<plugin>/` and never
overwritten afterwards — the copy there is the firmware actually under
test, which is regularly newer than the plugin. The per-plugin
subdirectory is also what keeps two vendors' `eObjIdx` apart;
where they disagree, the bare name stops resolving and
`$<plugin>:<NAME>` is the way through.

Only a small subset of C is accepted: enums (including implicit
successors and constant expressions over symbols already parsed, so
`(eLamp_Off << 8)` works) and `#define` constants. Anything else is
reported with file and line rather than guessed — a table that is subtly
wrong is worse than no table.

### Object fields

`symbol_dirs()` gives the bench the device's names; `object_fields()`
says which name belongs to which value. The common case is one enum for
a whole object, but the same construct covers the awkward ones — two
fields packed into a byte, an enum living in bits 16..23 of a word, one
channel of several, a flag register — by adding a mask:

```python
def object_fields(self, symbols):
    return {"0x2007:09": [Field("eStatus", mask=0x0F, label="state"),
                          Field("eLamp", mask=0x30, label="lamp")],
            "0x2102:11": [Field("eFlags", flags=True)]}
```

`shift` defaults to the mask's lowest set bit. `flags=True` switches from
"the value names one thing" to "each set bit names one thing".

Deriving these from a naming convention is a plugin's business, not the
core's — the convention belongs to whoever writes the headers.

Two things the renderer will not do. It never replaces the number with a
name: the value column keeps showing the number in the operator's chosen
base, with the reading beside it. And it never drops bits — a value no
symbol names shows as `?0x7`, bits outside every mask as `+0x80`. An
unexplained bit in a status word is exactly what someone needs to see.

### Device panels

A plugin cannot ship frontend code — and should not be able to, since
code injected into the page can do everything the page can. Instead a
`DevicePanel` returns a *description* and the core renders it: a small
vector canvas, up to four buttons around it, a row of LEDs. None of that
vocabulary mentions any device; which devices a panel applies to is the
plugin's own `matches()`, and the core never learns a device name.

Every part is optional, because partial capability is normal: a family
whose display cannot be read contributes buttons and no canvas, one that
only signals state contributes LEDs alone. An LED's `on` is tri-state —
`True`, `False`, or `None` for "cannot be read", which renders as a
neutral ring rather than as dark. Showing an unreadable LED as off would
turn a missing capability into a wrong measurement. A canvas primitive
and an LED both take `blink` (`"slow"`/`"fast"`), for the case where the
device is flashing the element rather than merely showing it — an
element drawn statically because the description had no word for
"flashing" reads as a different device state than the real one.

`render()` runs on every snapshot and must not touch the bus. It formats
cached values (`bench.obj_vals`); reads and writes belong in the
plugin's `actions()`, which the panel's buttons and its refresh control
dispatch. That is what keeps a visible panel from quietly becoming a
poll loop.

## Minimal example

A plugin that adds one trace decoder and one custom step type:

```python
# cob_acme/plugin.py
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
[`plugins/cob-cpcusb/`](../plugins/cob-cpcusb/) (MIT) in this
repository.
