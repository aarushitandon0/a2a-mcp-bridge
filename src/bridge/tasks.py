"""Tasks extension handlers: tools/call (opens a task), tasks/get,
tasks/update, tasks/cancel. Wires together state.py (sealed handle),
a2a_client.py (the A2A side), and mapping.py (pure translation).

Naming note (build brief anti-goal): this module's public functions are
`tools_call`/`tasks_get`/`tasks_update`/`tasks_cancel` -- the MCP-side
method names -- deliberately not `get_task`/`cancel_task`, which are
a2a_client.py's names for the *A2A* RPCs. Same shape of idea, two different
wire protocols; keep them lexically distinct here too.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from a2a.client.errors import A2AClientError

from bridge import a2a_client, mapping, state
from bridge.config import AgentConfig

# A2A's Task message has no ttl/expiry field at all (SPEC-NOTES.md §12c) --
# this bridge has to pick a bound itself. 24h is a judgment call, not a spec
# value; long enough for a human-in-the-loop approval to sit unanswered
# overnight, short enough that a leaked/forgotten handle doesn't stay valid
# forever.
DEFAULT_TASK_TTL_SECONDS = 24 * 3600
DEFAULT_POLL_INTERVAL_MS = 2000

TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"


def _client_declared_tasks_extension(params: dict) -> bool:
    meta = params.get("_meta") or {}
    capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY) or {}
    extensions = capabilities.get("extensions") or {}
    return TASKS_EXTENSION_ID in extensions


class TasksError(Exception):
    """Base for every error this module raises; server.py translates these
    into JSON-RPC errors. Never let one of these leak past dispatch as a
    raw 500 -- each represents a specific, expected rejection reason."""


class UnknownTool(TasksError):
    pass


class MissingRequiredCapability(TasksError):
    """The client didn't declare a capability this request needs -- e.g. a
    tool call that will open a task, but the client never declared the
    io.modelcontextprotocol/tasks extension. ext-tasks spec: "Never return a
    task to a client that did not declare support." Maps to MCP's
    -32021 MissingRequiredClientCapabilityError (SPEC-NOTES.md §11.6)."""

    def __init__(self, required: dict):
        super().__init__(f"client did not declare required capabilities: {required!r}")
        self.required = required


class InvalidTaskHandle(TasksError):
    pass


class TaskHandleExpired(TasksError):
    def __init__(self, handle: state.TaskHandle):
        super().__init__(f"task handle expired (agent_id={handle.agent_id!r})")
        self.handle = handle


class ElicitationNotAccepted(TasksError):
    pass


class MissingInputResponse(TasksError):
    pass


class DownstreamAgentError(TasksError):
    """The A2A call itself failed (agent-side JSON-RPC error)."""


def _unseal(token: str, *, keys: list[bytes], agents: dict[str, AgentConfig], now: int) -> state.TaskHandle:
    try:
        return state.unseal(token, keys=keys, known_agent_ids=set(agents), now=now)
    except state.ExpiredEnvelope as exc:
        raise TaskHandleExpired(exc.handle) from exc
    except state.EnvelopeError as exc:
        raise InvalidTaskHandle(str(exc)) from exc


async def _fetch_card(http_client: httpx.AsyncClient, agent: AgentConfig):
    return await a2a_client.fetch_agent_card(http_client, agent)


def _iso_now(now: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _last_updated_iso(a2a_task: Any, *, fallback_now: float) -> str:
    if a2a_task.status.HasField("timestamp"):
        return a2a_task.status.timestamp.ToJsonString()
    return _iso_now(fallback_now)


async def tools_call(
    http_client: httpx.AsyncClient,
    agents: dict[str, AgentConfig],
    keys: list[bytes],
    params: dict,
    *,
    now: float | None = None,
) -> dict:
    now = time.time() if now is None else now
    tool_name = params.get("name", "")
    arguments = params.get("arguments") or {}
    text = arguments.get("message", "")

    try:
        agent_id, skill_id = mapping.parse_tool_name(tool_name)
    except ValueError as exc:
        raise UnknownTool(str(exc)) from exc
    agent = agents.get(agent_id)
    if agent is None:
        raise UnknownTool(f"unknown agent in tool name: {agent_id!r}")

    card = await _fetch_card(http_client, agent)
    if not any(skill.id == skill_id for skill in card.skills):
        raise UnknownTool(f"agent {agent_id!r} has no skill {skill_id!r}")

    message_id = a2a_client.stable_message_id(agent_id, skill_id, text)
    try:
        response = await a2a_client.send_message(http_client, agent, card, text=text, message_id=message_id)
    except A2AClientError as exc:
        raise DownstreamAgentError(str(exc)) from exc

    if response.WhichOneof("payload") == "message":
        # The agent answered immediately without ever creating a task --
        # legitimate per A2A (SendMessage MAY respond with a bare Message).
        # Returned directly as a normal (non-task) CallToolResult.
        texts = [part.text for part in response.message.parts if part.WhichOneof("content") == "text"]
        return {"resultType": "complete", "content": [{"type": "text", "text": "\n".join(texts)}]}

    # ext-tasks spec: never return a task to a client that didn't declare
    # the extension (SPEC-NOTES.md §3, §11.6). Checked here, not earlier --
    # a client that doesn't want tasks can still call a skill that happens
    # to answer immediately (the `.message` branch above) without needing
    # to declare anything, and there's no way to know which branch a call
    # will take without actually sending it.
    #
    # A task may already exist agent-side by the time we find out the
    # client can't have it (return_immediately=True already created one).
    # Leaving it running with no handle handed out would orphan it -- best
    # effort cleanup via cancel_task before rejecting, rather than silently
    # abandoning it. Best effort: if the cancel itself fails, the original
    # capability error is still what the client needs to see.
    if not _client_declared_tasks_extension(params):
        try:
            await a2a_client.cancel_task(http_client, agent, card, a2a_task_id=response.task.id)
        except A2AClientError:
            pass
        raise MissingRequiredCapability({TASKS_EXTENSION_ID: {}})

    a2a_task = response.task
    exp = int(now) + DEFAULT_TASK_TTL_SECONDS
    handle = state.TaskHandle(
        agent_id=agent_id, a2a_task_id=a2a_task.id, a2a_context_id=a2a_task.context_id, exp=exp
    )
    mcp_task_id = state.seal(handle, key=keys[0])

    created_at = _iso_now(now)
    task = mapping.a2a_task_to_mcp_task(
        a2a_task,
        mcp_task_id=mcp_task_id,
        created_at_iso=created_at,
        last_updated_at_iso=_last_updated_iso(a2a_task, fallback_now=now),
        ttl_ms=DEFAULT_TASK_TTL_SECONDS * 1000,
        poll_interval_ms=DEFAULT_POLL_INTERVAL_MS,
    )
    return {"resultType": "complete", **task}


async def tasks_get(
    http_client: httpx.AsyncClient,
    agents: dict[str, AgentConfig],
    keys: list[bytes],
    params: dict,
    *,
    now: float | None = None,
) -> dict:
    now = time.time() if now is None else now
    handle = _unseal(params["taskId"], keys=keys, agents=agents, now=int(now))
    agent = agents[handle.agent_id]
    card = await _fetch_card(http_client, agent)

    try:
        a2a_task = await a2a_client.get_task(http_client, agent, card, a2a_task_id=handle.a2a_task_id)
    except A2AClientError as exc:
        raise DownstreamAgentError(str(exc)) from exc

    task = mapping.a2a_task_to_mcp_task(
        a2a_task,
        mcp_task_id=params["taskId"],
        created_at_iso=_iso_now(now),  # SPEC-NOTES.md §12a: no iat kept -- see state.py
        last_updated_at_iso=_last_updated_iso(a2a_task, fallback_now=now),
        ttl_ms=max(0, (handle.exp - int(now))) * 1000,
        poll_interval_ms=DEFAULT_POLL_INTERVAL_MS,
    )
    return {"resultType": "complete", **task}


async def tasks_update(
    http_client: httpx.AsyncClient,
    agents: dict[str, AgentConfig],
    keys: list[bytes],
    params: dict,
    *,
    now: float | None = None,
) -> dict:
    now = time.time() if now is None else now
    handle = _unseal(params["taskId"], keys=keys, agents=agents, now=int(now))
    agent = agents[handle.agent_id]

    input_responses = params.get("inputResponses") or {}
    response = input_responses.get(mapping.INPUT_REQUEST_KEY)
    if response is None:
        raise MissingInputResponse(f"expected an input response keyed {mapping.INPUT_REQUEST_KEY!r}")
    if response.get("action") != "accept":
        raise ElicitationNotAccepted(f"client did not accept the elicitation (action={response.get('action')!r})")
    text = (response.get("content") or {}).get("message", "")

    card = await _fetch_card(http_client, agent)
    message_id = a2a_client.stable_message_id(handle.agent_id, handle.a2a_task_id, text)
    try:
        await a2a_client.send_message(
            http_client,
            agent,
            card,
            text=text,
            message_id=message_id,
            a2a_task_id=handle.a2a_task_id,
            a2a_context_id=handle.a2a_context_id,
        )
    except A2AClientError as exc:
        raise DownstreamAgentError(str(exc)) from exc

    # UpdateTaskResult = Result -- bare ack, no Task/taskId echoed (ext-tasks
    # schema; taskId is immutable for the task's lifetime, see state.py).
    return {"resultType": "complete"}


async def tasks_cancel(
    http_client: httpx.AsyncClient,
    agents: dict[str, AgentConfig],
    keys: list[bytes],
    params: dict,
    *,
    now: float | None = None,
) -> dict:
    now = time.time() if now is None else now
    handle = _unseal(params["taskId"], keys=keys, agents=agents, now=int(now))
    agent = agents[handle.agent_id]
    card = await _fetch_card(http_client, agent)

    try:
        await a2a_client.cancel_task(http_client, agent, card, a2a_task_id=handle.a2a_task_id)
    except A2AClientError as exc:
        raise DownstreamAgentError(str(exc)) from exc

    return {"resultType": "complete"}
