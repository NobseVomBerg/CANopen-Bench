# SPDX-License-Identifier: MIT
"""CPC-USB/ARM7 support for canopen-bench: python-can backend plus its
adapter card (``canopen_bench.plugins`` entry point).

The backend speaks the adapter's USB protocol directly over pyusb/libusb
(WinUSB on Windows) instead of going through the vendor's driver stack.
The protocol is documented in ``PROTOCOL.md``; its interface data —
identifiers, endpoints, byte layouts, register semantics — was read off
the mainline Linux driver ``drivers/net/can/usb/ems_usb.c`` (Copyright
(C) 2004-2009 EMS Dr. Thomas Wuensche), the only public verified
description of it.

The adapter card (``cob_cpcusb.plugin``) is not imported here: it needs
canopen-bench, and the driver is for any python-can user. The bench loads
it through its entry point.
"""
from .bus import CpcUsbBus

__all__ = ["CpcUsbBus"]
