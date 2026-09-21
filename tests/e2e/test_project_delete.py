"""Project deletion: full cascade + confirm guard + workflow termination."""
import time


def test_delete_project_cascades(client, project):
    key = {"X-API-Key": project["key"]}
    pid = project["id"]

    # populate: spec, a running run (gated → stays running), knowledge, playbook
    spec = {
        "apiVersion": "nightorder/v1", "kind": "Pipeline", "name": "doomed", "project": pid,
        "steps": [{"id": "wait", "executor": "noop",
                   "gate": {"type": "sign_off", "prompt": "never resolved", "channel": "log",
                            "timeout_minutes": 600, "on_timeout": "reject"}}],
    }
    client.post(f"/projects/{pid}/specs", json=spec, headers=key).raise_for_status()
    run = client.post(f"/projects/{pid}/pipelines/doomed/runs", json={}, headers=key).json()
    client.post(f"/projects/{pid}/knowledge",
                json={"kind": "doc", "title": "t", "content": "c"}, headers=key).raise_for_status()
    client.post(f"/projects/{pid}/playbooks",
                json={"name": "pb", "image": "img:1", "command": ["true"], "approved_by": "x"},
                headers=key).raise_for_status()

    # wait until the run is live (gate pending)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if client.get(f"/projects/{pid}/gates", headers=key).json():
            break
        time.sleep(1)

    # guard: no confirm → 422
    assert client.delete(f"/projects/{pid}", headers=key).status_code == 422
    assert client.delete(f"/projects/{pid}?confirm=wrong", headers=key).status_code == 422

    # delete
    resp = client.delete(f"/projects/{pid}?confirm={pid}", headers=key)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == pid
    assert body["terminated_workflows"] >= 1  # the gated run was live

    # everything gone
    assert client.get(f"/projects/{pid}/specs", headers=key).status_code == 404
    assert client.get(f"/runs/{run['run_id']}", headers=key).status_code == 404
    assert pid not in [p["id"] for p in client.get("/projects").json()]
