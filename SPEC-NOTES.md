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
  0: v,               // envelope format version (int)
  1: agent_id,         // short ID, keys into static config — NOT the full agent_url
  2: a2a_task_id,      // 16 raw bytes if the agent's id is a UUID, else the string verbatim
  3: a2a_context_id,   // same encoding rule as a2a_task_id
  4: iat,              // unix seconds
  5: exp,              // unix seconds
}
```
Integer CBOR keys (not string keys) to shave bytes. `agent_id` resolves against `config.py`'s static agent table, which is deployed identically to every bridge instance — so this is configuration, not server-side state, and doesn't reintroduce the thing M4 is proving doesn't exist.

**No `step` field, dropped from the brief's original sketch.** Verified against `ext-tasks/schema/draft/schema.ts`: `UpdateTaskResult = Result` and `CancelTaskResult = Result` — both bare acknowledgements, neither echoes a `taskId` or `Task` back. There is no mechanism for the server to hand the client a new handle mid-lifecycle, so the sealed `taskId` minted at `tools/call` time must stay byte-for-byte valid through every subsequent `tasks/get`/`tasks/update`/`tasks/cancel` — it cannot be re-sealed with an incremented counter, because the client has no way to learn the new value. A `step` field would either sit permanently at its minted value (dead weight) or be actively wrong if `state.py` ever tried to mutate it. `a2a_task_id`/`a2a_context_id` are themselves already A2A's own round-tracking mechanism (via `Message.reference_task_ids` and A2A's own history) — this bridge doesn't need a parallel counter.

Not yet measured against a live encode -- exact final byte count for the base64url `taskId` (target: under 100 characters) to be confirmed once `state.py` is implemented.

**b) Key rotation.** Since the handle *is* the ciphertext (not a lookup key into a store), rotating `BRIDGE_STATE_KEY` would silently orphan every in-flight task if unseal only tried one key. `state.py` unseal must accept two keys — current and previous — trying current first; seal always uses current only. Config needs `BRIDGE_STATE_KEY` (current) and `BRIDGE_STATE_KEY_PREVIOUS` (optional, unset after the rotation window closes).

**c) Expiry.** Brief §4's `exp` was written assuming it only staled a resume blob; on the sealed-`taskId` design, `exp` firing makes the **task itself unaddressable** through this bridge — a stronger consequence than the brief anticipated, since `taskId` is now the only handle the MCP client holds. `exp` must be set against the **longest plausible A2A task lifetime** (bounded by the Tasks extension's own `ttlMs` on the `Task` object — the envelope's `exp` should track/exceed the A2A task's own TTL, not use an arbitrary short window). When a client presents an expired-but-otherwise-valid handle for a task that's still alive agent-side, `tasks/get`/`tasks/update`/`tasks/cancel` must return a clear, distinct error (not a generic "not found") — the A2A task may still be running with no way for this client to reach it. Exact error shape (MCP error code + message content) to be decided in M2's `tasks.py`.

**d) Name the trade-off.** Statelessness is bought at the cost of a larger opaque handle than a random UUID would be, and disruptive-if-mishandled key rotation. `README.md` (once written) must say this plainly rather than presenting statelessness as free.

---

## 12. M1 findings from running the real conformance suite

M0's schema.ts reads were shallow in a few places — I extracted each interface's *own* fields but not always its full `extends` chain, and the conformance suite (actually run against a live instance of this bridge, not just read) caught the gaps. Recorded here since they weren't derivable from the M0 fetches alone.

1. **Every `Result` requires `resultType`.** `schema.ts`: `interface Result { _meta?: ResultMetaObject; resultType: ResultType; [key: string]: unknown; }`. `resultType: "complete"` for a normal successful result (not just `"input_required"` on the interrupted path noted in §1.1). Missed in M0 because §1.5's `CallToolResult` excerpt didn't surface the base interface.
2. **`CacheableResult` requires `ttlMs: number` and `cacheScope: "public" | "private"`, both mandatory.** `ListToolsResult extends PaginatedResult, CacheableResult` and `DiscoverResult extends CacheableResult` — so both `tools/list` and `server/discover` responses need these fields. This bridge's cache-policy values (`discover.py`): `server/discover` → `cacheScope: "public"`, `ttlMs: 300_000`; `tools/list` → `cacheScope: "public"`, `ttlMs: 60_000` (shorter, since it depends on live Agent Cards an operator could change). These specific numbers are this bridge's own judgment call, not a spec requirement — a config knob could expose them later if needed.
3. **`-32602 Invalid params` takes priority over `-32020 HeaderMismatch` for body-structural incompleteness.** If `params._meta` is missing entirely, or is present but missing `io.modelcontextprotocol/protocolVersion` or `io.modelcontextprotocol/clientCapabilities`, the correct response is `-32602` + HTTP 400 — **not** `-32020`. `-32020` is reserved for the case where the body value *is* present but disagrees with (or is contradicted by the absence of) the header. This ordering isn't written down anywhere in schema.ts or the transport prose fetched in M0 — it only surfaced by running `modelcontextprotocol/conformance`'s `server-stateless` scenario (SEP-2575) against a live instance of `server.py` and reading its exact expected-vs-actual diffs. **`io.modelcontextprotocol/clientCapabilities` being a hard requirement in `_meta` (not just documented as present, per §1.4) is confirmed the same way** — a request missing it is rejected, not defaulted.
4. **`UnsupportedProtocolVersionError.error.data` has a required shape**: `{"supported": string[], "requested": string}` — `supported` echoes the server's supported-versions list (same list as `DiscoverResult.supportedVersions`), `requested` echoes back the version the client asked for. Confirmed by reading the conformance suite's own scenario source (`src/scenarios/server/stateless.ts`), not just its check output — the PR/blog/schema sources from M0 never mentioned this error carries `data` at all.
5. **Unknown or removed methods (`initialize`, `ping`, `logging/setLevel`, `resources/subscribe`, `resources/unsubscribe`, or any other unrecognized method) are HTTP `404 Not Found`, carrying `-32601 Method not found` in the body — not HTTP 200.** M1's first draft assumed 200-with-JSON-RPC-error "by analogy" with generic JSON-RPC-over-HTTP convention; that assumption was wrong, and the conformance suite is what caught it. HeaderMismatch/UnsupportedProtocolVersion/InvalidParams-on-malformed-`_meta` stay 400 as before (SEP-2575 groups all of these as "reject before dispatch," but method-not-found specifically gets 404, not 400 — a 2026-07-28-specific behavior tied to `initialize`/`ping`/etc. being *removed* endpoints, not merely unrecognized ones).
6. **`-32021 MissingRequiredClientCapabilityError`** exists (`error.data.requiredCapabilities` is a `ClientCapabilities`-shaped object keyed by capability name, e.g. `{"sampling": {}}`, not an array) for when a request needs a capability (e.g. `sampling`, or plausibly `io.modelcontextprotocol/tasks` itself) the client didn't declare. **Not implemented in M1** — the conformance suite's own check for this is gated on the server exposing a diagnostic-only tool (`test_missing_capability`) that exists purely to be conformance-testable and has no equivalent in this bridge's real functionality; recorded here as a fact for the future (e.g. if M2/M5 needs to reject a `tools/call` because a client didn't declare `io.modelcontextprotocol/tasks`), not as an M1 gap.
7. Two more `server-stateless` checks are gated the same way on other diagnostic-only fixture tools (`test_streaming_elicitation`, `test_logging_tool`) this bridge has no reason to implement — reported by the suite as "not testable," not failures, and not acted on.

**Net M1 conformance result, run 2026-08-11 against a live local instance:** `http-header-validation` 13/14 (the one gap needs `tools/call`, out of scope until M2), `tools-list` 3/3, `server-stateless` 20/24 (all 4 remaining gaps are diagnostic-tool-gated, not real behavior gaps). `http-custom-header-server-validation` scenario is entirely inapplicable (`x-mcp-header`-annotated tools; this bridge doesn't use that MCP feature). `server/discover` still has no dedicated scenario in the suite as run (confirms §9 Unresolved #2 — genuinely absent, not a search-miss).

---

## 13. M2 findings

### 13.1 SDK API surface used (not previously recorded — discovered by inspecting the installed `a2a-sdk` directly, faster and just as authoritative as re-fetching GitHub for code already pinned in the venv)

- `a2a.client.card_resolver.A2ACardResolver(httpx_client, base_url, agent_card_path=AGENT_CARD_WELL_KNOWN_PATH).get_agent_card(http_kwargs=...)` — confirmed exact signature via `inspect.signature`.
- Task RPCs go through `a2a.client.transports.jsonrpc.JsonRpcTransport(httpx_client, agent_card, url)` directly, **not** the higher-level `ClientFactory`/`Client` abstraction — that wraps every call (streaming or not) in `AsyncIterator[StreamResponse]`, unneeded complexity here since this bridge always sends non-streaming (`return_immediately: true`). `JsonRpcTransport.send_message/get_task/cancel_task` return the SDK's real proto types directly (`SendMessageResponse`, `Task`).
- Per-call headers (including `A2A-Version`) go through `ClientCallContext(service_parameters={...})` — `service_parameters` is a plain `dict[str, str]`, merged into `httpx`'s request headers by `get_http_args()`. Confirmed by reading `a2a/client/transports/http_helpers.py` directly, not guessed.
- **Wire shapes captured empirically**, not derived from prose: mounted a recording `httpx.AsyncBaseTransport` and inspected the actual JSON `JsonRpcTransport` sends. `SendMessage` params: `{message: {messageId, role, parts: [{text}], taskId?, contextId?}, configuration: {returnImmediately}}` (camelCase, protobuf JSON mapping). `GetTask`/`CancelTask` params: `{id}` — **not** `{taskId}`, despite the MCP-side Tasks extension using `taskId` for the analogous field; another instance of the two protocols' method namespaces needing to stay lexically separate in code (build brief anti-goal).
- `SendMessageResponse` and `StreamResponse` are both `oneof` types (`payload`/`content` resp.) — check via `.WhichOneof("payload")` (`"task"` or `"message"`); A2A permits an agent to answer a `SendMessage` immediately with a bare `Message` and no `Task` at all. `tasks.py`'s `tools_call` handles both branches (see §13.3).
- `AgentInterface.protocol_binding` is a **plain string field** ("JSONRPC"/"GRPC"/"HTTP+JSON"), not a generated enum — confirmed via the descriptor (`type: TYPE_STRING`), despite reading as enum-shaped in the prose spec.
- `Part` is a `oneof` (`content`: `text`/`raw`/`url`/`data`) plus non-oneof `metadata`/`filename`/`media_type`. `artifacts.py` switches on `part.WhichOneof("content")`.
- `TaskStatus.timestamp` is a `google.protobuf.Timestamp` (`HasField("timestamp")` to check presence, `.ToJsonString()` for MCP's ISO8601 `lastUpdatedAt`) — A2A's `Task` message has **no task-creation timestamp anywhere**, which is what forced the `iat`-dropping decision in §12a.
- Agent-side JSON-RPC failures surface as `a2a.client.errors.A2AClientError` (or a more specific subclass, `A2AClientError`'s own fallback for unrecognized codes) — `tasks.py` catches this class specifically to wrap A2A failures as `DownstreamAgentError`, rather than swallowing a bare `Exception`.

### 13.2 taskId is immutable for a task's whole lifecycle — this killed the `step` field

Verified directly (not assumed) by reading `ext-tasks/schema/draft/schema.ts`: `UpdateTaskResult = Result` and `CancelTaskResult = Result` — both bare acknowledgements, neither echoes `taskId` or any `Task` data. There is no protocol mechanism for the server to hand the client a new/updated handle mid-lifecycle. Consequence recorded in §12a: the `step` field from the brief's original sketch is dead weight in the sealed-`taskId` design and was dropped before `state.py` was written, not after.

### 13.3 `tools/call` must handle two response shapes A2A legitimately produces, both flagged in advance by the user and both implemented:

1. **Immediate `TASK_STATE_INPUT_REQUIRED` on the very first `SendMessage` response.** With `return_immediately: true`, A2A does not guarantee the first status is `WORKING` — the agent may resolve to input-required before the bridge ever gets a chance to poll. `tasks.py::tools_call` builds the `CreateTaskResult` from whatever status the first response actually carries, rather than assuming `working`. Verified with a scriptable fake agent (`fake.initial_state = "TASK_STATE_INPUT_REQUIRED"`), not just asserted.
2. **`SendMessage` answering with a bare `Message`, no `Task` at all.** A2A permits this for skills that don't need async handling. `tools_call` returns a plain (non-Tasks-extension) `CallToolResult`-shaped result in this case — `{resultType: "complete", content: [...]}}` — and skips the Tasks-capability check entirely, since no task was ever created for the client to need permission for.

### 13.4 `-32021 MissingRequiredClientCapabilityError` is real, required behavior — not diagnostic-only after all

M1 (§11.6) recorded this as gated behind a conformance-only diagnostic tool and out of scope. Wrong to leave unimplemented: the Tasks extension spec itself (`ext-tasks/specification/draft/tasks.md`) requires it — "Never return a task to a client that did not declare support." `tools_call` checks `params._meta["io.modelcontextprotocol/clientCapabilities"].extensions["io.modelcontextprotocol/tasks"]` before returning any `CreateTaskResult`, and rejects with `-32021` + `error.data.requiredCapabilities = {"io.modelcontextprotocol/tasks": {}}` + HTTP 400 if absent.

**Real bug this caught in its own first draft, via the test suite (not conformance this time — my own integration test)**: because A2A's `return_immediately: true` already creates the task agent-side before the bridge can know whether the client is even allowed to have a handle for it (there's no way to ask A2A "would you create a task for this" without actually sending it), the naive implementation left the task running, orphaned, with no handle ever handed out. Fixed to best-effort `cancel_task` the orphan before returning the capability error. `tests/test_tasks.py::test_tools_call_without_declaring_tasks_extension_is_rejected` asserts the agent-side task ends up `TASK_STATE_CANCELED`, not merely absent.

### 13.5 messageId idempotency — the trade-off, verified against a live fake agent

Per the user's explicit direction: `SendMessage` MAY dedupe on `messageId` (A2A spec), so `messageId` must be **derived deterministically from the call**, not a fresh UUID per attempt, or an MCP-layer HTTP retry spawns a duplicate A2A task. Implemented as `a2a_client.stable_message_id(*parts)` — SHA-256 over the call's identifying parts (agent_id, skill_id, message text for a new call; agent_id, a2a_task_id, message text for a `tasks/update` continuation), truncated to 32 hex chars. `tests/test_tasks.py::test_retrying_tools_call_with_same_arguments_does_not_duplicate_the_a2a_task` confirms two identical calls produce the same `messageId`.

**Named trade-off** (per the same discipline applied to the envelope in §12d): this also dedupes two *genuinely separate* calls that happen to carry identical content (same skill, same text) into one A2A message, since there's no client-supplied nonce available at the bridge layer to distinguish "the same call, retried" from "a deliberate exact repeat." Accepted for retry-safety; documented in `a2a_client.py`'s docstring, not hidden.

### 13.6 Conformance suite re-run against a live instance with a working fake A2A agent (2026-08-11)

- `http-header-validation`: still 13/14. The one remaining failure (`ServerAcceptsWhitespaceHeaderValue`) is **not a real gap**: verified via `--verbose` that the request passes header validation cleanly (proving Starlette/httptools already strips OWS from header values before this bridge's code ever sees them — no whitespace-handling bug exists) and reaches dispatch; it then correctly receives `-32021` because the scenario's generic conformance-client fixture doesn't declare the `io.modelcontextprotocol/tasks` extension, and this bridge's `tools/call` always attempts to open a task (real A2A skills are inherently asynchronous by design — see §3.1). The check only accepts a bare success response as proof of whitespace-handling; there is no way to make it pass without violating the Tasks extension's own "never return a task to a client that didn't declare support" rule. Not fixable without a spec violation; recorded, not chased further.
- `tasks-capability-negotiation`: requires the server to expose specific hardcoded diagnostic fixture tools (`greet`, `slow_compute`) that the conformance harness calls by literal name. This bridge's tools are dynamically projected from configured A2A agents' skills, not hardcoded — same category of out-of-scope diagnostic-tool-gated scenario as M1's §11.6/§11.7 findings, not a real gap.

---

## 14. M3 findings — schema derivation and `AUTH_REQUIRED`, redefined per user direction 2026-08-11

M3 as originally scoped (build brief) largely already existed after M2 (`tasks/update` with `inputResponses`, tested through a full round trip). The user redefined M3 around two things the original scoping got wrong or left vague: prose-to-schema derivation for `input_required`, and `AUTH_REQUIRED` handling, which the original brief's "InputRequiredResult carrying the auth challenge" phrasing would have made into a credential-handling security bug if implemented literally.

### 14.1 `ElicitRequestURLParams` — merged schema vs. SEP-1036 proposal text disagree; merged schema wins (amendment 4)

Fetched both. **Merged `schema.ts`** (`raw.githubusercontent.com/modelcontextprotocol/specification/main/schema/2026-07-28/schema.ts`, same commit family as earlier fetches):
```typescript
export interface ElicitRequestURLParams {
  mode: "url";
  message: string;
  url: string;
}
```
**SEP-1036 proposal text** (`modelcontextprotocol/modelcontextprotocol/seps/1036-url-mode-elicitation-for-secure-out-of-band-intera.md`) additionally described an `elicitationId: string` field and a server-initiated `notifications/elicitation/complete` notification carrying it. **Neither `elicitationId` nor the completion notification made it into the merged schema.** Per amendment 4 (SEP PRs describe proposals that get amended before merge; take field names from the merged schema, not the PR/SEP text), this bridge uses the three-field shape above, with no `elicitationId` and no reliance on a completion notification ever arriving. Confirmed also from `ElicitResult` staying unchanged: `{action: "accept"|"decline"|"cancel", content?: {...}}`, `content` omitted for url-mode responses per its own doc comment ("Omitted for out-of-band mode responses").

The SEP's own rationale (fetched for context, not for field names): url-mode elicitation exists specifically so that "API keys, passwords, and other credentials must never transit through intermediary systems" — for OAuth-style external-authorization flows and PCI-relevant payment flows. It does **not** mention A2A's `AUTH_REQUIRED` state at all; the connection between the two is this project's own, not a documented cross-reference. Worth stating plainly since it means the design decision in §14.2 below is this bridge's synthesis of two independent specs' security postures, not something either spec spells out jointly.

### 14.2 `TASK_STATE_AUTH_REQUIRED` ≠ `INPUT_REQUIRED` with a form — corrects the original build brief

The original brief (§3.1's table) said: `TASK_STATE_AUTH_REQUIRED` → interrupted → "`InputRequiredResult` carrying the auth challenge." Read literally today, against the design this bridge actually settled on (Tasks-extension `inputRequests`/`tasks/update`, not core `InputRequiredResult` — see §3.1 above), this would mean: surface a form-mode elicitation asking for a credential, and forward the client's answer into the A2A task via `tasks/update` → `SendMessage`, exactly like a normal `input_required` answer. **That is a real security bug, flagged by the user before it was built, not caught after the fact:**

- A2A's own spec requires credentials to be exchanged out-of-band unless an in-band mechanism was explicitly negotiated, and states the agent **may resume on its own once the credential arrives, with no follow-up message required**.
- Anything placed in MCP's `inputResponses` travels through the calling model's context — transcripts, logs, anywhere else context goes. A credential routed that way is exposed to every layer of that pipeline, which is precisely what A2A's out-of-band requirement and MCP's own SEP-1036 (§14.1) both exist to prevent.
- A2A's spec text also warns explicitly that in-band credential exchange exposes credentials to every agent in a chain (already noted in the original build brief's own §3.1 bullet on `AUTH_REQUIRED` chains) — the same underlying concern from a different angle.

**Resolution, implemented in `mapping.py::auth_requests_for_task` and `tasks.py::tasks_update`:**
- `AUTH_REQUIRED` still collapses to MCP status `"input_required"` (MCP's `TaskStatus` enum has no separate value — confirmed, see §3), but is routed through a **separate key** (`AUTH_REQUEST_KEY = "auth"`, distinct from `INPUT_REQUEST_KEY = "response"`) and a **separate builder function**, decided from the *raw* A2A state (`a2a_task.status.state`), not the already-collapsed MCP status string — collapsing first and branching after would have silently reintroduced the conflation risk.
- If the agent's status message contains a `url`-kind `Part`, build a SEP-1036 url-mode elicitation (§14.1's three-field shape) — no `requestedSchema` at all, so there is structurally nothing for a model to populate.
- If there's no URL, fall back to a message-only elicitation with `requestedSchema: {type: "object", properties: {}, required: []}` — zero properties, so even in the worst case there is no fillable field, regardless of what the agent's prose says. Tested directly: `tests/test_mapping.py::test_auth_requests_never_produces_a_credential_shaped_field_even_if_agent_text_asks_for_one` constructs a status message that literally asks for a password and API key, and asserts the resulting schema still has empty `properties`. The guarantee is structural, not a keyword filter on the agent's text.
- `tasks/update` for the `auth` key **never calls `a2a_client.send_message`** — it validates `action == "accept"` and returns the bare ack. The client's role is only ever to consent/acknowledge; A2A's own resume-on-its-own semantics mean forwarding anything into the task would be both unnecessary and a channel this design is specifically trying to close. Tested end-to-end (`test_auth_required_full_flow_never_sends_anything_into_a2a`): agent posts a url-mode challenge, client "accepts," asserted zero `SendMessage` calls resulted, then the agent independently transitions to `WORKING`→`COMPLETED` (simulating out-of-band resumption) and the client reaches `completed` purely by polling.
- Answering an outstanding `AUTH_REQUIRED` with the *ordinary* `response` key (as if it were a normal question) is rejected (`MissingInputResponse`) rather than silently accepted — tested (`test_auth_required_answered_with_wrong_key_is_rejected`) — since accepting it would open exactly the credential-mislabeling risk this whole section exists to close.

CLAUDE.md's anti-goals list was updated with this rule directly (not just recorded here), since it's exactly the kind of thing a future edit relying on the original brief's literal wording could reintroduce.

### 14.3 Schema derivation for `input_required` — prefer a structured `DataPart`, else free text; never invent structure

A2A's `TaskStatus.message` is prose (a `Message` with `Part`s); MCP's `inputRequests` wants a JSON Schema. This is a genuine, unavoidable translation gap between the two protocols — there is no spec text on either side that resolves it, because it's not really a single-protocol question. Per user direction: derive from a structured `DataPart` when the agent provides one, otherwise fall back to a single free-text field, and never synthesize a schema the agent didn't actually signal.

Implementation (`mapping.py`): if any `Part` in the status message has its `data` field set (a `google.protobuf.Value` wrapping a `struct_value` — confirmed via `Part.DESCRIPTOR`, not assumed; see §13.1), decode it with `google.protobuf.json_format.MessageToDict` and build one JSON Schema property per key, typed from the concrete Python value (`bool`→`boolean`, `int`/`float`→`number`, `list`→`array`, `dict`→`object`, else `string`), all marked required. No field is invented that wasn't a key in the agent's own example — tested directly (`test_input_requests_for_task_does_not_synthesise_fields_not_present`). Absent a `DataPart`, falls back to the pre-M3 single `message: string` field.

**Reply shape must match request shape**, which forced a design change in `a2a_client.send_message` (now accepts `text` *or* `data`, mutually exclusive) and in `tasks.py::tasks_update` (re-fetches the task via `GetTask` before replying — needed anyway for the terminal-state check in §14.4 — and checks whether *that* status message has a `DataPart` to decide whether to reply with a structured `Part(data=...)` or a plain `Part(text=...)`). Wire shape for a structured `data` Part, captured empirically rather than assumed: `google.protobuf.Value`'s well-known-type JSON mapping collapses a `struct_value` directly to a plain JSON object under the `data` key — `{"data": {"city": "Paris", "confirm": true}}`, not `{"data": {"structValue": {...}}}`. Verified by round-tripping through the SDK's own `json_format.MessageToDict`/`ParseDict` before writing any fake-agent test fixtures by hand.

### 14.4 `tasks/update` against a terminal task — A2A's `UnsupportedOperationError` (code `-32004`) mapped, not leaked

Confirmed via the installed SDK (`a2a.utils.errors.JSON_RPC_ERROR_CODE_MAP`), not guessed: `UnsupportedOperationError` = A2A JSON-RPC code `-32004` (alongside `TaskNotFoundError`=`-32001`, `TaskNotCancelableError`=`-32002`, `PushNotificationNotSupportedError`=`-32003`, in a private A2A error-code space distinct from both the base JSON-RPC codes and MCP's own `-3202x` range — see §10's note on the two code spaces never being interchangeable).

Two layers, both tested independently against the fake agent:
1. **Preemptive**: `tasks_update` already fetches the current `Task` via `GetTask` (needed for §14.3's reply-shape decision); if its state is one of the four terminal A2A states, it raises a clean `TaskAlreadyTerminal` error *before* ever attempting `SendMessage` — `test_tasks_update_against_terminal_task_is_rejected_without_ever_calling_send_message` asserts zero `SendMessage` calls resulted.
2. **Defense-in-depth**: `send_message` is still wrapped to catch the SDK's `UnsupportedOperationError` specifically (not the generic `A2AClientError`) and translate it the same way, covering the race window between the `GetTask` fetch and the `SendMessage` call — `test_tasks_update_maps_a2a_unsupported_operation_error_without_leaking_it_raw` forces this path independent of the preemptive check (the fake agent is configured to reject `SendMessage` for a specific task id regardless of what `GetTask` reports) and asserts the raw A2A code never appears in the MCP-facing error message.

Both surface as MCP `-32602 Invalid params` with `data: {reason: "task_terminal", a2aState: "TASK_STATE_..."}`  — no MCP code maps naturally to "operation invalid given current server-side state" any better than the existing `expired`-handle precedent from M2 (§12c), so the same pattern (shared code family, distinguishing `data.reason`) was reused rather than inventing a new one.

A third related case, not explicitly requested but symmetric with the above: `tasks/update` against a task that's merely `WORKING`/`SUBMITTED` (nothing outstanding to answer) is rejected the same way, `data.reason: "not_awaiting_input"` — tested (`test_tasks_update_when_task_is_just_working_is_rejected`).

### 14.5 Confirmed: all five envelope-rejection tests from the original build brief §4 exist and pass

Re-verified by name, not just by count, since the design moved from a `requestState` field to a sealed `taskId` (§11/§12a) and it would have been easy to lose one silently in the rewrite: `test_rejects_bad_mac_wrong_key`, `test_rejects_bit_flipped_ciphertext`, `test_rejects_expired_envelope`, `test_rejects_unknown_envelope_version`, `test_rejects_unknown_agent_not_in_allowlist` — all five present in `tests/test_state_envelope.py`, all passing (confirmed 2026-08-11 alongside the full 101-test run).

### 14.6 Conformance regression check (2026-08-11, post-M3)

Re-ran the three scenarios M1/M2 had already characterized (`server-stateless`, `tools-list`, `http-header-validation`) against a live instance after the M3 changes: identical scores to the M2 baseline (20/24, 3/3, 13/14 respectively) — no regression. No M3-specific conformance scenario exists to check against (the `input-required-result-*` scenarios target the core, non-Tasks-extension `requestState` pattern this bridge deliberately doesn't use, per §3.1; the `tasks-*` scenarios remain gated on hardcoded diagnostic fixture tools per §13.6).

## 15. M4 — `Bridge-Instance-Id` is bridge-internal, not a protocol addition (2026-08-11)

Recorded here even though it introduces no new wire-protocol surface, precisely because CLAUDE.md's "no invented field names" anti-goal is about MCP/A2A fields specifically — worth being explicit that this isn't one, rather than leaving the distinction implicit.

`server.py` adds a `Bridge-Instance-Id` HTTP response header (`_InstanceIdMiddleware`), set from the `BRIDGE_INSTANCE_ID` env var, so the M4 demo and `tests/test_stateless.py` can observe from outside which process served a given request. Deliberately:

- **An HTTP header, never a JSON-RPC body or `_meta` field.** Neither MCP's `2026-07-28` schema nor A2A's proto has any concept of "which server process handled this" — putting it in the wire body would be exactly the kind of invented field name §0 exists to prevent. A header is additive and inert to any conformant client; nothing in `_dispatch`/`mapping.py`/`state.py` ever reads it back.
- **Never part of a `taskId` handle's contents.** `state.py`'s envelope (§12) carries `agent_id`/`a2a_task_id`/`a2a_context_id`/`exp` only; adding an instance id there would leak which process minted a handle into something the client holds, for zero protocol benefit, and would be a second place capacity has to be spent per §12a's byte-budget accounting.
- **Optional in production.** `app_from_env()` falls back to a random id when `BRIDGE_INSTANCE_ID` is unset (fine for a single-instance dev run); `demo/docker-compose.yml` sets it explicitly per container (`bridge1`/`bridge2`/`bridge3`) so the demo can identify instances by name.

No source fetch needed for this entry — it's an operational addition this bridge owns outright, not a spec detail.

---

