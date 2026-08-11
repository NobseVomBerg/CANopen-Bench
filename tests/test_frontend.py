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


def test_the_filter_chip_memoises_its_select():
    """Preact clears and rewrites ``option.value`` while diffing an
    ``<option>``'s text child, on every render, changed or not. The page
    re-renders on every state snapshot, so an open native dropdown is
    rebuilt ten times a second and Chromium throws the highlight back to
    the current selection — you cannot pick anything unless you click
    inside one tick.

    Returning the identical vnode while the chip's inputs are unchanged
    makes Preact skip the subtree. Nothing else in the suite would notice
    if that came back out: the file still parses, every Python test still
    passes, and the filter is simply unusable again.
    """
    src = (STATIC / "app.js").read_text(encoding="utf-8")
    start = src.index("function FilterChip(")
    body = src[start:src.index("\nfunction ", start + 1)]
    assert "useMemo" in body, "FilterChip must memoise, see this test's docstring"
    # the options arrive as a fresh array every tick, so the dependency has
    # to compare their contents — the array itself never matches
    assert "options.join(" in body


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
