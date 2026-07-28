"""Guard: no real device's names may reach this repository.

The core is public and vendor-neutral. Device families, firmware symbols
and screen names belong to whoever builds the devices, and they live in
plugin packages — a private one, if the manufacturer's names are not
public. A display and four buttons are unremarkable; `eFooBar_SomeScreen`
is not, because it identifies whose device the bench is talking to.

This test cannot hold a list of forbidden names — writing them down here
would be the leak. It matches on *shape* instead: the C enum-member style
these headers use, `eSomething_SomethingElse`. Everything the core is
allowed to say is listed below, and it is all invented. Anything else
fails, and the message says what to do about it.

The counterpart lives in the plugin repository, where the real names are
allowed to exist: a script there checks a core checkout against the
symbols its own headers actually define. This test is the cheap half that
runs on every push; that one is exact.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: C enum members are written like this in every header we have seen
_SYMBOL = re.compile(r"\be[A-Z][A-Za-z0-9]*_[A-Za-z0-9_]+\b")

#: everything the core may name — all of it invented for documentation and
#: tests. Extend this list only with names that mean nothing to anyone.
ALLOWED = {
    # this file's own prose and samples — it scans itself, deliberately
    "eSomething_SomethingElse", "eFooBar_SomeScreen",
    "eLeakIndex_SomeInputData", "eLeakScreen_SomeMenuName", "eLeak1234_SomeState",
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


def test_no_foreign_firmware_symbols_anywhere_in_the_repo():
    found: list[str] = []
    for path in _files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for hit in _SYMBOL.findall(line):
                if hit not in ALLOWED:
                    found.append(f"{path.relative_to(ROOT)}:{lineno}: {hit}")
    assert not found, (
        "firmware symbol names that do not belong in the public core:\n  "
        + "\n  ".join(found)
        + "\n\nIf this is a real device's symbol, it belongs in a plugin package, "
          "not here — replace it with an invented example. If it is invented, add "
          "it to ALLOWED in this file."
    )


def test_no_c_headers_are_committed_to_the_core():
    """Headers are a device's own source. The core ships the parser for
    them (canopen_bench/symbols.py) and never a header."""
    headers = [p.relative_to(ROOT) for p in ROOT.rglob("*.h")
               if ".git" not in p.parts and "node_modules" not in p.parts]
    assert not headers, f"C headers belong in a plugin package, not here: {headers}"


@pytest.mark.parametrize("sample", [
    "eLeakIndex_SomeInputData = 0x1234,",          # an index table member
    "    eLeakScreen_SomeMenuName,",               # a screen name, implicit value
    'assert bench.symbols.value("eLeak1234_SomeState") == 0x15',  # digits in the table
])
def test_the_guard_would_catch_a_real_symbol(sample):
    """The guard is worth exactly as much as its regex, so check the shape
    it looks for against the shapes these headers actually use.

    The samples are invented on purpose, and the assertion is on the regex
    rather than on ALLOWED — they have to be allowed, since the scan above
    reads this file too. An earlier version used real symbols "as a
    realistic sample" and leaned on the scan skipping its own file, which
    is the leak this exists to prevent, hidden inside the guard.
    """
    assert _SYMBOL.findall(sample), f"the shape check missed {sample!r}"
