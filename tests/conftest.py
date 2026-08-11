from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

# demo/fake_agent/agent.py is the single source of truth for the fake A2A
# agent's JSONRPC wire shapes -- shared with the M4 demo container so the
# two never drift apart. Repo root isn't on sys.path by default under
# pytest's rootdir-relative test discovery, so add it explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from demo.fake_agent.agent import FakeA2AAgent, build_app  # noqa: E402

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
    server = _BackgroundAgent(build_app(fake, card))
    server.start()
    card["supportedInterfaces"] = [
        {"url": f"{server.base_url}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ]
    try:
        yield fake, server.base_url
    finally:
        server.stop()
