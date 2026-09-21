"""Framework contract test: the hello-world project runs end-to-end using
only the public spec schema, extension contracts, and API."""
from pathlib import Path

import yaml

from .conftest import approve_pending_gates, poll_run

ROOT = Path(__file__).resolve().parents[2]


def test_hello_world_end_to_end(client, project):
    key = {"X-API-Key": project["key"]}
    spec = yaml.safe_load((ROOT / "examples/hello_world/pipeline.yaml").read_text())
    spec["project"] = project["id"]

    # validate endpoint (lint before registration)
    v = client.post(f"/projects/{project['id']}/specs/validate", json=spec, headers=key)
    assert v.json() == {"valid": True, "errors": [], "pipeline": "hello-world"}

    # register + start
    r = client.post(f"/projects/{project['id']}/specs", json=spec, headers=key)
    assert r.status_code == 201 and r.json()["version"] == 1
    run = client.post(f"/projects/{project['id']}/pipelines/hello-world/runs", json={}, headers=key).json()
    run_id = run["run_id"]

    # one sign-off gate expected, then completion
    approve_pending_gates(client, project, run_id, expect=1)
    final = poll_run(client, run_id, project["key"])
    assert final["status"] == "completed", final

    steps = {s["step_id"]: s for s in final["steps"] if s["unit"] == ""}
    assert set(steps) == {"preflight-demo", "fan-out-regions", "sign-off", "release"}
    assert all(s["status"] == "succeeded" for s in steps.values())

    # fan-out produced per-unit rows with timings
    units = {s["unit"] for s in final["steps"] if s["step_id"] == "fan-out-regions" and s["unit"]}
    assert units == {"us", "eu", "apac"}
    for s in final["steps"]:
        if s["status"] == "succeeded" and s["step_id"] != "sign-off":
            assert s["duration_seconds"] is not None

    # parameters resolved with provenance (custom resolver + expression)
    params = {p["name"]: p for p in final["parameters"]}
    assert params["greeting"]["value"] == "hello-platform"
    assert "hello_greeting" in params["greeting"]["provenance"]
    assert params["banner"]["value"] == "hello-platform!"

    # event audit trail
    events = client.get(f"/runs/{run_id}/events", headers=key).json()
    types = [e["type"] for e in events]
    for expected in ("WorkflowStarted", "ParameterResolved", "PreflightCheckPassed",
                     "StepStarted", "GateRequested", "GateGranted", "StepCompleted",
                     "WorkflowCompleted"):
        assert expected in types, f"missing event {expected}"

    # timing-history product feature
    timings = client.get(f"/projects/{project['id']}/pipelines/hello-world/timings", headers=key).json()
    assert any(t["step_id"] == "fan-out-regions" and t["unit"] == "us" for t in timings)


def test_spec_versioning(client, project):
    key = {"X-API-Key": project["key"]}
    spec = yaml.safe_load((ROOT / "examples/hello_world/pipeline.yaml").read_text())
    spec["project"] = project["id"]
    assert client.post(f"/projects/{project['id']}/specs", json=spec, headers=key).json()["version"] == 1
    assert client.post(f"/projects/{project['id']}/specs", json=spec, headers=key).json()["version"] == 2


def test_open_platform_and_project_boundaries(client, project):
    # open platform: no API key needed to read or validate
    assert client.get(f"/projects/{project['id']}/specs").status_code == 200
    assert client.get(f"/projects/{project['id']}/runs").status_code == 200
    # projects remain distinct namespaces: unknown project → 404
    assert client.get("/projects/nope/specs").status_code == 404
    # everyone can see which projects exist (no keys exposed)
    ids = [p["id"] for p in client.get("/projects").json()]
    assert project["id"] in ids


def test_invalid_spec_rejected(client, project):
    key = {"X-API-Key": project["key"]}
    bad = {"apiVersion": "nightorder/v1", "kind": "Pipeline", "name": "bad", "project": project["id"],
           "steps": [{"id": "a", "executor": "no-such-backend", "depends_on": ["ghost"]}]}
    resp = client.post(f"/projects/{project['id']}/specs", json=bad, headers=key)
    assert resp.status_code == 422
    errors = " ".join(resp.json()["detail"]["errors"])
    assert "unregistered executor" in errors and "unknown step" in errors
