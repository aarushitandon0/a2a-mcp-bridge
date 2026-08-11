# SPEC-NOTES.md

**Fetched:** 2026-08-11
**Gate result:** MCP `2026-07-28` is the **Current** protocol revision (not Draft — see §0). All notes below are traceable to a source URL and, where the source is a git repo, a commit SHA captured at fetch time.

---

## 0. Gate: did 2026-07-28 ship?

**Yes — it is the "Current" revision as of 2026-08-11.**

Source: [`modelcontextprotocol.io/specification/versioning`](https://modelcontextprotocol.io/specification/versioning) (fetched 2026-08-11):

> Revisions may be marked as: **Draft** (in-progress, not ready for consumption), **Current** (ready for use, may still receive backwards-compatible changes), **Final** (past, complete, will not change).
> The **current** protocol version is **2026-07-28**.

Corroborating evidence:
- `https://modelcontextprotocol.io/specification/2026-07-28` resolves directly (no redirect to `/draft`, no draft banner).
- `https://modelcontextprotocol.io/specification/draft/changelog` is **empty** ("Changes since the most recent release will accumulate here") — i.e. nothing has diverged from the latest release, so `draft` and `2026-07-28` currently coincide.
- The RC blog post ([blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/), fetched 2026-08-11) states the final spec ships **2026-07-28**; today is 2026-08-11, so that date has passed. Note: this post is itself an RC announcement, not proof of shipping — the versioning-page "Current" status is the actual proof.

**Note on "Current" vs "Final":** 2026-07-28 is *not yet* marked Final — it may still receive backwards-compatible changes. Nothing in this project depends on Final status; "Current" is the right revision to build against. Flagging only so it isn't misread as frozen.

---

## 1. MCP core schema (authoritative: schema.ts)

Source: [`raw.githubusercontent.com/modelcontextprotocol/specification/main/schema/2026-07-28/schema.ts`](https://raw.githubusercontent.com/modelcontextprotocol/specification/main/schema/2026-07-28/schema.ts)
Commit: `b25c0874bf0ba699a58e21ef06f659d839659de3` (2026-08-10) — see §8 provenance note; this raw path resolves through to the `modelcontextprotocol/modelcontextprotocol` repo.
`export const LATEST_PROTOCOL_VERSION = "2026-07-28";` confirmed in-file.

### 1.1 `InputRequiredResult` (core, non-Tasks pattern — see §3 for when this applies)

```typescript
export type ResultType = "complete" | "input_required" | string;

export interface InputRequiredResult extends Result {
  inputRequests?: InputRequests;
  requestState?: string;   // "Request state passed back to the server from the client."
}
```

JSDoc on `requestState`: **"The client must treat this as an opaque blob; it must not interpret it in any way."** — confirms the seal/unseal design in `state.py` is correct: the client never parses it, only echoes it.

Casing confirmed: **`input_required`** for the `resultType` string value (snake_case), **`inputRequests`** / **`requestState`** for field names (camelCase). Not `inputRequired`.

### 1.2 Re-issuing a request with answers (core pattern)

```typescript
export interface InputResponseRequestParams extends RequestParams {
  inputResponses?: InputResponses;
  requestState?: string;
}
```

`CallToolRequest`, `GetPromptRequest`, `ReadResourceRequest` all extend `InputResponseRequestParams`. The client re-sends the **same** request (same method, same original params) with `inputResponses` + the echoed `requestState` added to `params`. There is no dedicated "respond" method — the retry *is* the original call.

### 1.3 `server/discover`

```typescript
export interface DiscoverRequest extends JSONRPCRequest {
  method: "server/discover";
  params: RequestParams;
}

export interface DiscoverResult extends CacheableResult {
  supportedVersions: string[];
  capabilities: ServerCapabilities;
  instructions?: string;
}
```

Mandatory RPC (per versioning page). Clients may skip it and just send requests, handling `UnsupportedProtocolVersionError` if the version is wrong.

### 1.4 `_meta` keys

```
"io.modelcontextprotocol/protocolVersion": string;          // required, every request
"io.modelcontextprotocol/clientInfo"?: Implementation;
"io.modelcontextprotocol/clientCapabilities": ClientCapabilities;
"io.modelcontextprotocol/logLevel"?: LoggingLevel;
```

### 1.5 `CallToolResult`

```typescript
export interface CallToolResult extends Result {
  content: ContentBlock[];
  structuredContent?: unknown;
  isError?: boolean;
}

export interface CallToolResultResponse extends JSONRPCResultResponse {
  result: CallToolResult | InputRequiredResult;
}
```

Note: this union does **not** include `CreateTaskResult` at the core-schema level — the Tasks extension adds that as a further possibility at the wire level when the extension is negotiated (§3).

### 1.6 `ElicitRequestParams` (for deriving `inputSchema` from an agent's prompt in M3)

```typescript
export type ElicitRequestParams = ElicitRequestFormParams | ElicitRequestURLParams;

// Form variant:
requestedSchema: {
  $schema?: string;
  type: "object";
  properties: { [key: string]: PrimitiveSchemaDefinition };
  required?: string[];
}
// URL variant carries `url` instead of requestedSchema.
```

### 1.7 `ServerCapabilities` / `ClientCapabilities` extensions field

```typescript
extensions?: { [key: string]: JSONObject };
```

Keyed by reverse-DNS extension ID (e.g. `"io.modelcontextprotocol/tasks"`), value is the extension's own capability object (empty `{}` is valid — just signals support).

---

## 2. MCP error codes

Source: same `schema.ts`.

```
INVALID_PARAMS            = -32602
HEADER_MISMATCH            = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022
```

There is **no dedicated "resource not found" code** — that case now falls under `INVALID_PARAMS (-32602)`. Confirms anti-goal in the build brief: `-32002` is gone.

`HEADER_MISMATCH` was originally proposed as `-32001` in SEP-2243 and changed to `-32020` before merge — noted per amendment 4 (spec text wins over PR text).

---

## 3. MCP Tasks extension (`io.modelcontextprotocol/tasks`)

Source: [`github.com/modelcontextprotocol/ext-tasks`](https://github.com/modelcontextprotocol/ext-tasks), schema at [`schema/draft/schema.ts`](https://raw.githubusercontent.com/modelcontextprotocol/ext-tasks/main/schema/draft/schema.ts), spec prose at [`specification/draft/tasks.md`](https://raw.githubusercontent.com/modelcontextprotocol/ext-tasks/main/specification/draft/tasks.md).
Commit: `2c1425d9a288b9b1f489430fe1e00bb392b47e48` (2026-07-15).

**Extension ID:** `io.modelcontextprotocol/tasks` (reverse-DNS, confirmed in both the schema and the modelcontextprotocol.io overview page).

**Negotiation:**
- Client, per-request: `_meta["io.modelcontextprotocol/clientCapabilities"].extensions["io.modelcontextprotocol/tasks"] = {}`
- Server, in `server/discover` response: `capabilities.extensions["io.modelcontextprotocol/tasks"] = {}`
- Server MUST verify the client declared the extension before ever returning a task handle.

**Wire shapes:**

```typescript
type CreateTaskResult = Result & Task;   // resultType: "task", returned from tools/call etc.

interface Task {
  taskId: string;          // opaque, server-chosen, MUST have "sufficient entropy that a
                            // third party cannot enumerate or guess them" (spec, tasks.md)
  status: TaskStatus;       // "working" | "input_required" | "completed" | "failed" | "cancelled"
  statusMessage?: string;
  createdAt: string;        // ISO 8601
  lastUpdatedAt: string;    // ISO 8601
  ttlMs: number | null;
  pollIntervalMs?: number;
}

// Discriminated union (DetailedTask), by status:
// WorkingTask     — base Task fields only
// InputRequiredTask — base fields + inputRequests: InputRequests   (NO requestState — see §3.1)
// CompletedTask   — base fields + result: { [key: string]: unknown }
// FailedTask      — base fields + error: { [key: string]: unknown }
// CancelledTask   — base fields only

interface GetTaskRequest extends JSONRPCRequest {
  method: "tasks/get";
  params: { taskId: string };
}
// GetTaskResult = Result & DetailedTask

interface UpdateTaskRequest extends JSONRPCRequest {
  method: "tasks/update";
  params: { taskId: string; inputResponses: InputResponses };
}

interface CancelTaskRequest extends JSONRPCRequest {
  method: "tasks/cancel";
  params: { taskId: string };
}
// CancelTaskResult = Result  (empty ack; cancellation is cooperative)

type InputRequest  = CreateMessageRequest | ListRootsRequest | ElicitRequest;
type InputResponse = CreateMessageResult  | ListRootsResult  | ElicitResult;
interface InputRequests  { [key: string]: InputRequest; }
interface InputResponses { [key: string]: InputResponse; }
```

**No `tasks/list`.** Confirmed absent from both the schema and the spec prose — matches anti-goal §6 in the build brief (removed because it can't be scoped safely without sessions).

### 3.1 THE CRITICAL FINDING — two separate interrupt mechanisms, not one

The build brief's M3 describes returning `InputRequiredResult` with a sealed `requestState`, then "on the re-issued call... post `inputResponses` back into the same A2A task." **This conflates two mechanisms that the spec keeps deliberately separate**, distinguished by *timing*:

> From `ext-tasks/specification/draft/tasks.md`: "A server that needs client input **before** returning a `CreateTaskResult` (e.g. to decide whether to proceed) uses the multi round-trip request flow on the original request; a server that needs client input **during** task execution uses the `inputRequests`/`inputResponses` mechanism described here [tasks/update]." They do not compose — a server uses one or the other, chosen per request, based on when the input is needed.

Confirmed independently: `requestState` does **not** appear anywhere in the ext-tasks schema or spec prose (grepped both files). It is exclusively a core-spec, non-Tasks-extension concept for the "before a task exists" case.

**Consequence for this bridge's design:**

A2A's `TASK_STATE_INPUT_REQUIRED` only ever happens *after* an A2A task has already been submitted and started running (`SUBMITTED` → `WORKING` → `INPUT_REQUIRED`). That is always the "during execution" case. So:

- M2's `tools/call` → `CreateTaskResult` should use **non-blocking** A2A `SendMessage` (`return_immediately: true`) so the bridge can hand back a task handle immediately rather than holding the HTTP request open — consistent with the Tasks extension's whole reason for existing ("no long-lived connections," per the M0 brief's own §3.1 anti-goals, and per the SEP-2322/2567/2575 rationale below).
- A2A `TASK_STATE_INPUT_REQUIRED` maps to MCP `Task.status == "input_required"`, surfaced via `tasks/get`, carrying `inputRequests` (derived from the A2A input request).
- The client resumes via **`tasks/update`** with `{taskId, inputResponses}` — **not** a re-issued `tools/call` with a `requestState` blob. There is no `requestState` field anywhere in this path.
- **The statelessness proof (M4) still works, and arguably more cleanly than the brief's design**, because the Tasks extension leaves `taskId` format entirely to the server, with only one constraint: high entropy / unguessable. A sealed AEAD envelope (§4 of the build brief) is a perfect fit *as the `taskId` itself* — not as a separate `requestState` field. Any bridge instance can unseal `taskId` on `tasks/get` / `tasks/update` / `tasks/cancel` and recover `{agent_url, a2a_task_id, a2a_context_id}` to serve the request statelessly.
- The core `InputRequiredResult`/`requestState` pattern (§1.1–1.2) is very likely **unused** by this bridge, since every `tools/call` immediately produces a task handle rather than needing input before one exists. Keeping `mapping.py` capable of raising clearly on that path (rather than silently no-op) is still worth it in case a future skill needs pre-task elicitation, but M3's acceptance criteria and `state.py`'s envelope purpose should be rewritten around `taskId`, not `requestState`.

**This directly contradicts the M3 section of the build brief as written. Per §7 of the brief, the spec wins.** Flagged to the user; proceeding on this corrected design per "spec wins" + the standing "start coding if required" authorization. See the message accompanying this M0 delivery for the explicit callout.

---

## 4. MCP Streamable HTTP transport — required headers

Source: [`specification/2026-07-28/basic/transports/streamable-http`](https://raw.githubusercontent.com/modelcontextprotocol/specification/main/docs/specification/2026-07-28/basic/transports/streamable-http.mdx) (same repo/commit as §1).

**Request headers, every POST:**
- `MCP-Protocol-Version` — **MUST** be present, **MUST** match `_meta["io.modelcontextprotocol/protocolVersion"]` in the body. Mismatch → `400` + `HeaderMismatch` (`-32020`).
- `Mcp-Method` — mirrors the JSON-RPC `method` field, on **every** request (routing hint for infra that can't/shouldn't parse the body).
- `Mcp-Name` — mirrors `params.name` (tools/call) or `params.uri` (resources/read) etc., required for those specific request types.
- `Accept: application/json, text/event-stream`
- `Content-Type: application/json`

**Response headers:**
- `Content-Type: application/json` or `text/event-stream`
- `X-Accel-Buffering: no` recommended on SSE responses.

**Header/body disagreement:** `400 Bad Request` + JSON-RPC error `-32020` `HeaderMismatch`. Example from spec: `"Header mismatch: Mcp-Name header value 'foo' does not match body value 'bar'"`.

**No `Mcp-Session-Id` anywhere** — confirmed absent, matches anti-goals.

Rationale (SEP-2243, `modelcontextprotocol/modelcontextprotocol/seps/2243-http-standardization.md`): headers let load balancers/gateways route on method/name without deep packet inspection; body remains authoritative for execution, hence the mandatory equality check to prevent infra and server from acting on different values.

---

## 5. A2A `TaskState` enum and related types

**Primary source (authoritative per A2A spec — proto is "the single authoritative normative definition"):** [`raw.githubusercontent.com/a2aproject/A2A/main/specification/a2a.proto`](https://raw.githubusercontent.com/a2aproject/A2A/main/specification/a2a.proto), commit `4f82944ddf6821b9d19bc4aa03a59acd8a09a4ce` (2026-08-10).
**Cross-checked against:** rendered site [`a2a-protocol.org/v1.0.0/specification/`](https://a2a-protocol.org/v1.0.0/specification/). **Both agree exactly** on the enum below.

```protobuf
enum TaskState {
  TASK_STATE_UNSPECIFIED  = 0;
  TASK_STATE_SUBMITTED    = 1;
  TASK_STATE_WORKING      = 2;
  TASK_STATE_COMPLETED    = 3;
  TASK_STATE_FAILED       = 4;
  TASK_STATE_CANCELED     = 5;   // one "L" — American spelling, note for mapping.py
  TASK_STATE_INPUT_REQUIRED = 6;
  TASK_STATE_REJECTED     = 7;
  TASK_STATE_AUTH_REQUIRED = 8;
}
```

Matches the build brief's table exactly (§3 of the brief). Good — no correction needed there.

**Proto package:** `lf.a2a.v1` (the `lf.` prefix explains the `a2a.SendMessage`-style shorthand seen on the rendered docs site — see §8 Conflicts; it is not part of the actual JSON-RPC method string).

**`Message` fields relevant to resume** (`specification/a2a.proto`):
```protobuf
message Message {
  string message_id = 1 [REQUIRED];
  string context_id = 2;
  string task_id = 3;
  Role role = 4 [REQUIRED];
  repeated Part parts = 5 [REQUIRED];
  google.protobuf.Struct metadata = 6;
  repeated string extensions = 7;
  repeated string reference_task_ids = 8;
}
```
Proto fields are `snake_case` (`context_id`, `task_id`); standard protobuf JSON mapping renders these as `camelCase` (`contextId`, `taskId`) on the wire — confirmed by the rendered site's JSON examples using `contextId`/camelCase. `bridge/mapping.py` and `bridge/a2a_client.py` should use the SDK's generated types (camelCase-aware) rather than hand-building JSON, per anti-goal "no hand-rolled A2A types."

**Resume semantics** (A2A spec §3.4.2–3.4.3, `docs/specification.md`):
> "When a client includes a `taskId` in a `Message`, it **MUST** reference an existing task." / "the provided `contextId` is different from that of the referenced Task" → rejection.

Confirms the build brief's §3.1 bullet: envelope must carry both `taskId` and `contextId` and pass them back unmodified; A2A itself enforces the match, so the bridge doesn't need to duplicate that validation (though it should surface the A2A rejection cleanly rather than swallow it).

**`SendMessageConfiguration`:**
```protobuf
message SendMessageConfiguration {
  repeated string accepted_output_modes = 1;
  TaskPushNotificationConfig task_push_notification_config = 2;
  optional int32 history_length = 3;
  bool return_immediately = 4;  // default false = blocking
}
```
Field is `return_immediately`, not the build brief's `SendMessageConfiguration.return_immediately` — same name, confirmed correct in the brief. Default `false` (blocking). **Design decision (§3.1 above): the bridge sets `return_immediately: true`** so `tools/call` returns a task handle promptly instead of holding the connection open — required for consistency with MCP Tasks' "no long-lived connections" goal, and the reason M2 targets `tasks/get`-based polling rather than relying on a blocking send to surface `INPUT_REQUIRED` synchronously.

**JSON-RPC method names** — confirmed via `docs/specification.md` §5.3 "Method Mapping Reference" table (raw markdown, not the rendered/proxied site — see Conflicts):

| Functionality | JSON-RPC `method` string |
|---|---|
| Send message | `SendMessage` |
| Send streaming message | `SendStreamingMessage` |
| Get task | `GetTask` |
| Cancel task | `CancelTask` |
| List tasks | `ListTasks` |
| Subscribe to task | `SubscribeToTask` |

These are the literal, unprefixed 1.0 method names — matches the build brief exactly. **Do not use** `message/send`, `tasks/get` (those are 0.x names, and also collide confusingly with MCP's own `tasks/get` from a completely different protocol — another reason to keep the two method namespaces mentally separate in code).

**`A2A-Version` header** (`docs/specification.md` §3.6.1, quoted verbatim):
> "Clients MUST send the `A2A-Version` header with each request to maintain compatibility after an agent upgrades to a new version of the protocol (except for 0.3 Clients - 0.3 will be assumed for empty header)."

**Consequence:** the bridge's `a2a_client.py` must set `A2A-Version` explicitly on every outbound call. Given the SDK's primary target is 1.0 (§6), set it to the SDK's exact 1.0 version string — see Unresolved §9, the precise string (`"1.0"` vs `"1.0.0"`) wasn't pinned down from the SDK source in M0.

**§4.1.3 macro non-expansion, confirmed exactly as the user described:** `docs/specification.md` §4.1.3 literally contains the unexpanded macro `{{ proto_enum_to_table("TaskState") }}` in the raw markdown — this is real, not a fetching artifact. The proto file is the correct source, as the user directed.

---

## 6. A2A Python SDK version target

Source: [`github.com/a2aproject/a2a-python`](https://github.com/a2aproject/a2a-python), `README.md` and `pyproject.toml`, commit `cff67270d1da52bad96172c57580dad9b002aa11` (2026-08-03).

> "This SDK implements the A2A Protocol Specification **1.0**, with compatibility mode for **0.3**."

Confirmed via a compatibility matrix (JSON-RPC / HTTP+JSON-REST / gRPC × client/server, all full support for both spec versions) and a `src/a2a/compat/v0_3/` module (containing `grpc_handler.py`, `grpc_transport.py` — 0.3-specific transport shims). A `v0.3 → v1.0 migration guide` is referenced for SDK users upgrading.

**No divergence to report**: SDK's primary target (1.0) matches the spec version fetched in §5. Good — this does not change M2's shape as the brief worried it might.

`pyproject.toml` version is dynamically generated via `uv-dynamic-versioning` from git tags — no static version pin in the file itself; pin the dependency by tag/commit in this project's `pyproject.toml` once M1 starts.

---

## 7. Conformance suite

Source: [`github.com/modelcontextprotocol/conformance`](https://github.com/modelcontextprotocol/conformance), commit `c321dd32035556e6769d3724a8ee97d87c3faaac` (2026-08-07, version `0.2.0-alpha.11`).

Relevant scenario files located:
- `src/scenarios/server/http-standard-headers.ts` (+ `.test.ts`) — covers M1's header validation acceptance criterion.
- `src/scenarios/client/http-standard-headers.ts` (+ `.test.ts`)
- `src/scenarios/server/tasks/lifecycle.test.ts`, `src/scenarios/server/tasks/dispatch.ts` — covers M2/M3 task lifecycle.
- `src/scenarios/server/input-required-result.ts` (+ `-helpers.ts`) — core (non-Tasks) input-required path (§1.1–1.2, §3.1).
- `src/scenarios/server/elicitation-defaults.ts`, `elicitation-enums.ts`, `src/scenarios/client/elicitation-defaults.ts`.

**No `server/discover` scenario found** by name search — noted in Unresolved (§9); worth re-checking directly against the repo once M1 starts, since discovery might be folded into a general handshake/routing scenario file rather than a dedicated one.

---

## 8. Conflicts

| # | Where | Conflict | Resolution |
|---|---|---|---|
| 1 | A2A JSON-RPC method naming | `a2a-protocol.org/v1.0.0/specification/` (rendered) showed `a2a.SendMessage`; the raw `docs/specification.md` §5.3 Method Mapping Reference shows bare `SendMessage`. | **Raw spec wins** (amendment 4). The `a2a.` prefix is explained by the proto's `package lf.a2a.v1;` declaration — it's gRPC-style fully-qualified shorthand on the rendered site, not the JSON-RPC wire method string. Use bare `SendMessage`, `GetTask`, etc. |
| 2 | Build brief M3 vs. Tasks-extension spec | Brief describes input-required via `InputRequiredResult` + sealed `requestState`, echoed on a re-issued call. Spec (`ext-tasks/specification/draft/tasks.md`) defines this as the **pre-task** pattern only; the **during-task** pattern (which is what A2A's `INPUT_REQUIRED` always is) uses `Task.status == "input_required"` + `tasks/update({taskId, inputResponses})`, with no `requestState` field at all. | **Spec wins, per brief §7.** See §3.1 above for the full resolution: the sealed envelope becomes the `taskId` itself, not a separate `requestState` field. Flagged explicitly to the user. |
| 3 | SEP-2243 draft vs. merged spec | The PR text for SEP-2243 originally proposed `-32001` for `HeaderMismatch`; the merged SEP doc and `schema.ts` both show `-32020`. | **Merged spec/schema wins** (amendment 4) — use `-32020`. |
| 4 | Repo identity | `raw.githubusercontent.com/modelcontextprotocol/specification/...` resolves to real, self-consistent spec content, but the org's repo listing (`api.github.com/orgs/modelcontextprotocol/repos`) did not show a repo literally named `specification`, and a commit-SHA lookup against `modelcontextprotocol/specification` returned content identical to `modelcontextprotocol/modelcontextprotocol` (same SHA, same commit summary about a Ruby SDK doc change). | Treated as a renamed/redirected repo (GitHub redirects raw/API requests transparently on rename) — **not a content-authenticity problem**: the schema content itself cross-validates against ext-tasks, the blog post, and the docs site everywhere it overlaps. Recorded here for provenance; worth a quick `git remote -v`-style sanity check if this ever looks stale. |

---

## 9. Unresolved

1. ~~Exact `A2A-Version` header value~~ **RESOLVED 2026-08-11.** A2A spec (`docs/specification.md`, protocol versioning section): protocol versions are identified `Major.Minor`; patch numbers **SHOULD NOT** appear in requests, responses, or Agent Cards. Confirmed against the SDK source, not just the spec text — `a2a-python/src/a2a/utils/constants.py` (commit `cff67270d1da52bad96172c57580dad9b002aa11`): `VERSION_HEADER = 'A2A-Version'`, `PROTOCOL_VERSION_1_0 = '1.0'`, `PROTOCOL_VERSION_CURRENT = PROTOCOL_VERSION_1_0`. `a2a-python/tests/integration/test_version_header.py` confirms server-side handling treats `'1.2'` as compatible-with-`'1.0'` (minor forward-compat) and rejects unknown major versions. **Bridge sends `A2A-Version: 1.0` on every outbound A2A call.**
2. **No dedicated `server/discover` conformance scenario found** by filename search in the conformance repo (§7). May exist under a different name (e.g. folded into a routing/handshake scenario) — recheck when M1's acceptance criteria are being run against the suite, don't assume it's simply missing.
3. **Tasks extension is still under `schema/draft/`** in `ext-tasks` (not a dated/versioned schema directory like MCP core's `2026-07-28`). This is normal for an extension that's explicitly incubating (SEP-2663 rationale, §5's rationale fetch), but means the Tasks extension schema could still move under this project's feet in a way the core `2026-07-28` schema won't. No action needed now; worth re-fetching before M2 actually starts coding against it if much time has passed.
4. **Core `InputRequiredResult`/`requestState` path may end up genuinely dead code** in this bridge per the §3.1 finding (A2A never needs pre-task input). Confirm this holds once A2A's `AUTH_REQUIRED` state is examined in M2/M5 — it's conceivable an auth challenge could arrive before a task is ever created (e.g. agent-card-level auth negotiation) rather than mid-task, in which case the core pattern *would* apply after all. Don't delete `state.py` support for it without checking that first.

---

## 10. Agent Card discovery (needed for M1 `discover.py` / `tools/list` projection)

Source: `docs/specification.md`, same commit as §5.

**Well-known path:** `https://{server_domain}/.well-known/agent-card.json` (§8.2).

**`AgentCard` (proto, §5):**
```protobuf
message AgentCard {
  string name = 1 [REQUIRED];
  string description = 2 [REQUIRED];
  repeated AgentInterface supported_interfaces = 3 [REQUIRED];
  AgentProvider provider = 4;
  string version = 5 [REQUIRED];
  optional string documentation_url = 6;
  AgentCapabilities capabilities = 7 [REQUIRED];
  map<string, SecurityScheme> security_schemes = 8;
  repeated SecurityRequirement security_requirements = 9;
  repeated string default_input_modes = 10 [REQUIRED];
  repeated string default_output_modes = 11 [REQUIRED];
  repeated AgentSkill skills = 12 [REQUIRED];
  repeated AgentCardSignature signatures = 13;
  optional string icon_url = 14;
}

message AgentSkill {
  string id = 1 [REQUIRED];
  string name = 2 [REQUIRED];
  string description = 3 [REQUIRED];
  repeated string tags = 4 [REQUIRED];
  repeated string examples = 5;
  repeated string input_modes = 6;   // overrides card-level default_input_modes
  repeated string output_modes = 7;
  repeated SecurityRequirement security_requirements = 8;
}
```

**`AgentInterface`** (§4.4.6): `url`, `protocolBinding` (enum: `JSONRPC` | `GRPC` | `HTTP+JSON`), `protocolVersion`, optional `tenant`. The bridge always selects the `JSONRPC` binding (per §5's confirmed method-name table) when multiple are offered.

**Design note — `AgentSkill` has no per-field JSON Schema.** `input_modes`/`output_modes` are MIME-type strings (e.g. `"text/plain"`), not a JSON Schema. This means the brief's M1 phrase "`inputSchema` derived from the skill's declared input modes" cannot mean field-level schema extraction — A2A doesn't expose that. **Resolution:** each projected MCP tool gets a minimal, uniform `inputSchema`: a required `message` string property (mapped to the text `Part` of the outbound A2A `Message`), plus the skill's `input_modes` surfaced in the tool `description` (not the schema) as a hint of what content types the skill accepts. This is a pragmatic reading, not a spec fact — flagged here rather than silently assumed, since it's the kind of gap between the two protocols the brief didn't anticipate.

**JSON-RPC error envelope** — MCP side (`schema.ts`): `{jsonrpc: "2.0", id?: RequestId, error: {code, message, data?}}`. A2A side (`docs/specification.md` §9.5): standard JSON-RPC error object, `{code, message, data?}`, with A2A-specific codes like `-32001 TaskNotFoundError` layered on top of the base JSON-RPC codes — **do not confuse these with MCP's own error codes in §2 above; they share the JSON-RPC 2.0 envelope shape but are two separate code spaces**, translated (not passed through) by `mapping.py`.

---

## 11. Superseding note — original brief §4 (`requestState` envelope)

**The original build brief's §4 ("The `requestState` envelope") is superseded by the finding in §3.1 above, per user direction 2026-08-11: "record in SPEC-NOTES.md that §4 of the original brief is superseded."**

What's kept from brief §4: the core idea (AEAD-sealed, opaque, unguessable handle so any bridge instance can resume a paused task) and the four rejection paths (bad MAC, expired, unknown version, agent not in allowlist) are exactly right and still apply — just to `taskId`, not to a `requestState` field.

What changes, per the user's 2026-08-11 stress test of the sealed-`taskId` design:

**a) Size.** The brief's plaintext example (`agent_url` as a full URL string, `a2a_task_id`/`a2a_context_id` as UUID strings, JSON encoding) lands north of 300 chars once AEAD-sealed and base64url'd — too large for a value that gets threaded through model context as a tool argument, potentially repeatedly. Revised plaintext payload:

```
{
  v: 2,                      // envelope format version (uint8)
  agent_id: "...",           // short ID, keys into static config — NOT the full agent_url
  a2a_task_id: <16 bytes>,   // raw UUID bytes, not the 36-char string form
  a2a_context_id: <16 bytes>,
  step: <uint>,
  iat: <uint32, unix seconds>,
  exp: <uint32, unix seconds>,
}
```
CBOR-encoded (not JSON) before sealing. `agent_id` resolves against `config.py`'s static agent table, which is deployed identically to every bridge instance — so this is configuration, not server-side state, and doesn't reintroduce the thing M4 is proving doesn't exist. Target: under 100 characters for the final base64url `taskId`. Exact byte budget and the CBOR field-key scheme (integer keys, not string keys, to shave more bytes) to be finalized in M2 when `state.py` is written and can be measured directly rather than estimated.

**b) Key rotation.** Since the handle *is* the ciphertext (not a lookup key into a store), rotating `BRIDGE_STATE_KEY` would silently orphan every in-flight task if unseal only tried one key. `state.py` unseal must accept two keys — current and previous — trying current first; seal always uses current only. Config needs `BRIDGE_STATE_KEY` (current) and `BRIDGE_STATE_KEY_PREVIOUS` (optional, unset after the rotation window closes).

**c) Expiry.** Brief §4's `exp` was written assuming it only staled a resume blob; on the sealed-`taskId` design, `exp` firing makes the **task itself unaddressable** through this bridge — a stronger consequence than the brief anticipated, since `taskId` is now the only handle the MCP client holds. `exp` must be set against the **longest plausible A2A task lifetime** (bounded by the Tasks extension's own `ttlMs` on the `Task` object — the envelope's `exp` should track/exceed the A2A task's own TTL, not use an arbitrary short window). When a client presents an expired-but-otherwise-valid handle for a task that's still alive agent-side, `tasks/get`/`tasks/update`/`tasks/cancel` must return a clear, distinct error (not a generic "not found") — the A2A task may still be running with no way for this client to reach it. Exact error shape (MCP error code + message content) to be decided in M2's `tasks.py`.

**d) Name the trade-off.** Statelessness is bought at the cost of a larger opaque handle than a random UUID would be, and disruptive-if-mishandled key rotation. `README.md` (once written) must say this plainly rather than presenting statelessness as free.
