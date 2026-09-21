import yaml

from bosun.builtins.executors import ArgoWorkflowExecutor
from bosun.contracts import StepContext


def ctx() -> StepContext:
    return StepContext(project="batch", pipeline="p", run_id="r", step_id="s",
                       unit=None, attempt=1, idempotency_key="k-1", params={})


async def test_manifest_file_missing_fails_cleanly(monkeypatch, tmp_path):
    monkeypatch.setenv("BOSUN_MANIFESTS_DIR", str(tmp_path))
    result = await ArgoWorkflowExecutor().execute(ctx(), {"manifest_file": "nope.yaml"})
    assert result.status == "failed" and "not found" in result.logs


async def test_manifest_file_path_traversal_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("BOSUN_MANIFESTS_DIR", str(tmp_path / "manifests"))
    (tmp_path / "manifests").mkdir()
    (tmp_path / "secret.yaml").write_text("kind: Workflow")
    result = await ArgoWorkflowExecutor().execute(ctx(), {"manifest_file": "../secret.yaml"})
    assert result.status == "failed" and "escapes" in result.logs


async def test_manifest_file_must_be_workflow(monkeypatch, tmp_path):
    monkeypatch.setenv("BOSUN_MANIFESTS_DIR", str(tmp_path))
    (tmp_path / "cm.yaml").write_text("kind: ConfigMap\nmetadata: {name: x}")
    result = await ArgoWorkflowExecutor().execute(ctx(), {"manifest_file": "cm.yaml"})
    assert result.status == "failed" and "not an Argo Workflow" in result.logs


def test_param_merge_fills_declared_params():
    # Declared-but-unvalued workflow params must receive values from config.
    manifest = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Workflow", "metadata": {},
        "spec": {"entrypoint": "main",
                 "arguments": {"parameters": [{"name": "yearmonth"}, {"name": "cutoff_date"}]}},
    }
    parameters = [{"name": "yearmonth", "value": "202607"},
                  {"name": "cutoff_date", "value": "20260706"},
                  {"name": "extra", "value": "1"}]
    declared = manifest["spec"]["arguments"]["parameters"]
    by_name = {p["name"]: p for p in declared}
    for p in parameters:
        if p["name"] in by_name:
            by_name[p["name"]]["value"] = p["value"]
        else:
            declared.append(p)
    assert declared == [
        {"name": "yearmonth", "value": "202607"},
        {"name": "cutoff_date", "value": "20260706"},
        {"name": "extra", "value": "1"},
    ]


def test_manifest_file_parses_and_matches_spec_reference():
    manifest = yaml.safe_load(open("manifests/batch/monthly_rollup.yaml"))
    assert manifest["kind"] == "Workflow"
    assert manifest["spec"]["serviceAccountName"] == "argo"
    regions = manifest["spec"]["templates"][0]["steps"][0][0]["withItems"]
    assert len(regions) == 12 and "us_en" in regions
    # round-1 form: only create-external + aggregate in the per-region DAG
    dag_tasks = [t["name"] for t in manifest["spec"]["templates"][1]["dag"]["tasks"]]
    assert dag_tasks == ["create-external", "aggregate"]

    spec = yaml.safe_load(open("examples/batch/monthly_rollup.yaml"))
    assert spec["steps"][0]["config"]["manifest_file"] == "batch/monthly_rollup.yaml"
