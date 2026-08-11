# a2a-mcp-bridge

A stateless bridge exposing A2A remote agents to MCP clients, built native on
the MCP `2026-07-28` specification. See `SPEC-NOTES.md` for the protocol
research this project is built on, and `CLAUDE.md` for the working rules.

**Status:** M0 (recon), M1 (stateless MCP skeleton), and M2 (task handles)
complete. `tools/call` opens an A2A task and returns a sealed handle;
`tasks/get`/`tasks/update`/`tasks/cancel` drive it to completion. M3
(dedicated input-required polish), M4 (statelessness demo), M5 (identity
chain) not started.

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
`{"agent_id": "...", "base_url": "...", "headers": {...}}`, and the sealed
task-handle key via `BRIDGE_STATE_KEY` (base64url-encoded 32 bytes; add
`BRIDGE_STATE_KEY_PREVIOUS` when rotating):

```
BRIDGE_AGENTS_CONFIG='[{"agent_id":"docs","base_url":"https://docs-agent.example"}]' \
BRIDGE_STATE_KEY="$(python -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')" \
  ./.venv/Scripts/python -m uvicorn bridge.server:app_from_env --factory --app-dir src
```

## Conformance

Both M1 and M2 were checked against the real `modelcontextprotocol/conformance`
suite (`server-stateless`, `http-header-validation`, `tools-list`), not just
this project's own tests — see SPEC-NOTES.md §12 (M1) and §13 (M2) for what
those runs caught and corrected, and what they left out of scope
(diagnostic-only test tools this bridge has no reason to implement).

## Design trade-offs

Statelessness here is not free — two costs are accepted deliberately, both
recorded in detail in SPEC-NOTES.md §11:

**The task handle (`taskId`) is a larger, opaque blob, not a short random ID.**
It's an AES-256-GCM-sealed, CBOR-encoded envelope containing the A2A agent
id, task id, and context id — any bridge instance can unseal it and resume
the task without a shared task store (that's the whole point; see M4). The
honest cost: two full 128-bit UUIDs plus AES-GCM's 28-byte nonce/tag overhead
is ~60 unavoidable raw bytes before any other field, so handles land around
100–130 base64 characters depending on how long the configured `agent_id`
slug is — noticeably longer than a bare UUID, and long enough to be worth
knowing about if it's threaded through model context repeatedly as a tool
argument. Short `agent_id` values keep this manageable; long ones don't.

**Rotating `BRIDGE_STATE_KEY` is disruptive if done carelessly.** Because the
handle *is* the ciphertext rather than a lookup key, unsealing with only the
new key would silently reject every task handle minted before the rotation —
in-flight tasks would simply become unreachable through this bridge, with no
error pointing at the real cause. `state.py` accepts a `BRIDGE_STATE_KEY_PREVIOUS`
specifically so operators can rotate safely (seal always uses the current
key; unseal tries current then previous), but forgetting to set it, or
dropping it before every handle minted under the old key has either expired
or been re-issued, has exactly this failure mode. There's no protocol
mechanism to warn a client that its handle is now permanently unreadable
versus merely expired (`state.py`'s `ExpiredEnvelope` vs `TamperedEnvelope`
are exceptions this bridge can tell apart internally, but a mis-rotated key
produces the same `TamperedEnvelope` a genuinely forged handle would).

A related, smaller cost: `taskId` carries no reliable task-creation
timestamp (A2A's own `Task` message has no such field, and keeping one in
the envelope purely for display cost more bytes than it was worth — see
SPEC-NOTES.md §12a). `tasks/get` reports `createdAt` equal to the latest
known `lastUpdatedAt` rather than the task's true original creation time.
