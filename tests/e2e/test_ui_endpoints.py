"""Endpoints added for the UI: component health, runs/incidents/knowledge
lists, spec content, UI static mount."""
from pathlib import Path

import yaml

from .conftest import approve_pending_gates, poll_run

ROOT = Path(__file__).resolve().parents[2]


def test_health_components(client):
    body = client.get("/health/components", timeout=30).json()
    components = body["components"]
    assert set(components) == {"postgres", "temporal", "worker", "relay", "qdrant", "ai_advisor"}
    assert components["postgres"]["ok"] is True
    assert components["temporal"]["ok"] is True
    assert components["worker"]["ok"] is True, components["worker"]  # poller registered
    assert components["qdrant"]["ok"] is True
    assert components["relay"]["ok"] is True


def test_lists_and_spec_content(client, project):
    key = {"X-API-Key": project["key"]}
    spec = yaml.safe_load((ROOT / "examples/hello_world/pipeline.yaml").read_text())
    spec["project"] = project["id"]
    client.post(f"/projects/{project['id']}/specs", json=spec, headers=key).raise_for_status()

    # spec content endpoint
    got = client.get(f"/projects/{project['id']}/specs/hello-world", headers=key).json()
    assert got["version"] == 1 and got["spec"]["steps"]

    # runs list
    run = client.post(f"/projects/{project['id']}/pipelines/hello-world/runs", json={}, headers=key).json()
    runs = client.get(f"/projects/{project['id']}/runs", headers=key).json()
    assert any(r["run_id"] == run["run_id"] for r in runs)

    # projects list never exposes keys
    projects = client.get("/projects").json()
    me = next(p for p in projects if p["id"] == project["id"])
    assert "api_key" not in me and "key" not in me

    # knowledge list
    client.post(f"/projects/{project['id']}/knowledge",
                json={"kind": "doc", "title": "t", "content": "c"}, headers=key).raise_for_status()
    records = client.get(f"/projects/{project['id']}/knowledge", headers=key).json()
    assert len(records) == 1 and records[0]["title"] == "t"

    # incidents list (empty is fine — endpoint exists and is scoped)
    assert client.get(f"/projects/{project['id']}/incidents", headers=key).status_code == 200

    # finish the run so the session teardown isn't racing a live workflow
    approve_pending_gates(client, project, run["run_id"], expect=1)
    poll_run(client, run["run_id"], project["key"])


def test_ui_served(client):
    resp = client.get("/ui/")
    assert resp.status_code == 200
    assert "Bosun" in resp.text or "root" in resp.text
    # root redirects to the app
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
