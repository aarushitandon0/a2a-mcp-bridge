from __future__ import annotations

import httpx
import pytest

from bridge import discover
from bridge.config import AgentConfig


@pytest.mark.asyncio
async def test_list_tools_against_two_live_fake_agents_is_namespaced(fake_agents):
    agents = {
        agent_id: AgentConfig(agent_id=agent_id, base_url=base_url)
        for agent_id, base_url in fake_agents.items()
    }

    async with httpx.AsyncClient() as client:
        tools = await discover.list_tools(client, agents)

    names = {tool["name"] for tool in tools}
    assert names == {"docs-agent__summarize", "code-agent__review", "code-agent__refactor"}

    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["required"] == ["message"]


@pytest.mark.asyncio
async def test_discover_result_advertises_tasks_extension():
    result = await discover.discover_result()
    assert result["supportedVersions"] == ["2026-07-28"]
    assert "io.modelcontextprotocol/tasks" in result["capabilities"]["extensions"]
