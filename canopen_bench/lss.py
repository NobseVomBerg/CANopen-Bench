"""LSS master (CiA 305) on raw frames: what the `lss_fastscan` step runs.

The master asks on 0x7E5 and the slaves answer on 0x7E4, every frame eight
bytes with zeros after the payload. A fastscan finds the unconfigured slave
(node-ID 0xFF) with the lowest LSS address (vendor, product, revision,
serial, 32 bits each) by asking bit by bit whether anyone has a 0 there:
silence means every slave still in the race has a 1. Several slaves answer
one question, each on its own and a millisecond or so apart, so every
question waits out its whole window. Asking on after the first answer, as
this did at first, hands the late ones to the next question, which then
reads as "someone has a 0 here" where nobody has.

A part of the address the master already knows is confirmed with one
question instead of 32. After the last part exactly one slave is in LSS
configuration. It takes its node-ID and stores it, and the switch back to
waiting makes the ID active: the slave resets communication and sends its
boot-up there. That switch comes after every slave, not once at the end. A
slave still in configuration would take the next slave's node-ID as well.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

REQUEST, ANSWER = 0x7E5, 0x7E4
SWITCH_GLOBAL, CONFIGURE_NODE_ID, STORE, FASTSCAN, IDENTIFIED = 0x04, 0x11, 0x17, 0x51, 0x4F
WAITING, CONFIGURATION = 0x00, 0x01
RESET_SCAN = 0x80  # BitChecked of the question that starts a fastscan
PARTS = ("vendor", "product", "revision", "serial")  # the LSS address, LSSSub 0..3
#: configure node-ID and store; storing may mean an EEPROM write
ANSWER_TIMEOUT = 0.5

# the error codes CiA 305 names; 0xFF is the device's own, told apart by the byte after it
_CONFIGURE_ERRORS = {1: "node-ID out of range"}
_STORE_ERRORS = {1: "store not supported", 2: "storage media access error"}


def frame(*head: int) -> bytes:
    return bytes(head) + bytes(8 - len(head))


@dataclass
class Assigned:
    node: int
    address: tuple[int, ...]

    def text(self) -> str:
        return f"node {self.node}: " + " · ".join(
            f"{part} 0x{value:08X}" for part, value in zip(PARTS, self.address))


class LssError(Exception):
    """Where a procedure stopped and why; ``assigned`` is what it got done first."""

    def __init__(self, text: str, assigned: list[Assigned] | None = None) -> None:
        super().__init__(text)
        self.assigned = assigned or []


class LssMaster:
    def __init__(self, bus, timeout: float = 0.01) -> None:
        self.bus = bus
        self.timeout = timeout  # one fastscan question; slaves answer in a millisecond or two
        self._answers = bus.listen([ANSWER])

    def close(self) -> None:
        self._answers.close()

    def switch_global(self, mode: int) -> None:
        self.bus.send_raw(REQUEST, frame(SWITCH_GLOBAL, mode))

    def _ask(self, request: bytes, answer: int, timeout: float, whole: bool = False) -> bytes | None:
        """The first answer to ``request`` within ``timeout``, and with
        ``whole`` only once the window is over. What was already waiting
        answered an earlier question, so it goes first."""
        self._answers.clear()
        self.bus.send_raw(REQUEST, request)
        first = None
        deadline = time.perf_counter() + timeout
        while (left := deadline - time.perf_counter()) > 0:
            got = self._answers.get(left)
            if got is not None and got[1][:1] == bytes([answer]) and first is None:
                first = got[1]
                if not whole:
                    break
        return first

    def _scan(self, id_number: int, bit: int, sub: int, nxt: int) -> bool:
        request = frame(FASTSCAN, *id_number.to_bytes(4, "little"), bit, sub, nxt)
        return self._ask(request, IDENTIFIED, self.timeout, whole=True) is not None

    def fastscan(self, known: dict[int, int]) -> tuple[int, ...] | None:
        """The unconfigured slave with the lowest LSS address, now in LSS
        configuration, or None when no unconfigured slave answers at all.
        ``known`` maps an LSSSub to the value that part has on every slave."""
        if not self._scan(0, RESET_SCAN, 0, 0):
            return None
        address: list[int] = []
        for sub in range(4):
            value = known.get(sub, 0)
            if sub not in known:
                for bit in range(31, -1, -1):
                    if not self._scan(value, bit, sub, sub):
                        value |= 1 << bit
            if not self._scan(value, 0, sub, (sub + 1) % 4):
                if sub in known:
                    raise LssError(f"an unconfigured device answers, but none with "
                                   f"{PARTS[sub]} 0x{value:08X}")
                raise LssError(f"the device went quiet while its {PARTS[sub]} was read")
            address.append(value)
        return tuple(address)

    def _confirmed(self, request: bytes, what: str, errors: dict[int, str]) -> None:
        got = self._ask(request, request[0], ANSWER_TIMEOUT)
        if got is None:
            raise LssError(f"{what}: no answer within {ANSWER_TIMEOUT:g} s")
        if got[1] == 0xFF:
            raise LssError(f"{what}: refused, device error 0x{got[2]:02X}")
        if got[1]:
            meaning = errors.get(got[1])
            raise LssError(f"{what}: refused, error {got[1]}" + (f" ({meaning})" if meaning else ""))

    def configure_node_id(self, node: int) -> None:
        self._confirmed(frame(CONFIGURE_NODE_ID, node), f"configure node-ID {node}", _CONFIGURE_ERRORS)

    def store(self, node: int) -> None:
        self._confirmed(frame(STORE), f"store node-ID {node}", _STORE_ERRORS)


def _booted(bootups, timeout: float) -> bool:
    deadline = time.perf_counter() + timeout
    while (left := deadline - time.perf_counter()) > 0:
        got = bootups.get(left)
        if got is not None and got[1][:1] == b"\x00":
            return True
    return False


def assign(bus, first: int, last: int, known: dict[int, int], timeout: float = 0.01,
           bootup: float = 0.5, stop: Callable[[], bool] = lambda: False) -> list[Assigned]:
    """Give every unconfigured slave a node-ID, ``first`` upwards in the
    order the fastscan finds them, which is lowest LSS address first. Each
    one is stored and switched back to waiting, and its boot-up on the new
    ID has to come within ``bootup`` seconds. Raises LssError where it
    stops. Returns early, with what it assigned, once ``stop()`` says so."""
    master = LssMaster(bus, timeout)
    done: list[Assigned] = []
    try:
        master.switch_global(WAITING)
        node = first
        while not stop():
            address = master.fastscan(known)
            if address is None:
                break
            if node > last:
                raise LssError(f"more unconfigured devices than node-IDs {first}..{last}")
            master.configure_node_id(node)
            master.store(node)
            with bus.listen([0x700 + node]) as bootups:
                master.switch_global(WAITING)
                if not _booted(bootups, bootup):
                    raise LssError(f"node {node}: no boot-up within {bootup:g} s of the switch to waiting")
            done.append(Assigned(node, address))
            node += 1
        return done
    except LssError as exc:
        master.switch_global(WAITING)  # nothing stays in configuration, whatever stopped it
        exc.assigned = done
        raise
    finally:
        master.close()
