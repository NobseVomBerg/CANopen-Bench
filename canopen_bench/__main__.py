"""Run with:  python -m canopen_bench  [--host 0.0.0.0] [--port 8000]

Hardware adapters (CPC-USB / IXXAT / PCAN) talk to the bus via
python-can/canopen; pick the adapter on the Setup page. Demo mode needs no
hardware — it generates virtual DUTs from the uploaded EDS files. The
CPC-USB adapter additionally needs the cob-cpcusb driver package.
"""
import argparse
import os
import sys
from pathlib import Path

import uvicorn

from .app import create_app

#: Name of the lock file inside a data folder. Dot-prefixed, so it stays out
#: of the workspace list (Bench._workspace_names skips those).
LOCK_NAME = ".bench.lock"

#: Held open for the lifetime of the process: the lock lives on the open
#: file, so the operating system drops it the moment the process is gone,
#: crash and kill included — there is no stale lock to clean up and no pid
#: to test for liveness. Module level rather than a local in main(), or the
#: garbage collector closes the file and hands the folder to the next
#: starter while this bench is still running.
_lock_handle = None


def _lock(f) -> None:
    """Exclusive, non-blocking lock, per open file rather than per process.

    Both calls below lock a single open file rather than the process, so a
    second attempt from *this* process is refused the same way another
    process would be — which is what makes it testable without spawning
    one. POSIX ``lockf`` would not: its record locks belong to the process,
    so a second claim inside one test run would quietly succeed.
    """
    if os.name == "nt":
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def claim_data_folder(root: Path) -> int | None:
    """Claim a data folder for this process. None if it is ours now,
    otherwise the pid of the bench already holding it (0 if unreadable).

    Two benches on one data folder share a database, a plugin directory and
    whatever hardware is plugged into the machine. The damage is not loud:
    the second one to start takes the serial port, and the first one's
    search then finds nothing and reports "no known power supply found" —
    which sends the reader to the cable, the port and the instrument, when
    what was wrong was that the bench was running twice.

    The HTTP port needs no guard of its own: the operating system already
    refuses the second bind. The data folder is the resource with no such
    protection, and it is also the right granularity — a release bench on
    its own folder and port is meant to run beside an everyday one.
    """
    root.mkdir(parents=True, exist_ok=True)
    f = open(root / LOCK_NAME, "a+", encoding="utf-8")
    try:
        _lock(f)
    except OSError:
        # Only the first byte is locked on Windows, so the pid behind it
        # stays readable — the message can name what to stop.
        f.seek(1)
        held = f.read().strip()
        f.close()
        return int(held) if held.isdigit() else 0
    f.seek(0)
    f.truncate()
    f.write(f"\n{os.getpid()}")   # byte 0 is the locked one, the pid follows
    f.flush()
    global _lock_handle
    _lock_handle = f
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="CANopen Bench web tool")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=None,
                    help="expert override: explicit sqlite workspace db path. Normally "
                         "workspaces live as subfolders of ./data (or $CANOPEN_BENCH_DATA) "
                         "and are selected on the Setup page. Skips the one-bench-per-data-"
                         "folder check, since there is no data folder to speak of")
    args = ap.parse_args()

    if not (args.db or os.environ.get("CANOPEN_BENCH_DB")):
        root = Path(os.environ.get("CANOPEN_BENCH_DATA", "data"))
        held_by = claim_data_folder(root)
        if held_by is not None:
            who = f"as process {held_by}" if held_by else "already"
            print(f"CANopen Bench is running {who} on {root.resolve()}.\n"
                  f"Two of them share that folder's database, plugins and the hardware "
                  f"plugged into this machine.\n"
                  f"Stop that one first, or give this one a folder and a port of its own:\n"
                  f"  CANOPEN_BENCH_DATA=<folder> canopen-bench --port <other port>",
                  file=sys.stderr)
            raise SystemExit(1)

    uvicorn.run(create_app(args.db), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
