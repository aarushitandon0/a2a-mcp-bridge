"""server/discover and tools/list -- Agent Card -> MCP tool projection.

No session state: every call re-fetches Agent Cards. Provenance for the
shapes here is SPEC-NOTES.md §1.3 (server/discover), §3 (Tasks extension
capability advertisement), §10 (AgentCard).
"""

from __future__ import annotations

import asyncio

import httpx

from bridge import mapping
from bridge.a2a_client import fetch_agent_card
from bridge.config import AgentConfig

SUPPORTED_PROTOCOL_VERSIONS = ["2026-07-28"]
TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"

# Every MCP Result requires resultType (schema.ts `Result`); CacheableResult
# additionally requires cacheScope + ttlMs. Both server/discover's
# DiscoverResult and tools/list's ListToolsResult extend CacheableResult.
# Values below are this bridge's own cache-policy choice, not a spec fact:
# discover capabilities change rarely (long TTL); tools/list depends on live
# Agent Cards that an agent operator could update at any time (shorter TTL).
# "public" because neither depends on the calling client's auth context.
DISCOVER_CACHE_TTL_MS = 300_000
TOOLS_LIST_CACHE_TTL_MS = 60_000


def server_capabilities() -> dict:
    return {
        "tools": {},
        "extensions": {TASKS_EXTENSION_ID: {}},
    }


async def discover_result() -> dict:
    return {
        "resultType": "complete",
        "cacheScope": "public",
        "ttlMs": DISCOVER_CACHE_TTL_MS,
        "supportedVersions": SUPPORTED_PROTOCOL_VERSIONS,
        "capabilities": server_capabilities(),
    }


async def list_tools(http_client: httpx.AsyncClient, agents: dict[str, AgentConfig]) -> list[dict]:
    agent_list = list(agents.values())
    cards = await asyncio.gather(*(fetch_agent_card(http_client, agent) for agent in agent_list))

    tools: list[dict] = []
    for agent, card in zip(agent_list, cards):
        for skill in card.skills:
            tools.append(mapping.skill_to_tool(agent.agent_id, skill, card.default_input_modes))
    return tools


async def list_tools_result(http_client: httpx.AsyncClient, agents: dict[str, AgentConfig]) -> dict:
    return {
        "resultType": "complete",
        "cacheScope": "public",
        "ttlMs": TOOLS_LIST_CACHE_TTL_MS,
        "tools": await list_tools(http_client, agents),
    }
