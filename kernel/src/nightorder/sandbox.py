"""Phase 3: hardened ephemeral Kubernetes Job sandbox.

The control plane never executes AI-generated shell commands. Only
pre-approved, versioned playbooks (immutable image + fixed command from the
playbook registry) run here; approved parameters arrive as env vars.

Hardening (complete set from the architecture spec):
non-root, read-only rootfs, no privilege escalation, drop ALL capabilities,
RuntimeDefault seccomp, cpu/mem requests+limits, activeDeadline timeout,
no service-account token automount. Pair with the default-deny NetworkPolicy
and dedicated ServiceAccount in k8s/sandbox/.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from nightorder.config import settings


def build_sandbox_job_manifest(*, name: str, image: str, command: list[str],
                               env: dict[str, str], namespace: str,
                               timeout_seconds: int = 600,
                               labels: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/managed-by": "nightorder",
                       "nightorder/component": "sandbox", **(labels or {})},
        },
        "spec": {
            "backoffLimit": 0,  # a remediation either works or a human looks
            "activeDeadlineSeconds": timeout_seconds,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": {"nightorder/component": "sandbox"}},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": "nightorder-sandbox",
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65534,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [{
                        "name": "playbook",
                        "image": image,  # immutable ref from the playbook registry
                        "command": command,  # fixed command — params only via env
                        "env": [{"name": k, "value": str(v)} for k, v in env.items()],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "privileged": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "64Mi"},
                            "limits": {"cpu": "500m", "memory": "256Mi"},
                        },
                        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                    }],
                    "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}}],
                },
            },
        },
    }


def _job_name(idempotency_key: str) -> str:
    return "nightorder-sbx-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:12]


def _run_sync(manifest: dict, namespace: str, timeout_seconds: int) -> dict:
    import time

    from kubernetes import client as k8s_client, config as k8s_config
    from kubernetes.client.rest import ApiException

    cfg = settings()
    try:
        k8s_config.load_kube_config(context=cfg.kube_context or None)
    except Exception:
        k8s_config.load_incluster_config()
    batch = k8s_client.BatchV1Api()
    core = k8s_client.CoreV1Api()
    name = manifest["metadata"]["name"]

    try:
        batch.create_namespaced_job(namespace, manifest)
    except ApiException as e:
        if e.status != 409:  # idempotent re-attach
            return {"status": "failed", "logs": f"job create failed: {e.status} {e.reason} {e.body}"}

    deadline = time.monotonic() + timeout_seconds + 60
    status = "failed"
    while time.monotonic() < deadline:
        job = batch.read_namespaced_job(name, namespace)
        if job.status.succeeded:
            status = "succeeded"
            break
        if job.status.failed:
            status = "failed"
            break
        time.sleep(4)

    logs = ""
    try:
        pods = core.list_namespaced_pod(namespace, label_selector=f"job-name={name}")
        for pod in pods.items:
            logs += core.read_namespaced_pod_log(pod.metadata.name, namespace, tail_lines=200)
    except Exception as e:
        logs += f"\n(log fetch failed: {e})"
    from nightorder.builtins.executors import redact

    return {"status": status, "logs": redact(logs[-8000:]), "job": f"{namespace}/{name}"}


async def run_sandbox_job(*, idempotency_key: str, image: str, command: list[str],
                          env: dict[str, str], namespace: str | None = None,
                          timeout_seconds: int = 600) -> dict:
    ns = namespace or settings().sandbox_namespace
    manifest = build_sandbox_job_manifest(
        name=_job_name(idempotency_key), image=image, command=command,
        env=env, namespace=ns, timeout_seconds=timeout_seconds,
    )
    return await asyncio.to_thread(_run_sync, manifest, ns, timeout_seconds)
