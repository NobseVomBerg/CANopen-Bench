# Contributing

Thanks for your interest in CANopen-Bench. This file describes how the
project is developed, so that anyone reading the code can follow what it
expects of itself. It is not an invitation to send patches.

**Pull requests from outside are not accepted.** Reviewing code is work
this project has no capacity for, and a patch nobody can review is worse
than no patch. That is a decision about time, not about the quality of
what you might send.

What is open instead:

- **Inside the company:** commit access is granted personally. Ask.
- **From outside, with an idea:** open an issue and say what you need.
  Ideas, hardware reports and "this does not work with my adapter" are
  genuinely useful and cost nobody a review.
- **From outside, with the time to build it:** clone the repository and
  take it wherever you need. That is what the licence is for. Nothing is
  planned to be backported, so build without waiting for anyone here.

The rest of this file is for whoever holds commit access.

## Dev setup

```bash
git clone https://github.com/NobseVomBerg/CANopen-Bench.git
cd CANopen-Bench
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
canopen-bench            # http://127.0.0.1:8000 — pick "Demo" as adapter
```

Demo mode simulates devices from real EDS files — the bundled one is
`canopen_bench/seed/DemoDevice.eds` — so the whole flow (scan, object
browser, trace, test runs) works without an adapter.

## Before you push

```bash
ruff check .
pytest tests/
```

Both must be clean. CI runs the suite on Python 3.10, 3.11 and 3.12 —
3.10 is the one that catches you, since it predates `tomllib` and other
recent standard-library additions. Worth a local run on the oldest
version before pushing anything that touches imports. If you touch the plugin API
(`canopen_bench/plugin.py`), check it against the real plugin package in
[`plugins/cob-cpcusb/`](plugins/cob-cpcusb/) — CI installs it and
runs its tests, and also re-runs the core suite with it installed to
prove the core still works plugin-free.

New behavior needs a test next to it in `tests/`. The suite runs entirely
against the demo bus; look at `tests/conftest.py` for the fixtures
(`connect_and_scan`, seed EDS files).

## Versioning

There are no tagged releases: `main` is the current state. The version in
`pyproject.toml` is what identifies that state, which is what makes a bug
report answerable — so it moves **once per merge into `main`, and only
when the tool's own code changed.**

Once per merge, not per commit: a branch lands as a single state on
`main` no matter how many commits it took to get there. Bump it in the
branch, before merging.

Only for the tool's code — `canopen_bench/**`, or dependencies and
metadata in `pyproject.toml` that change what gets installed. Not for
changes to tests, documentation, CI or `CLAUDE.md`: they leave the
running tool byte-for-byte identical, and a version that moved without
the tool moving tells a bug reporter nothing.

| Position | When | Example |
|---|---|---|
| third | the default for a change to the tool | 1.0.4 → 1.0.5 |
| second | a larger change, or anything that makes an existing workspace database or capture file need migrating | 1.0.7 → 1.1.0 |
| first | a redesign, or a break that needs a manual step from the user | 1.4.2 → 2.0.0 |

Bumping the second or first position resets the ones after it. The
`pyproject.toml` entry is the only place to change — `canopen_bench.__version__`,
`core.VERSION` and the version in the UI all read from it, and a test
enforces that (`test_version_has_one_source_of_truth`).

Two branches at once break this without either of them doing anything
wrong. Both fork off 1.0.9, both bump to 1.0.10, and both are right when
they are pushed — each is above the main it forked from, and CI on each
is green. Whichever merges second finds the same line on both sides, git
takes it without a conflict, and two different states of the tool ship
as 1.0.10. Nothing says so afterwards.

So the branch is not where this is checked.
`test_the_version_moves_when_the_tool_does` anchors on the **merge**: a
merge commit that touches `canopen_bench/**` (or what `pyproject.toml`
installs) must carry a version above the one its first parent had — the
main it is merging into, whatever that has become in the meantime. If
you merge and CI on `main` goes red with that test, bump and amend the
merge. Merges that touch only tests, docs or CI need no bump and the
test does not ask for one.

`CHANGELOG.md` is not a log of every bump. It gets a dated section per
second-position change, summarising what happened since the last one.

Plugin packages version themselves independently — `plugins/cob-cpcusb/`
has its own `pyproject.toml`, and most changes over time are expected to
land in plugins rather than in the core. A plugin bump never touches the
core version, and vice versa.

## What goes where

- **Neutral core** (`canopen_bench/`): CiA-301 behavior, UI, test
  executor. Nothing vendor- or device-family-specific.
- **Plugins**: vendor hardware, device families, custom flows — see
  `docs/extending.md`. If your change only matters for one manufacturer's
  devices, it is probably a plugin.
- **Docs**: `README.md` (user-facing), `IMPLEMENTATION.md`
  (architecture), `docs/ablaeufe/` (test-sequence specs, German). Keep
  docs in sync with code changes — stale docs are treated as bugs here.

## Architecture in one paragraph

All state lives in `Bench` (`canopen_bench/core.py`). The frontend is a
thin renderer: it receives full state snapshots over a WebSocket and
sends actions to `POST /api/action`, which lands in `Bench.dispatch()`.
Hardware is abstracted behind `BusInterface`
(`canopen_bench/bus/interface.py`); `EdsDemoBus` is the hardware-free
implementation. Details in `IMPLEMENTATION.md`.

## Style

`ruff` enforces the baseline (see `pyproject.toml`). Beyond that: match
the surrounding code, comment constraints rather than mechanics, and
keep the frontend free of business logic — decisions belong in `Bench`.
