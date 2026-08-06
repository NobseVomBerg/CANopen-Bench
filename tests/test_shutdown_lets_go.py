"""A bench that stops hands the hardware back — see Bench.shutdown and the
lifespan in app.py. What this prevents is a process left holding the
supply's serial port, which no other program on the machine can take back.
"""
import pytest

from canopen_bench.__main__ import SHUTDOWN_S
from canopen_bench.core import Bench
from canopen_bench.db import Db


@pytest.fixture()
def bench(tmp_path):
    """Plain bench on the demo adapter — no EDS seeding needed here, these
    tests never scan; they only stop it."""
    return Bench(Db(tmp_path / "test.db"))


class _FakeLink:
    def __init__(self):
        self.port = "COM9"


class _FakeSupply:
    """Stands in for a connected supply: only close() matters here."""

    name = "fake"

    def __init__(self, fail=False):
        self.link = _FakeLink()
        self.closed = False
        self._fail = fail

    def close(self):
        if self._fail:
            raise OSError("port already gone")
        self.closed = True


def test_shutdown_closes_the_supply_port(bench):
    psu = _FakeSupply()
    bench.psu = psu
    bench.shutdown()
    assert psu.closed
    assert bench.psu is None


def test_shutdown_keeps_the_port_in_the_database(bench):
    """Closed, not released: the next start finds the same supply again
    rather than making the operator search for it once per restart."""
    bench.db.set("psu_port", "COM9")
    bench.psu = _FakeSupply()
    bench.shutdown()
    assert bench.db.get("psu_port") == "COM9"


def test_a_port_that_cannot_be_closed_does_not_stop_the_shutdown(bench):
    """Unplugging the cable is the other way out of a stuck port, and it
    leaves a handle that raises on close. Stopping is not the moment to
    give up on stopping."""
    bench.psu = _FakeSupply(fail=True)
    bench.shutdown()
    assert bench.psu is None
    assert any("not closed cleanly" in log["msg"] for log in bench.logs)


def test_shutdown_without_a_supply_is_quiet(bench):
    bench.psu = None
    bench.shutdown()
    assert bench.psu is None


def test_the_graceful_shutdown_is_capped(bench):
    """uvicorn's own default is to wait as long as it takes, and a browser
    tab left open never closes the state channel."""
    assert 0 < SHUTDOWN_S <= 30
