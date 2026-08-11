"""Streamable HTTP entrypoint: header validation, JSON-RPC routing. No session state.

Header rules and error codes trace to SPEC-NOTES.md §4 (Streamable HTTP) and §2
(error codes). MCP-Protocol-Version, Mcp-Method, and (where applicable) Mcp-Name
must each agree with the JSON-RPC body; disagreement is HTTP 400 + -32020
HeaderMismatch, per the spec's quoted example.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from bridge import discover, tasks
from bridge.config import AgentConfig

logger = logging.getLogger("bridge.server")

PROTOCOL_VERSION = "2026-07-28"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"

# JSON-RPC / MCP error codes -- SPEC-NOTES.md §2.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021

# method -> params key that Mcp-Name must mirror (SPEC-NOTES.md §4).
_MCP_NAME_SOURCE_KEY = {
    "tools/call": "name",
    "resources/read": "uri",
    "prompts/get": "name",
}


class BridgeError(Exception):
    """A JSON-RPC-level error with an associated HTTP status."""

    def __init__(self, code: int, message: str, *, http_status: int = 200, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.data = data


def _error_body(request_id: Any, code: int, message: str, data: dict | None = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _validate_headers(headers: Any, body: dict) -> None:
    method = body.get("method")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else None

    # Body-structural completeness is checked BEFORE header/body agreement:
    # a request missing _meta, or missing one of its required subfields, is
    # -32602 Invalid params (malformed request), distinct from -32020
    # HeaderMismatch (a well-formed request whose header disagrees with a
    # present body value). Confirmed against the modelcontextprotocol
    # conformance suite's server-stateless scenario -- not discoverable from
    # schema.ts alone, since the schema doesn't encode this priority.
    if meta is None:
        raise BridgeError(INVALID_PARAMS, "params._meta is required", http_status=400)
    if PROTOCOL_VERSION_META_KEY not in meta:
        raise BridgeError(
            INVALID_PARAMS, f"params._meta[{PROTOCOL_VERSION_META_KEY!r}] is required", http_status=400
        )
    if CLIENT_CAPABILITIES_META_KEY not in meta:
        raise BridgeError(
            INVALID_PARAMS, f"params._meta[{CLIENT_CAPABILITIES_META_KEY!r}] is required", http_status=400
        )

    header_version = headers.get("mcp-protocol-version")
    body_version = meta[PROTOCOL_VERSION_META_KEY]
    if header_version is None or header_version != body_version:
        raise BridgeError(
            HEADER_MISMATCH,
            f"Header mismatch: MCP-Protocol-Version header value {header_version!r} "
            f"does not match body value {body_version!r}",
            http_status=400,
        )
    if header_version != PROTOCOL_VERSION:
        raise BridgeError(
            UNSUPPORTED_PROTOCOL_VERSION,
            f"Unsupported protocol version {header_version!r}; this server supports {PROTOCOL_VERSION!r}",
            http_status=400,
            data={"supported": discover.SUPPORTED_PROTOCOL_VERSIONS, "requested": header_version},
        )

    header_method = headers.get("mcp-method")
    if header_method is None or header_method != method:
        raise BridgeError(
            HEADER_MISMATCH,
            f"Header mismatch: Mcp-Method header value {header_method!r} does not match body value {method!r}",
            http_status=400,
        )

    if method in _MCP_NAME_SOURCE_KEY:
        source_key = _MCP_NAME_SOURCE_KEY[method]
        body_name = params.get(source_key)
        header_name = headers.get("mcp-name")
        if header_name is None or header_name != body_name:
            raise BridgeError(
                HEADER_MISMATCH,
                f"Header mismatch: Mcp-Name header value {header_name!r} does not match body value {body_name!r}",
                http_status=400,
            )


def _translate_tasks_error(exc: tasks.TasksError) -> BridgeError:
    # tasks.py raises its own domain exceptions rather than importing
    # BridgeError itself, to keep the dependency direction one-way
    # (server.py -> tasks.py, never the reverse) -- see tasks.py's module
    # docstring. This is the single place that translates them.
    if isinstance(exc, tasks.TaskHandleExpired):
        # SPEC-NOTES.md §12c: a client presenting an expired handle for a
        # task that may still be running agent-side needs to know that's
        # what happened, not get a bare "invalid params."
        return BridgeError(
            INVALID_PARAMS, str(exc), http_status=400, data={"reason": "expired", "exp": exc.handle.exp}
        )
    if isinstance(exc, tasks.MissingRequiredCapability):
        return BridgeError(
            MISSING_REQUIRED_CLIENT_CAPABILITY,
            str(exc),
            http_status=400,
            data={"requiredCapabilities": exc.required},
        )
    if isinstance(exc, tasks.DownstreamAgentError):
        return BridgeError(INTERNAL_ERROR, str(exc), http_status=500)
    # InvalidTaskHandle, UnknownTool, ElicitationNotAccepted,
    # MissingInputResponse -- all client-supplied-bad-input cases.
    return BridgeError(INVALID_PARAMS, str(exc), http_status=400)


async def _dispatch(method: str, params: dict, request: Request) -> dict:
    agents: dict[str, AgentConfig] = request.app.state.agents
    http_client: httpx.AsyncClient = request.app.state.http_client

    if method == "server/discover":
        return await discover.discover_result()
    if method == "tools/list":
        return await discover.list_tools_result(http_client, agents)

    if method in ("tools/call", "tasks/get", "tasks/update", "tasks/cancel"):
        keys: list[bytes] = request.app.state.state_keys
        handler = {
            "tools/call": tasks.tools_call,
            "tasks/get": tasks.tasks_get,
            "tasks/update": tasks.tasks_update,
            "tasks/cancel": tasks.tasks_cancel,
        }[method]
        try:
            return await handler(http_client, agents, keys, params)
        except tasks.TasksError as exc:
            raise _translate_tasks_error(exc) from exc

    # Unknown/removed methods (including pre-2026-07-28 methods like
    # 'initialize' or 'ping') are HTTP 404, not 200 -- confirmed against the
    # conformance suite's server-stateless scenario.
    raise BridgeError(METHOD_NOT_FOUND, f"Unknown method: {method}", http_status=404)


async def mcp_endpoint(request: Request) -> JSONResponse:
    try:
        raw = await request.body()
        body = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse(_error_body(None, PARSE_ERROR, "Invalid JSON"), status_code=400)

    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
        request_id = body.get("id") if isinstance(body, dict) else None
        return JSONResponse(
            _error_body(request_id, INVALID_REQUEST, "Invalid JSON-RPC 2.0 request"),
            status_code=400,
        )

    request_id = body.get("id")
    try:
        _validate_headers(request.headers, body)
        result = await _dispatch(body["method"], body.get("params") or {}, request)
    except BridgeError as exc:
        return JSONResponse(
            _error_body(request_id, exc.code, exc.message, exc.data), status_code=exc.http_status
        )
    except Exception:
        logger.exception("Unhandled error processing method=%s", body.get("method"))
        return JSONResponse(_error_body(request_id, INTERNAL_ERROR, "Internal error"), status_code=500)

    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def create_app(agents: dict[str, AgentConfig], state_keys: list[bytes]) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.agents = agents
        app.state.state_keys = state_keys
        async with httpx.AsyncClient() as client:
            app.state.http_client = client
            yield

    return Starlette(routes=[Route("/", mcp_endpoint, methods=["POST"])], lifespan=lifespan)


def app_from_env() -> Starlette:
    from bridge.config import load_agents_from_env
    from bridge.state import load_keys_from_env

    return create_app(load_agents_from_env(), load_keys_from_env())
