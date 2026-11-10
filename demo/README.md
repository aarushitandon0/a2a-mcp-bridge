# M4 demo: no bridge-side state

Three bridge instances behind nginx round robin, one fake A2A agent in its
own container, sharing nothing across bridge instances but config, the seal
key, and the agent itself. See the top-level README's M4 section for the
claim this is meant to support, and why it's phrased "no bridge-side
state" rather than "stateless" -- the A2A agent still holds real task
state; this bridge doesn't hold any of its own.

**Status: verified.** All three scenarios below pass. They were run with
`demo/run_local.py`, which drives the same `driver.py` scenarios against real
processes (three bridges, one fake agent, a round-robin proxy with no
affinity) instead of containers. The recorded transcript is in the
top-level README's "Multi-instance deployment" section. The Docker path
(`run_demo.py`, and the three container-level tests in
`tests/test_stateless.py`) runs the same scenarios against containers; it
needs a Docker install that can pull `nginx:1.27-alpine`.

## Running it

Without Docker:

```
python demo/run_local.py
```

With Docker:

```
export BRIDGE_STATE_KEY="$(python -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
python demo/run_demo.py
```

This builds and starts the stack (`docker compose up -d --build`), runs all
three scenarios below in sequence against it, prints a transcript, and
tears the stack down (`docker compose down -v`) whether or not it
succeeded.

To run just the automated pytest version:

```
./.venv/Scripts/python -m pytest tests/test_stateless.py -v
```

## What each scenario proves

**Kill-one-mid-task.** `tools/call` opens a task through nginx; the fake
agent (via its `/control` endpoints -- see `demo/fake_agent/agent.py`'s
docstring for why those exist and aren't part of the real A2A/MCP wire
protocol) is told to put that task into `input_required`. The driver reads
which instance served the original call from the `Bridge-Instance-Id`
response header, `docker kill`s exactly that container, then answers the
prompt through nginx again. nginx can only route to a survivor, so the
resume necessarily lands on a different instance -- asserted directly by
comparing the header before and after, not inferred.

**Restart-all-three.** Stronger: while a task sits at `input_required`, all
three bridge containers are restarted (`docker compose restart`), wiping
whatever in-process memory any of them had, none of the original three
survives untouched. The prompt is then answered and the task still
completes -- proving the property holds even when literally nothing about
the original serving instance's process state persists anywhere, not just
that a *different* instance can pick it up.

**Key rotation.** A task handle is minted under `BRIDGE_STATE_KEY=KEY_A`.
Every bridge instance is then recreated one at a time with
`BRIDGE_STATE_KEY=KEY_B` and `BRIDGE_STATE_KEY_PREVIOUS=KEY_A` -- the
two-key window `state.py` exists to support (SPEC-NOTES.md §12b). The
KEY_A-sealed handle is answered *after* the rotation and still resumes
(unseal tries the current key, falls back to previous). A fresh
`tools/call` issued after rotation is then confirmed to still work,
proving new handles under KEY_B are minted correctly too.

## Harness constraints this demo depends on

- **The fake A2A agent runs in its own container**, with its own Dockerfile
  (`demo/Dockerfile.fake_agent`) and its own tiny dependency set (Starlette
  + uvicorn only). Killing or restarting a bridge container must never take
  it down -- if it did, every scenario above would "pass" for the wrong
  reason (nothing to resume against, rather than successful resumption).
- **nginx has no sticky-session configuration.** `demo/nginx.conf`'s
  upstream block is plain round robin with no `ip_hash`, no cookie
  affinity, and deliberately no `keepalive` directive (which would let
  nginx reuse one persistent connection to the same backend across
  requests) -- see the comments in that file for why each absence matters
  here specifically.
- **Bridge instances share nothing but config, the seal key, and the A2A
  agent.** No shared volume, no shared cache, no database -- check
  `demo/docker-compose.yml` before adding anything to make a future test
  pass; a shared store anywhere in that file would make the "no
  bridge-side state" claim false again.
