"""Argo executor against a real cluster. OPT-IN: set NIGHTORDER_E2E_ARGO=1.

Submits one tiny, clearly-labeled alpine workflow (10m CPU / 32Mi) to the
namespace in NIGHTORDER_ARGO_NAMESPACE using the kube context in
NIGHTORDER_KUBE_CONTEXT. The workflow self-cleans via ttlStrategy; the test also
deletes it explicitly on success.
"""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from .conftest import poll_run

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    os.environ.get("NIGHTORDER_E2E_ARGO") != "1",
    reason="cluster test is opt-in: NIGHTORDER_E2E_ARGO=1",
)


def test_argo_smoke_on_cluster(client, project, stack):
    key = {"X-API-Key": project["key"]}
    spec = yaml.safe_load((ROOT / "examples/rollup_mini/argo-smoke.yaml").read_text())
    spec["project"] = project["id"]

    client.post(f"/projects/{project['id']}/specs", json=spec, headers=key).raise_for_status()
    run = client.post(f"/projects/{project['id']}/pipelines/argo-smoke/runs", json={}, headers=key).json()
    final = poll_run(client, run["run_id"], project["key"], timeout=600)
    assert final["status"] == "completed", final

    step = next(s for s in final["steps"] if s["step_id"] == "argo-echo")
    assert step["status"] == "succeeded"
    assert step["external_ref"].startswith("nightorder-e2e/nightorder-"), step
    assert step["duration_seconds"] is not None

    # Explicit cleanup (ttlStrategy would do it anyway) — leave no trace.
    namespace, name = step["external_ref"].split("/", 1)
    kube_context = stack["env"].get("NIGHTORDER_KUBE_CONTEXT", "")
    if kube_context:
        subprocess.run(
            ["kubectl", "--context", kube_context, "-n", namespace, "delete",
             f"workflow.argoproj.io/{name}", "--ignore-not-found"],
            check=False, capture_output=True, timeout=60,
        )
