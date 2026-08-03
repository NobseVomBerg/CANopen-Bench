"""CpcUsbPlugin — the adapter-card half of CPC-USB/ARM7 support: entry-point
discovery, and the card/backend mapping landing where the core consumes it.
Mirrors the style of cob-memiro's own test_memiro_plugin.py.
"""
from __future__ import annotations

from cob_cpcusb import CpcUsbPlugin

from canopen_bench.core import Bench
from canopen_bench.db import Db
from canopen_bench.plugin import load_plugins


def test_entry_point_discovers_cpcusb_plugin():
    assert any(isinstance(p, CpcUsbPlugin) for p in load_plugins())


def test_bench_aggregates_cpcusb_adapter_card(tmp_path):
    bench = Bench(Db(tmp_path / "bench.db"), plugins=[CpcUsbPlugin()])
    assert bench.adapter_cards[0]["key"] == "cpc"
    assert bench.adapter_cards[0]["label"] == "CPC-USB / ARM7"
    # routable to the python-can interface the driver half registers. Asked
    # by part, not as a whole tuple: how CanopenBus stores an entry is its
    # own business (it grew a slot for backend keyword arguments), what this
    # plugin promises is the interface name and the channel.
    interface, channel, _ = bench._hw_bus._backends["cpc"]
    assert (interface, channel) == ("cpcusb", None)
