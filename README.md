# CANopen Bench

Web tool to test and control CANopen devices on a hardware bench — scan,
object access, system tests, firmware download and a live CAN trace,
driven by the devices' EDS files. Python backend (Starlette +
canopen/python-can), no-build frontend (Preact/HTM), one self-contained
workspace folder per bench.

![Live CAN trace (dark mode): SDO transfers decoded against the EDS, heartbeats, state log](docs/img/trace.png)

## Try it — no hardware needed

```bash
pip install .
canopen-bench                  # → http://127.0.0.1:8000
```

No virtualenv needed to just run it, and no `-e` — that installs the
package for editing, which is what `## Development` below is for.

`canopen-bench` is a shortcut the install writes; the module runs just as
well by itself, from the repository root:

```bash
python -m canopen_bench                       # same thing, same options
python -m canopen_bench --port 8001 --host 0.0.0.0
```

Two reasons to prefer it. It runs *this* directory rather than whatever
`canopen-bench` happens to point at, which matters as soon as a second
copy or a second virtualenv is around. And it needs only the
dependencies, not the package itself: `pip install starlette uvicorn
websockets canopen python-can pyyaml` (the `dependencies` list in
`pyproject.toml`) and a clone is enough — worth knowing on a bench
machine where you would rather not install anything.

Note the underscore: the distribution is `canopen-bench`, the module is
`canopen_bench`. `python -m canopen-bench` cannot work — a hyphen is not
valid in a Python module name.

Pick **Demo mode** on the Setup page, **Connect**, **Scan**: virtual
devices are generated from the bundled `canopen_bench/seed/DemoDevice.eds`
(installed into
the workspace on first run). SDO reads/writes are answered from the EDS
object dictionary — including realistic aborts on out-of-range writes —
and every access shows up in the Trace like real bus traffic.

![Setup page: bus interface, EDS registry with identity matching, machine control](docs/img/setup.png)

## Features

- **Setup** — adapter/bitrate/scan-range, EDS registry with identity
  matching (0x1018), machine control: adopt the bench's expected state,
  scan & verify against it, re-address via exchangeable flow files.
- **Objects** — EDS object browser with read/write, favorites
  (auto-saved), typed RAW rows (SDO / PDO / NMT with per-row node-id),
  last-known values per device serial number.
- **Tests** — declarative YAML test cases
  (`docs/ablaeufe/testfall-format.md`, examples in
  `examples/testcases/`) executed for real against the selected device.
  A run leaves HTML behind: one file per case, one summary, and a JSON
  sibling. From those, "Overview by variant" folds the last so many days
  into one collapsible section per hardware variant — runs, successes
  and last verdict per case — which is how you tell one broken variant
  from a broken device family.
- **Bench instruments** — a remote-controllable lab power supply is set
  from the Setup page and from a test case (`psu` step), which is what
  keeps an under-voltage case automated instead of asking an operator to
  turn a knob. Serial port, `pip install ".[serial]"`; drivers in
  `canopen_bench/instruments/`.
- **SWDL** — firmware download; protocols are manufacturer-specific and
  ship as vendor extension packages (see the note on the page).
- **Trace** — live monitor, newest frame at the top, scrollable back
  through the whole 200k-frame ring buffer or an opened capture rather
  than a screenful. Class + device filters, ms/µs timestamps, capture
  save/load, and autosave — every recorded frame written to a capture
  file as it arrives, kept for two weeks, for the endurance runs that are
  longer than the ring is deep.

## Hardware

Which adapters the tool talks to, via python-can. Names are the
manufacturers' — see the trademark note in `THIRD-PARTY-NOTICES.md`;
this project is not affiliated with any of them, and the table says what
has been exercised, not what is warranted.

| Adapter | Driver | State |
|---|---|---|
| IXXAT USB-to-CAN | VCI4 (Windows) | in regular use on the author's bench |
| PCAN-USB | PCANBasic | wired up, never run — no such adapter here |
| Vector VN1600 (VN1610/VN1630) | XL driver (Windows) | run against a VN1630 |
| Demo mode | — | no hardware, EDS-driven simulation |
| other adapters (e.g. CPC-USB) | via plugin packages | separate install |

Everything above demo mode goes through python-can's own backends, so
adapter support tracks python-can. The Vector entry exists because plain
CAN on a VN1600 needs only Vector's freely downloadable XL driver — the
CANoe/CANalyzer licences are for their applications, not for talking to
the hardware — and a lot of that hardware outlives the software budget
it was bought with. Bring-up checklists live in
`docs/ablaeufe/A-01-verbinden.md` and `A-02-scan.md`.

## Workspaces

Every subfolder of `./data` (or `$CANOPEN_BENCH_DATA`, e.g. a mounted
Docker volume) is one workspace: its own sqlite db, EDS files, trace
captures and flow files. Create and switch workspaces on the Setup page;
copy a folder to clone or back up a bench. `--db <path>` remains as an
expert override for a single explicit database file.

## Docker

```bash
docker build -t canopen-bench .
docker run -p 8000:8000 -v canopen-bench:/data canopen-bench
```

All workspaces live on the `/data` volume. Demo mode works out of the
box; USB CAN adapters need the device passed through
(`--device=/dev/bus/usb`). The tool has no authentication — keep the
port on a trusted network (see `SECURITY.md`).

## Extending

Vendor- and device-specific functionality (adapter cards, EDS seeds,
firmware catalogs, addressing procedures with session identities, demo
protocol simulations, trace decoders, EMCY code tables, custom
test-step primitives, SWDL strategies) lives in separate pip packages
registered under the
`canopen_bench.plugins` entry-point group — installing one activates it.
In multi-workspace mode this needs no shell at all: Setup > Extensions
installs a plugin package (`.whl`) straight from the browser, active
immediately. Guide with hook table and a minimal example:
`docs/extending.md`; API: `canopen_bench/plugin.py`. A complete worked
example ships in this repo under
[`plugins/cob-cpcusb/`](plugins/cob-cpcusb/) — python-can driver plus
adapter card, its own tests, MIT. It is a separate distribution, not part
of the `canopen-bench` package: install it only if you have that
adapter.

## Documentation

- `IMPLEMENTATION.md` — architecture and design decisions
- `CONTRIBUTING.md` — dev setup, test conventions, what goes where
- `docs/extending.md` — plugin guide
- `docs/ablaeufe/` — operational sequence specs (German)
- `docs/ablaeufe/testfall-format.md` — YAML test-case format

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"         # editable + pytest/ruff
pytest                          # core suite
```

`CONTRIBUTING.md` has the rest: what CI checks, where things belong, the
versioning rule.

Behind a TLS-inspecting corporate proxy, pip fails with
`CERTIFICATE_VERIFY_FAILED` on pypi.org: the proxy's CA is trusted by the
operating system but not by the certificate bundle Python ships. On
pip 23.2+, `--use-feature=truststore` makes pip use the OS store
instead. On older pip that route is closed — it needs the `truststore`
package, which also comes from pypi.org — so export the OS root
certificates to a PEM and point pip at it with `--cert`, or set it once
with `pip config set global.cert <file>`. Either way certificate
verification stays on.

## License & author

Core application: MIT (`LICENSE`). Plugin packages are licensed
independently — vendor plugins proprietary, `cob-cpcusb` MIT.
Bundled and depended-on third-party components, and the trademark note
(CANopen® and CiA® belong to CAN in Automation e.V.; this is not a
CiA-certified or CiA-affiliated tool), are listed in
[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).

Created by NobseVomBerg · [unsix.de](https://unsix.de) ·
[GitHub](https://github.com/NobseVomBerg/CANopen-Bench)
