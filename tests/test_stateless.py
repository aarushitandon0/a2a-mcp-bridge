"""M4: statelessness.

Two tiers here, deliberately different in what they prove:

- `test_task_handle_resumes_across_independently_constructed_bridge_instances`
  runs with no Docker, as part of the normal `pytest` run. It builds two
  fully separate `create_app()` Starlette instances -- separate Python
  objects, separate httpx.AsyncClient, separate in-process everything --
  sharing only the state key and the fake A2A agent, and proves a task
  handle minted by one resumes correctly on the other. This is real
  evidence about the *code*: nothing in `tasks.py`/`state.py` depends on
  the object that opened a task being the one that continues it.

- `test_kill_one_mid_task`, `test_restart_all_three`, `test_key_rotation`
  are the actual M4 acceptance criteria: three real containers behind
  nginx, `docker kill`/`docker compose restart`, driven through
  `demo/driver.py` (the same code the scripted demo in
  `demo/run_demo.py` uses). They're skipped with a clear reason -- not
  silently -- when `docker`/`docker compose` isn't on PATH, since a
  container-process claim can only be proven by actually running
  containers, not by a same-process stand-in for one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from bridge.config import AgentConfig
from bridge.server import INSTANCE_ID_HEADER, create_app

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from demo import driver  # noqa: E402

PROTOCOL_VERSION = "2026-07-28"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
STATE_KEY = b"\x07" * 32


def _headers(*, method, name=None):
    headers = {
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
        "Content-Type": "application/json",
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


def _body(*, method, params=None):
    meta = {
        PROTOCOL_VERSION_META_KEY: PROTOCOL_VERSION,
        CLIENT_CAPABILITIES_META_KEY: {"extensions": {TASKS_EXTENSION_ID: {}}},
    }
    p = dict(params or {})
    p["_meta"] = meta
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": p}


def test_task_handle_resumes_across_independently_constructed_bridge_instances(fake_a2a_agent):
    fake, base_url = fake_a2a_agent
    agents = {"docs-agent": AgentConfig(agent_id="docs-agent", base_url=base_url)}

    # Two apps, deliberately never sharing a Python object beyond `agents`
    # and `[STATE_KEY]` (config, not state -- SPEC-NOTES.md §11) and the
    # fake agent standing in for the one thing M4's real demo also shares
    # (the remote A2A agent). No dict, no cache, nothing else in common.
    app_a = create_app(agents, [STATE_KEY], instance_id="instance-a")
    app_b = create_app(agents, [STATE_KEY], instance_id="instance-b")

    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        call_resp = client_a.post(
            "/",
            json=_body(
                method="tools/call",
                params={"name": "docs-agent__summarize", "arguments": {"message": "hi"}},
            ),
            headers=_headers(method="tools/call", name="docs-agent__summarize"),
        )
        assert call_resp.status_code == 200
        assert call_resp.headers[INSTANCE_ID_HEADER] == "instance-a"
        task_id = call_resp.json()["result"]["taskId"]
        a2a_task_id = next(iter(fake.tasks))

        fake.set_state(a2a_task_id, "TASK_STATE_INPUT_REQUIRED", message_text="Which format?")

        # Resume happens entirely through instance B, which never made the
        # original call and has no record of it beyond the sealed taskId.
        update_resp = client_b.post(
            "/",
            json=_body(
                method="tasks/update",
                params={
                    "taskId": task_id,
                    "inputResponses": {"response": {"action": "accept", "content": {"message": "PDF"}}},
                },
            ),
            headers=_headers(method="tasks/update"),
        )
        assert update_resp.status_code == 200, update_resp.json()
        assert update_resp.headers[INSTANCE_ID_HEADER] == "instance-b"

        fake.set_state(a2a_task_id, "TASK_STATE_COMPLETED")
        get_resp = client_b.post(
            "/",
            json=_body(method="tasks/get", params={"taskId": task_id}),
            headers=_headers(method="tasks/get"),
        )
        assert get_resp.json()["result"]["status"] == "completed"


# -- Real-container tests below. Require Docker; skipped with a reason, not
# silently, when it isn't available (see module docstring).

requires_docker = pytest.mark.skipif(
    not driver.docker_compose_available(),
    reason="docker / docker compose not found on PATH -- M4's container-level "
    "claims can only be checked by actually running containers",
)


@pytest.fixture(scope="module")
def compose_stack():
    import base64
    import os

    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    env = {**os.environ, "BRIDGE_STATE_KEY": key}
    driver.compose("up", "-d", "--build", env=env)
    try:
        driver.wait_for_nginx_ready(timeout=60.0)
        yield key
    finally:
        driver.compose("down", "-v", check=False)


@requires_docker
def test_kill_one_mid_task(compose_stack):
    driver.scenario_kill_one_mid_task()


@requires_docker
def test_restart_all_three(compose_stack):
    driver.scenario_restart_all_three()


@requires_docker
def test_key_rotation(compose_stack):
    import base64
    import os

    key_a = compose_stack
    key_b = base64.urlsafe_b64encode(os.urandom(32)).decode()
    driver.scenario_key_rotation(key_a, key_b)
