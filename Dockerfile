# Demo mode and TCP-attached adapters work out of the box. USB CAN
# adapters additionally need the device passed through, e.g.
#   docker run --device=/dev/bus/usb -v canopen-bench:/data -p 8000:8000 canopen-bench
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY canopen_bench ./canopen_bench
RUN pip install --no-cache-dir .

# All workspaces (db, EDS files, traces, test cases, results) live under
# one volume — back up or migrate by copying the folder.
ENV CANOPEN_BENCH_DATA=/data
VOLUME /data

EXPOSE 8000
CMD ["canopen-bench", "--host", "0.0.0.0", "--port", "8000"]
