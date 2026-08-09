"""MCP transport tests: one thin adapter, stdio and streamable HTTP."""
from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from target_agent.mcp_server import _serve, create_mcp_server


class StubServer:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_serve_uses_stdio_transport():
    server = StubServer()
    _serve(server, transport="stdio", host="127.0.0.1", port=8000, path="/mcp")
    assert server.calls == [{"transport": "stdio"}]


def test_serve_uses_streamable_http_transport():
    server = StubServer()
    _serve(server, transport="streamable-http", host="0.0.0.0", port=9000, path="/mcp")
    assert server.calls == [{
        "transport": "streamable-http",
        "host": "0.0.0.0",
        "port": 9000,
        "streamable_http_path": "/mcp",
    }]


def test_created_server_exposes_run_transport():
    server = create_mcp_server()
    assert callable(server.run)

def test_mcp_session_tools_drive_a_real_project(tmp_path):
    import asyncio
    import json

    from target_agent.mcp_server import create_mcp_server

    from .test_research_runtime import fake_research_runtime, research_project

    runtime, _ = fake_research_runtime(tmp_path)
    project = research_project("project-mcp-session")
    runtime.run(project)
    server = create_mcp_server(runtime=runtime)

    async def scenario():
        names = [tool.name for tool in await server.list_tools()]
        for name in (
            "target_create_session",
            "target_list_sessions",
            "target_read_session",
            "target_post_session_message",
            "target_session_intervene",
        ):
            assert name in names

        async def call(name, arguments):
            result = await server.call_tool(name, arguments)
            return json.loads(result.content[0].text)

        created = await call("target_create_session", {
            "project_id": project.project_id,
            "title": "MCP 会话",
            "role": "reviewer",
        })
        session_id = created["session"]["session_id"]
        assert created["session"]["role"] == "reviewer"

        listed = await call("target_list_sessions", {"project_id": project.project_id})
        assert listed["sessions"][0]["session_id"] == session_id

        posted = await call("target_post_session_message", {
            "project_id": project.project_id,
            "session_id": session_id,
            "text": "现在到哪一步了？",
            "ask_agent": True,
        })
        assert [m["role"] for m in posted["messages"]] == ["user", "assistant"]
        assert posted["messages"][1]["source_bound"] is False

        read = await call("target_read_session", {
            "project_id": project.project_id,
            "session_id": session_id,
        })
        assert len(read["messages"]) == 2

        snap = await call("target_get_project", {"project_id": project.project_id})
        item_id = snap["plan"]["items"][0]["item_id"]
        fork = await call("target_session_intervene", {
            "project_id": project.project_id,
            "session_id": session_id,
            "action": "propose_fork",
            "rationale": "补充输入并重跑",
            "target_id": item_id,
            "input_overrides": {item_id: {"record_count": 2}},
        })
        assert fork["fork"]["status"] == "proposed"
        assert fork["messages"][1]["kind"] == "intervention_result"

    asyncio.run(scenario())

def test_mcp_tool_schemas_bound_parameters_and_reject_invalid_values(tmp_path):
    import asyncio

    from target_agent.mcp_server import create_mcp_server

    from .test_research_runtime import fake_research_runtime, research_project

    runtime, _ = fake_research_runtime(tmp_path)
    project = research_project("project-mcp-bounds")
    runtime.run(project)
    server = create_mcp_server(runtime=runtime)

    async def scenario():
        tools = {tool.name: tool for tool in await server.list_tools()}
        activities_schema = getattr(tools["target_get_domain_activities"], "input_schema", None) or getattr(tools["target_get_domain_activities"], "inputSchema")
        properties = activities_schema["properties"]
        assert properties["project_id"].get("minLength") == 1
        assert properties["after_sequence"].get("minimum") == 0
        assert properties["limit"].get("minimum") == 1
        assert properties["limit"].get("maximum") == 500
        read_schema = getattr(tools["target_read_text_artifact"], "input_schema", None) or getattr(tools["target_read_text_artifact"], "inputSchema")
        assert read_schema["properties"]["project_id"].get("minLength") == 1
        assert read_schema["properties"]["max_characters"].get("minimum") == 1
        assert read_schema["properties"]["max_characters"].get("maximum") == 1_000_000

        with pytest.raises(Exception, match="(?i)limit|invalid"):
            await server.call_tool("target_get_domain_activities", {
                "project_id": project.project_id,
                "limit": 0,
            })

    asyncio.run(scenario())
