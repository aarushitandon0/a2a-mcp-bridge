"""Scripted M4 demo: brings the compose stack up, runs all three scenarios
(kill-one-mid-task, restart-all-three, key-rotation), tears it down, and
prints a transcript -- the same transcript that belongs in the README's
"scripted demo with captured output" section once this has actually been
run against a real Docker install.

Usage:
    python demo/run_demo.py

Requires Docker + Docker Compose. Not run in the environment this file was
authored in (no `docker` binary available there) -- see README.md's M4
section for that caveat before trusting the "Accept" checkbox on this
milestone.
"""

from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo import driver  # noqa: E402


def _fresh_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def main() -> None:
    if not driver.docker_compose_available():
        print("docker / docker compose not found on PATH -- cannot run the M4 demo.", file=sys.stderr)
        raise SystemExit(1)

    key_a = _fresh_key()
    key_b = _fresh_key()
    log = driver.ScenarioLog([])

    up_env = {**os.environ, "BRIDGE_STATE_KEY": key_a}
    log.add("=== docker compose up -d --build ===")
    driver.compose("up", "-d", "--build", env=up_env)
    try:
        driver.wait_for_nginx_ready()

        log.add("\n=== Scenario 1: kill the instance that served the original call ===")
        driver.scenario_kill_one_mid_task(log)

        log.add("\n=== Scenario 2: restart all three instances mid-task ===")
        driver.scenario_restart_all_three(log)

        log.add("\n=== Scenario 3: rotate BRIDGE_STATE_KEY across a two-key window ===")
        driver.scenario_key_rotation(key_a, key_b, log)

        log.add("\nAll M4 scenarios passed.")
    finally:
        log.add("\n=== docker compose down ===")
        driver.compose("down", "-v", check=False)

    print("\n--- transcript ---")
    print("\n".join(log.lines))


if __name__ == "__main__":
    main()
