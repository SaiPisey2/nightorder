"""Signed single-use gate token path + AI advisor endpoint."""
import os
import time
from pathlib import Path

import pytest

from .conftest import poll_run

ROOT = Path(__file__).resolve().parents[2]


def _pending_gate(client, project, run_id, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        gates = client.get(f"/projects/{project['id']}/gates",
                           headers={"X-API-Key": project["key"]}).json()
        for g in gates:
            if g["run_id"] == run_id:
                return g
        time.sleep(1)
    raise TimeoutError("no pending gate appeared")


def test_gate_token_single_use(client, project, stack):
    key = {"X-API-Key": project["key"]}
    spec = {
        "apiVersion": "bosun/v1", "kind": "Pipeline", "name": "token-gate", "project": project["id"],
        "steps": [{"id": "gated", "executor": "noop",
                   "gate": {"type": "sign_off", "prompt": "approve?", "channel": "log",
                            "timeout_minutes": 30, "on_timeout": "reject"}}],
    }
    client.post(f"/projects/{project['id']}/specs", json=spec, headers=key).raise_for_status()
    run = client.post(f"/projects/{project['id']}/pipelines/token-gate/runs", json={}, headers=key).json()
    gate = _pending_gate(client, project, run["run_id"])

    # Mint a token exactly like the kernel does (same HMAC secret env).
    os.environ["BOSUN_GATE_TOKEN_SECRET"] = stack["env"].get("BOSUN_GATE_TOKEN_SECRET", "dev-only-secret-change-me")
    from bosun.api.security import mint_gate_token

    token = mint_gate_token(gate["gate_id"], "approve")
    first = client.get("/gate-action", params={"token": token})
    assert first.status_code == 200 and "approved" in first.text

    # Replay is rejected — single use.
    replay = client.get("/gate-action", params={"token": token})
    assert replay.status_code == 409

    # Tampered token rejected.
    assert client.get("/gate-action", params={"token": token[:-3] + "abc"}).status_code == 403

    final = poll_run(client, run["run_id"], project["key"])
    assert final["status"] == "completed"
    assert final["gates"][0]["decided_by"] == "token-link"


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY") and not (ROOT / ".env").exists(),
                    reason="no ANTHROPIC_API_KEY available")
def test_ai_advisor_diagnoses_failed_step(client, project):
    key = {"X-API-Key": project["key"]}
    spec = {
        "apiVersion": "bosun/v1", "kind": "Pipeline", "name": "crash", "project": project["id"],
        "steps": [{"id": "oom", "executor": "script",
                   "config": {"command": ["sh", "-c", "echo 'FATAL: java.lang.OutOfMemoryError: Java heap space'; exit 137"]},
                   "retry": {"maximum_attempts": 1}}],
    }
    client.post(f"/projects/{project['id']}/specs", json=spec, headers=key).raise_for_status()
    run = client.post(f"/projects/{project['id']}/pipelines/crash/runs", json={}, headers=key).json()
    final = poll_run(client, run["run_id"], project["key"])
    assert final["status"] == "failed"

    resp = client.post(f"/runs/{run['run_id']}/steps/oom/diagnose", headers=key, timeout=120)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["advisory_only"] is True
    assert "OutOfMemory" in body["analysis"] or "memory" in body["analysis"].lower()
