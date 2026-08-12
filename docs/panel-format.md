# Panel files

A panel turns the Objects page from a numeric table into the boxes the
people at the machine actually read: named values, grouped, with the unit
and the scaling that no EDS carries. It is a file, not code — the core
renders it, a plugin ships it (`BenchPlugin.object_panels()`,
[extending.md](extending.md)).

Panels are read out of the plugin package and never copied into a
workspace: a panel describes the device its plugin was built for, so it
belongs to that plugin's version, and a file seeded into every workspace
is a file somebody has to carry from bench to bench once it changes.

The parser is `canopen_bench/panelspec.py`; a worked example ships with
the core as `canopen_bench/panels/DemoDevice.panel.yaml`.

## A file

```yaml
name: Sample Feeder                 # what the view calls itself
match: {eds: "Sample*"}             # which devices it applies to

groups:
  - title: Temperatures
    cols: 2
    fields:
      - {label: MCU,     obj: "0x2100:01", unit: "°C"}
      - {label: MCU Max, obj: "0x2100:02", unit: "°C"}

  - title: Tension
    fields:
      - {label: Working, obj: "0x2007:02", unit: cN, scale: 0.1, rw: true}
      - {label: Reduced, obj: "0x2007:01", unit: cN, scale: 0.1, rw: true}

  - title: Identity
    collapsed: true
    fields:
      - {label: Serial number, obj: "0x1018:04"}
```

Unknown keys are an error, not a shrug — a misspelled `unit` that is
silently dropped leaves its author staring at an unchanged box. A file
that does not parse is logged with the reason and skipped; the other
panels, and the page, are unaffected.

## Panel

| key | | |
|---|---|---|
| `name` | optional | Heading of the view; the file name without suffixes if omitted |
| `match` | optional | `{eds: <glob>, name: <glob>}`, matched against the device's assigned EDS file and its product name. Every given key must match. No `match` takes every device |
| `groups` | required | The boxes, in the order they are drawn |

The panel matching the selected device wins. Plugin panels are asked
before the core's own, so a vendor panel takes its own devices from a
general-purpose one rather than competing with it.

## Group

| key | | |
|---|---|---|
| `title` | required | Box heading, and the key the folded state is remembered under — two groups may not share one |
| `fields` | required | The values in the box |
| `cols` | optional | 1…4 columns inside the box (default 1) |
| `collapsed` | optional | How the box opens the first time. What the operator folds afterwards outranks it, per workspace |

## Field

| key | | |
|---|---|---|
| `obj` | required | `"0x2007:01"`, or `"0x2007"` for sub-index 0. Both halves are hex, with or without `0x` |
| `label` | optional | What to call it (default: the address) |
| `unit` | optional | Printed after the value — `mA`, `mV`, `%`, `°C`. Display only |
| `scale` | optional | Raw × scale = what is shown; the reverse on write (default 1) |
| `digits` | optional | Decimals shown (default: what the scale implies — `0.1` → 1) |
| `rw` | optional | `true` gives the field an input and a Write button. Default read-only: a panel is written from a device's documentation, and a typo that only displays is cheaper than one that writes |

Values are shown in decimal, always. The object table's hex/dec chip is a
developer's reading habit; a box that says `mA` is read by someone who
wants 167, not 0xA7. A value that is not a number — a device name, a
version string — is passed through as it is.

`scale` and `unit` are separate on purpose: `scale` computes, `unit`
labels. A device storing tenths of a centinewton shows 16.0 with
`{unit: cN, scale: 0.1}`, and what a Write sends is the raw 160 again,
padded to the width the EDS declares for that object.

## Reading and writing

Nothing polls. A value arrives when somebody asks for it:

- **⟳ at the field** — one object.
- **Read at the box** — every value in that box.
- **⟳ Read all open** — every box that is not folded away. Folding one is
  the only thing that says "not interested right now", so a page-wide
  read skips it.

Writing works like the object table's: typing stages the value, `Write`
sends it. A field that sent on every keystroke would be the wrong default
for tensions and motor currents.

## What a panel does not do

There is deliberately no way to write display logic into a file:
conditions, enums, flag checkboxes and device test functions are not part
of the format yet, and arbitrary markup never will be. A picture — a
front-panel mirror, LEDs, buttons — is a different hook that already
exists (`device_panels()`); this one is for values.
