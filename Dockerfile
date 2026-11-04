# Demo mode works out of the box. A CPC-USB on a Linux host needs the USB
# bus passed through, e.g.
#   docker run --device=/dev/bus/usb -v canopen-bench:/data -p 8000:8000 canopen-bench
# SocketCAN (can0, brought up with its bitrate on the host) needs the host's
# network instead: docker run --network host -v canopen-bench:/data canopen-bench
# Which adapters a container can reach at all: README, "Docker".
# publish.yml builds this into ghcr.io/nobsevomberg/canopen-bench with every
# PyPI upload; CI builds and starts it on every push.
FROM python:3.12-slim

# libusb for the CPC-USB driver (cob-cpcusb, a dependency); iproute2 for
# SocketCAN — python-can lists the interfaces with `ip`, and the bench asks
# it for their bitrate. The slim image has neither.
RUN apt-get update && apt-get install -y --no-install-recommends libusb-1.0-0 iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# LICENSE + THIRD-PARTY-NOTICES.md are declared as license-files in
# pyproject.toml — they must be present at build time so the attribution
# for the bundled Preact/htm and for LGPL python-can travels with the image.
COPY pyproject.toml README.md LICENSE THIRD-PARTY-NOTICES.md ./
COPY canopen_bench ./canopen_bench
RUN pip install --no-cache-dir .

# All workspaces (db, EDS files, traces, test cases, results) live under
# one volume — back up or migrate by copying the folder.
VOLUME /data

EXPOSE 8000
CMD ["canopen-bench", "--host", "0.0.0.0", "--port", "8000", "--data", "/data"]
