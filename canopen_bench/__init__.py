"""CANopen Bench — web tool to test and control CANopen devices."""
import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path


def _read_version() -> str:
    """The one place the version is read from.

    pyproject.toml first: this project bumps the version on every commit
    and does not tag releases, so a source checkout is the normal way to
    run it — and the installed metadata of an editable install goes stale
    until the next `pip install -e`, happily reporting an old number.
    Wheel installs have no pyproject next to the package and fall through
    to the (then correct) metadata.

    The file is scanned for the version line rather than parsed as TOML:
    ``tomllib`` only exists from Python 3.11, and on 3.10 falling back to
    the metadata would quietly reintroduce exactly the staleness this
    function exists to avoid. Adding a ``tomli`` dependency to read one
    line is not worth it.

    Kept in this module rather than in ``core`` so that reading the
    version costs no imports beyond the standard library.
    """
    try:
        pp = Path(__file__).resolve().parent.parent / "pyproject.toml"
        for line in pp.read_text(encoding="utf-8").splitlines():
            # first `version = "..."` in the file; [project] declares it
            # before any other table can (build-system has no version key)
            match = re.match(r'\s*version\s*=\s*["\'](?P<v>[^"\']+)["\']', line)
            if match:
                return match.group("v")
    except OSError:
        pass
    try:
        return _pkg_version("canopen-bench")
    except PackageNotFoundError:  # bare checkout without install
        return "dev"


__version__ = _read_version()
