# a2a-mcp-bridge

A bridge server that exposes Agent2Agent (A2A) remote agents as tools to Model Context Protocol (MCP) clients. It is built natively against the MCP `2026-07-28` specification revision and targets A2A 1.0. See `SPEC-NOTES.md` for the protocol research this project is built on and `CLAUDE.md` for the working rules that govern this codebase.

## Overview

A2A and MCP solve adjacent but different problems. A2A lets independent agents delegate work to one another over a task-oriented protocol; MCP lets a model-facing client discover and invoke tools. This bridge sits between the two: it discovers A2A agents from their Agent Cards, projects each agent skill as a namespaced MCP tool, and translates every subsequent call, poll, and update between the two protocols' wire shapes.

The bridge holds no state of its own beyond static configuration. Every piece of information needed to resume a task, poll its status, or answer an outstanding prompt travels in a sealed, opaque `taskId` handle that the calling MCP client holds and echoes back. Any running instance of the bridge can service any request for any task, because nothing about a task lives only in one process's memory. The A2A agent on the other end of the connection is the one system that holds real task state throughout a run; the bridge itself does not duplicate it.

## Key properties

- **No bridge-side state.** Task progress, identity, and context all live inside a sealed handle or on the remote A2A agent. No in-process dictionary, no shared cache, no database. A bridge instance can be killed and replaced mid-task without losing the ability to resume that task, as long as the seal key and agent configuration are available to the instance that picks it up.
- **Native to MCP `2026-07-28`.** No `initialize` handshake, no `Mcp-Session-Id`, no held-open SSE stream for eliciting input. Task-oriented interactions use the MCP Tasks extension (`io.modelcontextprotocol/tasks`), not the removed session model.
- **Exhaustive, explicit protocol state translation.** Every A2A `TaskState` value is mapped to an MCP task status through a single exhaustive match in `mapping.py`. An unrecognized state raises an error rather than silently degrading.
- **Credential-safe handling of authentication challenges.** A2A's `TASK_STATE_AUTH_REQUIRED` is treated as structurally distinct from `TASK_STATE_INPUT_REQUIRED`. The bridge never constructs a schema that a model could fill in with a password, API key, or other secret, and never forwards anything into the A2A task while a credential challenge is outstanding.
- **No hand-rolled protocol types.** A2A types come from the official `a2a-sdk`. Every field name, header name, method name, and JSON shape used in the implementation traces back to a documented source in `SPEC-NOTES.md`.

## Architecture

```
MCP client
    |
    | Streamable HTTP, JSON-RPC 2.0
    v
+----------------------------------------------------+
|                    bridge (this repo)               |
|                                                       |
|  server.py     Streamable HTTP entrypoint, header    |
|                 validation, JSON-RPC routing         |
|  discover.py    server/discover, tools/list           |
|                 (Agent Card -> MCP tool projection)   |
|  tasks.py       tools/call, tasks/get, tasks/update,   |
|                 tasks/cancel                          |
|  mapping.py     pure A2A <-> MCP translation functions |
|  state.py       sealed taskId envelope (seal/unseal)   |
|  a2a_client.py  thin async A2A client wrapper          |
|  artifacts.py   artifact summarisation and truncation  |
|  config.py      agent configuration and seal keys      |
+----------------------------------------------------+
    |
    | A2A JSON-RPC, A2A-Version header
    v
A2A remote agent(s)
```

`mapping.py` is deliberately free of I/O: every function in it is a pure, deterministic transformation over already-fetched data, which keeps the A2A-to-MCP state machine exhaustively unit testable without a network or a mock server.

## Protocol compliance

- **MCP:** `2026-07-28`. Streamable HTTP transport only. No `initialize`/`initialized` handshake and no `Mcp-Session-Id` header, both of which were removed in this revision. Task-oriented calls use the Tasks extension (`io.modelcontextprotocol/tasks`), advertised in `server/discover`'s capabilities.
- **A2A:** 1.0, with the JSON-RPC binding. Every outbound A2A call sets the `A2A-Version` header explicitly (an omitted header is silently interpreted as `0.3` by some agents, which this bridge avoids by always setting it). Uses 1.0 method names (`SendMessage`, `GetTask`, `CancelTask`), not the earlier `message/send` / `tasks/get` forms.
- **Sampling, roots, and logging capabilities are not implemented.** All three were deprecated in the targeted MCP revision. Diagnostics go to `stderr` and are intended to be consumed through OpenTelemetry in a real deployment.
- **`tasks/list` is intentionally not implemented.** A2A itself has `ListTasks`, but MCP removed the equivalent because it cannot be scoped safely without a session concept to bind it to a specific client. Projecting `ListTasks` through this bridge without an authorization story would leak visibility across callers.

Two independent runs against the real `modelcontextprotocol/conformance` suite (`server-stateless`, `http-header-validation`, `tools-list` scenarios) were used to validate the header-handling and discovery behavior described below; see `SPEC-NOTES.md` sections 12 through 14 for exact scores and what each run caught.

## Installation

Requires Python 3.12 or later.

```
python -m venv .venv
./.venv/Scripts/pip install -e ".[dev]"
```

On macOS or Linux, use `.venv/bin/pip` instead of `.venv/Scripts/pip`.

## Configuration

The bridge is configured entirely through environment variables. There is no configuration file and no runtime administrative API.

| Variable | Required | Description |
|---|---|---|
| `BRIDGE_AGENTS_CONFIG` | Yes | A JSON array of agent entries: `{"agent_id": "...", "base_url": "...", "headers": {...}}`. `headers` is optional and merged into every outbound A2A request to that agent. `agent_id` should be short: it is embedded in every sealed task handle, and a longer id makes every handle proportionally longer. |
| `BRIDGE_STATE_KEY` | Yes | A base64url-encoded 32-byte AES-256 key used to seal and unseal task handles. Must be identical across every bridge instance serving the same clients. Generate one with `python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`. |
| `BRIDGE_STATE_KEY_PREVIOUS` | No | A second key of the same shape, consulted only when unsealing (never when sealing new handles). Set this alongside a new `BRIDGE_STATE_KEY` while rotating keys so that handles minted under the old key remain valid until they expire or are re-issued. Remove it once every handle minted under the old key is guaranteed to be gone. |
| `BRIDGE_INSTANCE_ID` | No | A short identifier for this process, surfaced on every HTTP response as the `Bridge-Instance-Id` header and included in structured log lines. Falls back to a random id if unset. This is a bridge-internal operational header, not part of the MCP or A2A wire protocol; it exists to make it observable, from outside a deployment, which process served a given request. Useful when running multiple instances behind a load balancer. |

Each agent's base URL must serve a well-known Agent Card at `/.well-known/agent-card.json`, and that card must advertise at least one `JSONRPC`-binding interface. Agents that only offer a GRPC or HTTP+JSON binding are not reachable by this bridge.

## Running the server

```
BRIDGE_AGENTS_CONFIG='[{"agent_id":"docs","base_url":"https://docs-agent.example"}]' \
BRIDGE_STATE_KEY="$(python -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')" \
  ./.venv/Scripts/python -m uvicorn bridge.server:app_from_env --factory --app-dir src
```

The server listens on a single route, `POST /`, and speaks JSON-RPC 2.0 over Streamable HTTP. There is no separate health-check endpoint; `server/discover` with no side effects can be used for that purpose.

## Required headers

Every request must include:

- `MCP-Protocol-Version`, which must match `_meta["io.modelcontextprotocol/protocolVersion"]` in the request body.
- `Mcp-Method`, which must match the JSON-RPC `method` field.
- `Mcp-Name`, required only for `tools/call`, `resources/read`, and `prompts/get`, and must match the corresponding identifier in the request body (`name` for tools and prompts, `uri` for resources).

A request whose headers disagree with its body is rejected with HTTP 400 and JSON-RPC error code `-32020` (`HeaderMismatch`), independent of whether the body itself is otherwise well formed. A request missing `params._meta` or its required subkeys is rejected with `-32602` (`InvalidParams`) before header agreement is even checked, since a structurally incomplete request cannot meaningfully disagree with its headers.

## API reference

All requests are JSON-RPC 2.0 objects posted to `/`. `params._meta` must always include `io.modelcontextprotocol/protocolVersion` and `io.modelcontextprotocol/clientCapabilities`; the latter must declare `{"extensions": {"io.modelcontextprotocol/tasks": {}}}` for any call that might open a task.

### server/discover

Returns the server's supported protocol versions and capabilities. Takes no required parameters and has no side effects.

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "server/discover",
  "params": { "_meta": { "io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {} } }
}
```

### tools/list

Fetches the Agent Card for every configured agent and projects each declared skill into one MCP tool, namespaced as `{agent_id}__{skill_id}`. Every call re-fetches Agent Cards; nothing is cached beyond the `ttlMs` hint the client is free to honor on its own side.

Each projected tool exposes a single required string field, `message`, since A2A skills declare accepted MIME types rather than a structured input schema.

### tools/call

Opens an A2A task (or, if the agent answers immediately without creating one, returns a completed result directly). Requires the client to have declared the `io.modelcontextprotocol/tasks` extension in `clientCapabilities`; a call that omits this is rejected with `-32021` (`MissingRequiredClientCapabilityError`).

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "docs__summarize",
    "arguments": { "message": "Summarize this document." },
    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientCapabilities": { "extensions": { "io.modelcontextprotocol/tasks": {} } }
    }
  }
}
```

A successful response that opened a task returns a sealed `taskId`, the current status, and polling guidance:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "resultType": "complete",
    "taskId": "opaque-sealed-handle",
    "status": "working",
    "createdAt": "2026-08-11T12:00:00Z",
    "lastUpdatedAt": "2026-08-11T12:00:00Z",
    "ttlMs": 86400000,
    "pollIntervalMs": 2000
  }
}
```

### tasks/get

Polls the current state of a previously opened task. Requires only `taskId`.

### tasks/update

Answers an outstanding prompt on a task currently in `input_required` status, or acknowledges an authentication challenge (see Interrupted tasks below). Requires `taskId` and `inputResponses`.

### tasks/cancel

Cancels a task. Requires only `taskId`.

## Task state mapping

Every A2A `TaskState` value maps to exactly one MCP task status. The mapping is implemented as an exhaustive match; a state value not in this table causes the bridge to raise rather than guess.

| A2A `TaskState` | MCP status |
|---|---|
| `TASK_STATE_SUBMITTED` | `working` |
| `TASK_STATE_WORKING` | `working` |
| `TASK_STATE_INPUT_REQUIRED` | `input_required` |
| `TASK_STATE_AUTH_REQUIRED` | `input_required` (handled through a structurally distinct path; see below) |
| `TASK_STATE_COMPLETED` | `completed` |
| `TASK_STATE_FAILED` | `failed` |
| `TASK_STATE_REJECTED` | `failed` |
| `TASK_STATE_CANCELED` | `cancelled` |

A2A's own send-message call blocks by default until the task reaches either a terminal or an interrupted state, which means a blocking call to the remote agent already hands back `TASK_STATE_INPUT_REQUIRED` without any polling being necessary on the bridge's part for the first transition.

## Interrupted tasks

### Ordinary input requests

When a task enters `TASK_STATE_INPUT_REQUIRED`, the next `tasks/get` response includes an `inputRequests` entry describing what the agent needs. The bridge derives this from A2A's `TaskStatus.message`, which is prose, not a schema:

- If the agent's status message includes a structured data part, the bridge derives a JSON Schema directly from that example's keys and value types. No field is invented that was not present in the agent's own data.
- Otherwise, the bridge falls back to a single free-text field.

The client answers by calling `tasks/update` with `inputResponses` keyed `"response"`, containing `{"action": "accept", "content": {...}}` shaped to match whichever schema was presented. The reply is sent back into the same A2A task using the same shape it was asked in: structured in, structured out; free text in, free text out.

### Authentication challenges

`TASK_STATE_AUTH_REQUIRED` collapses to the same MCP status (`input_required`) as an ordinary prompt, because MCP's status enum has no separate value for it, but it is never treated the same way internally. This distinction exists because A2A requires credentials to be exchanged out-of-band unless an in-band mechanism was explicitly negotiated, and the agent may resume entirely on its own once it observes the credential arrive through that out-of-band channel, with no follow-up message required from the client at all.

Anything placed in MCP's `inputResponses` travels through the calling model's context: transcripts, logs, and anywhere else that context is persisted or forwarded. Routing a password or API key through that path would expose it in exactly the places A2A's own design, and MCP's own SEP-1036 url-mode elicitation pattern, are both built to keep credentials out of.

The bridge's handling, keyed `"auth"` rather than `"response"` so it can never be confused with an ordinary prompt:

- If the agent's status message includes a URL, it is surfaced as a url-mode elicitation with no `requestedSchema` at all. There is structurally nothing in the resulting shape for a model to fill in.
- If there is no URL, the fallback is a message-only elicitation with a schema whose `properties` object is empty. Still nothing fillable, only an instruction to complete authentication out of band and wait.
- Either way, calling `tasks/update` for this key never sends anything into the underlying A2A task. The only two things a client can do from here are consent to wait, by acknowledging, or give up, by calling `tasks/cancel`. Resumption, when it happens, is entirely the remote agent's own decision, observed only by polling `tasks/get`.

This is a deliberate trade-off, not an oversight: the A2A specification says agents should continue accepting messages while a task is in `TASK_STATE_AUTH_REQUIRED`, specifically so a client can negotiate, correct, or reject the pending request in-band. This bridge's blanket rule against forwarding anything during that state forecloses in-band negotiation; the only way to reject the interruption is `tasks/cancel`. Given that the alternative is a channel a model's context could use to leak a credential, the more restrictive behavior is the deliberate choice here. An agent that expects in-band negotiation during `TASK_STATE_AUTH_REQUIRED` will not get it through this bridge.

## Task handle design

The MCP `taskId` returned from `tools/call` is not a random identifier backed by a server-side lookup table. It is a sealed, self-describing envelope: an AES-256-GCM-encrypted, CBOR-encoded structure containing the A2A agent id, the A2A task id, the A2A context id, and an expiry timestamp. Any bridge instance holding the correct seal key can unseal it and resume the task without needing to have seen the original request, which is what makes running multiple stateless instances behind a load balancer possible.

Two costs are accepted deliberately in exchange for that property:

**Handle size.** Two full 128-bit UUIDs plus AES-GCM's nonce and authentication tag overhead amount to roughly 60 unavoidable raw bytes before any other field is added. Handles typically land in the range of 100 to 130 base64 characters, depending on the length of the configured `agent_id`. This is noticeably longer than a bare UUID and worth accounting for if the handle is threaded through model context repeatedly as a tool argument. Short `agent_id` values keep the overhead manageable.

**Key rotation risk.** Because the handle is the ciphertext itself rather than a lookup key, unsealing with only a new key would silently reject every handle minted before the rotation, and any task referenced by one of those handles would become unreachable through the bridge with no error pointing at the real cause. `BRIDGE_STATE_KEY_PREVIOUS` exists specifically to avoid this: sealing always uses the current key, but unsealing tries the current key first and falls back to the previous one. Removing the previous key too early, before every handle minted under the old key has expired or been re-issued, reproduces the same failure mode. There is no protocol-level way to distinguish, from the client's point of view, between a handle that is merely expired and one that has become permanently unreadable due to a mis-managed key rotation; both surface identically.

A related, smaller limitation: the handle carries no reliable task-creation timestamp, since A2A's own task message has no such field and reserving envelope bytes purely for display purposes was judged not worth the additional overhead. `tasks/get` reports `createdAt` equal to the most recently observed `lastUpdatedAt` rather than the task's true original creation time.

Rejection of an unsealed handle is exhaustive and specific rather than a generic failure: a bad authentication tag, an expired envelope, an unknown envelope version, and an unknown agent id are each distinguished so that a client presenting an expired handle for a task that may still be running agent-side gets told exactly that, not a bare not-found response.

## Artifact handling

A2A task artifacts are summarized before being placed into a tool result, never dumped whole. Text content is included up to a fixed character limit, with the original length reported alongside if truncation occurred. Binary, URL, and structured data parts are described (media type, byte length, or a presence note) but never inlined. There is currently no follow-up call wired up to fetch a truncated or described-not-inlined artifact's full content; when that lands, the natural mechanism is an MCP `resources/read` URI per artifact, and that URI would need to be a sealed handle of the same kind `taskId` is, not a plain id backed by a server-side lookup table, to preserve the same no-bridge-side-state property.

## Error codes

| Code | Name | Meaning |
|---|---|---|
| `-32700` | Parse error | Request body is not valid JSON. |
| `-32600` | Invalid Request | Body is not a well-formed JSON-RPC 2.0 request. |
| `-32601` | Method not found | Unknown or removed method, including pre-`2026-07-28` methods such as `initialize`. |
| `-32602` | Invalid params | Malformed request, or a rejection reason specific to task state (see `data.reason` below). |
| `-32603` | Internal error | An unhandled error, or a translated downstream A2A task failure. |
| `-32020` | Header mismatch | A header disagrees with the corresponding value in the request body. |
| `-32021` | Missing required client capability | The client attempted `tools/call` without declaring the `io.modelcontextprotocol/tasks` extension. |
| `-32022` | Unsupported protocol version | The requested `MCP-Protocol-Version` is not supported by this server. |

`-32602` responses related to task state carry a `data.reason` field distinguishing the specific cause: `expired` (the sealed handle's expiry has passed), `task_terminal` (the referenced A2A task already reached a terminal state), or `not_awaiting_input` (a `tasks/update` call arrived for a task that has nothing outstanding to answer). A2A's own error codes, such as `UnsupportedOperationError` (`-32004`), are always translated into one of these bridge-facing shapes and never leaked to the client verbatim.

## Testing

```
./.venv/Scripts/python -m pytest
```

The test suite is organized by module: `test_mapping.py` covers the pure A2A-to-MCP translation functions in isolation, `test_state_envelope.py` covers sealing, unsealing, and every handle-rejection path, `test_tasks.py` covers the full `tools/call` through `tasks/update`/`tasks/cancel` lifecycle against a scriptable fake A2A agent, `test_discover.py` and `test_server.py` cover discovery and header validation, and `test_stateless.py` covers cross-instance task resumption (see Multi-instance deployment below).

The fake A2A agent used throughout the test suite (`demo/fake_agent/agent.py`) is the same implementation used by the multi-instance deployment demo, kept as a single source of truth so the two never describe two different agents with subtly different wire behavior.

## Multi-instance deployment

Because the bridge holds no state of its own, multiple instances can run behind a load balancer, sharing nothing but static configuration, the seal key, and the A2A agents they talk to. `demo/` contains a working demonstration of this: three bridge containers behind an nginx round-robin proxy and one A2A agent container that the three bridge instances share.

Three scenarios are exercised, driven by shared code in `demo/driver.py` so the scripted demo (`demo/run_demo.py`) and the automated test suite (`tests/test_stateless.py`) describe the same behavior:

- **Instance failure mid-task.** A task is opened and allowed to reach `input_required`. The specific bridge instance that served the original call, identified from the `Bridge-Instance-Id` response header, is killed. The outstanding prompt is then answered through the load balancer, which necessarily routes to a surviving instance, and the task is confirmed to complete.
- **Full pool restart.** All three bridge instances are restarted while a task is paused, so none of the three retains any in-process memory of the original request. The task is still confirmed to complete correctly.
- **Key rotation.** A handle is minted under one seal key. Every instance is then recreated one at a time with a new current key and the old key set as `BRIDGE_STATE_KEY_PREVIOUS`. The old handle is confirmed to still resume during that two-key window, and a freshly minted handle is confirmed to work under the new key.

nginx is configured with plain round-robin balancing and no session affinity: no IP-hash routing, no cookie-based stickiness, and no persistent keep-alive connection to a single backend, since any of those would defeat the point of the demonstration by pinning a client to one instance.

A general note on how to read this section: the demo and its associated code exist and are believed correct based on their design and on the fact that the individual pieces (the fake agent, the header-based instance identification, the sealing and unsealing logic) are independently tested elsewhere in the suite. Whether it has actually been exercised against real Docker containers in a given environment is a separate question; check `tests/test_stateless.py`'s container-level tests directly (they are skipped, with an explicit reason visible under `pytest -rs`, in any environment without a working Docker installation) rather than assuming from this document alone. See `demo/README.md` for the current status and instructions for running it.

## Known limitations

- Artifact detail beyond the summarized preview is not yet retrievable through a follow-up call.
- `TASK_STATE_AUTH_REQUIRED` cannot be answered in-band, only acknowledged or cancelled, as described above.
- Task handles carry no true creation timestamp.
- Identity propagation across an agent chain (RFC 8693 token exchange, an `act` claim carried through to the A2A call) is not implemented.

## Project layout

```
src/bridge/
    server.py        Streamable HTTP entrypoint, header validation, routing
    discover.py       server/discover; Agent Card -> tools/list projection
    tasks.py          Tasks extension handlers
    state.py          sealed taskId envelope: seal/unseal
    a2a_client.py      thin async A2A client wrapper
    mapping.py         A2A TaskState <-> MCP result translation, pure functions
    artifacts.py        artifact summarisation and truncation
    config.py            agent card URLs, per-agent headers, seal keys
tests/
    test_mapping.py, test_state_envelope.py, test_tasks.py, test_discover.py,
    test_server.py, test_a2a_client.py, test_artifacts.py, test_stateless.py
demo/
    docker-compose.yml, nginx.conf, Dockerfile.bridge, Dockerfile.fake_agent,
    driver.py, run_demo.py, fake_agent/, README.md
```
