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
| `cols` | optional | At most this many columns of values inside the box, 1…4 (default 1). Fewer where the page is too narrow to give each one room — the label is the first thing a squeezed row drops, and a box of `Pr…` is worse than a longer box |
| `flow` | optional | `rows` (default) fills across and then down: first value top left, second top right. `columns` fills down and then across: the first half of the values in the left column, the rest in the right |
| `collapsed` | optional | How the box opens the first time. What the operator folds afterwards outranks it, per workspace |
| `when` | optional | `{obj: <address>, bit: N}` or `{obj: <address>, value: N}` — the box exists only where that holds |

`flow` is worth setting where the order of the values means something.
Filling across puts consecutive values side by side, so a box read
top-to-bottom is read in the order they were written — but inserting one
moves every value after it to the other side of the box, and a list that
belongs together ends up split across the fold. `columns` keeps each half
in its own column: an insertion moves the values below it and leaves the
other column where it was.

Both narrow the same way. A page too narrow for two columns gets one, and
the values are then in the order they are written either way.

`when` and `collapsed` answer different questions. Folding says "not
interested right now" and belongs to the operator; `when` says "this
machine does not have that part" and belongs to the device — a second
axis the device does not carry has no box, rather than an empty one to
open.

`value` also takes a list — `{obj: "0x2001:00", value: [3, 4]}` — because
a device family is usually numbered rather than flagged: the part is on
two of the variants and on none of the others, and the variant object
answers one number or the other. Object dictionaries rarely carry a bit
that says "has that part", and the alternative is the same box written
out twice, once per number.

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
| `lane` | enum only | Which of the object's declared fields this one shows, named by its symbol table or its label. Omitted takes the first |
| `parts` | number only | The readings of this value, where it is a word assembled out of several — each an `enum` or a `flag`, each without an `obj` of its own. Drawn under the row that owns the object |
| `base` | number only | `dec` (default) or `hex` — which base the value is shown in. Display only; typed input is hex only where it says `0x` |

A field that gives neither `unit` nor `scale` takes whatever the plugin
declared for that address (`BenchPlugin.object_units`,
[extending.md](extending.md)) — what an object means physically is a fact
about the device, not about this view of it, and the same fact written
down in two places is the same fact drifting apart in two. Giving them
here overrides that, which is what a box written against one device's
documentation is for.

`scale`, `digits` and `unit` belong to a number; on an `enum` or a `flag`
they are an error, because there is nothing they could mean there.

Every value in a box ends at the same edge, whichever of the four it is —
a staged number, a value only read, a checkbox, a dropdown — and the unit
column is reserved for all of them. A dropdown is the one that may reach
further left, because it has to hold the firmware's own names.

### enum and flag

```yaml
      - {label: Operation Mode, obj: "0x2008:00", widget: enum, rw: true}
      - {label: Stop On Drift,  obj: "0x2010:00", widget: flag, bit: 1, rw: true}
```

An `enum` takes its choices from the symbol table the plugin declared for
that object (`BenchPlugin.object_fields`), so the names are the
firmware's own — a list written into the panel file would be a second
copy of the same table, kept in step by hand. A value no symbol names is
offered as the bare `0x7` rather than snapped to the nearest name: an
unexpected value is a fact about the device, and a question mark beside
it only says the bench has nothing to add.

A `flag` says which bit it is and needs no table for that; a status word
has bit 3 whether or not a header names it.

An `enum` on a read-only field is printed as the name, not offered as a
dropdown. A select on a value nobody can write opens, lists the device's
other states and changes neither the device nor the page.

`lane` is for the objects that carry more than one named field. A status
word assembled out of a mode, a speed and a lock bit is one object with
several names in it, and a box that could only ever show the first showed
a quarter of the word without saying so. One row per lane, and a `flag`
for the bit beside them — written as `parts` of the value they read:

```yaml
      - label: Status
        obj: "0x2011:00"
        base: hex
        parts:
          - {label: Locked, widget: flag, bit: 24}
          - {label: Mode,   widget: enum, lane: eMode}
          - {label: Speed,  widget: enum, lane: eSpeed}
```

A part carries no `obj`: it reads the value of the row above it, and one
that pointed somewhere else would be a field rather than a part. Writing
the address once is also what makes them a group — four rows that happen
to repeat one address are four objects as far as anything reading the
file can tell.

The box draws them as one: the row that owns the object keeps the ⟳ and
the address, and the parts hang under it, indented against a hairline,
without a ⟳ of their own. One Read fetches the word and every reading of
it, because there is one object underneath.

A part is `enum` or `flag` — one reading of a value. A plain number as a
part would be the whole value again, which is the row it is already under.
The row that owns them shows the value itself, and `base: hex` is usually
right there: a word that has parts is a bit pattern.

A lane is named by the symbol table behind it — the firmware's own word
for what those bits hold, so nothing has to be invented to point at it.
Where several lanes share one table, which is what three colours of one
LED enum look like, the plugin's `label` tells them apart and `lane`
takes that instead.

Where a lane is writable, staging one changes its bits and leaves the
rest of the word alone — the same read-modify-write a flag does. Both
write the whole object, so both fold the change into the value last read
and leave the rest of it alone. A part of a value nobody has read is
refused rather than guessed — a checkbox that assumed zeros would clear
every other flag in the register.

Fields may still sit on the same object without being parts of one, which
is what to write where they are not readings of one word.

Values are shown in decimal unless the field says otherwise. The object
table's hex/dec chip is a developer's reading habit; a box that says `mA`
is read by someone who wants 167, not 0xA7. A value that is not a
number — a device name, a version string — is passed through as it is.

### base

Some objects are not quantities. A command word where each bit asks the
device for something else is written in hex in its documentation, thought
about in hex, and made harder to read by being converted:

```yaml
      - {label: Command, obj: "0x2012:00", base: hex, rw: true}
```

`base: hex` shows the value as `0x0208`, padded to the width the EDS
declares. It says nothing about what is typed back: hex is written `0x20`
here exactly as everywhere else in the bench, and bare digits are
decimal, always. A field where the same digits meant different things
depending on a key in a file would be the one number on the page nobody
could check — and the `0x` the box prints is what closes the loop anyway:
type back what it shows and it means what it showed.

It belongs to a `number`; an `enum` and a `flag` show no number to write.
`scale`, `digits` and `unit` are refused beside it: hex says "this is a
bit pattern" and they say "this is a quantity", and a field cannot be
both.

Not every device documents such a word as bits somebody can name. Where
it does — and where the plugin declares the tables — `widget: enum` with
one `lane` per part is the better box, because it says what the bits mean
instead of what they are. `base: hex` is for the rest: the command word
whose bits are a table in a manual, sent as a pattern and answered in a
status object rather than read back.

`scale` and `unit` are separate on purpose: `scale` computes, `unit`
labels. A device storing tenths of a centinewton shows 16.0 with
`{unit: cN, scale: 0.1}`, and what a Write sends is the raw 160 again,
padded to the width the EDS declares for that object.

The object table shows the same reading beside the raw number wherever a
plugin declared one — `0x00A0` and `16.0 cN` — rather than instead of it.
There you type the number the device stores; in a box you type the
quantity, because a box is read by somebody who thinks in centinewtons
and the table by somebody who is looking at a register.

Signedness comes from the EDS too, and nothing in the file says it: a
word carries no sign of its own, so an object the EDS calls INTEGER16
shows -500 where an unsigned one shows 65036. A box that shows a negative
can also send one — typed with its minus, stored as the two's complement
of that object's width. Where the EDS says unsigned, a minus is refused
rather than wrapped.

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
came from and how old it is, and the tooltip says it in words — "read,
4 min ago". It is not said in the rendering: a bench reads a value once
and then works with it, so fading everything that is not seconds old
leaves the whole page looking like it is failing at something.

A value the EDS declares as text is printed as the word it is. The bus
carries a device name as bytes like everything else, and read as a number
those bytes are nineteen digits of nothing.

Write-only objects are skipped by a box read and a page read — the SDO
could only abort, and a row of aborts in the log reads as a fault when it
is the EDS telling the truth. They have no ⟳ of their own either: a
button whose only possible outcome is an error in the log is worse than
no button. The column it sat in stays empty rather than closing up, so
the labels in a box still line up.

A command word is read by watching what it does. A device that acts on
each bit differently answers in its status, and the status is usually the
object next door and usually in a PDO — so the box shows the result a
frame later without anybody reading the object that caused it.

## What a panel does not do

There is deliberately no way to write display logic into a file, and
arbitrary markup never will be. A device's own test functions are not in
the format yet. A picture — a front-panel mirror, LEDs, buttons — is a
different hook that already exists (`device_panels()`); this one is for
values.
