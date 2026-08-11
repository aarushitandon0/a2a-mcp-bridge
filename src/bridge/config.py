"""Agent configuration: static, deployed identically to every bridge instance.

Per SPEC-NOTES.md §11, the sealed task-handle envelope (state.py, M2) stores a
short agent_id and resolves it against this config rather than embedding the
agent's full base URL in every handle. Reading this config is not server
state in the M4 sense -- it's the same file/env var on every instance.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


class ConfigError(Exception):
    """Raised for malformed or invalid agent configuration."""


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    base_url: str
    headers: dict[str, str] = field(default_factory=dict)


def _agent_from_entry(entry: dict[str, Any]) -> AgentConfig:
    try:
        agent_id = entry["agent_id"]
        base_url = entry["base_url"]
    except KeyError as exc:
        raise ConfigError(f"agent entry missing required key: {exc}") from exc
    return AgentConfig(
        agent_id=agent_id,
        base_url=base_url,
        headers=dict(entry.get("headers", {})),
    )


def load_agents_from_json(raw: str) -> dict[str, AgentConfig]:
    """Parse a JSON array of {agent_id, base_url, headers?} into AgentConfig, keyed by agent_id."""
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"agent config is not valid JSON: {exc}") from exc
    if not isinstance(entries, list):
        raise ConfigError("agent config must be a JSON array")

    agents: dict[str, AgentConfig] = {}
    for entry in entries:
        agent = _agent_from_entry(entry)
        if agent.agent_id in agents:
            raise ConfigError(f"duplicate agent_id in config: {agent.agent_id!r}")
        agents[agent.agent_id] = agent
    return agents


def load_agents_from_env(env: dict[str, str] | None = None) -> dict[str, AgentConfig]:
    env = env if env is not None else os.environ
    raw = env.get("BRIDGE_AGENTS_CONFIG")
    if not raw:
        raise ConfigError(
            "BRIDGE_AGENTS_CONFIG environment variable is not set "
            "(expected a JSON array of {agent_id, base_url, headers?})"
        )
    return load_agents_from_json(raw)
