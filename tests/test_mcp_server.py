"""
Tests for the MCP tools (mcp_server.py) and the assistant's tool plumbing,
run against the real serving data. Build it first:

    .venv/bin/python src/build_serving_data.py
    .venv/bin/python -m pytest tests

No Claude API calls are made.
"""

import asyncio
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from factory_service import SERVING_DIR  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (SERVING_DIR / "parts.parquet").exists(),
    reason="Serving data not built; run src/build_serving_data.py first",
)

from anthropic.lib.tools.mcp import async_mcp_tool  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError  # noqa: E402

import mcp_server  # noqa: E402


EXPECTED_TOOLS = {
    "get_factory_summary",
    "get_line_status",
    "get_part",
    "explain_part_risk",
    "get_inspection_queue",
    "get_batch_mate_alerts",
    "get_station",
    "list_stations",
}


def over_stdio(work):
    """Run `work(session)` against the real server process, like an MCP client."""

    async def main():
        params = StdioServerParameters(command=sys.executable, args=[str(SRC / "mcp_server.py")])

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                info = await session.initialize()
                return await work(session, info)

    return asyncio.run(main())


# ============================================================
# IN PROCESS
# ============================================================

def test_tools_are_read_only_and_say_when_to_call_them():
    tools = asyncio.run(mcp_server.server.list_tools())

    assert {tool.name for tool in tools} == EXPECTED_TOOLS

    for tool in tools:
        assert tool.annotations.read_only_hint is True
        assert "Call this" in tool.description


def test_tools_return_the_service_answers():
    result = asyncio.run(mcp_server.server.call_tool("get_line_status", {"at_hour": 7500}))

    assert result.is_error is False
    assert result.structured_content["campaign"] == "L1 campaign"


def test_unknown_part_is_an_anticipated_error():
    """Anticipated errors keep their message, so the model can recover."""

    with pytest.raises(ToolError) as error:
        asyncio.run(mcp_server.server.call_tool("get_part", {"part_id": 3}))

    assert not isinstance(error.value, UnexpectedToolError)
    assert "not known" in str(error.value)


# ============================================================
# OVER STDIO, LIKE A REAL CLIENT
# ============================================================

def test_stdio_round_trip():
    async def work(session, info):
        tools = (await session.list_tools()).tools
        queue = await session.call_tool("get_inspection_queue", {"hours": 168, "limit": 5})
        unknown = await session.call_tool("get_part", {"part_id": 3})
        return info, tools, queue, unknown

    info, tools, queue, unknown = over_stdio(work)

    assert "not probabilities" in info.instructions
    assert {tool.name for tool in tools} == EXPECTED_TOOLS

    scores = [item["risk_score"] for item in queue.structured_content["items"]]
    assert queue.is_error is False
    assert scores == sorted(scores, reverse=True)

    assert unknown.is_error is True
    assert "not known" in unknown.content[0].text


def test_tools_convert_for_the_claude_tool_runner():
    """The assistant's path: MCP tools -> Claude tool definitions -> real results."""

    async def work(session, info):
        tools = [async_mcp_tool(tool, session) for tool in (await session.list_tools()).tools]
        by_name = {tool.name: tool for tool in tools}
        station = await by_name["get_station"].call({"station_id": "S32"})
        return tools, by_name, station

    tools, by_name, station = over_stdio(work)

    assert {tool.to_dict()["name"] for tool in tools} == EXPECTED_TOOLS
    assert by_name["explain_part_risk"].to_dict()["input_schema"]["required"] == ["part_id"]
    assert '"station": "L3_S32"' in station[0]["text"]
