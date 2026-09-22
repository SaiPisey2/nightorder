"""Phase 2 + 2.5 e2e: knowledge seeding → failure → troubleshoot flow
(bundle → Qdrant retrieval → LangGraph RCA → remediation gate) → approval →
learning capture → retrieval-quality metric.

Needs Qdrant (compose) + ANTHROPIC_API_KEY (.env). First run downloads the
fastembed embedding model (~100MB) — be patient.
"""
import os
import time
from pathlib import Path

import pytest

from .conftest import poll_run

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY") and not (ROOT / ".env").exists(),
    reason="needs ANTHROPIC_API_KEY",
)

SEED_KNOWLEDGE = [
    {"kind": "incident",
     "title": "fetch_results fails silently when the volume is undersized",
     "content": "Collector pods die with 'No space left on device' when the PVC is "
                "sized below row volume (~4M rows needs 1600Gi). Failure is silent — "
                "produces large datasets. Fix: resize the volume to match row count, rerun locale.",
     "source": "INC-1024"},
    {"kind": "incident",
     "title": "Second metrics collection run required after RabbitMQ queue drain",
     "content": "google_ads_sv_collection queue must drain to zero before the second collection "
                "run. Rows missing in initial run are collected on rerun. Symptom: missing summary rows.",
     "source": "runbook"},
    {"kind": "runbook",
     "title": "loader_agent must run on the correct analytics cluster",
     "content": "Staging and production analytics clusters swap roles each release (primary and "
                "secondary). Verify cluster identity before steps 500/501 — "
                "loading into prod is a recurring hazard.",
     "source": "runbook"},
]


def _seed_and_wait_indexed(client, project) -> dict[str, str]:
    """Returns title→record_id. Waits until relay has indexed into Qdrant."""
    key = {"X-API-Key": project["key"]}
    ids = {}
    for doc in SEED_KNOWLEDGE:
        r = client.post(f"/projects/{project['id']}/knowledge", json=doc, headers=key)
        assert r.status_code == 201, r.text
        ids[doc["title"]] = r.json()["id"]
    deadline = time.monotonic() + 300  # first run: embedding model download
    while time.monotonic() < deadline:
        hits = client.get(f"/projects/{project['id']}/knowledge/search",
                          params={"q": "no space left on device PVC", "limit": 3},
                          headers=key, timeout=60).json()
        if hits:
            return ids
        time.sleep(3)
    raise TimeoutError("relay never indexed knowledge into Qdrant (see .e2e-logs/relay.log)")


def test_phase2_troubleshoot_and_phase25_learning(client, project):
    key = {"X-API-Key": project["key"]}
    record_ids = _seed_and_wait_indexed(client, project)

    # 1. a step that fails like the PVC incident
    spec = {
        "apiVersion": "nightorder/v1", "kind": "Pipeline", "name": "pvc-crash", "project": project["id"],
        "steps": [{"id": "fetch-results", "executor": "script",
                   "config": {"command": ["sh", "-c",
                              "echo 'writing dataset for locale us_en'; "
                              "echo 'OSError: [Errno 28] No space left on device'; exit 1"]},
                   "retry": {"maximum_attempts": 1}}],
    }
    client.post(f"/projects/{project['id']}/specs", json=spec, headers=key).raise_for_status()
    run = client.post(f"/projects/{project['id']}/pipelines/pvc-crash/runs", json={}, headers=key).json()
    final = poll_run(client, run["run_id"], project["key"])
    assert final["status"] == "failed"

    # 2. full troubleshooting flow (bundle → retrieval → LangGraph → gate)
    resp = client.post(f"/runs/{run['run_id']}/steps/fetch-results/troubleshoot",
                       json={}, headers=key, timeout=300)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["advisory_only"] is True
    assert result["rca"], "RCA missing"
    assert "space" in result["rca"].lower() or "disk" in result["rca"].lower() or "pvc" in result["rca"].lower()
    assert result["remediation_proposal"]
    # retrieval surfaced the seeded PVC incident
    titles = [h["title"] for h in result["similar_incidents"]]
    assert any("undersized" in t.lower() for t in titles), titles
    assert result["agents_visited"] == ["knowledge_retrieval", "troubleshooting",
                                        "remediation_planning", "human_interaction"]

    # incident persisted with bundle + reduced log + events
    incident = client.get(f"/incidents/{result['incident_id']}", headers=key).json()
    assert incident["status"] == "remediation_proposed"
    assert "No space left on device" in incident["reduced_log"]
    events = [e["type"] for e in client.get(f"/runs/{run['run_id']}/events", headers=key).json()]
    assert "IncidentBundleCreated" in events and "RemediationSuggested" in events

    # 3. approve the remediation gate (records approval; executes nothing)
    gate_id = result["gate_id"]
    r = client.post(f"/gates/{gate_id}/resolve",
                    json={"decision": "approve", "actor": "oncall@example.com"}, headers=key)
    assert r.status_code == 200
    incident = client.get(f"/incidents/{result['incident_id']}", headers=key).json()
    assert incident["status"] == "approved"
    events = [e["type"] for e in client.get(f"/runs/{run['run_id']}/events", headers=key).json()]
    assert "RemediationApproved" in events

    # 4. Phase 2.5 learning capture: outcome → knowledge record → indexed
    r = client.post(f"/incidents/{result['incident_id']}/outcome",
                    json={"outcome": "Resized PVC to 1600Gi for us_en and re-ran fetch_results; dataset normal.",
                          "success": True}, headers=key)
    assert r.status_code == 200
    fix_record_id = r.json()["knowledge_record_id"]
    assert fix_record_id

    deadline = time.monotonic() + 120
    fix_indexed = False
    while time.monotonic() < deadline and not fix_indexed:
        hits = client.get(f"/projects/{project['id']}/knowledge/search",
                          params={"q": "resized volume rerun collection results", "kind": "fix"},
                          headers=key, timeout=60).json()
        fix_indexed = any(h["id"] == fix_record_id for h in hits)
        time.sleep(3)
    assert fix_indexed, "learned fix never appeared in retrieval"

    # 5. Phase 2.5 retrieval-quality metric against ground truth
    import asyncio
    from nightorder.ai.evaluate import LabeledQuery, evaluate_retrieval

    labeled = [
        LabeledQuery("no space left on device dataset silently wrong",
                     record_ids["fetch_results fails silently when the volume is undersized"]),
        LabeledQuery("missing summary rows after first collection",
                     record_ids["Second metrics collection run required after RabbitMQ queue drain"]),
        LabeledQuery("loader agent wrong analytics cluster production hazard",
                     record_ids["loader_agent must run on the correct analytics cluster"]),
    ]
    report = asyncio.run(evaluate_retrieval(project["id"], labeled, k=3))
    assert report.hit_at_k >= 2 / 3, f"retrieval quality too low: {report}"


def test_agent_registry_populated(client, stack):
    agents = client.get("/agents").json()
    ids = {a["id"] for a in agents}
    assert {"supervisor", "knowledge_retrieval", "troubleshooting",
            "remediation_planning", "human_interaction", "learning"} <= ids
    planning = next(a for a in agents if a["id"] == "remediation_planning")
    assert planning["requires_approval"] is True
