# Security Policy

CANopen-Bench is a bench/lab tool. It binds to `127.0.0.1` by default
and has **no authentication** — anyone who can reach the port can
control the connected CAN bus. Do not expose it to untrusted networks;
if you must bind to `0.0.0.0` (e.g. in Docker), put it behind your own
access control.

## Reporting a vulnerability

Please report vulnerabilities privately to **[redacted]**
instead of opening a public issue. Include steps to reproduce and, if
possible, an assessment of impact. You will get a response within a few
days; a fix or an agreed disclosure plan follows as soon as realistic.

## Supported versions

Only the latest release receives security fixes.
