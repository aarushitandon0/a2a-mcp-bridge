"""Thin async A2A client wrapper. Never hand-roll A2A types -- use the SDK's.

Field/method names here trace to SPEC-NOTES.md §5, §6, §10. Task operations
use the SDK's JsonRpcTransport directly rather than the higher-level
Client/ClientFactory abstraction: the transport's send_message/get_task/
cancel_task return the SDK's real proto types directly (SendMessageResponse,
Task), while Client wraps everything in an AsyncIterator[StreamResponse]
interface designed to unify streaming and non-streaming -- unnecessary
complexity here since this bridge always sends non-streaming
(return_immediately=True; see SPEC-NOTES.md §3.1 for why).
"""

from __future__ import annotations

import hashlib

import httpx
from a2a.client.card_resolver import A2ACardResolver
from a2a.client.client import ClientCallContext
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    Message,
    Part,
    SendMessageConfiguration,
    SendMessageRequest,
    SendMessageResponse,
    Task,
    a2a_pb2 as pb2,
)
from a2a.types import GetTaskRequest
from google.protobuf.struct_pb2 import Value

from bridge.config import AgentConfig

A2A_VERSION_HEADER = "A2A-Version"
A2A_VERSION = "1.0"  # SPEC-NOTES.md §9 Unresolved #1 (resolved)


class NoJsonRpcInterface(Exception):
    """The agent's card doesn't advertise a JSONRPC-binding interface.

    This bridge only speaks the JSONRPC binding (SPEC-NOTES.md §5's
    confirmed method-name table is JSON-RPC-specific); an agent that only
    offers GRPC or HTTP+JSON isn't reachable by this bridge.
    """


def _agent_headers(agent: AgentConfig) -> dict[str, str]:
    return {A2A_VERSION_HEADER: A2A_VERSION, **agent.headers}


def _context(agent: AgentConfig) -> ClientCallContext:
    return ClientCallContext(service_parameters=_agent_headers(agent))


def _select_jsonrpc_url(card: AgentCard) -> str:
    for iface in card.supported_interfaces:
        if iface.protocol_binding == "JSONRPC":
            return iface.url
    raise NoJsonRpcInterface("agent card has no JSONRPC-binding entry in supported_interfaces")


def _transport(http_client: httpx.AsyncClient, card: AgentCard) -> JsonRpcTransport:
    return JsonRpcTransport(http_client, card, _select_jsonrpc_url(card))


async def fetch_agent_card(http_client: httpx.AsyncClient, agent: AgentConfig) -> AgentCard:
    """Fetch and parse the AgentCard from an agent's well-known path."""
    resolver = A2ACardResolver(http_client, agent.base_url)
    return await resolver.get_agent_card(http_kwargs={"headers": _agent_headers(agent)})


def stable_message_id(*parts: str) -> str:
    """Deterministic messageId from the parts of an MCP call, per the build
    brief: "derive a stable messageId from the MCP call rather than a fresh
    UUID per attempt, or an MCP-layer retry spawns a duplicate A2A task"
    (SendMessage MAY dedupe on messageId).

    Trade-off, named rather than hidden: this also dedupes two genuinely
    separate calls that happen to carry identical parts (same skill, same
    task/context, same text) into one A2A message. There is no
    client-supplied nonce available at this layer to distinguish "the same
    call, retried" from "a deliberate repeat" -- accepted for retry-safety.
    """
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


async def send_message(
    http_client: httpx.AsyncClient,
    agent: AgentConfig,
    card: AgentCard,
    *,
    text: str | None = None,
    data: dict | None = None,
    message_id: str,
    a2a_task_id: str | None = None,
    a2a_context_id: str | None = None,
) -> SendMessageResponse:
    """Non-blocking send (return_immediately=True -- SPEC-NOTES.md §3.1):
    opens a new task if a2a_task_id is None, otherwise continues the
    referenced task (A2A requires context_id to match when both are given).

    Exactly one of `text`/`data` -- reply in kind: if the agent asked via a
    structured DataPart (mapping.py's input_requests_for_task derived a
    schema from it), reply with a DataPart back; a free-text question gets
    a plain text reply. See tasks.py::tasks_update.
    """
    if (text is None) == (data is None):
        raise ValueError("send_message requires exactly one of text or data")

    if data is not None:
        value = Value()
        value.struct_value.update(data)
        parts = [Part(data=value)]
    else:
        parts = [Part(text=text)]

    message_kwargs: dict[str, object] = {
        "message_id": message_id,
        "role": pb2.ROLE_USER,
        "parts": parts,
    }
    if a2a_task_id:
        message_kwargs["task_id"] = a2a_task_id
    if a2a_context_id:
        message_kwargs["context_id"] = a2a_context_id

    request = SendMessageRequest(
        message=Message(**message_kwargs),
        configuration=SendMessageConfiguration(return_immediately=True),
    )
    return await _transport(http_client, card).send_message(request, context=_context(agent))


async def get_task(http_client: httpx.AsyncClient, agent: AgentConfig, card: AgentCard, *, a2a_task_id: str) -> Task:
    request = GetTaskRequest(id=a2a_task_id)
    return await _transport(http_client, card).get_task(request, context=_context(agent))


async def cancel_task(
    http_client: httpx.AsyncClient, agent: AgentConfig, card: AgentCard, *, a2a_task_id: str
) -> Task:
    request = CancelTaskRequest(id=a2a_task_id)
    return await _transport(http_client, card).cancel_task(request, context=_context(agent))
