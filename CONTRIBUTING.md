# Contributing

Thanks for your interest in CANopen-Bench. Small fixes, hardware reports
and plugin experiments are all welcome — you do not need CAN hardware to
contribute, demo mode covers most of the tool.

## Dev setup

```bash
git clone https://github.com/NobseVomBerg/CANopen-Bench.git
cd CANopen-Bench
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
canopen-bench            # http://127.0.0.1:8000 — pick "Demo" as adapter
```

Demo mode simulates devices from real EDS files (`examples/eds/`), so the
whole flow — scan, object browser, trace, test runs — works without an
adapter.

## Before you open a PR

```bash
ruff check .
pytest tests/
```

Both must be clean. If you touch the plugin API
(`canopen_bench/plugin.py`), sanity-check the change against a real
plugin package too — e.g.
[`bench-cpcusb`](https://github.com/NobseVomBerg/bench-cpcusb), a
GPL-licensed CPC-USB adapter plugin.

New behavior needs a test next to it in `tests/`. The suite runs entirely
against the demo bus; look at `tests/conftest.py` for the fixtures
(`connect_and_scan`, seed EDS files).

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
