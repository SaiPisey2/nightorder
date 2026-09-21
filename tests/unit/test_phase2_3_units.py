from nightorder.ai.logreduce import reduce_log, score_line
from nightorder.sandbox import build_sandbox_job_manifest


def test_score_kubernetes_signals_rank_highest():
    assert score_line("Last State: Terminated Reason: OOMKilled") == 100
    assert score_line("Warning  Failed  ... ImagePullBackOff") == 100
    assert score_line("ERROR something broke") >= 50
    assert score_line("2026-07-06 INFO processed batch 42") == 0


def test_noise_downranked_but_errors_kept():
    assert score_line("GET /health 200 OK") == 0
    assert score_line("healthcheck ERROR connection refused") >= 50  # anomaly wins over noise


def test_reduce_keeps_anomaly_with_context_and_elides_noise():
    lines = [f"INFO routine line {i}" for i in range(200)]
    lines[100] = "Traceback (most recent call last):"
    lines[101] = '  File "app.py", line 3, in run'
    lines[102] = "MemoryError: java.lang.OutOfMemoryError"
    raw = "\n".join(lines)
    reduced = reduce_log(raw)
    assert "Traceback" in reduced and "OutOfMemoryError" in reduced
    assert "INFO routine line 99" in reduced  # context window
    assert "lines elided" in reduced
    assert len(reduced.splitlines()) < 40  # heavy reduction
    assert "INFO routine line 199" in reduced  # tail always kept


def test_reduce_empty():
    assert reduce_log("") == ""


def test_sandbox_manifest_hardening_complete():
    m = build_sandbox_job_manifest(name="nightorder-sbx-x", image="img:1", command=["sh", "-c", "true"],
                                   env={"PARAM_A": "1"}, namespace="ns", timeout_seconds=300)
    pod = m["spec"]["template"]["spec"]
    container = pod["containers"][0]
    # complete hardening set from the architecture spec
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert pod["automountServiceAccountToken"] is False
    assert pod["serviceAccountName"] == "nightorder-sandbox"
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["privileged"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["resources"]["limits"]["memory"]
    assert m["spec"]["activeDeadlineSeconds"] == 300
    assert m["spec"]["backoffLimit"] == 0
    assert container["env"] == [{"name": "PARAM_A", "value": "1"}]
    assert "hostPath" not in str(m)
