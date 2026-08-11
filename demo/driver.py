"""HTTP + docker-compose driving code for the M4 statelessness demo.

Shared by `demo/run_demo.py` (manual/scripted run, captured output goes in
the README) and `tests/test_stateless.py` (automated pytest run) so the two
never describe two different demos.

Wire-level constants below (header names, `_meta` keys, method names) are
the same ones `tests/test_tasks.py` hardcodes as a real client would --
traced to SPEC-NOTES.md §4, not re-derived here.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

DEMO_DIR = Path(__file__).resolve().parent
COMPOSE_FILE = DEMO_DIR / "docker-compose.yml"

PROTOCOL_VERSION = "2026-07-28"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
INSTANCE_ID_HEADER = "Bridge-Instance-Id"

NGINX_BASE_URL = "http://localhost:8080"
FAKE_AGENT_CONTROL_URL = "http://localhost:8001"


def docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(["docker", "compose", "version"], capture_output=True)
    return result.returncode == 0


def compose(*args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(cmd, cwd=DEMO_DIR, env=env, capture_output=True, text=True, check=check)


def _headers(*, method: str, name: str | None = None) -> dict:
    headers = {
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
        "Content-Type": "application/json",
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


def _body(*, method: str, params: dict | None = None) -> dict:
    meta = {
        PROTOCOL_VERSION_META_KEY: PROTOCOL_VERSION,
        CLIENT_CAPABILITIES_META_KEY: {"extensions": {TASKS_EXTENSION_ID: {}}},
    }
    p = dict(params or {})
    p["_meta"] = meta
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": p}


@dataclass
class RpcResult:
    result: dict
    instance_id: str | None


def _post(client: httpx.Client, *, method: str, params: dict, name: str | None = None) -> RpcResult:
    resp = client.post(
        "/", json=_body(method=method, params=params), headers=_headers(method=method, name=name)
    )
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"{method} returned an error: {body['error']}")
    return RpcResult(result=body["result"], instance_id=resp.headers.get(INSTANCE_ID_HEADER))


def tools_call_summarize(client: httpx.Client, text: str = "Summarize this doc") -> RpcResult:
    return _post(
        client,
        method="tools/call",
        params={"name": "docs__summarize", "arguments": {"message": text}},
        name="docs__summarize",
    )


def tasks_get(client: httpx.Client, task_id: str) -> RpcResult:
    return _post(client, method="tasks/get", params={"taskId": task_id})


def tasks_update(client: httpx.Client, task_id: str, response_text: str) -> RpcResult:
    return _post(
        client,
        method="tasks/update",
        params={"taskId": task_id, "inputResponses": {"response": {"action": "accept", "content": {"message": response_text}}}},
    )


def fake_agent_known_task_ids(control_client: httpx.Client) -> set[str]:
    return set(control_client.get("/control/tasks").json())


def fake_agent_wait_for_new_task(control_client: httpx.Client, known: set[str], timeout: float = 10.0) -> str:
    """Poll the fake agent's control endpoint until a task NOT in `known`
    appears, and return its A2A task id -- the driver only ever holds the
    sealed MCP taskId, never the underlying A2A one, by design (see
    state.py). Diffing against `known` rather than just grabbing the first
    task matters once a single compose session runs more than one scenario.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = fake_agent_known_task_ids(control_client)
        new = current - known
        if new:
            return next(iter(new))
        time.sleep(0.2)
    raise TimeoutError("fake agent never registered a new task")


def fake_agent_set_state(control_client: httpx.Client, a2a_task_id: str, state: str, *, message_text: str | None = None) -> None:
    body: dict = {"state": state}
    if message_text is not None:
        body["message_text"] = message_text
    resp = control_client.post(f"/control/tasks/{a2a_task_id}/set-state", json=body)
    resp.raise_for_status()


def wait_for_nginx_ready(timeout: float = 30.0) -> None:
    """Poll with server/discover -- no side effects on A2A, unlike tools/call,
    so retries here don't leave stray tasks on the fake agent."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    with httpx.Client(base_url=NGINX_BASE_URL, timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                _post(client, method="server/discover", params={})
                return
            except Exception as exc:  # noqa: BLE001 -- readiness polling
                last_exc = exc
                time.sleep(0.5)
    raise TimeoutError(f"nginx/bridge pool never became ready: {last_exc}")


@dataclass
class ScenarioLog:
    """Captured output for the README's scripted-demo section."""

    lines: list[str]

    def add(self, line: str) -> None:
        self.lines.append(line)
        print(line)


def _start_paused_task(client: httpx.Client, control_client: httpx.Client, log: ScenarioLog) -> tuple[str, str]:
    known = fake_agent_known_task_ids(control_client)
    call = tools_call_summarize(client)
    task_id = call.result["taskId"]
    log.add(f"tools/call -> taskId={task_id[:16]}... served by instance={call.instance_id}")

    a2a_task_id = fake_agent_wait_for_new_task(control_client, known)
    fake_agent_set_state(control_client, a2a_task_id, "TASK_STATE_INPUT_REQUIRED", message_text="Which format?")

    get = tasks_get(client, task_id)
    assert get.result["status"] == "input_required", get.result
    log.add(f"tasks/get -> status=input_required, served by instance={get.instance_id}")
    return task_id, a2a_task_id


def scenario_kill_one_mid_task(log: ScenarioLog | None = None) -> None:
    """Start a task, let it reach input_required, kill the exact bridge
    instance that served the original tools/call, then answer the prompt
    through nginx (necessarily landing on a survivor) and confirm the task
    still completes -- from a bridge instance that never saw the original
    request.
    """
    log = log or ScenarioLog([])
    with httpx.Client(base_url=NGINX_BASE_URL, timeout=5.0) as client, httpx.Client(
        base_url=FAKE_AGENT_CONTROL_URL, timeout=5.0
    ) as control_client:
        known = fake_agent_known_task_ids(control_client)
        call = tools_call_summarize(client)
        task_id = call.result["taskId"]
        original_instance = call.instance_id
        log.add(f"tools/call -> taskId={task_id[:16]}... served by instance={original_instance}")
        assert original_instance in ("bridge1", "bridge2", "bridge3"), original_instance

        a2a_task_id = fake_agent_wait_for_new_task(control_client, known)
        fake_agent_set_state(control_client, a2a_task_id, "TASK_STATE_INPUT_REQUIRED", message_text="Which format?")
        get = tasks_get(client, task_id)
        assert get.result["status"] == "input_required", get.result
        log.add(f"tasks/get -> status=input_required, served by instance={get.instance_id}")

        log.add(f"docker kill {original_instance} (the instance that served the original call)")
        compose("kill", original_instance)

        update = tasks_update(client, task_id, "PDF")
        log.add(f"tasks/update (answering the prompt) -> served by instance={update.instance_id}")
        assert update.instance_id != original_instance, (
            f"resume was served by the killed instance {original_instance!r} -- impossible unless "
            "nginx routed to a container that's supposed to be dead"
        )

        fake_agent_set_state(control_client, a2a_task_id, "TASK_STATE_COMPLETED")
        final = tasks_get(client, task_id)
        assert final.result["status"] == "completed", final.result
        log.add(f"tasks/get -> status=completed, served by instance={final.instance_id}")

    compose("up", "-d", original_instance, check=False)  # restore the pool for later scenarios


def scenario_restart_all_three(log: ScenarioLog | None = None) -> None:
    """Stronger variant: restart EVERY bridge instance (wiping any in-process
    memory all three might have had) while a task sits at input_required,
    then confirm it still resumes correctly -- proving the property doesn't
    depend on at least one original instance surviving.
    """
    log = log or ScenarioLog([])
    with httpx.Client(base_url=NGINX_BASE_URL, timeout=5.0) as client, httpx.Client(
        base_url=FAKE_AGENT_CONTROL_URL, timeout=5.0
    ) as control_client:
        task_id, a2a_task_id = _start_paused_task(client, control_client, log)

        log.add("docker compose restart bridge1 bridge2 bridge3 (all three, none survive)")
        compose("restart", "bridge1", "bridge2", "bridge3")
        wait_for_nginx_ready()

        update = tasks_update(client, task_id, "PDF")
        log.add(f"tasks/update (answering the prompt) -> served by instance={update.instance_id}")

        fake_agent_set_state(control_client, a2a_task_id, "TASK_STATE_COMPLETED")
        final = tasks_get(client, task_id)
        assert final.result["status"] == "completed", final.result
        log.add(f"tasks/get -> status=completed, served by instance={final.instance_id}")


def scenario_key_rotation(key_a_b64: str, key_b_b64: str, log: ScenarioLog | None = None) -> None:
    """Start a task under KEY_A, rotate every bridge instance to
    BRIDGE_STATE_KEY=KEY_B / BRIDGE_STATE_KEY_PREVIOUS=KEY_A one at a time
    (the two-key window), and confirm the KEY_A-sealed handle still
    resumes -- then confirm a freshly minted handle is sealed under KEY_B.
    """
    import os

    log = log or ScenarioLog([])
    with httpx.Client(base_url=NGINX_BASE_URL, timeout=5.0) as client, httpx.Client(
        base_url=FAKE_AGENT_CONTROL_URL, timeout=5.0
    ) as control_client:
        task_id, a2a_task_id = _start_paused_task(client, control_client, log)
        log.add(f"handle minted under BRIDGE_STATE_KEY=A: taskId={task_id[:16]}...")

        rotated_env = {**os.environ, "BRIDGE_STATE_KEY": key_b_b64, "BRIDGE_STATE_KEY_PREVIOUS": key_a_b64}
        log.add("rotating bridge1, bridge2, bridge3 one at a time to KEY_B (KEY_A as PREVIOUS)")
        for service in ("bridge1", "bridge2", "bridge3"):
            compose("up", "-d", "--force-recreate", service, env=rotated_env)
        wait_for_nginx_ready()

        update = tasks_update(client, task_id, "PDF")
        log.add(f"tasks/update on the KEY_A-sealed handle, post-rotation -> served by instance={update.instance_id}")

        fake_agent_set_state(control_client, a2a_task_id, "TASK_STATE_COMPLETED")
        final = tasks_get(client, task_id)
        assert final.result["status"] == "completed", final.result
        log.add(f"tasks/get -> status=completed (old handle survived the rotation window)")

        known = fake_agent_known_task_ids(control_client)
        new_call = tools_call_summarize(client, text="post-rotation call")
        log.add(f"a fresh tools/call after rotation -> taskId={new_call.result['taskId'][:16]}... (now sealed under KEY_B)")
        fake_agent_wait_for_new_task(control_client, known)
