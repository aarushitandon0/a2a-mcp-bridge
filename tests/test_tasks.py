from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from bridge.config import AgentConfig
from bridge.server import create_app

PROTOCOL_VERSION = "2026-07-28"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
STATE_KEY = b"\x04" * 32


def _headers(*, method, name=None):
    headers = {
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
        "Content-Type": "application/json",
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


def _body(*, method, params=None, declare_tasks_extension=True):
    meta = {PROTOCOL_VERSION_META_KEY: PROTOCOL_VERSION, CLIENT_CAPABILITIES_META_KEY: {}}
    if declare_tasks_extension:
        meta[CLIENT_CAPABILITIES_META_KEY] = {"extensions": {TASKS_EXTENSION_ID: {}}}
    p = dict(params or {})
    p["_meta"] = meta
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": p}


@pytest.fixture
def app_and_agent(fake_a2a_agent):
    fake, base_url = fake_a2a_agent
    agents = {"docs-agent": AgentConfig(agent_id="docs-agent", base_url=base_url)}
    app = create_app(agents, [STATE_KEY])
    with TestClient(app) as client:
        yield client, fake


def _call_summarize(client, text="Summarize this doc", declare_tasks_extension=True):
    body = _body(
        method="tools/call",
        params={"name": "docs-agent__summarize", "arguments": {"message": text}},
        declare_tasks_extension=declare_tasks_extension,
    )
    return client.post("/", json=body, headers=_headers(method="tools/call", name="docs-agent__summarize"))


def test_tools_call_opens_a_task_and_returns_a_sealed_handle(app_and_agent):
    client, fake = app_and_agent
    resp = _call_summarize(client)
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["resultType"] == "complete"
    assert result["status"] == "working"
    assert result["taskId"]
    # the handle is opaque and unrelated to the A2A task id it wraps
    assert result["taskId"] != next(iter(fake.tasks))


def test_tools_call_without_declaring_tasks_extension_is_rejected(app_and_agent):
    # A2A's return_immediately=True already creates the task agent-side
    # before the bridge can know it won't be allowed to hand out a handle
    # for it (there's no way to know in advance whether SendMessage will
    # answer immediately or open a task) -- the bridge's responsibility is
    # to not leave it orphaned, i.e. cancel it rather than never touch it.
    client, fake = app_and_agent
    resp = _call_summarize(client, declare_tasks_extension=False)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == -32021
    assert error["data"]["requiredCapabilities"] == {TASKS_EXTENSION_ID: {}}
    a2a_task_id = next(iter(fake.tasks))
    assert fake.tasks[a2a_task_id]["status"]["state"] == "TASK_STATE_CANCELED"


def test_tools_call_unknown_agent_is_invalid_params(app_and_agent):
    client, fake = app_and_agent
    body = _body(method="tools/call", params={"name": "nope-agent__summarize", "arguments": {"message": "x"}})
    resp = client.post("/", json=body, headers=_headers(method="tools/call", name="nope-agent__summarize"))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32602


def test_tools_call_unknown_skill_is_invalid_params(app_and_agent):
    client, fake = app_and_agent
    body = _body(method="tools/call", params={"name": "docs-agent__nope-skill", "arguments": {"message": "x"}})
    resp = client.post("/", json=body, headers=_headers(method="tools/call", name="docs-agent__nope-skill"))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32602


def test_tools_call_immediate_input_required_is_reflected_on_first_return(app_and_agent):
    # SPEC-NOTES.md §3.1: with return_immediately=True the first response
    # may already be TASK_STATE_INPUT_REQUIRED -- must not assume "working".
    client, fake = app_and_agent
    fake.initial_state = "TASK_STATE_INPUT_REQUIRED"
    resp = _call_summarize(client)
    result = resp.json()["result"]
    assert result["status"] == "input_required"
    assert "response" in result["inputRequests"]


def test_full_lifecycle_call_poll_answer_complete(app_and_agent):
    client, fake = app_and_agent

    call_resp = _call_summarize(client)
    task_id = call_resp.json()["result"]["taskId"]
    a2a_task_id = next(iter(fake.tasks))

    # poll while working
    get_resp = client.post(
        "/", json=_body(method="tasks/get", params={"taskId": task_id}), headers=_headers(method="tasks/get")
    )
    assert get_resp.json()["result"]["status"] == "working"

    # agent asks a question
    fake.set_state(a2a_task_id, "TASK_STATE_INPUT_REQUIRED", message_text="Which format?")
    get_resp = client.post(
        "/", json=_body(method="tasks/get", params={"taskId": task_id}), headers=_headers(method="tasks/get")
    )
    result = get_resp.json()["result"]
    assert result["status"] == "input_required"
    assert result["statusMessage"] == "Which format?"

    # client answers via tasks/update
    update_body = _body(
        method="tasks/update",
        params={
            "taskId": task_id,
            "inputResponses": {"response": {"action": "accept", "content": {"message": "PDF please"}}},
        },
    )
    update_resp = client.post("/", json=update_body, headers=_headers(method="tasks/update"))
    assert update_resp.status_code == 200
    assert update_resp.json()["result"] == {"resultType": "complete"}

    # the answer reached the fake agent as a continuation of the same task
    last_send = fake.received_send_message_params[-1]["message"]
    assert last_send["taskId"] == a2a_task_id
    assert last_send["parts"][0]["text"] == "PDF please"

    # agent finishes
    fake.set_state(a2a_task_id, "TASK_STATE_COMPLETED")
    fake.tasks[a2a_task_id]["artifacts"] = [
        {"artifactId": "a1", "parts": [{"text": "Summary: it's a PDF."}]}
    ]
    final = client.post(
        "/", json=_body(method="tasks/get", params={"taskId": task_id}), headers=_headers(method="tasks/get")
    )
    result = final.json()["result"]
    assert result["status"] == "completed"
    assert result["result"]["content"] == [{"type": "text", "text": "Summary: it's a PDF."}]
    # taskId is stable across the whole lifecycle (ext-tasks: immutable handle)
    assert result["taskId"] == task_id


def test_tasks_cancel(app_and_agent):
    client, fake = app_and_agent
    call_resp = _call_summarize(client)
    task_id = call_resp.json()["result"]["taskId"]

    cancel_resp = client.post(
        "/", json=_body(method="tasks/cancel", params={"taskId": task_id}), headers=_headers(method="tasks/cancel")
    )
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["result"] == {"resultType": "complete"}

    a2a_task_id = next(iter(fake.tasks))
    assert fake.tasks[a2a_task_id]["status"]["state"] == "TASK_STATE_CANCELED"


def test_tasks_get_with_tampered_taskid_is_invalid_params(app_and_agent):
    client, fake = app_and_agent
    resp = client.post(
        "/",
        json=_body(method="tasks/get", params={"taskId": "not-a-real-handle"}),
        headers=_headers(method="tasks/get"),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32602


def test_tasks_get_with_expired_handle_is_distinct_error(app_and_agent):
    client, fake = app_and_agent
    from bridge import state

    handle = state.TaskHandle(agent_id="docs-agent", a2a_task_id="t1", a2a_context_id="c1", exp=1)
    expired_token = state.seal(handle, key=STATE_KEY)
    resp = client.post(
        "/",
        json=_body(method="tasks/get", params={"taskId": expired_token}),
        headers=_headers(method="tasks/get"),
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == -32602
    assert error["data"]["reason"] == "expired"


def test_retrying_tools_call_with_same_arguments_does_not_duplicate_the_a2a_task(app_and_agent):
    # build brief: "derive a stable messageId from the MCP call rather than
    # a fresh UUID per attempt, or an MCP-layer retry spawns a duplicate A2A
    # task." The fake agent creates a new task per unique messageId it
    # hasn't seen with a taskId attached; since our bridge doesn't send a
    # taskId on the first call, dedup at the fake-agent layer isn't what's
    # under test here -- what's under test is that the SAME messageId is
    # sent both times, which a real A2A agent would dedupe on.
    client, fake = app_and_agent
    _call_summarize(client, text="same text")
    _call_summarize(client, text="same text")
    message_ids = [p["message"]["messageId"] for p in fake.received_send_message_params]
    assert message_ids[0] == message_ids[1]
