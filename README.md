# a2a-mcp-bridge

A stateless bridge exposing A2A remote agents to MCP clients, built native on
the MCP `2026-07-28` specification. See `SPEC-NOTES.md` for the protocol
research this project is built on, and `CLAUDE.md` for the working rules.

**Status:** M0 (recon) and M1 (stateless MCP skeleton) complete. M2 (task
handles) in progress. Not yet usable end-to-end.

## Setup

```
python -m venv .venv
./.venv/Scripts/pip install -e ".[dev]"   # or .venv/bin/pip on macOS/Linux
```

## Running the tests

```
./.venv/Scripts/python -m pytest
```

## Running the server

Configure agents via `BRIDGE_AGENTS_CONFIG`, a JSON array of
`{"agent_id": "...", "base_url": "...", "headers": {...}}`:

```
BRIDGE_AGENTS_CONFIG='[{"agent_id":"docs-agent","base_url":"https://docs-agent.example"}]' \
  ./.venv/Scripts/python -m uvicorn bridge.server:app_from_env --factory --app-dir src
```

## Conformance

M1 was checked against the real `modelcontextprotocol/conformance` suite
(`server-stateless`, `http-header-validation`, `tools-list` scenarios), not
just this project's own tests — see SPEC-NOTES.md §11 for what that run
caught and corrected, and what it left out of scope (diagnostic-only test
tools this bridge has no reason to implement).

## Design trade-offs

_To be filled in once M2 lands `state.py` (the sealed task-handle envelope)
— see SPEC-NOTES.md §12 for the design and the trade-offs it accepts
(handle size, key rotation, expiry semantics)._
