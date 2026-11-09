"""The MCP endpoint (canopen_bench/mcp.py): the protocol, and its tools acting on a demo bench."""
from __future__ import annotations

import asyncio
import json

import pytest
from conftest import connect_and_scan, write_seed_eds_files
from starlette.testclient import TestClient

from canopen_bench import mcp
from canopen_bench.app import create_app
from canopen_bench.core import Bench
from canopen_bench.db import Db
from canopen_bench.plugin import BenchPlugin, McpTool

PASS_TC = """\
id: "0001"
name: "identity readable"
steps:
  - sdo_read: {index: "0x2020", sub: "0x01", expect: "0x260001"}
"""


class _ToolPlugin(BenchPlugin):
    name = "acme"

    def mcp_tools(self):
        return [McpTool("hello", "says hello", lambda b, a: {"hello": a.get("who", "bench")},
                        {"who": {"type": "string"}}, read_only=True)]

    def mcp_instructions(self):
        return "ACME devices need a key."


@pytest.fixture
def bench(tmp_path):
    b = Bench(Db(tmp_path / "t.db"), plugins=[_ToolPlugin()])
    write_seed_eds_files(b)
    tc_dir = tmp_path / "tcs"
    tc_dir.mkdir()
    (tc_dir / "TC0001_pass.yaml").write_text(PASS_TC)
    b.dispatch("set_path", {"which": "tc", "value": str(tc_dir)})
    connect_and_scan(b)
    return b


def rpc(b, method, params=None, mid=1):
    return asyncio.run(mcp.handle(b, {"jsonrpc": "2.0", "id": mid, "method": method,
                                      "params": params or {}}))


def call(b, name, **args):
    """A tool's answer: (parsed JSON or text, isError)."""
    res = rpc(b, "tools/call", {"name": name, "arguments": args})["result"]
    text = res["content"][0]["text"]
    try:
        return json.loads(text), res["isError"]
    except ValueError:
        return text, res["isError"]


def test_initialize_answers_the_asked_version_and_says_what_the_bench_is(bench):
    res = rpc(bench, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "t", "version": "1"}})["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert res["capabilities"] == {"tools": {"listChanged": False}}
    assert "bench_status" in res["instructions"]
    assert res["instructions"].endswith("ACME devices need a key.")
    unknown = rpc(bench, "initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert unknown["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]


def test_tools_list_has_the_core_tools_and_a_plugins_under_its_name(bench):
    tools = {t["name"]: t for t in rpc(bench, "tools/list")["result"]["tools"]}
    assert {"bench_status", "sdo_read", "psu", "tests_run", "swdl", "trace_timing"} <= set(tools)
    assert tools["sdo_read"]["inputSchema"]["required"] == ["node", "index"]
    assert tools["acme_hello"]["annotations"]["readOnlyHint"] is True
    assert call(bench, "acme_hello", who="you") == ({"hello": "you"}, False)


def test_status_lists_the_scanned_devices(bench):
    status, failed = call(bench, "bench_status")
    assert not failed and status["connected"]
    assert [d["node"] for d in status["devices"]] == [d["node"] for d in bench.devices]


def test_sdo_read_returns_the_value_and_says_so_in_the_log(bench):
    got, failed = call(bench, "sdo_read", node=1, index="0x2020", sub=1)
    assert not failed and got["ok"] and int(got["value"], 16) == 0x260001
    assert "MCP" in bench.logs[-1]["msg"]


def test_sdo_write_is_sized_from_the_eds_and_reads_back(bench):
    wrote, failed = call(bench, "sdo_write", node=1, index="0x2000", sub=0, value=500)
    assert not failed and wrote == {"ok": True, "written": "0x000001F4"}  # U32 in the seed EDS
    got, _ = call(bench, "sdo_read", node=1, index="0x2000")
    assert got["decimal"] == 500


def test_a_usage_error_comes_back_as_a_tool_error_the_assistant_can_read(bench):
    text, failed = call(bench, "sdo_read", node=99, index="0x1000")
    assert failed and "no device on node 99" in text
    text, failed = call(bench, "sdo_read", node=1)
    assert failed and "index" in text
    err = rpc(bench, "tools/call", {"name": "no_such_tool"})["error"]
    assert err["code"] == -32602


def test_tests_run_waits_for_the_result_and_leaves_the_pages_selection_alone(bench):
    bench.test_sel = {"something"}
    got, failed = call(bench, "tests_run", ids=["0001"], node=1, wait_s=10)
    assert not failed and got["running"] is False and got["results"] == {"0001": "PASS"}
    assert bench.test_sel == {"something"}


def test_psu_sets_and_reports_back_and_says_when_there_is_none(tmp_path):
    from conftest import FakeSupplyPort
    b = Bench(Db(tmp_path / "psu.db"), plugins=[])
    text, failed = call(b, "psu", volt=24)
    assert failed and "no power supply" in text
    port = FakeSupplyPort()
    b._psu_opener = lambda device, baud, timeout: port
    assert b._psu_connect("COM99")
    got, failed = call(b, "psu", ch=1, volt=24, output=True)
    assert not failed and got["psu"]["channels"][0]["volt"] == 24.0 and got["psu"]["output"]


def test_testcase_check_uses_the_bench_parser(bench):
    ok, _ = call(bench, "testcase_check", text=PASS_TC, filename="TC0001_x.yaml")
    assert ok["ok"] and ok["steps"] == 1
    bad, _ = call(bench, "testcase_check", text=PASS_TC.replace("sdo_read", "sdo_raed"))
    assert bad["ok"] is False and "sdo_raed" in bad["error"]


def _hb(t: str, node: int = 1) -> dict:
    return {"time": t, "dir": "RX", "cob": f"0x{0x700 + node:03X}", "len": "1", "data": "05",
            "dec": "HB", "flag": "", "cls": "HB", "node": node, "obj": "", "val": ""}


def test_trace_timing_measures_a_heartbeat_and_names_the_late_one(bench):
    bench.trace = [_hb("12:00:00.000000"), _hb("12:00:01.000000"), _hb("12:00:02.000000", 2),
                   _hb("12:00:02.000000"), _hb("12:00:03.900000"), _hb("12:00:04.900000")]
    timing, _ = call(bench, "trace_timing", cob="0x701")
    assert timing["frames"] == 5 and timing["interval_ms"]["median"] == 1000.0
    assert timing["longer_than_1.5x_median"] == [{"after_ms": 1900.0, "at": "12:00:03.900000"}]
    rows, _ = call(bench, "trace", node=2)
    assert rows["matched"] == 1 and rows["rows"][0]["cob"] == "0x702"


def test_http_answers_json_and_turns_away_a_foreign_origin(tmp_path):
    app = create_app(db_path=str(tmp_path / "x.db"))
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"}}
    with TestClient(app) as client:
        res = client.post("/mcp", json=init)
        assert res.status_code == 200 and res.json()["result"]["serverInfo"]["name"] == "canopen-bench"
        assert client.post("/mcp", json={"jsonrpc": "2.0",
                                         "method": "notifications/initialized"}).status_code == 202
        assert client.get("/mcp").status_code == 405
        assert client.post("/mcp", json=init, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/mcp", json=init, headers={"Origin": "http://localhost:8000"}).status_code == 200
