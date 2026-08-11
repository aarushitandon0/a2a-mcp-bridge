from __future__ import annotations

from types import SimpleNamespace

import pytest

from bridge import mapping


def _skill(id_, name, description, input_modes=("text/plain",)):
    return SimpleNamespace(id=id_, name=name, description=description, input_modes=list(input_modes))


def test_make_and_parse_tool_name_round_trip():
    name = mapping.make_tool_name("docs-agent", "summarize")
    assert name == "docs-agent__summarize"
    assert mapping.parse_tool_name(name) == ("docs-agent", "summarize")


def test_parse_tool_name_rejects_non_namespaced():
    with pytest.raises(ValueError):
        mapping.parse_tool_name("summarize")


def test_skill_to_tool_namespaces_by_agent():
    tool = mapping.skill_to_tool("docs-agent", _skill("summarize", "Summarize", "Summarize a document."), [])
    assert tool["name"] == "docs-agent__summarize"
    assert tool["title"] == "Summarize"
    assert "Summarize a document." in tool["description"]


def test_skill_to_tool_input_schema_is_uniform_message_field():
    tool = mapping.skill_to_tool("docs-agent", _skill("summarize", "Summarize", "desc"), [])
    assert tool["inputSchema"] == {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Message text to send to the agent for this skill.",
            }
        },
        "required": ["message"],
    }


def test_skill_to_tool_falls_back_to_card_default_input_modes():
    tool = mapping.skill_to_tool(
        "docs-agent", _skill("summarize", "Summarize", "desc", input_modes=[]), ["application/json"]
    )
    assert "application/json" in tool["description"]


def test_skill_to_tool_prefers_skill_input_modes_over_default():
    tool = mapping.skill_to_tool(
        "docs-agent",
        _skill("summarize", "Summarize", "desc", input_modes=["text/markdown"]),
        ["application/json"],
    )
    assert "text/markdown" in tool["description"]
    assert "application/json" not in tool["description"]


def test_two_agents_same_skill_id_do_not_collide():
    tool_a = mapping.skill_to_tool("docs-agent", _skill("review", "Review", "desc"), [])
    tool_b = mapping.skill_to_tool("code-agent", _skill("review", "Review", "desc"), [])
    assert tool_a["name"] != tool_b["name"]
