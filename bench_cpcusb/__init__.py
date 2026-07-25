# SPDX-License-Identifier: GPL-2.0-only
"""CPC-USB/ARM7 support for canopen-bench: python-can backend plus its
adapter card (``canopen_bench.plugins`` entry point).

The backend is ported from the mainline Linux kernel driver
``drivers/net/can/usb/ems_usb.c`` (GPL-2.0-only, Copyright (C) 2004-2009
EMS Dr. Thomas Wuensche) so the same wire protocol can be driven on Windows
via pyusb/libusb (WinUSB) instead of the vendor's Windows driver stack.
"""
from .bus import CpcUsbBus
from .plugin import CpcUsbPlugin

__all__ = ["CpcUsbBus", "CpcUsbPlugin"]
