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

import os
import re
import subprocess
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
    # an invented manufacturer error code, for the EMCY example in
    # docs/ablaeufe/testfall-format.md — that field is only interesting
    # with a name in it, and a real device's name belongs in its plugin
    "eErrCode_MotorStalled",
    # value tables
    "eLamp_Off", "eLamp_Blinking", "eLampState_GreenOff", "eLampState_BlueBlinking",
    "eLampState_RedOff", "eKey_Up", "eKey_Down", "eKey_Enter",
    "eMode_Off", "eMode_Run", "eA_One", "eB_First", "eX_A", "eX_Bad", "eX_Good",
    "eX_AlsoGood", "eFlags_First", "eFlags_Second", "eFlags_Fourth", "eFlags_Mask",
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


def _published(pattern: str) -> list[str]:
    """What git would carry for a pathspec: tracked, plus new and not ignored.

    Asked of git rather than of the folder, because the folder holds more
    than this repository publishes: the bench keeps its workspace inside
    its own checkout (``data/``, gitignored) and installing a plugin there
    puts that device's headers a few directories below the source. Real
    files, none of them ours to answer for — and a walk reported every one
    of them. New files stay in scope, so a header dropped in and not yet
    committed is still caught, which is when catching it helps.
    """
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files",
                          "--cached", "--others", "--exclude-standard", "-z",
                          "--", pattern],
                         capture_output=True, text=True, check=False)
    if out.returncode:
        pytest.skip(f"not a git checkout: {out.stderr.strip()}")
    return [name for name in out.stdout.split("\0") if name]


def test_no_c_headers_are_committed():
    """Headers are a device's own source. The core ships the parser for
    them (canopen_bench/symbols.py) and never a header."""
    headers = _published("*.h")
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


# -- the version identifies one state of the tool ---------------------------
#
# `main` is the release (there are no tags), so the version in
# pyproject.toml is the only thing that says *which* main a bug report is
# about. CONTRIBUTING requires it to move once per merge that changes the
# tool. Two people working in parallel break that without either of them
# doing anything wrong: both branch off 1.0.69, both bump to 1.0.70, both
# CI runs are green — each sees a version above the main it forked from.
# The second merge then finds the identical line on both sides, git takes
# it without a conflict, and two different states ship as 1.0.70.
#
# That is why the check below anchors on the merge commit rather than on
# the branch: the branch really was fine when it was pushed. It is the
# merge that has to move the number past what main already had.

def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, check=False)


def _version_at(rev: str) -> tuple[int, ...] | None:
    """The declared version at a revision, as comparable numbers."""
    out = _git("show", f"{rev}:pyproject.toml")
    if out.returncode:
        return None
    found = re.search(r'(?m)^version\s*=\s*"([^"]+)"', out.stdout)
    if not found:
        return None
    try:
        return tuple(int(part) for part in found.group(1).split("."))
    except ValueError:
        return None


def _tool_changed(base: str) -> str:
    """What changed between `base` and HEAD that the version has to answer
    for: the package itself, or the parts of pyproject.toml that decide
    what gets installed. Tests, docs, CI and CLAUDE.md leave the running
    tool byte-for-byte identical and need no bump (CONTRIBUTING)."""
    names = _git("diff", "--name-only", base, "HEAD").stdout.split()
    changed = [n for n in names if n.startswith("canopen_bench/")]
    if "pyproject.toml" in names:
        # every changed line except the version itself — a dependency, an
        # entry point or package data moves what a `pip install` produces
        body = _git("diff", "-U0", base, "HEAD", "--", "pyproject.toml").stdout
        edits = [ln for ln in body.splitlines()
                 if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
                 and not re.match(r'^[-+]version\s*=', ln)]
        if edits:
            changed.append("pyproject.toml")
    return ", ".join(sorted(changed)[:6])


def test_the_version_moves_when_the_tool_does():
    if _git("rev-parse", "--git-dir").returncode:
        pytest.skip("not a git checkout")
    parents = _git("rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    if len(parents) >= 3:
        base, what = parents[1], "the main this was merged into"
    else:
        merge_base = _git("merge-base", "HEAD", "origin/main")
        if merge_base.returncode:
            # a guard that quietly skips where it matters is not a guard
            if os.environ.get("CI"):
                pytest.fail("origin/main is missing — CI needs an unshallow checkout")
            pytest.skip("no origin/main to compare against")
        base, what = merge_base.stdout.strip(), "the main this branched from"

    here, there = _version_at("HEAD"), _version_at(base)
    if here is None or there is None:
        # a shallow checkout has the parent's hash but not its content
        if os.environ.get("CI"):
            pytest.fail(f"cannot read the version at {base[:8]} — CI needs "
                        f"a checkout with history (fetch-depth: 0)")
        pytest.skip(f"no readable version at HEAD or {base[:8]}")
    changed = _tool_changed(base)
    if not changed:
        return  # tests, docs or CI only — the running tool is unchanged
    assert here > there, (
        f"the tool changed ({changed}) but the version did not move past "
        f"{'.'.join(map(str, there))}, which is {what} ({base[:8]}). "
        f"Two states of the tool would answer to the same number — bump "
        f"pyproject.toml, see CONTRIBUTING.md under Versioning.")
