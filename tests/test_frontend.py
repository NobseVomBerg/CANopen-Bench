"""The frontend is a file the test suite otherwise never opens.

``static/app.js`` is a single ES module full of nested ``html`` template
literals. A missing closing backtick in one of them is a *parse* error:
the module never executes, the page renders nothing at all, and every
Python test still passes — which is exactly how one shipped. So the file
gets parsed here, by a real JavaScript parser, before anything else.

Node is used rather than a hand-written scanner because the file has
regex literals, and telling a regex from a division is the part of
JavaScript lexing that nobody gets right by hand. Absent Node this skips
on a developer machine, but never in CI: a guard that quietly skips in
the one place it matters is not a guard.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "canopen_bench" / "static"


def _node() -> str | None:
    return shutil.which("node")


def test_app_js_parses_as_an_es_module(tmp_path):
    node = _node()
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is missing in CI — the frontend would go unparsed")
        pytest.skip("node not installed")
    # --check needs the module extension to parse `import` at all
    target = tmp_path / "app.mjs"
    target.write_text((STATIC / "app.js").read_text(encoding="utf-8"), encoding="utf-8")
    done = subprocess.run([node, "--check", str(target)],
                          capture_output=True, text=True)
    assert done.returncode == 0, f"static/app.js does not parse:\n{done.stderr}"


def test_the_page_loads_the_module_it_is_served(tmp_path):
    """A parse guard on a file nothing imports would guard nothing."""
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'type="module" src="/static/app.js"' in index


def test_every_dropdown_goes_through_the_one_memoised_select():
    """Preact clears and rewrites ``option.value`` while diffing an
    ``<option>``'s text child, on every render, changed or not. The page
    re-renders on every state snapshot, so an open native dropdown is
    rebuilt ten times a second and Chromium throws the highlight back to
    the current selection — you cannot pick anything unless you click
    inside one tick.

    Returning the identical vnode while the options and the selection are
    unchanged makes Preact skip the subtree. This was fixed once per
    dropdown, which meant the next dropdown someone wrote started broken
    again — and it did, twice. So there is exactly one ``<select>`` in the
    file and everything else uses it.

    Nothing else in the suite would notice if a raw ``<select>`` came
    back: the file still parses, every Python test still passes, and that
    dropdown is simply unusable.
    """
    src = (STATIC / "app.js").read_text(encoding="utf-8")
    assert src.count("<select") == 1, "a dropdown outside OptionSelect — see this docstring"
    start = src.index("function OptionSelect(")
    body = src[start:src.index("\nfunction ", start + 1)]
    assert "useMemo" in body
    # the options arrive as a fresh array every tick, so the dependency has
    # to compare their contents — the array itself never matches
    assert "JSON.stringify(options)" in body
    assert "[key," in body and "[options," not in body
    # …and the handler through a ref, or the memo pins the first render's
    # closure and the dropdown acts on state that has since moved on
    assert "useRef" in body and "pick.current" in body


def test_a_typed_number_is_decimal_unless_it_says_0x():
    """One rule for a number a person types, and the box says it. The base
    chip used to decide it too, so with the table in hex a typed 12345678
    came back as 0x12345678 — the same digits, a different number, and the
    hint beside it promised the opposite ("hex needs 0x").

    Both boxes take a number, so both carry the same sentence, and the
    chip is beside each of them rather than only above the table.
    """
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "hex needs 0x" not in app, "the old hint read as if it were the rule"
    assert "0x… for hex" in app and "otherwise decimal" in app
    assert "Typing is unaffected" in app     # the chip says what it does not do
    assert app.count("numberHint") == 3      # the definition and both boxes
    assert app.count("baseChip") == 3        # the definition and both headers


def test_the_trace_toolbar_offers_autosave():
    """Autosave has no visible effect on the page it runs on — the trace
    looks the same either way — so the chip is the only thing that says
    whether the record is being kept. Its state comes from the server
    (`trace.auto`), not from a local `useState`: a browser reloaded
    mid-run must not claim autosave is off while the file goes on
    growing.
    """
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "send('trace_autosave')" in app
    assert "s.trace.auto" in app
    assert "auto.on" in app and "auto.file" in app  # chip state and the open segment
    assert "ring buffer" in app                     # the tooltip says why it exists
    # and what it costs: a feature that quietly eats a disk for two weeks
    # has to say so where it is switched on, not only in the docs
    assert "14 days" in app and "2 GB clear" in app
    # …and a run left alone for months has to see at a glance that nothing
    # is reaching the disk — the chip goes red, it does not go quiet
    assert "auto.warn" in app and "never switches itself off" in app


def test_the_trace_asks_the_server_by_age_whichever_way_it_reads():
    """The table can read newest first or oldest first, but the record it
    reads is stored one way — oldest first — and the server counts a window
    back from the newest row. So the row index under the scrollbar is not
    the index to ask with: reading downwards, row 0 is the newest frame in
    one direction and the oldest in the other.

    `age` is that conversion, and it has to be what reaches the fetch and
    the slice. Hand either of them the scroll index instead and newest-first
    still looks perfect — the two are equal there — while oldest-first shows
    the frames from the wrong end of the record with entirely plausible
    timestamps on them. Nothing about that looks like a bug.
    """
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "cb-trace-asc" in app, "the direction is not remembered between visits"
    assert "const age = asc ?" in app, "no conversion from scroll index to age"
    assert "trace/rows?end=${end}" in app and "Math.max(0, age - OVERSCAN)" in app, \
        "the fetch does not go through age"
    assert "live.slice(age, age + count)" in app, "the snapshot slice does not"
    assert "live.slice(start" not in app, "a slice still uses the scroll index"


def test_the_run_box_keeps_its_height_whatever_the_step_line_says():
    """The step line under the progress bar updates several times a second
    and its text is a test case's own prose, so it wrapped to two lines on
    one step and back to one on the next — and the box, and everything
    below it in that column, moved with it. It reads as a flicker, on the
    one part of the screen somebody watches during a run.

    So the line is two lines tall whatever it says: `height` reserves the
    space, the clamp stops a third line from claiming more. Drop either and
    the jitter is back — the height alone lets long text overflow into the
    box, the clamp alone lets short text shrink it.
    """
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    line = next((ln for ln in app.splitlines() if "-webkit-line-clamp" in ln), "")
    assert line, "the step line no longer clamps — long text can grow the box"
    assert "height:3.4em" in line, (
        "the step line has no reserved height — short text shrinks the box")
    assert "-webkit-line-clamp:2" in line


def test_the_trace_table_places_rows_by_the_same_height_it_draws_them():
    """The table draws only the rows on screen and positions them by index
    rather than stacking them, because an hour of bus is 200k rows and no
    browser lays that out. That makes one number load-bearing: the height a
    row is given and the height it is placed by have to be the same
    constant. Two numbers there and the scroll position drifts away from
    the frames under it — silently, and worse the further you scroll.
    """
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "const ROW_H =" in app
    assert "height:${ROW_H}px" in app           # what a row is
    assert "top:${start * ROW_H}px" in app      # where it is put
    assert "height:${total * ROW_H}px" in app   # how long the scrollbar is
    assert "/api/trace/rows" in app             # and where rows past the snapshot come from


def test_the_panel_area_is_columns_rather_than_a_grid():
    """A grid puts the boxes in rows and makes every row as tall as the
    tallest box in it, so one long box leaves a hole beside it the same
    height. On a wide screen that hole is most of the screen, which is what
    the boxes-with-nothing-under-them looked like.

    Columns have no rows to align. The page column is two field columns
    wide, so a box asking for two gets the room a one-column box gives its
    rows — the other half of the same complaint. `cols` is the most a box
    uses, not a promise it keeps in a column too narrow to hold them: the
    inner grid falls back to fewer, rather than to squeezed labels.

    auto-fill inside the box, not auto-fit: auto-fit collapses a track
    nothing lands in, so a box's last row — and every row of a one-field
    box — stretched to the full width and put its value a hundred pixels
    right of the value above it.
    """
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "columns:${PANEL_COL}px" in app
    assert "break-inside:avoid" in app, "a box could be split across columns"
    assert "const PANEL_COL = 700" in app and "const FIELD_MIN = 300" in app
    assert "repeat(auto-fill,minmax(max(${FIELD_MIN}px" in app
    assert "grid-column:span" not in app, "the old row grid is still there"


def test_every_kind_of_value_in_a_box_ends_at_one_edge():
    """A staged number, a value only read, a checkbox and a dropdown are
    four different controls in one column, and they were four different
    widths — 78, 66, 110 and 126px. The Write column is reserved for the
    whole panel rather than per box: every box works out its own width, so
    per box left the boxes that write ending short of the boxes that do
    not."""
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "const VALUE_W = " in app
    assert app.count("${VALUE_W}px") >= 4, "input, read-only, flag and enum"
    assert "anyWrite=${panel.groups.some(" in app


def test_a_unit_is_part_of_the_name_rather_than_a_column():
    """A column of its own is reserved for the whole page, so it was empty
    in every box that has no unit — and there it read as a gap left open
    between a value and its Write button rather than as a column nothing
    landed in. In the label it costs nothing where there is no unit."""
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "`${f.label} (${f.unit})`" in app
    assert "width:32px;flex:none\">${f.unit}" not in app, "the unit column is back"


def test_the_panel_view_leaves_the_favorites_out():
    """Favorites are the table's companion — a shortlist pulled out of a
    long list. The panel is already a shortlist somebody wrote down, and on
    this page width is what the boxes are short of."""
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert app.count("view !== 'panel' && html`") == 2   # divider and column


def test_a_value_nobody_can_write_is_not_offered_as_a_dropdown():
    """A select on a read-only object opens, offers the device's other
    states, and picking one changes neither the device nor the page. The
    name is shown the way a read-only number shows its number."""
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "if (!f.rw) {" in app, "the enum branch does not split on writability"
    # …and a pick from the one that is writable says which lane it was:
    # several can sit on one object, and the address alone would stage
    # whichever the core happened to list first
    assert "lane: f.lane" in app


def test_a_write_only_field_has_no_read_button():
    """The SDO could only abort. A button whose one outcome is an error in
    the log is worse than no button — and the column it sat in stays, so
    the labels of a box still line up."""
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "f.wo" in app, "the panel row does not ask whether the object can be read"
    assert "write-only, nothing to read" in app


def test_a_value_is_not_dimmed_for_being_a_minute_old():
    """Age belongs in the tooltip, which says it in words. Fading the
    number instead makes every box that has not just been read look like
    it is failing at something, and a bench reads a value once."""
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "panelAge" not in app
    assert "panelWhen" in app, "the tooltip is where age is still said"


def test_the_result_filter_is_a_chip_and_not_a_third_verb():
    """The toolbar means "narrow it down, then select what is left" — the
    chips filter, `all`/`none` select. Red cases are a filter, so `all`
    after it is the selection, and it composes with Variant and Category
    instead of a verb deciding for itself which variant's failures it
    meant."""
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'label="Result"' in app
    assert "const RESULT_WINDOWS" in app and "'failed · last run'" in app
    # …and it asks the server only for the windows the server has to read
    assert "if (v && v !== 'run') send('tests_history'" in app


def test_a_skipped_case_is_not_a_failed_one():
    """FAIL and ERROR are red; SKIP is not. Selecting a case nobody ran to
    "run the failures" would put it back for no reason."""
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "const RED = new Set(['FAIL', 'ERROR'])" in app
