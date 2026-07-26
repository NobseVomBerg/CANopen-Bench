# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — unreleased

First public release. Everything before this point was internal
development; the version counts from here.

### Added

- Web UI (FastAPI + Preact) with live state snapshots over WebSocket:
  Setup, Objects, Trace, Tests, Machine Control pages
- Hardware backends via python-can: IXXAT VCI4 (the adapter this was
  built on), PCAN (wired up, never run against a device here) and
  CPC-USB — plus a hardware-free demo mode that simulates devices from
  real EDS files
- Object browser from EDS catalogs with identity matching (0x1018),
  favorites, and typed RAW rows (SDO / PDO / NMT, per-row node-id)
- Trace monitor: 200k-frame ring buffer, server-side class/node filters,
  µs timestamps, PDO/EMCY row coloring, capture save/load
- EMCY decoding: CiA-301 error-code texts and error-register bits in the
  trace and state log (badge counts real emergencies); vendor-specific
  codes contributable via the `emcy_codes` plugin hook
- Trace statistics view: frame counts and frames/s per COB-ID with
  share bars, per-class totals, 60 s bus-load history, error-frame
  counter
- PDO payload decoding: signals named and unpacked via the EDS default
  mapping (0x1600/0x1A00 series), bit-exact with sign extension; demo
  devices publish TPDO1 according to their EDS
- Cyclic transmit: PDO RAW rows can send on a per-row cycle time
  (server-side, never auto-resumed after a restart) and a SYNC
  producer with configurable interval runs from the Setup page; demo
  devices apply received RPDOs per their EDS mapping
- Signal plot: a third Trace view charts up to 4 selected object
  values over time, sourced from the same SDO/PDO decoding the trace
  already computes — no separate polling
- Test executor for YAML test cases (format v2: registers, jumps,
  arithmetic, `wait_for`, manual steps, `lss_assign`) with reports
- Machine Control: expected-state verification with adopt/teach flow;
  an operator-initiated teach addresses across the whole address range
  and adopts the freshly addressed bus as the new expected state;
  continuous heartbeat-loss monitoring of the adopted devices while
  active, configurable timeout
- Editable object values in the object table and favorites panel
  (stage a value, then Write sends it), resizable favorites panel;
  SDO writes are sized to the EDS-declared object width
- Multi-workspace mode under `data/` — each workspace is a complete,
  self-contained configuration (db, EDS folder, traces, test cases,
  results)
- Plugin system (`canopen_bench.plugins` entry-point group, 12 hooks)
  for vendor hardware, device families and custom flows; in
  multi-workspace mode, plugin packages (`.whl`) install and activate
  straight from Setup > Extensions, no shell or restart needed
- `plugins/bench-cpcusb/` — CPC-USB/ARM7 support as a worked reference
  for what a plugin package looks like: a python-can driver over pyusb
  plus the bench adapter card, its own tests, its own `PROTOCOL.md`,
  MIT. A separate distribution that happens to share this repository —
  `pip install canopen-bench` does not bring it. CI installs it and
  re-runs the core suite with it present, so the core stays provably
  usable plugin-free
- `THIRD-PARTY-NOTICES.md`, declared as a license file so it travels
  with every wheel, sdist and Docker image: attribution for the bundled
  Preact (MIT) and htm (Apache-2.0), the LGPL-3.0 note for python-can
  that matters when redistributing an image, and the trademark position
  — CANopen® and CiA® belong to CAN in Automation e.V., this is an
  independent project and not a CiA conformance test tool
- `Dockerfile` for the container workflow the README describes
