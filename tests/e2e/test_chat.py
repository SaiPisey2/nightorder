"""Chat assistant e2e: real Claude with platform tools against a real run."""
import os
from pathlib import Path

import pytest

from .conftest import poll_run

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY") and not (ROOT / ".env").exists(),
    reason="needs ANTHROPIC_API_KEY",
)


def _chat(client, project, messages):
    resp = client.post(f"/projects/{project['id']}/chat",
                       json={"messages": messages}, headers={"X-API-Key": project["key"]},
                       timeout=300)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_chat_reports_run_status_with_tools(client, project):
    key = {"X-API-Key": project["key"]}
    spec = {
        "apiVersion": "nightorder/v1", "kind": "Pipeline", "name": "chatdemo", "project": project["id"],
        "steps": [
            {"id": "ok-step", "executor": "script", "config": {"command": ["echo", "fine"]}},
            {"id": "bad-step", "executor": "script", "depends_on": ["ok-step"],
             "config": {"command": ["sh", "-c", "echo 'ValueError: cutoff_date missing'; exit 1"]},
             "retry": {"maximum_attempts": 1}},
        ],
    }
    client.post(f"/projects/{project['id']}/specs", json=spec, headers=key).raise_for_status()
    run = client.post(f"/projects/{project['id']}/pipelines/chatdemo/runs", json={}, headers=key).json()
    poll_run(client, run["run_id"], project["key"])

    result = _chat(client, project, [{"role": "user", "content": "Why did the latest run fail? Be specific."}])
    tools_used = {t["tool"] for t in result["tool_trace"]}
    assert tools_used & {"list_runs", "get_run"}, f"assistant didn't inspect the platform: {tools_used}"
    reply = result["reply"].lower()
    assert "bad-step" in reply or "cutoff_date" in reply or "valueerror" in reply, result["reply"]


def test_chat_fixes_invalid_spec_yaml(client, project):
    broken = """apiVersion: nightorder/v1
kind: Pipeline
name: broken
project: {p}
steps:
  - id: a
    executor: script
    config: {{command: ["echo", "hi"]}}
  - id: b
    executor: script
    depends_on: [a, ghost-step]
    config: {{command: ["echo", "bye"]}}
""".format(p=project["id"])
    result = _chat(client, project, [
        {"role": "user", "content": "This spec fails validation, fix it and give me the corrected yaml:\n```yaml\n" + broken + "\n```"},
    ])
    tools_used = [t["tool"] for t in result["tool_trace"]]
    assert "validate_pipeline_spec" in tools_used, tools_used
    assert "```yaml" in result["reply"]
    assert "ghost-step" not in result["reply"].split("```yaml")[1].split("```")[0]


def test_chat_requires_user_last(client, project):
    resp = client.post(f"/projects/{project['id']}/chat",
                       json={"messages": [{"role": "assistant", "content": "hello"}]},
                       headers={"X-API-Key": project["key"]})
    assert resp.status_code == 422
