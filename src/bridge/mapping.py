"""Pure translation functions: A2A <-> MCP. No I/O, exhaustively unit-testable.

Field/shape provenance: SPEC-NOTES.md §5 (A2A TaskState), §10 (AgentCard/AgentSkill).
"""

from __future__ import annotations

from typing import Any

TOOL_NAME_SEPARATOR = "__"


def make_tool_name(agent_id: str, skill_id: str) -> str:
    return f"{agent_id}{TOOL_NAME_SEPARATOR}{skill_id}"


def parse_tool_name(tool_name: str) -> tuple[str, str]:
    """Split a namespaced MCP tool name back into (agent_id, skill_id)."""
    agent_id, sep, skill_id = tool_name.partition(TOOL_NAME_SEPARATOR)
    if not sep:
        raise ValueError(f"not a namespaced tool name: {tool_name!r}")
    return agent_id, skill_id


def skill_to_tool(agent_id: str, skill: Any, default_input_modes: list[str]) -> dict:
    """Project one A2A AgentSkill into an MCP Tool description.

    A2A skills declare accepted MIME types (input_modes), not a JSON Schema of
    fields -- see SPEC-NOTES.md §10. The projected tool exposes a single
    required `message` string field; input_modes become a description hint,
    not schema properties, since A2A has nothing more granular to offer.
    """
    input_modes = list(skill.input_modes) or list(default_input_modes)
    description = skill.description
    if input_modes:
        description = f"{description}\n\nAccepts: {', '.join(input_modes)}"

    return {
        "name": make_tool_name(agent_id, skill.id),
        "title": skill.name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Message text to send to the agent for this skill.",
                }
            },
            "required": ["message"],
        },
    }
