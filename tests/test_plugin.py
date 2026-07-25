"""CpcUsbPlugin — the adapter-card half of CPC-USB/ARM7 support: entry-point
discovery, and the card/backend mapping landing where the core consumes it.
Mirrors the style of bench-vendor's own test_vendor_plugin.py.
"""
from __future__ import annotations

from bench_cpcusb import CpcUsbPlugin

from canopen_bench.core import Bench
from canopen_bench.db import Db
from canopen_bench.plugin import load_plugins


def test_entry_point_discovers_cpcusb_plugin():
    assert any(isinstance(p, CpcUsbPlugin) for p in load_plugins())


def test_bench_aggregates_cpcusb_adapter_card(tmp_path):
    bench = Bench(Db(tmp_path / "bench.db"), plugins=[CpcUsbPlugin()])
    assert bench.adapter_cards[0]["key"] == "cpc"
    assert bench.adapter_cards[0]["label"] == "CPC-USB / ARM7"
    # routable to the python-can interface the driver half registers
    assert bench._hw_bus._backends["cpc"] == ("cpcusb", None)
