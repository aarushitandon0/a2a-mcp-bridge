from __future__ import annotations

from types import SimpleNamespace

import pytest
from a2a.types import Artifact, Message, Part, Task, TaskStatus, a2a_pb2 as pb2

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


# -- A2A TaskState -> MCP status (SPEC-NOTES.md §5, §11)


@pytest.mark.parametrize(
    "a2a_state,expected",
    [
        (pb2.TASK_STATE_SUBMITTED, "working"),
        (pb2.TASK_STATE_WORKING, "working"),
        (pb2.TASK_STATE_INPUT_REQUIRED, "input_required"),
        (pb2.TASK_STATE_AUTH_REQUIRED, "input_required"),
        (pb2.TASK_STATE_COMPLETED, "completed"),
        (pb2.TASK_STATE_FAILED, "failed"),
        (pb2.TASK_STATE_REJECTED, "failed"),
        (pb2.TASK_STATE_CANCELED, "cancelled"),  # A2A one-L -> MCP two-L, deliberate
    ],
)
def test_a2a_state_to_mcp_status_exhaustive_mapping(a2a_state, expected):
    assert mapping.a2a_state_to_mcp_status(a2a_state) == expected


def test_a2a_state_to_mcp_status_raises_on_unspecified():
    # TASK_STATE_UNSPECIFIED (0) is never a legitimate observed value --
    # exhaustive match must raise, not silently degrade (build brief §3).
    with pytest.raises(ValueError):
        mapping.a2a_state_to_mcp_status(pb2.TASK_STATE_UNSPECIFIED)


def test_a2a_state_to_mcp_status_raises_on_garbage_int():
    with pytest.raises(ValueError):
        mapping.a2a_state_to_mcp_status(9999)


def _agent_message(text):
    return Message(message_id="m1", role=pb2.ROLE_AGENT, parts=[Part(text=text)])


def _task(state, *, message_text=None, artifacts_=None):
    status = TaskStatus(state=state)
    if message_text is not None:
        status = TaskStatus(state=state, message=_agent_message(message_text))
    return Task(id="t1", context_id="c1", status=status, artifacts=artifacts_ or [])


def test_input_requests_for_task_uses_status_message_as_prompt():
    task = _task(pb2.TASK_STATE_INPUT_REQUIRED, message_text="What is your name?")
    requests = mapping.input_requests_for_task(task)
    req = requests[mapping.INPUT_REQUEST_KEY]
    assert req["method"] == "elicitation/create"
    assert req["params"]["message"] == "What is your name?"
    assert req["params"]["requestedSchema"]["required"] == ["message"]


def test_input_requests_for_task_falls_back_without_status_message():
    task = _task(pb2.TASK_STATE_AUTH_REQUIRED)
    requests = mapping.input_requests_for_task(task)
    assert requests[mapping.INPUT_REQUEST_KEY]["params"]["message"]


def test_completed_task_result_from_artifacts():
    task = _task(pb2.TASK_STATE_COMPLETED, artifacts_=[Artifact(artifact_id="a1", parts=[Part(text="done")])])
    result = mapping.completed_task_result(task)
    assert result["resultType"] == "complete"
    assert result["content"] == [{"type": "text", "text": "done"}]
    assert "structuredContent" not in result


def test_completed_task_result_with_no_artifacts_has_empty_text_content():
    task = _task(pb2.TASK_STATE_COMPLETED)
    result = mapping.completed_task_result(task)
    assert result["content"] == [{"type": "text", "text": ""}]


def test_completed_task_result_surfaces_truncation_in_structured_content():
    long_text = "x" * 5000
    task = _task(
        pb2.TASK_STATE_COMPLETED, artifacts_=[Artifact(artifact_id="a1", parts=[Part(text=long_text)])]
    )
    result = mapping.completed_task_result(task)
    assert result["structuredContent"]["artifacts"][0]["truncated"] is True


def test_failed_task_error_shape():
    task = _task(pb2.TASK_STATE_FAILED, message_text="agent exploded")
    error = mapping.failed_task_error(task, a2a_state_name="TASK_STATE_FAILED")
    assert error["code"] == -32603
    assert error["message"] == "agent exploded"
    assert error["data"] == {"a2aState": "TASK_STATE_FAILED"}


def test_a2a_task_to_mcp_task_working():
    task = _task(pb2.TASK_STATE_WORKING)
    result = mapping.a2a_task_to_mcp_task(
        task, mcp_task_id="sealed-abc", created_at_iso="2026-08-11T00:00:00Z",
        last_updated_at_iso="2026-08-11T00:00:01Z", ttl_ms=60_000,
    )
    assert result["taskId"] == "sealed-abc"
    assert result["status"] == "working"
    assert "inputRequests" not in result
    assert "result" not in result
    assert "error" not in result


def test_a2a_task_to_mcp_task_input_required_carries_input_requests():
    task = _task(pb2.TASK_STATE_INPUT_REQUIRED, message_text="Confirm?")
    result = mapping.a2a_task_to_mcp_task(
        task, mcp_task_id="sealed-abc", created_at_iso="t0", last_updated_at_iso="t1", ttl_ms=60_000,
    )
    assert result["status"] == "input_required"
    assert result["statusMessage"] == "Confirm?"
    assert mapping.INPUT_REQUEST_KEY in result["inputRequests"]


def test_a2a_task_to_mcp_task_completed_carries_result():
    task = _task(pb2.TASK_STATE_COMPLETED, artifacts_=[Artifact(artifact_id="a1", parts=[Part(text="done")])])
    result = mapping.a2a_task_to_mcp_task(
        task, mcp_task_id="sealed-abc", created_at_iso="t0", last_updated_at_iso="t1", ttl_ms=60_000,
    )
    assert result["status"] == "completed"
    assert result["result"]["content"] == [{"type": "text", "text": "done"}]


def test_a2a_task_to_mcp_task_failed_carries_error():
    task = _task(pb2.TASK_STATE_REJECTED, message_text="not allowed")
    result = mapping.a2a_task_to_mcp_task(
        task, mcp_task_id="sealed-abc", created_at_iso="t0", last_updated_at_iso="t1", ttl_ms=60_000,
    )
    assert result["status"] == "failed"
    assert result["error"]["data"] == {"a2aState": "TASK_STATE_REJECTED"}
