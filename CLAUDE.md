# CLAUDE.md — a2a-mcp-bridge

## The one non-negotiable rule

**Do not write MCP or A2A protocol code from memory.** Training data predates the
`2026-07-28` MCP revision (no `initialize` handshake, no `Mcp-Session-Id`, Tasks moved to
an extension, elicitation is a re-issued-request pattern, not held-open SSE) and predates
A2A 1.0. Every field name, header name, method name, and JSON shape used in code must
trace to a line in `SPEC-NOTES.md`. If it isn't there, stop and ask — do not guess a
plausible-looking name, even (especially) one that looks like it should be right from a
2025-era MCP server.

`SPEC-NOTES.md` also records where the spec **contradicted** the original build brief for
this project — most importantly §3.1, on how A2A `TASK_STATE_INPUT_REQUIRED` maps to MCP.
The spec's resolution wins; read that section before touching `state.py` or `mapping.py`.
**The original brief's §4 (`requestState` envelope) is superseded — see SPEC-NOTES §11.**
`state.py` seals `taskId` itself (CBOR, short `agent_id` not a URL, raw UUID bytes, current
+ previous key support, `exp` tracking the A2A task's own TTL), not a separate
`requestState` field.

If a future change to this codebase needs a protocol detail not already in
`SPEC-NOTES.md`, re-fetch from primary sources (raw file content, not rendered GitHub
pages — see "Fetching sources" below) and append to it rather than relying on recall.

## Stack

- Python 3.12+, `uv` for dependency management
- Starlette or FastAPI for the Streamable HTTP transport
- `httpx` (async) for the A2A client
- Official A2A Python SDK (`a2a-sdk`, targets A2A spec 1.0 with 0.3 compat — see
  SPEC-NOTES §6) for A2A types — never hand-roll A2A types
- `cryptography` for the `requestState`/task-handle sealing envelope (AEAD)
- `pytest` + `pytest-asyncio`
- Docker Compose + nginx for the Milestone 4 statelessness demo

## Layout

```
src/bridge/
├── server.py        # Streamable HTTP entrypoint, header validation, routing
├── discover.py       # server/discover; Agent Card -> tools/list projection
├── tasks.py          # Tasks extension handlers (tasks/get, tasks/update, tasks/cancel)
├── state.py           # sealed taskId envelope: seal/unseal (see SPEC-NOTES §3.1)
├── a2a_client.py       # thin async A2A client wrapper
├── mapping.py          # A2A TaskState <-> MCP result translation (pure functions, no I/O)
├── artifacts.py         # artifact summarisation / truncation
└── config.py             # agent card URLs, per-agent headers, keys
```

`mapping.py` stays pure — no network calls, exhaustively unit-testable. Every A2A
`TaskState` must be handled explicitly; an unrecognised state raises, never silently
degrades.

## Fetching sources (when SPEC-NOTES.md needs an addition)

- GitHub file content: `raw.githubusercontent.com/<org>/<repo>/<branch>/<path>`, never the
  rendered `github.com/.../blob/...` page (navigation chrome pollutes the fetch).
- A2A's `docs/specification.md` uses unexpanded Mintlify-style macros
  (`{{ proto_to_table(...) }}`) for generated tables — §4.1.3 (`TaskState`) is one of
  these. The proto (`specification/a2a.proto`) is the authoritative source per the spec's
  own text; cross-check against the rendered site (`a2a-protocol.org/v1.0.0/specification/`)
  where macros are expanded, and record if they disagree.
- MCP: prefer `schema.ts` (or its generated `schema.json`) over prose docs or blog posts
  for field names/casing. Blog examples are simplified for readability and have already
  been wrong once in this project's own recon (see SPEC-NOTES §3.1).
- SEP PRs describe proposals that get amended before merge. Read them for rationale only;
  take field names/error codes from the merged SEP doc
  (`modelcontextprotocol/modelcontextprotocol/seps/<n>-*.md`) or schema, never the PR diff.
- Version-stamp any addition to `SPEC-NOTES.md` with the fetch date and, where available,
  the commit SHA.

## Anti-goals

- No `initialize` / `initialized` handshake. No `Mcp-Session-Id`. Both are gone from
  `2026-07-28`.
- No `tasks/list` — removed because it can't be scoped safely without sessions.
- No sampling, roots, or logging capabilities (all three deprecated) — use `stderr` and
  OpenTelemetry instead.
- No held-open SSE stream as the mechanism for eliciting input.
- No in-process dict as the task store. If it doesn't survive `docker kill`, it fails M4.
- No hardcoded `-32002` for missing-resource; that code moved to `-32602`
  (`INVALID_PARAMS`).
- No dumping full artifact bodies into tool results — summarise, expose detail behind a
  separate call.
- No hand-rolled A2A types when the SDK provides them.
- No invented field names. If it isn't in `SPEC-NOTES.md`, ask.
- No conflating MCP's `tasks/get`/`tasks/update`/`tasks/cancel` (Tasks extension) with
  A2A's `GetTask`/`SendMessage`/`CancelTask` — same shape of idea, two different wire
  protocols, translated by `mapping.py`. Keep the two method-name namespaces mentally and
  lexically separate in code (don't reuse a bare `get_task` helper name for both, etc.).

## Working process

One milestone at a time, commit at each boundary, don't start the next until acceptance
criteria pass. After each milestone, summarize what changed and what's uncertain. If a
fetched spec contradicts this file or the original build brief, the spec wins — flag the
contradiction and say so explicitly rather than silently picking one.
