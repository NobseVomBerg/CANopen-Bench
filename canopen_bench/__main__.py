"""Run with:  python -m canopen_bench  [--host 0.0.0.0] [--port 8000]

Hardware adapters (CPC-USB / IXXAT / PCAN) talk to the bus via
python-can/canopen; pick the adapter on the Setup page. Demo mode needs no
hardware — it generates virtual DUTs from the uploaded EDS files. The
CPC-USB adapter additionally needs the bench-cpcusb driver package.
"""
import argparse

import uvicorn

from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="CANopen Bench web tool")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=None,
                    help="expert override: explicit sqlite workspace db path. Normally "
                         "workspaces live as subfolders of ./data (or $CANOPEN_BENCH_DATA) "
                         "and are selected on the Setup page")
    args = ap.parse_args()

    uvicorn.run(create_app(args.db), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
