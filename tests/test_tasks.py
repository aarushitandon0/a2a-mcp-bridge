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


# -- M3: two sequential input-required rounds on one task.


def test_two_sequential_input_required_rounds(app_and_agent):
    client, fake = app_and_agent
    call_resp = _call_summarize(client)
    task_id = call_resp.json()["result"]["taskId"]
    a2a_task_id = next(iter(fake.tasks))

    def _update(text):
        body = _body(
            method="tasks/update",
            params={"taskId": task_id, "inputResponses": {"response": {"action": "accept", "content": {"message": text}}}},
        )
        return client.post("/", json=body, headers=_headers(method="tasks/update"))

    fake.set_state(a2a_task_id, "TASK_STATE_INPUT_REQUIRED", message_text="Which format?")
    resp1 = _update("PDF")
    assert resp1.status_code == 200

    fake.set_state(a2a_task_id, "TASK_STATE_INPUT_REQUIRED", message_text="Which language?")
    get_resp = client.post(
        "/", json=_body(method="tasks/get", params={"taskId": task_id}), headers=_headers(method="tasks/get")
    )
    assert get_resp.json()["result"]["statusMessage"] == "Which language?"
    resp2 = _update("English")
    assert resp2.status_code == 200

    texts_sent = [p["message"]["parts"][0]["text"] for p in fake.received_send_message_params[1:]]
    assert texts_sent == ["PDF", "English"]
    # both rounds continued the SAME A2A task -- never opened a second one
    assert list(fake.tasks) == [a2a_task_id]
    # and the two rounds got different messageIds despite hitting the same
    # task (different answer text)
    message_ids = [p["message"]["messageId"] for p in fake.received_send_message_params[1:]]
    assert message_ids[0] != message_ids[1]

    fake.set_state(a2a_task_id, "TASK_STATE_COMPLETED")
    final = client.post(
        "/", json=_body(method="tasks/get", params={"taskId": task_id}), headers=_headers(method="tasks/get")
    )
    assert final.json()["result"]["status"] == "completed"


# -- M3: tasks/update against a terminal task -- A2A's UnsupportedOperationError
# mapped cleanly, never leaked raw.


def test_tasks_update_against_terminal_task_is_rejected_without_ever_calling_send_message(app_and_agent):
    client, fake = app_and_agent
    call_resp = _call_summarize(client)
    task_id = call_resp.json()["result"]["taskId"]
    a2a_task_id = next(iter(fake.tasks))
    fake.set_state(a2a_task_id, "TASK_STATE_COMPLETED")

    calls_before = len(fake.received_send_message_params)
    body = _body(
        method="tasks/update",
        params={"taskId": task_id, "inputResponses": {"response": {"action": "accept", "content": {"message": "x"}}}},
    )
    resp = client.post("/", json=body, headers=_headers(method="tasks/update"))
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == -32602
    assert error["data"]["reason"] == "task_terminal"
    assert error["data"]["a2aState"] == "TASK_STATE_COMPLETED"
    # never attempted to send anything into the terminal task
    assert len(fake.received_send_message_params) == calls_before


def test_tasks_update_maps_a2a_unsupported_operation_error_without_leaking_it_raw(app_and_agent):
    # Simulates the race window between the bridge's own GetTask (reports
    # non-terminal) and its SendMessage (A2A itself rejects) -- the fake
    # agent is configured to reject SendMessage for this task id
    # unconditionally, independent of what GetTask reports.
    client, fake = app_and_agent
    call_resp = _call_summarize(client)
    task_id = call_resp.json()["result"]["taskId"]
    a2a_task_id = next(iter(fake.tasks))
    fake.set_state(a2a_task_id, "TASK_STATE_INPUT_REQUIRED", message_text="Which format?")
    fake.unsupported_operation_task_ids.add(a2a_task_id)

    body = _body(
        method="tasks/update",
        params={"taskId": task_id, "inputResponses": {"response": {"action": "accept", "content": {"message": "PDF"}}}},
    )
    resp = client.post("/", json=body, headers=_headers(method="tasks/update"))
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == -32602
    assert error["data"]["reason"] == "task_terminal"
    # the raw A2A error text/code never appears verbatim in the MCP error
    assert "-32004" not in error["message"]


def test_tasks_update_when_task_is_just_working_is_rejected(app_and_agent):
    client, fake = app_and_agent
    call_resp = _call_summarize(client)
    task_id = call_resp.json()["result"]["taskId"]
    # freshly created task is TASK_STATE_WORKING -- nothing outstanding
    body = _body(
        method="tasks/update",
        params={"taskId": task_id, "inputResponses": {"response": {"action": "accept", "content": {"message": "x"}}}},
    )
    resp = client.post("/", json=body, headers=_headers(method="tasks/update"))
    assert resp.status_code == 400
    assert resp.json()["error"]["data"]["reason"] == "not_awaiting_input"


# -- M3: TASK_STATE_AUTH_REQUIRED is a distinct path -- never inputResponses
# carrying a credential, never forwarded into the A2A task.


def test_auth_required_full_flow_never_sends_anything_into_a2a(app_and_agent):
    client, fake = app_and_agent
    call_resp = _call_summarize(client)
    task_id = call_resp.json()["result"]["taskId"]
    a2a_task_id = next(iter(fake.tasks))

    fake.set_state_with_parts(
        a2a_task_id,
        "TASK_STATE_AUTH_REQUIRED",
        [{"url": "https://idp.example/authorize?req=abc"}, {"text": "Please sign in."}],
    )
    get_resp = client.post(
        "/", json=_body(method="tasks/get", params={"taskId": task_id}), headers=_headers(method="tasks/get")
    )
    result = get_resp.json()["result"]
    assert result["status"] == "input_required"
    auth_request = result["inputRequests"]["auth"]
    assert auth_request["params"]["mode"] == "url"
    assert auth_request["params"]["url"] == "https://idp.example/authorize?req=abc"
    assert "requestedSchema" not in auth_request["params"]

    calls_before = len(fake.received_send_message_params)
    update_body = _body(
        method="tasks/update",
        params={"taskId": task_id, "inputResponses": {"auth": {"action": "accept"}}},
    )
    update_resp = client.post("/", json=update_body, headers=_headers(method="tasks/update"))
    assert update_resp.status_code == 200
    assert update_resp.json()["result"] == {"resultType": "complete"}
    # the acknowledgment must NEVER be forwarded into the A2A task -- the
    # agent resumes on its own once authenticated, per A2A's own semantics
    assert len(fake.received_send_message_params) == calls_before

    # agent resumes entirely on its own (out-of-band credential arrived) --
    # the bridge only ever polls from here
    fake.set_state(a2a_task_id, "TASK_STATE_WORKING")
    fake.set_state(a2a_task_id, "TASK_STATE_COMPLETED")
    final = client.post(
        "/", json=_body(method="tasks/get", params={"taskId": task_id}), headers=_headers(method="tasks/get")
    )
    assert final.json()["result"]["status"] == "completed"


def test_auth_required_answered_with_wrong_key_is_rejected(app_and_agent):
    client, fake = app_and_agent
    call_resp = _call_summarize(client)
    task_id = call_resp.json()["result"]["taskId"]
    a2a_task_id = next(iter(fake.tasks))
    fake.set_state_with_parts(a2a_task_id, "TASK_STATE_AUTH_REQUIRED", [{"url": "https://idp.example/authorize"}])

    # answering with the ordinary "response" key (as if it were a normal
    # input_required question) must not be accepted as an auth acknowledgment
    body = _body(
        method="tasks/update",
        params={"taskId": task_id, "inputResponses": {"response": {"action": "accept", "content": {"message": "hunter2"}}}},
    )
    resp = client.post("/", json=body, headers=_headers(method="tasks/update"))
    assert resp.status_code == 400
    assert len(fake.received_send_message_params) == 1  # only the original tools/call


# -- M3: structured DataPart input_required round trip end-to-end.


def test_structured_input_required_round_trip(app_and_agent):
    client, fake = app_and_agent
    call_resp = _call_summarize(client)
    task_id = call_resp.json()["result"]["taskId"]
    a2a_task_id = next(iter(fake.tasks))

    fake.set_state_with_parts(
        a2a_task_id,
        "TASK_STATE_INPUT_REQUIRED",
        [{"data": {"city": "Paris", "confirm": True}}, {"text": "Where and confirm?"}],
    )
    get_resp = client.post(
        "/", json=_body(method="tasks/get", params={"taskId": task_id}), headers=_headers(method="tasks/get")
    )
    schema = get_resp.json()["result"]["inputRequests"]["response"]["params"]["requestedSchema"]
    assert schema["properties"] == {"city": {"type": "string"}, "confirm": {"type": "boolean"}}

    update_body = _body(
        method="tasks/update",
        params={
            "taskId": task_id,
            "inputResponses": {"response": {"action": "accept", "content": {"city": "Lyon", "confirm": False}}},
        },
    )
    resp = client.post("/", json=update_body, headers=_headers(method="tasks/update"))
    assert resp.status_code == 200

    sent_part = fake.received_send_message_params[-1]["message"]["parts"][0]
    assert sent_part["data"] == {"city": "Lyon", "confirm": False}
