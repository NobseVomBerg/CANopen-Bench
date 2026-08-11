# CANopen Bench — implementation

Architecture and design decisions. What the tool does and how to run it
is in `README.md`.

## Quick start

```bash
pip install -e .          # or: pip install starlette uvicorn websockets
python -m canopen_bench   # → http://127.0.0.1:8000
```

Options: `--host`, `--port`. Workspaces live as subfolders of `./data`
(or `$CANOPEN_BENCH_DATA`), selectable on the Setup page; `--db <path>`
is an expert override binding the app to one explicit sqlite file with
workspace switching disabled.

Tests: `pip install -e .[dev] && pytest`

## What it does

Full clickable implementation of the chosen **1a** design — all five pages,
light + dark theme (toggle in the header, persisted per browser):

- **Setup** — workspace bar (create/switch workspaces, each a subfolder
  of the data root), bus interface (IXXAT / PCAN / Vector / Demo mode;
  vendor adapters such as the CPC-USB arrive via plugin packages) + bitrate
  (applied to a running connection immediately) + address range (node-ID)
  + own node-ID — the one range that governs this bus: the scan probes
  exactly it, and an operator teach addresses up to its top node-id —,
  EDS file management (upload real .eds files — stored in the
  workspace's configurable EDS folder, parsed for device name +
  identity —, delete, checkbox multi-active, editable DUT shortcodes,
  per-EDS variant-detection object, usage count, identity-conflict
  warnings with newest-file-wins matching), machine control (adopt the
  current bus as the workspace's expected state — device count,
  session-ID, node→EDS assignments in the kv store —, scan & verify
  against it, re-addressing via exchangeable flow files — the core ships
  standard LSS (`flows/lss_standard.yaml`, untested on real hardware),
  vendor procedures like button-teach arrive with plugins — see
  docs/ablaeufe/A-03/A-05 —, continuous heartbeat-loss monitoring of the
  adopted devices while active (`core._check_heartbeats`, configurable
  timeout — deliberately MC-only, the Devices box itself never tracks
  this), startup options), an Extensions box showing loaded plugins with
  GUI install/remove of plugin packages (upload a `.whl`, active
  immediately — see "Plugin install" below), and the test configuration
  paths (defaulting into the workspace folder).
  `data/` is gitignored, so a fresh checkout ships no real .eds files; on
  the workspace's first run (`Db.is_first_run`) `Bench` installs
  `canopen_bench/seed/DemoDevice.eds` the same way an upload would
  (`Bench._seed_demo_eds`), so Demo mode has one working DUT out of the
  box. One-shot and deletable — removing it via the registry doesn't
  bring it back.
  The tool starts disconnected: pick an adapter, connect, then scan.
  **Demo mode** needs no hardware — on scan it generates virtual DUTs from
  the active (real) EDS files and serves SDO reads/writes from each file's
  object dictionary — an out-of-range write aborts like a real device would
  (EDS LowLimit/HighLimit) — so the Objects page can be used against a real
  device profile before any adapter is available. Every read/write also
  synthesizes matching request/response CAN frames for the Trace page.
- **Objects** — EDS object tree by group, SDO read/write per object:
  writable objects show an inline value input in the table and in the
  favorites panel — typing stages the value (`act_obj_set`, no bus
  traffic), Write sends it. Favorites panel (one auto-saved list per
  workspace, width user-resizable via the drag divider), typed RAW
  rows —
  SDO read/write, PDO send, NMT send, each with its own node-id, up to
  8 rows, autosaved and restored on next start —, last-known values per
  device restored from the workspace db keyed by serial number
  (0x1018:04). PDO rows can transmit cyclically (per-row cycle time in
  ms, ⟳ toggle): `core._cyclic_loop` runs beside the tick loop with ms
  granularity and also drives the SYNC producer configured on the
  Setup page. Run flags never survive a restart or disconnect — no
  surprise traffic; asyncio timing feeds devices and generates load
  but is no hardware-timed generator. While anything transmits, a
  "⟳ TX" chip in the header (visible on every page) names the active
  senders and links to their controls — transmitting is server-side
  state, so it outlives the browser tab. On the demo bus, received
  RPDOs are applied to the device per its EDS mapping
  (`EdsDemoBus._apply_rpdo`), with a simple drive model (0x606C
  follows 0x60FF). A third **Plot** view (alongside Trace/Stats)
  charts up to `PLOT_SEL_MAX` (4) signals over time
  (`Bench.plot_sel`/`plot_series`, `core._plot_sample`): the SDO and
  PDO annotators already decode numeric values, so plotting taps that
  same pipeline instead of polling separately — a signal is watched
  whether it arrives via an SDO read/write request or a PDO. Selection
  toggles from a `∿` icon next to objects in the table/favorites
  (`act_plot_toggle`, capped, persisted like favorites); each series
  keeps its last `PLOT_POINTS` (600) samples and renders independently
  Y-scaled (comparable shapes, not absolute heights, across signals
  with different units) against a shared, monotonic-clock time axis.
- **Tests** — the catalog lists the `TC*.yaml` test-case files from the
  configured TestCases folder (declarative step format, see
  `docs/ablaeufe/testfall-format.md`; demo catalog as fallback). The
  folder is re-read by the list's own **↻ reload**, and again whenever a
  run starts, so an edited YAML needs neither a rescan nor a restart; a
  selected case that an edit has just made unreadable is named in the
  state log rather than quietly left out of the run. Runs
  execute the steps for real against the selected DUT via the bus —
  NMT/SDO/heartbeat-wait/operator prompts — with per-step progress
  ("step 3/9 …"), PASS/FAIL/ERROR/SKIP verdicts, tool filter, repeats,
  stop-on-error and report history.
- **SWDL** — firmware library, SDO-serial or PDO-parallel download to the
  devices selected in the Devices box, per-device progress.
- **Trace** — the record of what the bus carried, and a view onto it.
  Recording runs whenever the interface is connected: test steps read it
  (`wait_for` with a `cob` matches against it via
  `Bench._match_traced`), so pausing the panel or opening a saved capture
  only changes what is *shown* — `Bench._trace_view` picks the source
  while `Bench.trace` keeps filling underneath. The panel itself is a
  live CAN frame monitor with decode column, RX and own TX
  frames, bus timestamps, class filters (NMT/SDO/PDO/EMCY/HB),
  pause/clear, plus a device filter (all / selected devices, broadcasts
  always visible) and an ms/µs timestamp toggle. Filtering happens
  server-side over the full retained ring buffer (200k frames,
  `core.TRACE_CAP`), so a hidden class or device never pushes visible
  frames out of the browser window (`core.TRACE_VIEW`, 400 rows —
  enough scrollback to follow a multi-step sequence like addressing
  end to end). Captures can be saved to and
  reloaded from `<workspace>/traces/*.json` (loading pauses the trace).
  Autosave (`core._autosave_write`, off by default, the setting persists)
  answers what the ring buffer cannot: an hour in, the beginning is gone,
  and an hour is shorter than the runs where something happens once. With
  it on, every drained row is appended to `traces/auto_*.jsonl` and
  flushed — one row per line, because a file being written to cannot be a
  single JSON object, and a capture cut short by a crash still has to
  read. What goes in is the *record*, unfiltered: a trace filter is a
  property of the panel. A segment starts on the first frame after a
  connect and rolls over at `core.AUTOSAVE_SEGMENT_BYTES`. Retention is
  `core.AUTOSAVE_KEEP_DAYS` (14 — the length of an endurance run, since
  the fault worth autosaving for is the one that shows up once, days in),
  bounded by `core.AUTOSAVE_FREE_BYTES` (2 GB): under that reserve the
  oldest segments go early, a segment mid-write is rolled short so the
  reserve cannot be undercut by a whole one
  (`core.AUTOSAVE_SPACE_EVERY_BYTES` between checks), and if that still
  is not enough autosave switches itself off rather than take the last of
  the disk. The newest segment is never a candidate; nor are hand-saved
  captures; every removal is logged with its reason. Free space that the
  filesystem will not report is treated as unknown, not as full. A write
  error (disk full, read-only) switches autosave off with a log line
  instead of disturbing the run. Segments are ordinary captures: they
  appear in the same list and load the same way.
  The filtered trace (full matching set, not just the browser scrollback)
  can also be exported as CSV or a SocketCAN `candump -l` log
  (`GET /api/trace/export.csv` / `/api/trace/export/candump`, plain
  downloads outside the action/WebSocket flow since they don't mutate
  state), and a `candump -l` log from another tool can be imported
  (`act_trace_import`) — parsed frames run through the same SDO/PDO/EMCY
  annotation pipeline as live traffic, just with `live=False` so historical
  frames don't feed the signal plot or bump the state log/EMCY badge, then
  get saved as an ordinary capture and loaded, indistinguishable from a
  bench-native one from that point on. Timestamps on both sides are
  relative-seconds, not real epoch — trace rows only ever keep time-of-day.
  SDO payload bytes are highlighted, PDO rows carry a green background
  graded by PDO number, EMCY rows a red one. PDO payloads are decoded
  into named signals (`core._annotate_pdo`) via the default mapping
  (0x1600-/0x1A00-series) in the EDS assigned to the node — bit-exact
  LSB-first unpacking, INTEGER types sign-extended; this assumes the
  predefined connection set and the EDS *default* mapping (reading the
  live mapping off the device would be a mapping editor's job, and no
  such editor exists yet). Demo devices publish TPDO1 per their EDS mapping
  (`EdsDemoBus._tpdo1_frame`), values consistent with SDO reads.
  EMCY frames are decoded to
  plain text (`core._annotate_emcy`): error code against the CiA-301
  table in `data.EMCY_CODES` — vendor codes merged in via the
  `emcy_codes` plugin hook — plus the error-register bits; every EMCY
  is also mirrored into the state log, which drives the EMCY badge.
  A **Stats** toggle swaps the frame table for a statistics view
  (`core._trace_stats`): cumulative frame counts and frames/s per
  COB-ID (top 40, rest aggregated), share bars, per-class totals, a
  60 s bus-load sparkline and the error-frame counter. Counters run
  since connect or trace clear; the rate window spans the last ~5 s.

An **About** page (bottom-left sidebar entry) shows project summary,
documentation pointers, author and license.

Always available: the Devices box (scan with identity read + automatic EDS
assignment, multi-select, NMT quick commands, per-device ⋮ menu with
restart/NMT/EDS assignment, and per-EDS **device commands** — special
functions like a vendor's SuperUser mode are registry data
(`db.eds_set_commands`), rendered generically as chips/badges/menu
entries; the demo device ships a neutral "Service mode" example), display
mirror, and the dockable state log with EMCY badge.

## Architecture

```
canopen_bench/
├── app.py            Starlette: serves the frontend, POST /api/action, WS /ws;
│                     multi-workspace mode — every subfolder of ./data (or
│                     $CANOPEN_BENCH_DATA) is one workspace (db + eds/traces/
│                     flows), switchable at runtime from the Setup page, the
│                     choice persists in <root>/active-workspace. An explicit
│                     --db path keeps the single-file behaviour.
├── core.py           Bench service — owns all state; async step executor for
│                     test runs, 0.8 s tick loop for trace polling + SWDL sim
├── testcases.py      YAML test-case parser/catalog (strict schema)
├── db.py             sqlite per workspace: config kv (incl. machine-control
│                     expected state), EDS registry metadata, last-known
│                     values per SN
├── data.py           neutral seed/demo catalogs (tests fallback, demo firmware)
├── plugin.py         BenchPlugin API + entry-point discovery — vendor/device
│                     extensions ship as separate packages (see
│                     docs/extending.md)
├── bus/
│   ├── interface.py    BusInterface ABC — the hardware seam
│   ├── canopen_bus.py  CanopenBus — real hardware via canopen/python-can
│   └── demo.py         EdsDemoBus — virtual DUTs from uploaded EDS files
└── static/           frontend (Preact + HTM, no build step)
    ├── index.html
    ├── styles.css    theme variables (light/dark) + hover states
    ├── app.js        pixel-faithful port of the design prototype
    └── vendor/preact-htm.module.js
```

The browser is a thin renderer: every mutation goes through
`POST /api/action {action, params}`, the server pushes full state snapshots
over the WebSocket (initial snapshot on connect, then on every change/tick).
Server restarts and multiple browsers therefore always agree on the state.

## Real hardware vs. demo mode

`core.Bench` talks to the bus exclusively through `bus.interface.BusInterface`
(connect/disconnect, scan with identity readout, NMT, SDO read/write, LSS
re-addressing, frame polling). Which implementation answers is decided by
the adapter selected on the Setup page:

- **A device whose EDS is not to hand** → `canopen_bench/seed/CiA301Base.eds`,
  a generic communication-objects catalog (0x1000, 0x1001, 0x1005,
  0x1008–0x100A, 0x1017, 0x1018) seeded into every workspace and registered
  *without* an identity, so a scan can never assign it. Assigned by hand
  through the device's ⋮ menu, for a machine controller or a foreign node
  that shares the bus. A registry row with no identity describes no device
  (`Db.eds_list(devices_only=True)`), which is also why it raises no demo
  DUT and does not count as a seeded workspace. Manual SDO/PDO/NMT rows
  never needed an EDS at all — they address a node directly.

- **IXXAT / PCAN / Vector — plus plugin adapters like CPC-USB** →
  `CanopenBus` (`bus/canopen_bus.py`), real protocol via
  canopen/python-can. IXXAT needs the VCI4 driver (Windows); Vector
  (VN1600 family) needs only the free XL driver, not a CANoe/CANalyzer
  licence, and is addressed by hardware channel index (`app_name=None`)
  so no application entry in Vector's Hardware Config is required. A vendor adapter arrives as one
  separately installed package registering two entry points: a
  python-can driver (`can.interface`) plus a bench plugin contributing
  the adapter card and key mapping (`canopen_bench.plugins`) — e.g.
  the public [`cob-cpcusb`](plugins/cob-cpcusb/)
  package carries both for the CPC-USB/ARM7. Device-family
  plugins are a separate concern (EDS/firmware seeds, addressing flow)
  and contribute no adapter card of their own.
- **Demo mode** → `EdsDemoBus` (`bus/demo.py`), no hardware: virtual DUTs
  generated from the enabled uploaded EDS files. This is the way to
  inspect the tool without an adapter — the former standalone simulator
  was removed once real hardware support landed. SDO reads/writes are
  served from the EDS object dictionary and each call also queues a
  synthetic request/response CAN frame pair, byte-for-byte matching real
  expedited SDO framing, so `core._annotate_sdo` decodes demo-mode traces
  exactly like real-hardware ones.

If the interface vanishes mid-session (adapter unplugged, driver gone),
`CanopenBus` detects it — an error listener on the python-can Notifier
catches rx-thread failures, the send paths catch driver errors — tears the
dead network down on a background thread and reports through
`BusInterface.on_lost`; `Bench` then auto-disconnects (state, device list,
log entry `BUS  connection lost — … — auto-disconnected`) and pushes the
snapshot to the browsers. Details in `docs/ablaeufe/A-01-verbinden.md`.

For tests, any `BusInterface` implementation can be injected via
`Bench(db, bus=...)` / `create_app(bus=...)`; the same goes for plugins
(`plugins=[...]`, empty list = guaranteed plugin-free, `None` = discover
installed entry points).

## Extensions (plugins)

The core is vendor-neutral under an MIT license track; everything
device-family- or manufacturer-specific (vendor adapter cards, EDS
registry seeds incl. device commands, firmware catalogs, addressing
flows, session-identity providers, demo-bus protocol simulations, trace
decoders, namespaced extra actions, custom step primitives for the flow
VM, SWDL download strategies) lives in separate pip packages registered
under the `canopen_bench.plugins` entry-point group. Installing such a package activates it — no
configuration. The concept and full hook reference are in
`docs/extending.md`. Licensing is per package, not blanket: vendor
plugins are proprietary, while [`plugins/cob-cpcusb/`](plugins/cob-cpcusb/)
— the one that lives in this repo — is MIT like the core. Sharing a
repository does not merge the distributions: it is built, versioned,
installed and licensed on its own, and `pip install canopen-bench` does
not bring it along. Check a package's own `LICENSE` before redistributing
an environment that contains it; a copyleft plugin would put obligations
on the whole environment that the core alone does not.

Simulation still lives in two service-level spots: the SWDL progress
generator (real download protocols replace it via a plugin's
`swdl_strategy()`) and the local NMT state bookkeeping between
heartbeats. Machine-control verification runs a real scan and compares
real data.

### Plugin install from the GUI

Setup > Extensions can install a plugin package without a shell: upload
a `.whl`, and it's active immediately, no server restart. Mechanism
(`Bench._install_plugin_wheel` et al., `canopen_bench/core.py`):

- `Bench.plugin_dir` is `<data root>/plugins/` (multi-workspace mode
  only — needs `workspaces_root`, so `--db` expert mode can't install
  plugins this way), added to `sys.path` once at `Bench.__init__`
  before plugin discovery, so previously installed packages are found
  on every startup, not just right after an install.
- The uploaded wheel is extracted **flat, directly into `plugin_dir`**
  — verified empirically that this is required: `importlib.metadata`'s
  standard finder only recognizes a `*.dist-info` directory as a
  *direct child* of a scanned `sys.path` entry, exactly how a real
  site-packages folder is laid out (package dir and its dist-info side
  by side). Nesting each wheel into its own per-package subfolder
  silently finds nothing — no exception, just an empty plugin list.
- Every archive member is checked to resolve inside `plugin_dir`
  before extraction (zip-slip guard) — untrusted, uploaded input.
- Because the flat layout mixes files from every installed plugin
  together, a small self-maintained manifest
  (`plugin_dir/.manifest.json`: distribution name → version + the
  top-level paths that came from its wheel) is what makes removal and
  clean upgrades possible — re-uploading the same distribution name
  removes the old version's files first.
- After extraction, `importlib.invalidate_caches()` plus
  `Bench.on_plugin_reload()` (wired in `app.py`, the same live
  Bench-rebuild pattern workspace switching already uses, just without
  writing `active-workspace`) tears down and rebuilds the Bench for the
  *same* workspace and the *same* live db connection — fresh
  `load_plugins()` discovery included, so every hook (adapters,
  addressing, demo hooks, trace decoders, emcy codes, actions, step
  types, SWDL strategy) activates without dropping workspace state.
- `act_plugin_remove` deletes a package's recorded files and reloads
  the same way.

## Abläufe (sequence specs)

The operational sequences — connect, scan, scan & verify/LSS, test-case
execution, teach addressing — are specified in German under
`docs/ablaeufe/` (template and index in its README). These documents
define the behaviour against real hardware (IXXAT/VCI4, the adapter this
was built and validated on),
record findings F-1…F-6 (all resolved) and specify the YAML test-case
format (`docs/ablaeufe/testfall-format.md`) with examples under
`examples/testcases/` that the step executor runs for real.
