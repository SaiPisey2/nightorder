"""Knowledge view/delete + assistant answering 'what's the fix for X' from
the knowledge base."""
import os
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _seed(client, project, title, content, source=""):
    r = client.post(f"/projects/{project['id']}/knowledge",
                    json={"kind": "incident", "title": title, "content": content, "source": source},
                    headers={"X-API-Key": project["key"]})
    assert r.status_code == 201
    return r.json()["id"]


def _wait_indexed(client, project, query, record_id, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hits = client.get(f"/projects/{project['id']}/knowledge/search",
                          params={"q": query}, headers={"X-API-Key": project["key"]},
                          timeout=60).json()
        if any(h["id"] == record_id for h in hits):
            return
        time.sleep(3)
    raise TimeoutError("record never indexed")


def test_view_and_delete_knowledge(client, project):
    key = {"X-API-Key": project["key"]}
    rid = _seed(client, project, "Collector replicas crash on OOM",
                "Collector pods OOMKilled during SV collection. Fix: raise memory limit to 2Gi "
                "and restart the deployment.", source="INC-2048")
    _wait_indexed(client, project, "collector out of memory", rid)

    # full detail view
    detail = client.get(f"/projects/{project['id']}/knowledge/{rid}/detail", headers=key).json()
    assert detail["title"].startswith("Collector replicas")
    assert "2Gi" in detail["content"]
    assert detail["indexed"] is True

    # delete removes row AND embedding
    resp = client.delete(f"/projects/{project['id']}/knowledge/{rid}", headers=key).json()
    assert resp["deleted"] is True and resp["qdrant"] == "removed"
    assert client.get(f"/projects/{project['id']}/knowledge/{rid}/detail", headers=key).status_code == 404
    assert client.get(f"/projects/{project['id']}/knowledge", headers=key).json() == []
    hits = client.get(f"/projects/{project['id']}/knowledge/search",
                      params={"q": "collector out of memory"}, headers=key, timeout=60).json()
    assert not any(h["id"] == rid for h in hits), "embedding still retrievable after delete"


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY") and not (ROOT / ".env").exists(),
                    reason="needs ANTHROPIC_API_KEY")
def test_assistant_answers_fix_from_knowledge(client, project):
    key = {"X-API-Key": project["key"]}
    rid = _seed(client, project,
                "PVC undersized causes silent serp_get_results corruption",
                "When serp_get_results runs with an undersized PVC the pods hit 'No space left "
                "on device' and produce corrupted datasets silently. Fix: size the PVC from the "
                "keyword count (~4M keywords needs 1600Gi), then re-run the affected locale.",
                source="INC-1024")
    _wait_indexed(client, project, "no space left on device datasets", rid)

    resp = client.post(f"/projects/{project['id']}/chat",
                       json={"messages": [{"role": "user",
                             "content": "If serp results start producing weird datasets and pods "
                                        "log 'no space left on device', what's the solution?"}]},
                       headers=key, timeout=300)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert "search_knowledge" in [t["tool"] for t in result["tool_trace"]], result["tool_trace"]
    reply = result["reply"].lower()
    assert "pvc" in reply and ("1600" in reply or "resize" in reply or "size" in reply), result["reply"]
