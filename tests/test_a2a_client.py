from __future__ import annotations

import httpx
import pytest
from a2a.types import a2a_pb2 as pb2

from bridge import a2a_client
from bridge.a2a_client import fetch_agent_card
from bridge.config import AgentConfig


@pytest.fixture
def agent_and_card(fake_a2a_agent):
    fake, base_url = fake_a2a_agent
    return fake, AgentConfig(agent_id="docs-agent", base_url=base_url)


async def _card(agent):
    async with httpx.AsyncClient() as client:
        return await fetch_agent_card(client, agent)


def test_stable_message_id_is_deterministic():
    a = a2a_client.stable_message_id("docs-agent", "summarize", "hello")
    b = a2a_client.stable_message_id("docs-agent", "summarize", "hello")
    assert a == b


def test_stable_message_id_differs_for_different_content():
    a = a2a_client.stable_message_id("docs-agent", "summarize", "hello")
    b = a2a_client.stable_message_id("docs-agent", "summarize", "goodbye")
    assert a != b


@pytest.mark.asyncio
async def test_send_message_opens_a_new_task(agent_and_card):
    fake, agent = agent_and_card
    card = await _card(agent)
    async with httpx.AsyncClient() as client:
        response = await a2a_client.send_message(
            client, agent, card, text="Summarize this", message_id="mid-1"
        )
    assert response.WhichOneof("payload") == "task"
    assert response.task.status.state == pb2.TASK_STATE_WORKING
    assert len(fake.received_send_message_params) == 1


@pytest.mark.asyncio
async def test_send_message_sets_a2a_version_header(agent_and_card):
    fake, agent = agent_and_card
    card = await _card(agent)
    async with httpx.AsyncClient() as client:
        await a2a_client.send_message(client, agent, card, text="hi", message_id="mid-1")
    assert fake.received_headers[0].get("a2a-version") == "1.0"


@pytest.mark.asyncio
async def test_send_message_continues_existing_task(agent_and_card):
    fake, agent = agent_and_card
    card = await _card(agent)
    fake.seed_task("existing-task", "existing-ctx", "TASK_STATE_INPUT_REQUIRED")
    async with httpx.AsyncClient() as client:
        response = await a2a_client.send_message(
            client,
            agent,
            card,
            text="my answer",
            message_id="mid-2",
            a2a_task_id="existing-task",
            a2a_context_id="existing-ctx",
        )
    sent = fake.received_send_message_params[0]["message"]
    assert sent["taskId"] == "existing-task"
    assert sent["contextId"] == "existing-ctx"
    assert response.task.id == "existing-task"


@pytest.mark.asyncio
async def test_get_task_polls_current_state(agent_and_card):
    fake, agent = agent_and_card
    card = await _card(agent)
    fake.seed_task("t1", "c1", "TASK_STATE_COMPLETED")
    async with httpx.AsyncClient() as client:
        task = await a2a_client.get_task(client, agent, card, a2a_task_id="t1")
    assert task.status.state == pb2.TASK_STATE_COMPLETED
    assert task.id == "t1"


@pytest.mark.asyncio
async def test_cancel_task(agent_and_card):
    fake, agent = agent_and_card
    card = await _card(agent)
    fake.seed_task("t1", "c1", "TASK_STATE_WORKING")
    async with httpx.AsyncClient() as client:
        task = await a2a_client.cancel_task(client, agent, card, a2a_task_id="t1")
    assert task.status.state == pb2.TASK_STATE_CANCELED


@pytest.mark.asyncio
async def test_send_message_can_return_immediate_input_required(agent_and_card):
    # SPEC-NOTES.md §3.1: with return_immediately=True the very first
    # response may already be TASK_STATE_INPUT_REQUIRED -- tasks.py must not
    # assume WORKING on first return.
    fake, agent = agent_and_card
    fake.initial_state = "TASK_STATE_INPUT_REQUIRED"
    card = await _card(agent)
    async with httpx.AsyncClient() as client:
        response = await a2a_client.send_message(client, agent, card, text="hi", message_id="mid-1")
    assert response.task.status.state == pb2.TASK_STATE_INPUT_REQUIRED
