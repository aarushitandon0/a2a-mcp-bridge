from __future__ import annotations

import asyncio
import threading

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

# Minimal, valid AgentCard JSON (camelCase -- parsed via
# google.protobuf.json_format.ParseDict, see SPEC-NOTES.md §10 / a2a_client.py).

DOCS_AGENT_CARD = {
    "name": "Docs Agent",
    "description": "Summarizes and answers questions about documents.",
    "supportedInterfaces": [
        {"url": "http://placeholder/", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ],
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

CODE_AGENT_CARD = {
    "name": "Code Agent",
    "description": "Reviews and refactors code.",
    "supportedInterfaces": [
        {"url": "http://placeholder/", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ],
    "version": "1.0.0",
    "capabilities": {},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "review",
            "name": "Review",
            "description": "Review a code change.",
            "tags": ["code"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
        },
        {
            "id": "refactor",
            "name": "Refactor",
            "description": "Refactor a code snippet.",
            "tags": ["code"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
        },
    ],
}


def _fake_agent_app(card: dict) -> Starlette:
    async def agent_card(request):
        return JSONResponse(card)

    return Starlette(routes=[Route("/.well-known/agent-card.json", agent_card, methods=["GET"])])


class _BackgroundAgent:
    """Runs one ASGI app on a real TCP port, in its own thread and event loop.

    Deliberately decoupled from whatever loop the test (or TestClient, which
    manages its own internal loop/thread) is using -- avoids nested-loop /
    cross-thread deadlocks when a test mixes a sync HTTP client with live
    background servers.
    """

    def __init__(self, app: Starlette):
        self._app = app
        self._server: uvicorn.Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.port: int | None = None

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        config = uvicorn.Config(self._app, host="127.0.0.1", port=0, log_level="warning")
        self._server = uvicorn.Server(config)
        serve_task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.01)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        self._ready.set()
        await serve_task

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("fake agent server failed to start in time")

    def stop(self) -> None:
        assert self._server is not None
        self._server.should_exit = True
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture
def fake_agents():
    """Two independent live A2A agent-card servers, for real HTTP round trips."""
    docs = _BackgroundAgent(_fake_agent_app(DOCS_AGENT_CARD))
    code = _BackgroundAgent(_fake_agent_app(CODE_AGENT_CARD))
    docs.start()
    code.start()
    try:
        yield {"docs-agent": docs.base_url, "code-agent": code.base_url}
    finally:
        docs.stop()
        code.stop()


class FakeA2AAgent:
    """A scriptable, in-memory A2A JSONRPC agent for M2 tests.

    Statefulness here is fine -- it's a test double standing in for a real
    remote agent, not the bridge (which must stay stateless). Exercises the
    exact wire shapes JsonRpcTransport sends, captured empirically (see the
    a2a_client.py implementation notes) rather than assumed from prose.
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


def _fake_a2a_agent_app(fake: FakeA2AAgent, card: dict) -> Starlette:
    async def agent_card(request):
        return JSONResponse(card)

    return Starlette(
        routes=[
            Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
            Route("/a2a", fake.handle, methods=["POST"]),
        ]
    )


@pytest.fixture
def fake_a2a_agent():
    """One live A2A agent exposing both the card and a scriptable JSONRPC
    endpoint, for full tools/call -> tasks/get -> tasks/update round trips.
    """
    fake = FakeA2AAgent()
    card = {
        "name": "Docs Agent",
        "description": "Summarizes and answers questions about documents.",
        "supportedInterfaces": [],  # filled in once the port is known
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
    server = _BackgroundAgent(_fake_a2a_agent_app(fake, card))
    server.start()
    card["supportedInterfaces"] = [
        {"url": f"{server.base_url}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ]
    try:
        yield fake, server.base_url
    finally:
        server.stop()
