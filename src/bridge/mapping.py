"""Pure translation functions: A2A <-> MCP. No I/O, exhaustively unit-testable.

Field/shape provenance: SPEC-NOTES.md §5 (A2A TaskState), §10 (AgentCard/AgentSkill),
§3.1 (Tasks-extension input_required resolution), §11 (result envelope fields).

Callers pass in anything time-dependent (timestamps) rather than this module
reading the clock -- keeps every function here a pure, deterministic map from
inputs to output, testable without mocking.
"""

from __future__ import annotations

from typing import Any

from a2a.types import a2a_pb2 as _a2a_pb2
from google.protobuf import json_format

from bridge import artifacts

TOOL_NAME_SEPARATOR = "__"

# A2A TaskState -> MCP Tasks-extension TaskStatus. Exhaustive match: an
# unrecognised state raises rather than silently degrading (build brief §3).
# Note the spelling mismatch: A2A CANCELED (one L) -> MCP "cancelled" (two
# L's) -- easy to typo either direction.
_A2A_STATE_TO_MCP_STATUS: dict[int, str] = {
    _a2a_pb2.TASK_STATE_SUBMITTED: "working",
    _a2a_pb2.TASK_STATE_WORKING: "working",
    _a2a_pb2.TASK_STATE_INPUT_REQUIRED: "input_required",
    _a2a_pb2.TASK_STATE_AUTH_REQUIRED: "input_required",
    _a2a_pb2.TASK_STATE_COMPLETED: "completed",
    _a2a_pb2.TASK_STATE_FAILED: "failed",
    _a2a_pb2.TASK_STATE_REJECTED: "failed",
    _a2a_pb2.TASK_STATE_CANCELED: "cancelled",
}


def a2a_state_to_mcp_status(state: int) -> str:
    try:
        return _A2A_STATE_TO_MCP_STATUS[state]
    except KeyError:
        raise ValueError(f"unrecognised A2A TaskState: {state!r}") from None


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


def _status_message_text(a2a_task: Any) -> str | None:
    message = a2a_task.status.message
    # Message() with no parts set is protobuf's "not present" for an
    # optional sub-message on proto3 -- HasField would also work here, but
    # checking for text content directly is simpler and equally correct.
    texts = [part.text for part in message.parts if part.WhichOneof("content") == "text"]
    text = "\n".join(t for t in texts if t)
    return text or None


def _url_part(a2a_task: Any) -> str | None:
    for part in a2a_task.status.message.parts:
        if part.WhichOneof("content") == "url":
            return part.url
    return None


def data_part_dict(a2a_task: Any) -> dict | None:
    """The first structured DataPart in the status message, as a plain
    dict, or None if there isn't one. Used to derive a real schema instead
    of guessing one -- see input_requests_for_task.
    """
    for part in a2a_task.status.message.parts:
        if part.WhichOneof("content") == "data":
            decoded = json_format.MessageToDict(part.data)
            if isinstance(decoded, dict) and decoded:
                return decoded
    return None


def _json_type_for(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _schema_from_example(example: dict) -> dict:
    """Derive a JSON Schema from a concrete example the agent actually
    sent -- field names and types come directly from `example`, nothing is
    invented (build brief / user direction: "do not synthesise structure
    that isn't there").
    """
    properties = {key: {"type": _json_type_for(value)} for key, value in example.items()}
    return {"type": "object", "properties": properties, "required": list(properties)}


# Synthetic key for the single elicitation this bridge raises per
# input_required task. A2A doesn't hand back multiple discrete input
# requests for one status -- see SPEC-NOTES.md §3.1/§11 for why
# Tasks-extension inputRequests (not core requestState) is the right
# mechanism here, and this module's docstring for why the shape below
# (one ElicitRequest keyed "response") is this bridge's own design choice,
# not a spec-mandated structure.
INPUT_REQUEST_KEY = "response"

# Separate key for AUTH_REQUIRED -- deliberately never sharing a key or a
# schema shape with INPUT_REQUEST_KEY. See auth_requests_for_task.
AUTH_REQUEST_KEY = "auth"


def input_requests_for_task(a2a_task: Any) -> dict:
    """TASK_STATE_INPUT_REQUIRED -> inputRequests. A2A hands back prose in
    status.message, not a schema; MCP wants structured inputRequests. This
    is a genuine translation gap (build brief / user direction), so: prefer
    a structured DataPart if the agent included one (schema derived from
    its actual shape), otherwise fall back to a single free-text field --
    never invent structure the agent didn't provide.
    """
    prompt = _status_message_text(a2a_task) or "The agent needs additional input to continue."
    example = data_part_dict(a2a_task)
    if example is not None:
        schema = _schema_from_example(example)
    else:
        schema = {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "Your response to the agent."}},
            "required": ["message"],
        }
    return {
        INPUT_REQUEST_KEY: {
            "method": "elicitation/create",
            "params": {"message": prompt, "requestedSchema": schema},
        }
    }


def auth_requests_for_task(a2a_task: Any) -> dict:
    """TASK_STATE_AUTH_REQUIRED -> inputRequests. NOT "input_required with a
    password field" -- the A2A spec requires credentials to be exchanged
    out-of-band unless an in-band mechanism was explicitly negotiated, and
    the agent may resume on its own once the credential arrives, with no
    follow-up message required. Anything routed through inputResponses
    passes through the calling model's context (transcripts, logs,
    anywhere else context goes) -- credentials must never go there. See
    CLAUDE.md anti-goals and SPEC-NOTES.md §14.

    If the agent supplied a URL (SEP-1036 url-mode elicitation is exactly
    this case), surface it for the human to open out-of-band; the schema
    carries no requestedSchema at all, so there is nothing for a model to
    fill in. If there's no URL, fall back to a message-only, zero-property
    schema -- still nothing fillable, just an instruction to wait and poll.
    Either way, tasks/update for this key never forwards anything into the
    A2A task (see tasks.py); the client is only ever consenting/polling.
    """
    prompt = _status_message_text(a2a_task) or "The agent requires authentication."
    url = _url_part(a2a_task)
    if url:
        params: dict[str, Any] = {"mode": "url", "message": prompt, "url": url}
    else:
        params = {
            "mode": "form",
            "message": f"{prompt} Complete this out of band; the agent will resume automatically once authenticated.",
            "requestedSchema": {"type": "object", "properties": {}, "required": []},
        }
    return {AUTH_REQUEST_KEY: {"method": "elicitation/create", "params": params}}


def completed_task_result(a2a_task: Any) -> dict:
    """CompletedTask.result -- shaped like the CallToolResult tools/call
    would have returned synchronously (ext-tasks: "the result field contains
    what the original request would have returned"), so it carries its own
    resultType too.
    """
    summaries = artifacts.summarize_artifacts(list(a2a_task.artifacts))
    content = [s["content"] for s in summaries] or [{"type": "text", "text": ""}]
    result: dict[str, Any] = {"resultType": "complete", "content": content}
    extra = [
        {k: v for k, v in s.items() if k not in ("content",)}
        for s in summaries
        if any(k in s for k in ("truncated", "parts"))
    ]
    if extra:
        result["structuredContent"] = {"artifacts": extra}
    return result


def failed_task_error(a2a_task: Any, *, a2a_state_name: str) -> dict:
    """FailedTask.error -- "the JSON-RPC error" per ext-tasks docs, so it
    gets the standard {code, message, data?} shape. -32603 (Internal error)
    is this bridge's own judgment call for "downstream A2A task failed" --
    MCP has no dedicated code for a translated foreign-system failure; the
    real detail travels in `data.a2aState`.
    """
    message = _status_message_text(a2a_task) or "The A2A task did not complete successfully."
    return {"code": -32603, "message": message, "data": {"a2aState": a2a_state_name}}


def a2a_task_to_mcp_task(
    a2a_task: Any,
    *,
    mcp_task_id: str,
    created_at_iso: str,
    last_updated_at_iso: str,
    ttl_ms: int,
    poll_interval_ms: int = 2000,
) -> dict:
    """Build the full MCP DetailedTask dict for one A2A Task snapshot.

    createdAt/lastUpdatedAt/ttlMs are caller-supplied (see module docstring)
    -- ttl_ms in particular should come from state.py's envelope exp, so the
    two stay in sync (SPEC-NOTES.md §12c).
    """
    status = a2a_state_to_mcp_status(a2a_task.status.state)

    task: dict[str, Any] = {
        "taskId": mcp_task_id,
        "status": status,
        "createdAt": created_at_iso,
        "lastUpdatedAt": last_updated_at_iso,
        "ttlMs": ttl_ms,
        "pollIntervalMs": poll_interval_ms,
    }
    status_message = _status_message_text(a2a_task)
    if status_message:
        task["statusMessage"] = status_message

    if status == "input_required":
        # MCP's TaskStatus enum has no separate "auth_required" value (both
        # A2A states collapse to "input_required" -- see _A2A_STATE_TO_MCP_STATUS),
        # but the two need very different inputRequests: never conflate them.
        if a2a_task.status.state == _a2a_pb2.TASK_STATE_AUTH_REQUIRED:
            task["inputRequests"] = auth_requests_for_task(a2a_task)
        else:
            task["inputRequests"] = input_requests_for_task(a2a_task)
    elif status == "completed":
        task["result"] = completed_task_result(a2a_task)
    elif status == "failed":
        state_name = _a2a_pb2.TaskState.Name(a2a_task.status.state)
        task["error"] = failed_task_error(a2a_task, a2a_state_name=state_name)

    return task
