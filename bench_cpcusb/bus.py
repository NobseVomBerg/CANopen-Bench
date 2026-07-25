# SPDX-License-Identifier: GPL-2.0-only
"""``can.BusABC`` implementation for the CPC-USB/ARM7, driven directly over
native USB bulk transfers via pyusb — no vendor DLL, no COM-port framing.

On Windows the device must be bound to WinUSB instead of the vendor driver
before pyusb/libusb can open it. A one-time step with `Zadig
<https://zadig.akeo.ie/>`_ (select the CPC-USB/ARM7 device, driver = WinUSB)
does this; it's the same approach python-can's own gs_usb/candleLight
backend relies on for Windows support.
"""
from __future__ import annotations

import logging
from typing import Any

import can
import usb.core
import usb.util
from can.exceptions import CanInitializationError, CanOperationError

from . import protocol as p

logger = logging.getLogger(__name__)


class CpcUsbBus(can.BusABC):
    """One instance per attached CPC-USB/ARM7 adapter.

    :param channel: informational only (there is currently one supported
        product, identified purely by USB vendor/product ID); accepted for
        interface-uniformity with other python-can backends.
    :param bitrate: CAN bus bitrate in bit/s.
    :param bus: restrict device lookup to this USB bus number.
    :param address: restrict device lookup to this USB device address.
    """

    def __init__(
        self,
        channel: Any = None,
        bitrate: int = 500_000,
        bus: int | None = None,
        address: int | None = None,
        **kwargs: Any,
    ) -> None:
        find_kwargs: dict[str, Any] = {
            "idVendor": p.VENDOR_ID,
            "idProduct": p.PRODUCT_ID_ARM7,
        }
        if bus is not None:
            find_kwargs["bus"] = bus
        if address is not None:
            find_kwargs["address"] = address

        dev = usb.core.find(**find_kwargs)
        if dev is None:
            raise CanInitializationError(
                "No CPC-USB/ARM7 found (USB vendor 0x12D6 / product 0x0444). "
                "On Windows, bind the device to WinUSB with Zadig first."
            )

        try:
            dev.set_configuration()
        except usb.core.USBError as exc:
            raise CanInitializationError(
                f"Could not configure CPC-USB/ARM7: {exc}"
            ) from exc

        self._dev = dev
        self.channel_info = str(channel) if channel is not None else "CPC-USB/ARM7"
        self._can_protocol = can.CanProtocol.CAN_20
        self._rx_queue: list[p.RxMessage] = []

        # Bring the SJA1000 controller up: reset -> configure bit-timing -> enable
        # RX notifications -> normal mode. Mirrors ems_usb_open()/ems_usb_start().
        self._write_mode(p.SJA1000_MOD_RESET)
        btr0, btr1 = p.bitrate_to_btr(bitrate)
        self._write_mode(p.SJA1000_MOD_RESET, btr0=btr0, btr1=btr1)

        for control_value in (
            p.CONTR_CAN_MESSAGE | p.CONTR_CONT_ON,
            p.CONTR_CAN_STATE | p.CONTR_CONT_ON,
            p.CONTR_BUS_ERROR | p.CONTR_CONT_ON,
        ):
            self._bulk_write(p.encode_control_cmd(control_value))

        self._write_mode(p.SJA1000_MOD_NORMAL, btr0=btr0, btr1=btr1)

        super().__init__(channel=channel, **kwargs)

    # -- low level ------------------------------------------------------------
    def _bulk_write(self, buf: bytes) -> None:
        try:
            self._dev.write(p.BULK_EP_OUT, buf, timeout=1000)
        except usb.core.USBError as exc:
            raise CanOperationError(f"CPC-USB bulk write failed: {exc}") from exc

    def _write_mode(self, mode: int, *, btr0: int = 0, btr1: int = 0) -> None:
        self._bulk_write(p.encode_can_params(mode=mode, btr0=btr0, btr1=btr1))

    # -- python-can BusABC interface -------------------------------------------
    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        frame = p.encode_can_frame(
            msg.arbitration_id,
            bytes(msg.data),
            msg.dlc,
            is_extended=msg.is_extended_id,
            is_remote=msg.is_remote_frame,
        )
        self._bulk_write(frame)

    def _recv_internal(self, timeout: float | None) -> tuple[can.Message | None, bool]:
        if not self._rx_queue:
            timeout_ms = 1 if not timeout else max(1, round(timeout * 1000))
            try:
                raw = self._dev.read(p.BULK_EP_IN, p.RX_BUFFER_SIZE, timeout=timeout_ms)
            except usb.core.USBTimeoutError:
                return None, False
            except usb.core.USBError as exc:
                raise CanOperationError(f"CPC-USB bulk read failed: {exc}") from exc
            self._rx_queue.extend(p.decode_bulk_packet(bytes(raw)))

        while self._rx_queue:
            item = self._rx_queue.pop(0)
            if isinstance(item, p.RxCanFrame):
                return (
                    can.Message(
                        arbitration_id=item.arbitration_id,
                        data=item.data,
                        dlc=item.dlc,
                        is_extended_id=item.is_extended,
                        is_remote_frame=item.is_remote,
                        channel=self.channel_info,
                    ),
                    False,
                )
            # CAN-state / bus-error / overrun notifications: log and keep polling.
            logger.debug("CPC-USB status message: %r", item)

        return None, False

    def shutdown(self) -> None:
        if self._is_shutdown:
            return
        try:
            self._write_mode(p.SJA1000_MOD_RESET)
        except CanOperationError:
            pass
        usb.util.dispose_resources(self._dev)
        super().shutdown()
