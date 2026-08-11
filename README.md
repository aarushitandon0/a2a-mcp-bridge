# a2a-mcp-bridge

A bridge exposing A2A remote agents to MCP clients, holding no bridge-side
state of its own, built native on the MCP `2026-07-28` specification. See
`SPEC-NOTES.md` for the protocol research this project is built on, and
`CLAUDE.md` for the working rules.

Phrased deliberately as "no bridge-side state," not "stateless": the A2A
agent on the other end of every call holds real task state throughout a
run. What this bridge doesn't hold is any of its own — every resume
operation is reconstructed entirely from a sealed `taskId` handle plus the
remote agent, never from anything only one bridge process remembers. See
M4 below.

**Status:** M0 (recon), M1 (stateless MCP skeleton), M2 (task handles), M3
(input-required schema derivation + auth-required handling), and M4
(no-bridge-side-state demo) complete. `tools/call` opens an A2A task and
returns a sealed handle; `tasks/get`/`tasks/update`/`tasks/cancel` drive it
to completion, including two genuinely distinct interrupted paths (a real
question vs an authentication challenge — see Security below), and can
resume on any instance sharing only config, the seal key, and the A2A agent
— see M4 below; **the M4 container demo itself was written but not run in
this environment (no Docker install available) — its automated pytest
coverage (`tests/test_stateless.py`) skips the three container-level tests
with an explicit reason rather than silently passing when Docker is
missing, and only its in-process test has actually been verified here.**
M5 (identity chain) not started.

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

M1, M2, and M3 were all checked against the real `modelcontextprotocol/conformance`
suite (`server-stateless`, `http-header-validation`, `tools-list`), not just
this project's own tests — see SPEC-NOTES.md §12 (M1), §13 (M2), and §14
(M3) for what those runs caught and corrected, and what they left out of
scope (diagnostic-only test tools this bridge has no reason to implement).

## A bug worth calling out: capability checks that run after their own side effect

`tools/call` must reject a request with `-32021 MissingRequiredClientCapabilityError`
when the client hasn't declared the `io.modelcontextprotocol/tasks` extension
— the Tasks extension spec is explicit that a server must never hand out a
task to a client that didn't opt in. The natural place to check that is
right before building the response.

The problem: A2A's `SendMessage` with `return_immediately: true` already
creates the task **agent-side** before the bridge can know whether the
client is even allowed to have a handle for it — there is no way to ask A2A
"would you open a task for this?" without actually sending the message.
A capability check placed after the call that triggers the very side effect
it's meant to gate doesn't prevent that side effect; it just decides,
too late, whether to tell the client about it. The first version of this
code checked the capability, raised cleanly, and left a task running on the
remote agent forever — reachable by no one, since the client was never
given its handle.

This wasn't caught by the conformance suite (which has no fixture for it);
it was caught by this project's own integration test
(`tests/test_tasks.py::test_tools_call_without_declaring_tasks_extension_is_rejected`),
written to assert the capability-rejection path, which failed with a
leftover task instead of a clean rejection. The fix: best-effort
`cancel_task` the orphan before raising the capability error
(`tasks.py::tools_call`). Worth generalizing beyond this one case: **any
check that gates a side effect which already happened by the time the check
runs isn't a gate — it's a cleanup obligation.** Look for this pattern
anywhere a bridge does "ask permission" and "cause an effect" in two steps
that can't be reordered.

## Security: `TASK_STATE_AUTH_REQUIRED` is not `input_required` with a password field

An earlier version of this project's design brief described handling A2A's
`AUTH_REQUIRED` state the same way as `INPUT_REQUIRED` — surface it as an
elicitation, let the client fill in a form, post the answer back. Taken
literally, that's a security bug: A2A requires credentials to be exchanged
out-of-band unless an in-band mechanism was explicitly negotiated, and the
agent may resume on its own once the credential arrives, with no follow-up
message needed. Anything placed in MCP's `inputResponses` passes through
the calling model's context — transcripts, logs, anywhere else that context
goes — so routing a password or API key through that path would leak it
into exactly the places A2A's own design is trying to keep it out of. The
A2A spec's own warning about in-band credential exchange propagating
through agent chains is the same concern from a different angle.

This bridge (`mapping.py::auth_requests_for_task`) treats `AUTH_REQUIRED` as
a genuinely separate path from `INPUT_REQUIRED`:

- If the agent supplies a URL (A2A's equivalent of MCP's own SEP-1036
  url-mode elicitation), it's surfaced for the human to open out-of-band —
  the elicitation carries no `requestedSchema` at all, so there is
  structurally nothing for a model to fill in.
- If there's no URL, the fallback is a message-only, **zero-property**
  schema — still nothing fillable, just an instruction to wait and poll.
- Either way, `tasks/update` for this path (`tasks.py::tasks_update`) never
  forwards anything into the A2A task. The client is only ever consenting
  or acknowledging; resumption is entirely the agent's own doing.

`tests/test_mapping.py::test_auth_requests_never_produces_a_credential_shaped_field_even_if_agent_text_asks_for_one`
asserts this structurally — even a status message that explicitly asks for
a password produces an empty schema, because the guarantee doesn't depend
on pattern-matching the agent's prose.
`tests/test_tasks.py::test_auth_required_full_flow_never_sends_anything_into_a2a`
covers the other half — not just that nothing crosses the boundary, but that
a task actually reaches `TASK_STATE_COMPLETED` this way: `AUTH_REQUIRED` →
acknowledge → agent flips state out-of-band → `tasks/get` observes
`WORKING` → `COMPLETED`, with zero `SendMessage` calls the entire time.
Resumption is proven to work, not just proven safe.

**Trade-off accepted, not fixed:** the A2A spec says agents *SHOULD* continue
accepting messages while a task is `AUTH_REQUIRED`, specifically so a client
can negotiate, correct, or reject the pending request in-band. This bridge's
blanket "never forward anything into the A2A task while `AUTH_REQUIRED`"
rule forecloses that — `tasks/update` on this path can only acknowledge, and
the sole way to reject the interruption from here is `tasks/cancel`. Given
the alternative is a path a model's context could use to smuggle a
credential, this bridge chooses the more restrictive behavior deliberately.
An agent expecting in-band negotiation during `AUTH_REQUIRED` will not get
it through this bridge.

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

## M4: no bridge-side state

The design claim through M1–M3 was that a sealed `taskId` handle carries
everything needed to resume a task, so no bridge instance needs to
remember anything about a call it didn't make. M4 is that claim tested
against real separate processes rather than asserted from the design.

**`tests/test_stateless.py::test_task_handle_resumes_across_independently_constructed_bridge_instances`**
runs with the normal `pytest` suite, no Docker required: two fully separate
`create_app()` Starlette apps — separate objects, separate httpx clients,
nothing shared except the state key and the fake A2A agent standing in for
the real remote agent — and a task opened on one resumes correctly on the
other. This is genuine evidence about the code path, verified in this
environment.

**The container-level claim — actual separate OS processes, `docker kill`,
a real nginx round robin — lives in `demo/`** (`docker-compose.yml`,
`nginx.conf`, `Dockerfile.bridge`, `Dockerfile.fake_agent`, `driver.py`,
`run_demo.py`; see `demo/README.md` for the full writeup). Three bridge
containers behind nginx, sharing nothing but `BRIDGE_AGENTS_CONFIG`,
`BRIDGE_STATE_KEY`, and the fake A2A agent (itself in its own container, so
killing a bridge instance can't accidentally take it down too). Three
scenarios, each exercised by both `demo/run_demo.py` (scripted, captured
output) and `tests/test_stateless.py` (automated, same driver code via
`demo/driver.py`):

- **Kill one mid-task** — start a task, let it reach `input_required`,
  `docker kill` the exact instance (identified via the `Bridge-Instance-Id`
  response header — a bridge-internal header, not part of the MCP or A2A
  wire protocol) that served the original call, answer the prompt through
  nginx, confirm it completes and that the instance that served the resume
  is provably not the one that was killed.
- **Restart all three** — the stronger variant: restart every bridge
  container while a task is paused, so none of the original three survives
  untouched, then confirm the task still resumes.
- **Key rotation** — mint a handle under `BRIDGE_STATE_KEY=KEY_A`, recreate
  every instance with `BRIDGE_STATE_KEY=KEY_B` /
  `BRIDGE_STATE_KEY_PREVIOUS=KEY_A` (the two-key window from the Design
  trade-offs section above), and confirm the KEY_A-sealed handle still
  resumes during that window, then that a fresh post-rotation handle also
  works.

**This part was not run in the environment it was written in** — no Docker
install was available there. `tests/test_stateless.py`'s three
container-level tests check `shutil.which("docker")` and skip with an
explicit reason (visible under `pytest -rs`) rather than silently passing
when it's missing; they are not marked complete by virtue of existing. Run
them (`./.venv/Scripts/python -m pytest tests/test_stateless.py -v`, or
`python demo/run_demo.py` for the scripted version) somewhere Docker is
installed before treating M4 as actually accepted, and paste the resulting
transcript here in place of this paragraph.

## Out of scope: artifact "fetch full detail"

`artifacts.py` summarises and truncates (see its docstring) but there is no
wired-up follow-up call to fetch a truncated or described-not-inlined
artifact's full content. When this lands — most naturally an MCP
`resources/read` URI per artifact — **the URI itself must be a sealed
handle of the same kind `taskId` is**, not a plain id backed by a lookup
table. A server-side map from URI to artifact content would reintroduce
exactly the kind of state M4 exists to prove this bridge doesn't have;
the whole point of the sealed-`taskId` design is that nothing about
resuming or fetching more detail depends on which instance answers.
