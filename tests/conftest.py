"""Shared test helpers."""
from __future__ import annotations

import asyncio

import pytest

import canopen_bench.core as core_mod
from canopen_bench.core import Bench


@pytest.fixture(autouse=True)
def _no_installed_plugins(monkeypatch):
    """Keep the suite hermetic: Bench(plugins=None) must not discover
    whatever bench plugins happen to be installed in this environment."""
    monkeypatch.setattr("canopen_bench.core.load_plugins", lambda **kw: [])


# Registry rows the suite historically relied on being seeded (used to come
# from data.SEED_EDS_FILES, now neutralized — see write_seed_eds_files /
# seed_test_registry below).
TEST_EDS_ROWS = [
    ("dut_alpha_v2.eds", "DUT_ALPHA", "0x4D2·0x1150", "DTA", True),
    ("dut_beta_v7.eds", "DUT_BETA", "0x4D2·0x1160", "DTB", False),
    ("dut_gamma_v5.eds", "DUT_GAMMA", "0x4D2·0x1170", "DTG", True),
]


def seed_test_registry(bench: Bench) -> None:
    """Seed the historic EDS registry rows (TEST_EDS_ROWS) into an empty
    registry — data.SEED_EDS_FILES is neutralized, so tests seed their own."""
    if bench.db.eds_count(devices_only=True) == 0:
        for file, dev, ident, code, enabled in TEST_EDS_ROWS:
            bench.db.eds_add(file, dev, ident, code, enabled)

# Minimal but valid EDS used to give the seeded registry entries real files
# on disk, so demo mode generates DUTs for them. Carries the 0x2050 variant
# object (U8, default 0 -> reads as "0x00") the variant-detection tests use.
SEED_EDS = """\
[FileInfo]
FileName=seed.eds
FileVersion=1
FileRevision=1
EDSVersion=4.0

[DeviceInfo]
VendorName=Seed Vendor GmbH
VendorNumber=1234
ProductName=SEED_DEV
ProductNumber=4432
RevisionNumber=1

[1000]
ParameterName=Device type
ObjectType=0x7
DataType=0x0007
AccessType=ro
DefaultValue=0x00050195

[2040]
ParameterName=Product identification
ObjectType=0x9
SubNumber=2

[2040sub0]
ParameterName=Highest sub-index
ObjectType=0x7
DataType=0x0005
AccessType=ro
DefaultValue=1

[2040sub1]
ParameterName=Product code
ObjectType=0x7
DataType=0x0007
AccessType=ro
DefaultValue=0x00260001

[2050]
ParameterName=Variant id
ObjectType=0x7
DataType=0x0005
AccessType=ro
DefaultValue=0

[2000]
ParameterName=Writable counter
ObjectType=0x7
DataType=0x0007
AccessType=rw
DefaultValue=42
"""


def write_seed_eds_files(bench: Bench) -> None:
    """Seed the historic registry rows (TEST_EDS_ROWS) if the registry is
    still empty, then write an EDS file for every enabled registry entry
    that has none — the demo bus only generates DUTs for entries whose
    file parses."""
    seed_test_registry(bench)
    for e in bench.db.eds_list():
        if e["enabled"] and not (bench.db.eds_dir / e["file"]).exists():
            bench.db.eds_write_file(e["file"], SEED_EDS)


def connect_and_scan(bench: Bench, timeout: float = 10.0) -> None:
    """Connect + scan with the scan delay shrunk so tests stay fast.

    Waits for the scan to actually finish (bench.scan_busy) rather than
    sleeping a fixed span: a loaded CI runner is slower than a laptop, and
    a fixed wait turns that into "no devices found" in a test that has
    nothing to do with timing.
    """
    if not bench.connected:
        bench.dispatch("connect_toggle", {})
    orig = core_mod.SCAN_DELAY_S
    core_mod.SCAN_DELAY_S = 0.02
    try:
        async def run():
            bench.dispatch("scan", {})
            # wait on the task the dispatch spawned, not on the clock:
            # scan_busy is set inside the coroutine, so polling it right
            # after the dispatch races the scheduler
            pending = set(bench._tasks)
            if pending:
                done, still = await asyncio.wait(pending, timeout=timeout)
                assert not still, "scan did not finish in time"
        asyncio.run(run())
    finally:
        core_mod.SCAN_DELAY_S = orig


def drive_verify(bench: Bench, timeout: float = 10.0) -> None:
    """Dispatch mc_verify and wait for its async scan+compare to finish.

    Waits on the bench's own busy flag rather than on a stopwatch. A fixed
    sleep is the shape of flake that only ever fires in CI: 0.3 s was
    enough on a laptop and not on a loaded runner, where the scan had not
    finished yet and the test failed as "no devices found" — in a test
    about finding devices. Same bound as connect_and_scan, and for the
    same reason.
    """
    orig = core_mod.SCAN_DELAY_S
    core_mod.SCAN_DELAY_S = 0.02
    try:
        async def go():
            bench.dispatch("mc_verify", {})
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while bench.mc["busy"] and loop.time() < deadline:
                await asyncio.sleep(0.02)
        asyncio.run(go())
    finally:
        core_mod.SCAN_DELAY_S = orig
    assert not bench.mc["busy"], f"scan & verify did not finish within {timeout}s"


class FakeSupplyPort:
    """A Töllner-like serial port for tests that need the bench to have a
    power supply. Same answers as tests/test_instruments.py uses, kept
    here because the executor tests need one too.

    It remembers what was written to it, and its ``*LRN?`` answers with
    that. A fake that always read back the values it started with would
    pass every test about *sending* a setting and none about the setting
    arriving anywhere — which is the half somebody looking at the box
    actually sees.
    """

    IDN = "TOELLNER,TOE8952-60,102625,3.63-3.62"

    def __init__(self):
        self.written: list[str] = []
        self._pending = ""
        self.closed = False
        self._sel = 1                                   # SEL n, until the next one
        self._ch = {1: [5.0, 0.5], 2: [57.0, 7.0]}      # volts, amps per channel
        self._out = True

    @property
    def LRN(self) -> str:                               # noqa: N802 (SCPI's name)
        return ";".join(
            [f"SEL {n};V {v:06.2f};C {a:06.3f}" for n, (v, a) in sorted(self._ch.items())]
            + [f"EX {1 if self._out else 0}"])

    def write(self, data: bytes) -> None:
        cmd = data.decode("ascii").strip()
        self.written.append(cmd)
        for part in cmd.split(";"):
            head, _, tail = part.strip().partition(" ")
            if head == "SEL":
                self._sel = int(tail)
            elif head == "V":
                self._ch.setdefault(self._sel, [0.0, 0.0])[0] = float(tail)
            elif head == "C":
                self._ch.setdefault(self._sel, [0.0, 0.0])[1] = float(tail)
            elif head == "EX":
                self._out = tail == "1"
        self._pending = {"*IDN?": self.IDN, "*LRN?": self.LRN}.get(cmd, "")

    def readline(self) -> bytes:
        out, self._pending = self._pending, ""
        return out.encode("ascii") + b"\n"

    def close(self) -> None:
        self.closed = True
