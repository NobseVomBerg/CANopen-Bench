# The bench as an MCP server

A running bench is also an [MCP](https://modelcontextprotocol.io) server at
`http://127.0.0.1:8000/mcp`. An AI assistant that speaks MCP (Claude Code,
GitHub Copilot in VS Code, …) then drives the bench through tools instead of
working out the web API first. The tools act on the same devices, bus
connection and power supply the page shows, through the code the page uses,
and the page shows what they did.

The server describes itself when a client connects, with instructions and one
description per tool, so an assistant knows what it is talking to without
reading this repository first.

## Connecting

The bench has to be running; the tools are there as long as it is.

**Claude Code**:

```bash
claude mcp add --transport http --scope user canopen-bench http://127.0.0.1:8000/mcp
```

**VS Code (GitHub Copilot, agent mode)**: in `.vscode/mcp.json` of a
workspace, or under *MCP: Open User Configuration* for all of them:

```json
{
  "servers": {
    "canopen-bench": { "type": "http", "url": "http://127.0.0.1:8000/mcp" }
  }
}
```

Another port or host: the URL is the bench's address plus `/mcp`.

## Tools

| Tool | What it does |
|---|---|
| `bench_status` | Connection, devices (node, name, serial, firmware, NMT state, device commands), supply, test run, firmware download |
| `bench_log` | The bench's log, optionally filtered |
| `scan` | Scan the bus and wait for the result |
| `sdo_read` / `sdo_write` | One object of one device; index, sub-index and value as numbers or symbol names; a write is sized from the EDS |
| `nmt` | NMT command to one device or all |
| `can_send` | One raw frame |
| `device_command` | Switch a per-EDS device command (a vendor's service mode, say) |
| `psu` | Set voltage/current, switch the output, read the supply |
| `tests_list`, `testcase_check` | The test cases in the TestCases folder; a draft checked by the bench's own parser |
| `tests_run`, `tests_status`, `tests_stop`, `tests_answer` | Run cases on a device, follow the run, answer a case's question to the operator |
| `firmware_list`, `swdl` | The firmware library; a download to one or more devices in one run, by the transfer the installed download prefers unless one is named |
| `trace`, `trace_timing`, `trace_clear` | Frames from the trace, filtered; how regularly a COB-ID arrives (heartbeats, cyclic PDOs) and which frames came late |
| `bench_action` | Any page action by name, for what no tool covers |

Long work — a test run, a firmware download — starts and returns. With
`wait_s` the call waits for the end (up to ten minutes), otherwise
`tests_status` / `bench_status` tell how far it got.

## Security

The endpoint is as open as the rest of the web API: no authentication, so
the bench's port stays on a trusted network. A request carrying an `Origin`
header from another host is refused, so a web page elsewhere cannot drive
the bench through the browser of someone who has it open.

## For plugins

A plugin adds tools with `mcp_tools()`, a list of `McpTool(name,
description, run, params, required, read_only)`; they are listed as
`<plugin>_<name>`. `run(bench, args)` returns whatever JSON can carry, or an
awaitable of it, and an exception becomes an error result the assistant
reads. `mcp_instructions()` returns what an assistant should know about the
plugin's devices — appended to the server's instructions.

## Protocol

Streamable HTTP, JSON answers only: every tool answers within its call, so
there is no event stream (`GET /mcp` is 405) and no session. Protocol
versions 2024-11-05 to 2025-11-25.
