"""Thin async A2A client wrapper. Never hand-roll A2A types -- use the SDK's.

Field/method names here trace to SPEC-NOTES.md §5, §6, §10.
"""

from __future__ import annotations

import httpx
from a2a.client.card_resolver import A2ACardResolver
from a2a.types import AgentCard

from bridge.config import AgentConfig

A2A_VERSION_HEADER = "A2A-Version"
A2A_VERSION = "1.0"  # SPEC-NOTES.md §9 Unresolved #1 (resolved)


def _agent_headers(agent: AgentConfig) -> dict[str, str]:
    return {A2A_VERSION_HEADER: A2A_VERSION, **agent.headers}


async def fetch_agent_card(http_client: httpx.AsyncClient, agent: AgentConfig) -> AgentCard:
    """Fetch and parse the AgentCard from an agent's well-known path."""
    resolver = A2ACardResolver(http_client, agent.base_url)
    return await resolver.get_agent_card(http_kwargs={"headers": _agent_headers(agent)})
