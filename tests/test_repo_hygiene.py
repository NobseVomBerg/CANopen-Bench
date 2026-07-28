"""Repository invariants: what belongs in the core and what belongs in a
package on top of it.

The core is device-neutral. Object addresses, enum tables, screen names
and device families are a firmware's own vocabulary, and they enter the
tool through a plugin package and its headers (`canopen_bench/symbols.py`
parses them; `docs/extending.md` describes the hooks). Everything in this
repository — module docstrings, docs, examples, tests — therefore uses
invented names, the same way the docs use invented vendors.

That is easy to state and easy to forget in a hurry, so it is a test
rather than a review habit. It cannot work from a list of names; it
matches on *shape* instead, the C enum-member style headers are written
in, and allows the invented vocabulary listed below. It also refuses
committed headers, since the core ships the parser and never a header.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: C enum members are written like this in every header we have seen
_SYMBOL = re.compile(r"\be[A-Z][A-Za-z0-9]*_[A-Za-z0-9_]+\b")

#: the invented vocabulary this repository may use. Extend it only with
#: names that mean nothing to anyone.
ALLOWED = {
    # this file's own prose and samples — it scans itself, deliberately
    "eSomething_SomethingElse", "eFooBar_SomeScreen",
    "eSampleIndex_SomeInputData", "eSampleScreen_SomeMenuName",
    "eSample1234_SomeState",
    # object indices / sub-indices in the docs and the parser's examples
    "eObjIdx_LampControl", "eObjIdx_MotorCurrent", "eObjIdx_Lamp",
    "eObjIdx_Process", "eObjIdx_Foo", "eIdx_Button",
    "eSubProcess_Status", "eSub_A", "eSub_B", "eSub_C", "eSub_D", "eSub_E",
    "eSub_F", "eSub_Tension", "eObjIdx_Buttn",  # deliberate typo in a test
    # value tables
    "eLamp_Off", "eLamp_Blinking", "eLampState_GreenOff", "eLampState_BlueBlinking",
    "eLampState_RedOff", "eKey_Up", "eKey_Down", "eKey_Enter",
    "eMode_Off", "eMode_Run", "eA_One", "eB_First", "eX_A", "eX_Bad", "eX_Good",
    "eX_AlsoGood", "eFlags_First", "eFlags_Second", "eFlags_Fourth",
    "eStatus_Running", "eLamp_On",
}

#: directories worth scanning — everything a commit can add to this repo
_SCAN = ("canopen_bench", "docs", "tests", "examples", ".claude", ".github")
_SUFFIXES = {".py", ".md", ".js", ".yaml", ".yml", ".toml", ".txt", ".css", ".html"}


def _files() -> list[Path]:
    out = [p for name in _SCAN for p in (ROOT / name).rglob("*")
           if p.is_file() and p.suffix in _SUFFIXES]
    out += [p for p in ROOT.glob("*") if p.is_file() and p.suffix in _SUFFIXES]
    return [p for p in out if "vendor" not in p.parts]  # bundled preact/htm


def test_no_firmware_symbol_names_anywhere_in_the_repo():
    found: list[str] = []
    for path in _files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for hit in _SYMBOL.findall(line):
                if hit not in ALLOWED:
                    found.append(f"{path.relative_to(ROOT)}:{lineno}: {hit}")
    assert not found, (
        "firmware symbol names that do not belong in a device-neutral core:\n  "
        + "\n  ".join(found)
        + "\n\nIf this comes from a real device, it belongs in the plugin package "
          "for that device — replace it here with an invented example. If it is "
          "invented, add it to ALLOWED in this file."
    )


def test_no_c_headers_are_committed():
    """Headers are a device's own source. The core ships the parser for
    them (canopen_bench/symbols.py) and never a header."""
    headers = [p.relative_to(ROOT) for p in ROOT.rglob("*.h")
               if ".git" not in p.parts and "node_modules" not in p.parts]
    assert not headers, f"C headers belong in a plugin package, not here: {headers}"


@pytest.mark.parametrize("sample", [
    "eSampleIndex_SomeInputData = 0x1234,",        # an index table member
    "    eSampleScreen_SomeMenuName,",             # a screen name, implicit value
    'assert bench.symbols.value("eSample1234_SomeState") == 0x15',  # digits in the table
])
def test_the_shape_check_covers_how_headers_are_written(sample):
    """The check is worth exactly as much as its regex, so the shapes it
    looks for are asserted against the shapes headers actually use.

    The samples are invented, and the assertion is on the regex rather
    than on ALLOWED — they have to be allowed, since the scan above reads
    this file too, and a check that has to skip its own file to work is
    not a check.
    """
    assert _SYMBOL.findall(sample), f"the shape check missed {sample!r}"
