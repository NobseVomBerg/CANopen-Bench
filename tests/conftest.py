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
    monkeypatch.setattr("canopen_bench.core.load_plugins", lambda: [])


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
    if bench.db.eds_count() == 0:
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


def connect_and_scan(bench: Bench) -> None:
    """Connect + scan with the scan delay shrunk so tests stay fast."""
    if not bench.connected:
        bench.dispatch("connect_toggle", {})
    orig = core_mod.SCAN_DELAY_S
    core_mod.SCAN_DELAY_S = 0.02
    try:
        async def run():
            bench.dispatch("scan", {})
            await asyncio.sleep(0.15)
        asyncio.run(run())
    finally:
        core_mod.SCAN_DELAY_S = orig
