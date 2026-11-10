"""Run the three M4 scenarios without Docker.

Same scenarios, same driver code (`demo/driver.py`) as `demo/run_demo.py`, but
the topology is plain OS processes on localhost instead of containers:

    client -> round-robin proxy (:8080, no affinity) -> bridge1 (:9001)
                                                     -> bridge2 (:9002)
                                                     -> bridge3 (:9003)
              every bridge -> one fake A2A agent (:8001)

The proxy mirrors demo/nginx.conf: plain round robin, no stickiness, and it
moves on to the next backend only when a connection is refused (a killed
instance), like nginx's default `proxy_next_upstream error`.

`driver.compose(...)` is swapped for a process manager, so "docker kill",
"docker compose restart" and the rolling key rotation become real process
kills and restarts.

    python demo/run_local.py
"""

from __future__ import annotations

import base64
import itertools
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import Response  # noqa: E402
from starlette.routing import Route  # noqa: E402

from demo import driver  # noqa: E402

PORTS = {"bridge1": 9001, "bridge2": 9002, "bridge3": 9003}
AGENT_PORT = 8001
PROXY_PORT = 8080
AGENTS_CONFIG = f'[{{"agent_id":"docs","base_url":"http://127.0.0.1:{AGENT_PORT}"}}]'

_procs: dict[str, subprocess.Popen] = {}
_env: dict[str, str] = dict(os.environ)


def _start_bridge(name: str) -> None:
    env = {**_env, "BRIDGE_INSTANCE_ID": name, "BRIDGE_AGENTS_CONFIG": AGENTS_CONFIG}
    _procs[name] = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "bridge.server:app_from_env", "--factory",
         "--app-dir", "src", "--port", str(PORTS[name]), "--log-level", "warning"],
        cwd=REPO, env=env,
    )


def _kill(name: str) -> None:
    proc = _procs.pop(name, None)
    if proc:
        proc.kill()
        proc.wait()


def _wait_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/", timeout=0.3)
            return
        except httpx.TransportError:
            time.sleep(0.1)
    raise TimeoutError(f"nothing listening on :{port}")


def _restart(name: str) -> None:
    _kill(name)
    _start_bridge(name)
    _wait_port(PORTS[name])


def fake_compose(*args: str, env: dict[str, str] | None = None, check: bool = True):
    """Stand-in for driver.compose: the same verbs, applied to processes."""
    global _env
    verb, rest = args[0], [a for a in args[1:] if not a.startswith("-")]
    if verb == "kill":
        for name in rest:
            _kill(name)
    elif verb == "restart":
        for name in rest:
            _kill(name)
        for name in rest:
            _start_bridge(name)
        for name in rest:
            _wait_port(PORTS[name])
    elif verb == "up":
        if env is not None:
            _env = dict(env)
        for name in rest:
            _restart(name)
    return subprocess.CompletedProcess(list(args), 0)


driver.compose = fake_compose

_rr = itertools.cycle(PORTS)


async def _proxy(request):
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    for _ in range(len(PORTS)):
        port = PORTS[next(_rr)]
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.post(f"http://127.0.0.1:{port}/", content=body, headers=headers)
        except httpx.ConnectError:
            continue
        out = {k: v for k, v in r.headers.items() if k.lower() not in ("content-length", "transfer-encoding")}
        return Response(r.content, status_code=r.status_code, headers=out)
    return Response(b"no live backend", status_code=502)


def main() -> int:
    global _env
    proxy = Starlette(routes=[Route("/", _proxy, methods=["POST"])])
    threading.Thread(target=lambda: uvicorn.run(proxy, port=PROXY_PORT, log_level="warning"), daemon=True).start()

    agent = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "demo.fake_agent.agent:app", "--factory",
         "--port", str(AGENT_PORT), "--log-level", "warning"],
        cwd=REPO, env={**os.environ, "FAKE_AGENT_PUBLIC_URL": f"http://127.0.0.1:{AGENT_PORT}"},
    )
    key_a = base64.urlsafe_b64encode(os.urandom(32)).decode()
    key_b = base64.urlsafe_b64encode(os.urandom(32)).decode()
    _env = {**os.environ, "BRIDGE_STATE_KEY": key_a, "BRIDGE_STATE_KEY_PREVIOUS": ""}
    failed = False
    try:
        _wait_port(AGENT_PORT)
        for name in PORTS:
            _start_bridge(name)
        for name in PORTS:
            _wait_port(PORTS[name])
        driver.wait_for_nginx_ready()
        scenarios = [
            ("Kill the serving instance mid-task", driver.scenario_kill_one_mid_task),
            ("Restart the whole pool while a task is paused", driver.scenario_restart_all_three),
            ("Rotate the seal key across the pool", lambda: driver.scenario_key_rotation(key_a, key_b)),
        ]
        for i, (title, fn) in enumerate(scenarios, 1):
            print(f"\n=== {i}. {title} ===")
            try:
                fn()
                print("PASS")
            except Exception as exc:  # noqa: BLE001 -- report every scenario
                failed = True
                print(f"FAIL: {exc!r}")
    finally:
        for name in list(_procs):
            _kill(name)
        agent.kill()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
