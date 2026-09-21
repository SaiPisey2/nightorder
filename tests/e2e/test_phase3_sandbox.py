"""Phase 3 e2e.

Local part (always runs with ANTHROPIC key): playbook registry, execution
guardrails — no execution without approved gate, no params outside allow-list.

Cluster part (opt-in NIGHTORDER_E2E_SANDBOX=1): approved playbook actually runs
in the hardened sandbox Job on the staging cluster's nightorder-e2e namespace.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest

from .conftest import poll_run

ROOT = Path(__file__).resolve().parents[2]
ALPINE = "alpine:latest"

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY") and not (ROOT / ".env").exists(),
    reason="needs ANTHROPIC_API_KEY (troubleshoot creates the incident)",
)


def _make_incident(client, project) -> dict:
    """Failed run + troubleshoot → incident with remediation gate."""
    key = {"X-API-Key": project["key"]}
    spec = {
        "apiVersion": "nightorder/v1", "kind": "Pipeline", "name": "crash3", "project": project["id"],
        "steps": [{"id": "boom", "executor": "script",
                   "config": {"command": ["sh", "-c", "echo 'FATAL: loading agent on wrong cluster'; exit 1"]},
                   "retry": {"maximum_attempts": 1}}],
    }
    client.post(f"/projects/{project['id']}/specs", json=spec, headers=key).raise_for_status()
    run = client.post(f"/projects/{project['id']}/pipelines/crash3/runs", json={}, headers=key).json()
    poll_run(client, run["run_id"], project["key"])
    resp = client.post(f"/runs/{run['run_id']}/steps/boom/troubleshoot", json={}, headers=key, timeout=300)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_playbook_registry_and_execution_guardrails(client, project):
    key = {"X-API-Key": project["key"]}
    # register playbook v1, then v2
    body = {"name": "restart-loading-agent", "description": "supervisorctl restart rollup_loading_agent",
            "image": ALPINE, "command": ["sh", "-c", "echo restarting on $PARAM_HOST && echo done"],
            "allowed_params": {"host": "target CH host"}, "approved_by": "platform-lead"}
    assert client.post(f"/projects/{project['id']}/playbooks", json=body, headers=key).json()["version"] == 1
    assert client.post(f"/projects/{project['id']}/playbooks", json=body, headers=key).json()["version"] == 2
    books = client.get(f"/projects/{project['id']}/playbooks", headers=key).json()
    assert any(b["name"] == "restart-loading-agent" and b["version"] == 2 for b in books)

    incident = _make_incident(client, project)

    # guardrail 1: cannot execute before the gate is approved
    r = client.post(f"/incidents/{incident['incident_id']}/execute",
                    json={"playbook": "restart-loading-agent", "params": {"host": "ch1-s1r1"}},
                    headers=key)
    assert r.status_code == 409, r.text

    # approve gate
    client.post(f"/gates/{incident['gate_id']}/resolve",
                json={"decision": "approve", "actor": "oncall"}, headers=key).raise_for_status()

    # guardrail 2: params outside the allow-list rejected
    r = client.post(f"/incidents/{incident['incident_id']}/execute",
                    json={"playbook": "restart-loading-agent",
                          "params": {"host": "ch1", "evil": "rm -rf /"}}, headers=key)
    assert r.status_code == 422 and "evil" in r.text

    # guardrail 3: unknown playbook rejected
    r = client.post(f"/incidents/{incident['incident_id']}/execute",
                    json={"playbook": "not-registered"}, headers=key)
    assert r.status_code == 404


@pytest.mark.skipif(os.environ.get("NIGHTORDER_E2E_SANDBOX") != "1",
                    reason="cluster sandbox test is opt-in: NIGHTORDER_E2E_SANDBOX=1")
def test_sandbox_execution_on_cluster(client, project, stack):
    key = {"X-API-Key": project["key"]}
    kube_context = stack["env"].get("NIGHTORDER_KUBE_CONTEXT", "")
    # sandbox SA + default-deny NetworkPolicy into the e2e namespace (idempotent)
    subprocess.run(["kubectl", "--context", kube_context, "-n", "nightorder-e2e",
                    "apply", "-f", str(ROOT / "k8s/sandbox/sandbox-rbac.yaml")],
                   check=True, capture_output=True, timeout=60)

    client.post(f"/projects/{project['id']}/playbooks", json={
        "name": "echo-fix", "description": "harmless echo playbook",
        "image": ALPINE, "command": ["sh", "-c", "echo remediating $PARAM_TARGET && echo verification-ok"],
        "allowed_params": {"target": "what to fix"}, "approved_by": "platform-lead",
    }, headers=key).raise_for_status()

    incident = _make_incident(client, project)
    client.post(f"/gates/{incident['gate_id']}/resolve",
                json={"decision": "approve", "actor": "oncall"}, headers=key).raise_for_status()

    r = client.post(f"/incidents/{incident['incident_id']}/execute",
                    json={"playbook": "echo-fix", "params": {"target": "ch1-s1r1"},
                          "namespace": "nightorder-e2e", "timeout_seconds": 300}, headers=key)
    assert r.status_code == 202, r.text
    assert r.json()["playbook"] == "echo-fix@v1"

    deadline = time.monotonic() + 420
    status = ""
    while time.monotonic() < deadline:
        status = client.get(f"/incidents/{incident['incident_id']}", headers=key).json()["status"]
        if status in ("executed", "failed"):
            break
        time.sleep(5)
    incident_final = client.get(f"/incidents/{incident['incident_id']}", headers=key).json()
    assert status == "executed", incident_final
    assert "remediating ch1-s1r1" in incident_final["outcome"]

    events = [e["type"] for e in client.get(f"/runs/{incident_final['run_id']}/events", headers=key).json()]
    for expected in ("RemediationApproved", "SandboxJobStarted", "SandboxJobCompleted", "RemediationExecuted"):
        assert expected in events, f"missing {expected}"

    # cleanup sandbox job (ttl would handle it anyway)
    subprocess.run(["kubectl", "--context", kube_context, "-n", "nightorder-e2e",
                    "delete", "job", "-l", "nightorder/component=sandbox", "--ignore-not-found"],
                   check=False, capture_output=True, timeout=60)
