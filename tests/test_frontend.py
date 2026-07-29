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
