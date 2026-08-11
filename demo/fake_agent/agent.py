"""A scriptable A2A JSONRPC agent -- the wire-shape source of truth.

This is the same `FakeA2AAgent` used by `tests/test_tasks.py` and friends
(imported from here by `tests/conftest.py`, not duplicated -- CLAUDE.md's
"no protocol code from memory" rule cuts against maintaining two
hand-written copies of the same JSONRPC shapes that could quietly drift
apart). Its request/response shapes were captured empirically against the
real `a2a-sdk` `JsonRpcTransport` (see `a2a_client.py`'s module docstring),
not assumed from prose.

For M4 this module is also the entrypoint for the fake agent's own
container (see `demo/Dockerfile.fake_agent`): `uvicorn
demo.fake_agent.agent:app --factory`. Two additions beyond what the
in-process pytest fixture needed, both deliberately explicit rather than
automatic -- see `demo/README.md` for why:

- `/control/tasks` (GET) lists current task ids/states, so a driver script
  that only holds a sealed MCP `taskId` (never the underlying A2A id) can
  discover which A2A task it just caused to exist.
- `/control/tasks/{a2a_task_id}/set-state` (POST) forces a task into a
  given `TaskState`, standing in for both "the agent decides it needs more
  input" and "the agent finishes after receiving it" -- in real A2A both
  are the remote agent's own decision, which this demo has no real business
  logic to make, so a driver script makes it explicitly instead of the
  fake agent guessing.

Neither control endpoint is A2A or MCP wire protocol -- both are demo/test
tooling standing in for what a real agent's own task logic would do.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

DEFAULT_AGENT_CARD = {
    "name": "Demo Docs Agent",
    "description": "Summarizes documents; deliberately pauses for input, for the M4 statelessness demo.",
    "supportedInterfaces": [],  # filled in by build_app() once the base URL is known
    "version": "1.0.0",
    "capabilities": {},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "summarize",
            "name": "Summarize",
            "description": "Summarize a document.",
            "tags": ["docs"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
        }
    ],
}


class FakeA2AAgent:
    """A scriptable, in-memory A2A JSONRPC agent.

    Statefulness here is fine -- it's a stand-in for a real remote agent,
    not the bridge (which must stay stateless; M4 proves that property
    holds even when this agent is the only thing both bridge instances
    share).
    """

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.received_send_message_params: list[dict] = []
        self.received_headers: list[dict] = []
        self._next_id = 0
        # Task ids that must reject SendMessage with A2A's own
        # UnsupportedOperationError (-32004), regardless of GetTask's
        # reported state -- for testing the defense-in-depth catch
        # independent of the bridge's own preemptive terminal-state check.
        self.unsupported_operation_task_ids: set[str] = set()

    def new_task_id(self) -> str:
        self._next_id += 1
        return f"a2a-task-{self._next_id}"

    def seed_task(self, task_id: str, context_id: str, state: str, *, message_text: str | None = None, artifacts=None):
        status: dict = {"state": state}
        if message_text is not None:
            status["message"] = {
                "messageId": "seed-msg",
                "role": "ROLE_AGENT",
                "parts": [{"text": message_text}],
            }
        self.tasks[task_id] = {
            "id": task_id,
            "contextId": context_id,
            "status": status,
            "artifacts": artifacts or [],
        }

    def set_state(self, task_id: str, state: str, *, message_text: str | None = None):
        task = self.tasks[task_id]
        status: dict = {"state": state}
        if message_text is not None:
            status["message"] = {
                "messageId": "update-msg",
                "role": "ROLE_AGENT",
                "parts": [{"text": message_text}],
            }
        task["status"] = status

    def set_state_with_parts(self, task_id: str, state: str, parts: list[dict]):
        """Like set_state, but with raw wire-shape message parts (e.g.
        [{"data": {...}}] or [{"url": "..."}]) for scenarios set_state's
        text-only shortcut can't express.
        """
        self.tasks[task_id]["status"] = {
            "state": state,
            "message": {"messageId": "update-msg", "role": "ROLE_AGENT", "parts": parts},
        }

    # Controls what state a NEWLY created task (no taskId in the incoming
    # message) starts in -- override per test, e.g. to simulate a task that
    # is already input_required on its very first return.
    initial_state = "TASK_STATE_WORKING"

    async def handle(self, request):
        body = await request.json()
        method = body["method"]
        params = body.get("params", {})
        req_id = body["id"]
        self.received_headers.append(dict(request.headers))

        if method == "SendMessage":
            self.received_send_message_params.append(params)
            message = params["message"]
            task_id = message.get("taskId")
            context_id = message.get("contextId")
            if task_id and task_id in self.unsupported_operation_task_ids:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32004, "message": "unsupported operation: task is terminal"},
                    }
                )
            if task_id and task_id in self.tasks:
                result = {"task": self.tasks[task_id]}
            else:
                task_id = task_id or self.new_task_id()
                context_id = context_id or f"ctx-{task_id}"
                self.tasks[task_id] = {
                    "id": task_id,
                    "contextId": context_id,
                    "status": {"state": self.initial_state},
                }
                result = {"task": self.tasks[task_id]}
        elif method == "GetTask":
            task = self.tasks.get(params["id"])
            if task is None:
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": "task not found"}}
                )
            result = task
        elif method == "CancelTask":
            task = self.tasks.get(params["id"])
            if task is None:
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": "task not found"}}
                )
            task["status"] = {"state": "TASK_STATE_CANCELED"}
            result = task
        else:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"unknown method {method}"}}
            )

        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


def build_app(fake: FakeA2AAgent, card: dict) -> Starlette:
    """The plain A2A-facing app: agent-card + JSONRPC endpoint. No control
    routes -- used by tests/conftest.py, which drives `fake` directly in
    Python rather than over HTTP.
    """

    async def agent_card(request):
        return JSONResponse(card)

    return Starlette(
        routes=[
            Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
            Route("/a2a", fake.handle, methods=["POST"]),
        ]
    )


def build_app_with_control(fake: FakeA2AAgent, card: dict) -> Starlette:
    """The container entrypoint's app: adds the two /control routes
    described in this module's docstring, for a demo driver script that
    only has HTTP access to this process (no shared Python object).
    """

    async def agent_card(request):
        return JSONResponse(card)

    async def list_tasks(request):
        return JSONResponse(
            {task_id: task["status"]["state"] for task_id, task in fake.tasks.items()}
        )

    async def set_state(request: Request):
        task_id = request.path_params["task_id"]
        if task_id not in fake.tasks:
            return JSONResponse({"error": f"unknown task_id {task_id!r}"}, status_code=404)
        body = await request.json()
        state = body["state"]
        message_text = body.get("message_text")
        parts = body.get("parts")
        if parts is not None:
            fake.set_state_with_parts(task_id, state, parts)
        else:
            fake.set_state(task_id, state, message_text=message_text)
        return JSONResponse(fake.tasks[task_id])

    return Starlette(
        routes=[
            Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
            Route("/a2a", fake.handle, methods=["POST"]),
            Route("/control/tasks", list_tasks, methods=["GET"]),
            Route("/control/tasks/{task_id}/set-state", set_state, methods=["POST"]),
        ]
    )


def app() -> Starlette:
    """Factory entrypoint for `uvicorn demo.fake_agent.agent:app --factory`.

    `supportedInterfaces[0].url` is set from `FAKE_AGENT_PUBLIC_URL` (the
    URL other containers on the compose network reach this one at) --
    can't be `http://127.0.0.1:.../a2a` like the in-process pytest fixture
    uses, since bridge containers reach this one over the compose network,
    not localhost.
    """
    import os

    public_url = os.environ.get("FAKE_AGENT_PUBLIC_URL", "http://fake-agent:8000")
    fake = FakeA2AAgent()
    card = dict(DEFAULT_AGENT_CARD)
    card["supportedInterfaces"] = [
        {"url": f"{public_url}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ]
    return build_app_with_control(fake, card)
