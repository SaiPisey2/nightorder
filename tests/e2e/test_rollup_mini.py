"""rollup-mini: rollup-shaped dummy pipeline — cost gate, locale fan-out with tiers
and shared quota, designed re-run until queue drained, QA sign-off, manual
step, all through the generic interpreter."""
from pathlib import Path

import yaml

from .conftest import approve_pending_gates, poll_run

ROOT = Path(__file__).resolve().parents[2]


def test_rollup_mini_end_to_end(client, project):
    key = {"X-API-Key": project["key"]}
    spec = yaml.safe_load((ROOT / "examples/rollup_mini/pipeline.yaml").read_text())
    spec["project"] = project["id"]

    client.post(f"/projects/{project['id']}/specs", json=spec, headers=key).raise_for_status()
    run = client.post(f"/projects/{project['id']}/pipelines/rollup-mini/runs", json={}, headers=key).json()
    run_id = run["run_id"]

    # three human interactions: cost approval, QA sign-off, manual count-sheet step
    approve_pending_gates(client, project, run_id, expect=3, timeout=240)
    final = poll_run(client, run_id, project["key"], timeout=240)
    assert final["status"] == "completed", final

    # designed re-run: rerun-until-drained needed 2 iterations (4 fan-out lines + 2 drain passes)
    drains = [s for s in final["steps"] if s["step_id"] == "rerun-until-drained"]
    attempts = {s["attempt"] for s in drains}
    assert 2 in attempts, f"expected a designed re-run (attempt 2), got {drains}"

    # fan-out over 4 locales
    locales = {s["unit"] for s in final["steps"] if s["step_id"] == "sv-collection" and s["unit"]}
    assert locales == {"us_en", "uk_en", "de_de", "fr_fr"}

    # computed params recorded with provenance
    params = {p["name"]: p for p in final["parameters"]}
    assert params["serp_cost_usd"]["value"] == "31000"
    assert params["yearmonth"]["value"].isdigit()
    assert "now_yearmonth" in params["yearmonth"]["provenance"]

    # gate audit: three approvals by e2e-test
    gates = final["gates"]
    assert len(gates) == 3
    assert {g["type"] for g in gates} == {"budget_approval", "sign_off", "manual_step"}
    assert all(g["status"] == "approved" and g["decided_by"] == "e2e-test" for g in gates)

    # event stream includes rerun + gate family
    events = [e["type"] for e in client.get(f"/runs/{run_id}/events", headers=key).json()]
    assert "StepRerunTriggered" in events
    assert events.count("GateRequested") == 3 and events.count("GateGranted") == 3


def test_preflight_blocks_and_gate_reject_fails_run(client, project):
    key = {"X-API-Key": project["key"]}
    spec = {
        "apiVersion": "nightorder/v1", "kind": "Pipeline", "name": "guardrails", "project": project["id"],
        "steps": [
            {"id": "blocked", "executor": "noop",
             "preflight": [{"check": "always_fail", "params": {"reason": "feed file missing"}, "on_fail": "block"}]},
            {"id": "downstream", "executor": "noop", "depends_on": ["blocked"]},
        ],
    }
    client.post(f"/projects/{project['id']}/specs", json=spec, headers=key).raise_for_status()
    run = client.post(f"/projects/{project['id']}/pipelines/guardrails/runs", json={}, headers=key).json()
    final = poll_run(client, run["run_id"], project["key"])
    assert final["status"] == "failed"
    steps = {s["step_id"]: s for s in final["steps"]}
    assert steps["blocked"]["status"] == "failed"
    assert "feed file missing" in steps["blocked"]["error"]
    assert steps["downstream"]["status"] == "skipped"  # never silently proceeds
    events = [e["type"] for e in client.get(f"/runs/{run['run_id']}/events", headers=key).json()]
    assert "PreflightCheckFailed" in events and "StepSkipped" in events


def test_run_param_override_provenance(client, project):
    key = {"X-API-Key": project["key"]}
    spec = {
        "apiVersion": "nightorder/v1", "kind": "Pipeline", "name": "override", "project": project["id"],
        "parameters": [{"name": "target", "resolver": "static", "value": "default"}],
        "steps": [{"id": "s", "executor": "script", "config": {"command": ["echo", "{{params.target}}"]}}],
    }
    client.post(f"/projects/{project['id']}/specs", json=spec, headers=key).raise_for_status()
    run = client.post(f"/projects/{project['id']}/pipelines/override/runs",
                      json={"params": {"target": "overridden"}}, headers=key).json()
    final = poll_run(client, run["run_id"], project["key"])
    assert final["status"] == "completed"
    params = {p["name"]: p for p in final["parameters"]}
    assert params["target"]["value"] == "overridden"
    assert "override" in params["target"]["provenance"]
