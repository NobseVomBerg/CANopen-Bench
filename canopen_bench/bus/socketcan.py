"""SocketCAN: the adapters Linux drives itself.

PCAN-USB (``peak_usb``), Kvaser (``kvaser_usb``), candleLight/CANable
(``gs_usb``) and CPC-USB (``ems_usb``) all come up as a network interface,
``can0``, which python-can opens with ``interface="socketcan"``. What
python-can does not do there is set the bitrate: the interface carries its
own, set by root with ``ip link``, and the one python-can is handed is
dropped without a word. A bench at 500 kbit/s on an interface at 250 then
hears nothing, and nothing says why — so ``connect`` asks the interface
first (``CanopenBus.connect``).
"""
from __future__ import annotations

import json
import subprocess


def link_problem(channel: str, kbit: int) -> str:
    """Why ``channel`` cannot carry ``kbit`` kbit/s, with the ``ip`` line
    that fixes it — "" when it can, or when nobody can say (no ``ip`` to
    ask: python-can opens the interface as it always did).

    Only reports, never sets: setting needs root, and a bench that asks
    for it would be one that runs as root. A vcan has no bit timing at
    all, so there only "is it up" is asked.
    """
    if not channel:
        return ""  # bound to every interface at once: no one bitrate to ask about
    try:
        out = subprocess.run(["ip", "-details", "-json", "link", "show", channel],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        if "does not exist" not in out.stderr:
            return ""  # an ip too old for -json, say: not ours to judge
        return (f"no network interface {channel} — adapter unplugged, its kernel driver "
                "not loaded, or a container started without --network host")
    try:
        link = json.loads(out.stdout)[0]
    except (ValueError, IndexError):
        return ""
    kind = link.get("linkinfo", {}).get("info_kind")
    can = link.get("linkinfo", {}).get("info_data", {})
    up = (f"sudo ip link set {channel} up type can bitrate {kbit * 1000}" if kind == "can"
          else f"sudo ip link set {channel} up")
    if "UP" not in link.get("flags", []):
        return f"{channel} is down — on the host: {up}"
    have = can.get("bittiming", {}).get("bitrate")
    if have and have != kbit * 1000:
        return (f"{channel} runs at {have / 1000:g} kbit/s, not the {kbit} chosen here — "
                f"pick that, or on the host: sudo ip link set {channel} down && {up}")
    if can.get("state") == "BUS-OFF":
        return f"{channel} is bus-off — on the host: sudo ip link set {channel} down && {up}"
    return ""
