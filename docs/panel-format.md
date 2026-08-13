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
| `cols` | optional | 1…4 columns inside the box (default 1). The box is also that many of the page's columns wide, so a row has the same room it would have in a one-column box |
| `collapsed` | optional | How the box opens the first time. What the operator folds afterwards outranks it, per workspace |
| `when` | optional | `{obj: <address>, bit: N}` or `{obj: <address>, value: N}` — the box exists only where that holds |

`when` and `collapsed` answer different questions. Folding says "not
interested right now" and belongs to the operator; `when` says "this
machine does not have that part" and belongs to the device — a backwinder
the device does not carry has no box, rather than an empty one to open.

`value` also takes a list — `{obj: "0x2102:10", value: [920, 922]}` —
because a device family is numbered rather than flagged: the part is on
the 920 and the 922 and on nothing else, and the variant object says 920
or 922. There is no bit anywhere that says "has a backwinder", and the
alternative is the same box written out twice, once per number.

Unknown counts as yes: a condition may take a box away once the device has
answered, never keep one hidden before anything was asked — the object
that would settle it usually sits behind the box it is hiding. A page-wide
read asks for the condition objects too, so one Read settles them.

## Field

| key | | |
|---|---|---|
| `obj` | required | `"0x2007:01"`, or `"0x2007"` for sub-index 0. Both halves are hex, with or without `0x` |
| `label` | optional | What to call it (default: the address) |
| `unit` | optional | Printed after the value — `mA`, `mV`, `%`, `°C`. Display only |
| `scale` | optional | Raw × scale = what is shown; the reverse on write (default 1) |
| `digits` | optional | Decimals shown (default: what the scale implies — `0.1` → 1) |
| `rw` | optional | `true` gives the field an input and a Write button. Default read-only: a panel is written from a device's documentation, and a typo that only displays is cheaper than one that writes |
| `widget` | optional | `number` (default), `enum` or `flag` — see below |
| `bit` | flag only | Which bit of the value the checkbox stands for, 0…31 |

`scale`, `digits` and `unit` belong to a number; on an `enum` or a `flag`
they are an error, because there is nothing they could mean there.

### enum and flag

```yaml
      - {label: Operation Mode, obj: "0x2008:00", widget: enum, rw: true}
      - {label: Stop On Drift,  obj: "0x2010:00", widget: flag, bit: 1, rw: true}
```

An `enum` takes its choices from the symbol table the plugin declared for
that object (`BenchPlugin.object_fields`), so the names are the
firmware's own — a list written into the panel file would be a second
copy of the same table, kept in step by hand. A value no symbol names is
offered as `?0x7` rather than snapped to the nearest name: an unexpected
value is a fact about the device.

A `flag` says which bit it is and needs no table for that; a status word
has bit 3 whether or not a header names it.

Both write the whole object, so both fold the change into the value last
read and leave the rest of it alone. A part of a value nobody has read is
refused rather than guessed — a checkbox that assumed zeros would clear
every other flag in the register.

Several fields may sit on the same object: a mode lane and the flags
beside it are the normal case.

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

## Where the values come from

A box is filled by asking, and by listening. The trace already decodes
every SDO answer and unpacks every mapped PDO signal, so a field whose
object appears there follows along without a frame of its own — a test
case reading a register next door, a device publishing its TPDO. That is
the only kind of "live" here; the other kind is polling, and there is
none.

The newer of the two wins, which orders them correctly by itself: a value
just typed is newer than a PDO from before it, so typing is never
overwritten; a PDO from a second ago is newer than a read from ten
minutes back, so the box follows the device. Every value carries where it
came from and how old it is — fresh numbers are printed at full strength
and fade as they age, and the tooltip says it in words. A number a PDO
carried past three minutes ago must not look like a reading taken just
now.

## What a panel does not do

There is deliberately no way to write display logic into a file, and
arbitrary markup never will be. A device's own test functions are not in
the format yet. A picture — a front-panel mirror, LEDs, buttons — is a
different hook that already exists (`device_panels()`); this one is for
values.
