"""CANopen Bench — web tool to test and control CANopen devices."""
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path


def _read_version() -> str:
    """The one place the version is read from.

    pyproject.toml first: this project bumps the version on every commit
    and does not tag releases, so a source checkout is the normal way to
    run it — and the installed metadata of an editable install goes stale
    until the next `pip install -e`, happily reporting an old number.
    Wheel installs have no pyproject and use the (then correct) metadata.

    Kept in this module rather than in ``core`` so that reading the
    version costs no imports beyond the standard library.
    """
    try:
        import tomllib
        pp = Path(__file__).resolve().parent.parent / "pyproject.toml"
        return tomllib.loads(pp.read_text(encoding="utf-8"))["project"]["version"]
    except Exception:
        try:
            return _pkg_version("canopen-bench")
        except PackageNotFoundError:  # bare checkout without install
            return "dev"


__version__ = _read_version()
