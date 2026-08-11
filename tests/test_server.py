from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from bridge.config import AgentConfig
from bridge.server import create_app

PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
PROTOCOL_VERSION = "2026-07-28"
TEST_STATE_KEY = b"\x03" * 32


def _headers(*, protocol_version=PROTOCOL_VERSION, method="server/discover", name=None):
    headers = {
        "MCP-Protocol-Version": protocol_version,
        "Mcp-Method": method,
        "Content-Type": "application/json",
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return {k: v for k, v in headers.items() if v is not None}


def _body(
    *,
    method="server/discover",
    protocol_version=PROTOCOL_VERSION,
    params=None,
    include_meta=True,
    include_protocol_version=True,
    include_client_capabilities=True,
):
    p = dict(params or {})
    if include_meta:
        meta = {}
        if include_protocol_version:
            meta[PROTOCOL_VERSION_META_KEY] = protocol_version
        if include_client_capabilities:
            meta[CLIENT_CAPABILITIES_META_KEY] = {}
        p["_meta"] = meta
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": p}


@pytest.fixture
def client():
    app = create_app({}, [TEST_STATE_KEY])
    with TestClient(app) as c:
        yield c


def test_discover_succeeds_with_correct_headers(client):
    resp = client.post("/", json=_body(), headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["supportedVersions"] == ["2026-07-28"]
    assert "error" not in body


# -- Body-structural completeness: -32602 Invalid params, HTTP 400.
# Distinct from -32020 HeaderMismatch, which only applies once the body is
# known to be structurally complete. See server.py's _validate_headers
# docstring-comment and SPEC-NOTES.md.


def test_missing_meta_entirely_is_invalid_params(client):
    resp = client.post("/", json=_body(include_meta=False), headers=_headers())
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32602


def test_meta_missing_protocol_version_is_invalid_params(client):
    resp = client.post("/", json=_body(include_protocol_version=False), headers=_headers())
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32602


def test_meta_missing_client_capabilities_is_invalid_params(client):
    resp = client.post("/", json=_body(include_client_capabilities=False), headers=_headers())
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32602


def test_missing_protocol_version_header_is_rejected(client):
    headers = _headers()
    del headers["MCP-Protocol-Version"]
    resp = client.post("/", json=_body(), headers=headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32020


def test_protocol_version_header_body_mismatch_is_rejected(client):
    resp = client.post("/", json=_body(protocol_version="2025-11-25"), headers=_headers())
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32020
    assert "MCP-Protocol-Version" in resp.json()["error"]["message"]


def test_unsupported_but_agreeing_protocol_version_is_rejected_distinctly(client):
    resp = client.post(
        "/",
        json=_body(protocol_version="2025-11-25"),
        headers=_headers(protocol_version="2025-11-25"),
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == -32022
    # UnsupportedProtocolVersionError.data carries `supported` + `requested`
    # (confirmed against the conformance suite; not derivable from schema.ts
    # alone -- see SPEC-NOTES.md).
    assert error["data"] == {"supported": ["2026-07-28"], "requested": "2025-11-25"}


def test_missing_mcp_method_header_is_rejected(client):
    headers = _headers()
    del headers["Mcp-Method"]
    resp = client.post("/", json=_body(), headers=headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32020


def test_mcp_method_header_body_mismatch_is_rejected(client):
    headers = _headers(method="tools/list")
    resp = client.post("/", json=_body(method="server/discover"), headers=headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32020
    assert "Mcp-Method" in resp.json()["error"]["message"]


def test_mcp_name_header_required_and_checked_for_tools_call(client):
    # tools/call isn't implemented until M2, but header validation must reject
    # a name mismatch before dispatch ever sees the unknown method.
    headers = _headers(method="tools/call", name="wrong-tool")
    body = _body(method="tools/call", params={"name": "right-tool", "arguments": {}})
    resp = client.post("/", json=body, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32020
    assert "Mcp-Name" in resp.json()["error"]["message"]


def test_mcp_name_header_missing_for_tools_call_is_rejected(client):
    headers = _headers(method="tools/call")
    body = _body(method="tools/call", params={"name": "right-tool", "arguments": {}})
    resp = client.post("/", json=body, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32020


def test_mcp_name_not_required_for_tools_list(client):
    resp = client.post("/", json=_body(method="tools/list"), headers=_headers(method="tools/list"))
    assert resp.status_code == 200


def test_unknown_method_is_404_method_not_found(client):
    # Confirmed against the conformance suite: unknown/removed methods
    # (including pre-2026-07-28 methods like 'initialize') are HTTP 404, not
    # 200 -- corrected from an earlier, wrong assumption. See SPEC-NOTES.md.
    resp = client.post("/", json=_body(method="nope"), headers=_headers(method="nope"))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == -32601


def test_removed_initialize_method_is_404(client):
    resp = client.post(
        "/", json=_body(method="initialize"), headers=_headers(method="initialize")
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == -32601


def test_malformed_json_is_parse_error(client):
    resp = client.post(
        "/",
        content=b"{not json",
        headers={"Content-Type": "application/json", **_headers()},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32700


def test_no_session_header_ever_required(client):
    resp = client.post("/", json=_body(), headers=_headers())
    assert "mcp-session-id" not in {k.lower() for k in resp.headers.keys()}


def test_tools_list_end_to_end_against_two_fake_agents(fake_agents):
    agents = {
        agent_id: AgentConfig(agent_id=agent_id, base_url=base_url)
        for agent_id, base_url in fake_agents.items()
    }
    app = create_app(agents, [TEST_STATE_KEY])
    with TestClient(app) as c:
        resp = c.post(
            "/", json=_body(method="tools/list"), headers=_headers(method="tools/list")
        )
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()["result"]["tools"]}
    assert names == {"docs-agent__summarize", "code-agent__review", "code-agent__refactor"}
