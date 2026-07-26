# SPDX-License-Identifier: GPL-2.0-only
"""Bench-plugin half of the CPC-USB/ARM7 adapter: the UI card and its
python-can key mapping. The driver half (``bus.py``) registers with
python-can's own ``can.interface`` entry-point group, independently of
this — see the module docstring in ``bus.py`` and docs/extending.md
("Adapter = Treiberpaket + Bench-Plugin" pattern) in the canopen-bench
core repo. Both halves live in this one package: the adapter card is
meaningless without the driver it points at, and vice versa, so
splitting them across two packages (as an earlier revision did, into
this package plus an unrelated device-family plugin) only made the
card orphanable from its own driver.
"""
from __future__ import annotations

from canopen_bench.plugin import BenchPlugin


class CpcUsbPlugin(BenchPlugin):
    name = "cpcusb"

    def adapters(self) -> list[dict]:
        return [
            {"key": "cpc", "label": "CPC-USB / ARM7", "sub": "Dr. Wünsche · GTI-HV",
             "conn": "CPC-USB connected", "foot": "CPC-USB/ARM7", "iface": "CPC-USB",
             "driver": "driver: cpc_usb · m4d 5.6.6", "full": "CPC-USB/ARM7-GTI-HV"},
        ]

    def adapter_backends(self) -> dict[str, tuple[str, str | int | None]]:
        return {"cpc": ("cpcusb", None)}
