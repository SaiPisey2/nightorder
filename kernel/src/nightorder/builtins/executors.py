"""Built-in step executors: script (local/dev), argo (leaf-level Argo
Workflows, P2), remote_exec (SSH to non-K8s hosts), noop.

All executors honor the idempotency contract (P5): the same
ctx.idempotency_key must not duplicate the side effect.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import shlex
from typing import Any

from nightorder.config import settings
from nightorder.contracts.base import ExecutionResult, StepContext, StepExecutor

_SECRET_PATTERN = re.compile(
    r"(?i)((?:api[_-]?key|token|password|secret|authorization)[\"'=:\s]+)([^\s\"']{8,})"
)


def redact(text: str) -> str:
    """Redact known secret patterns before persistence (cross-cutting req)."""
    return _SECRET_PATTERN.sub(r"\1[REDACTED]", text)


def _render(value: Any, ctx: StepContext) -> Any:
    """Substitute {{params.x}} and {{item}} templates in step config."""
    if isinstance(value, str):
        out = value
        for name, v in ctx.params.items():
            out = out.replace("{{params." + name + "}}", str(v))
        if ctx.unit is not None:
            out = out.replace("{{item}}", ctx.unit)
        return out
    if isinstance(value, list):
        return [_render(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: _render(v, ctx) for k, v in value.items()}
    return value


class NoopExecutor(StepExecutor):
    async def execute(self, ctx: StepContext, config: dict[str, Any]) -> ExecutionResult:
        return ExecutionResult(status="succeeded", output={"noop": True})


class ScriptExecutor(StepExecutor):
    """Run a command on the worker host. Dev/glue executor — the "python
    script between Argo steps" case. config: {command: "..." | [...],
    timeout_seconds: int}."""

    async def execute(self, ctx: StepContext, config: dict[str, Any]) -> ExecutionResult:
        config = _render(config, ctx)
        command = config.get("command")
        if isinstance(command, str):
            command = shlex.split(command)
        if not command:
            return ExecutionResult(status="failed", logs="script executor: no command configured")
        timeout = int(config.get("timeout_seconds", 600)) or None  # 0 = no timeout
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return ExecutionResult(status="failed", logs=f"timed out after {timeout}s")
        logs = redact(stdout.decode(errors="replace")[-8000:])
        status = "succeeded" if proc.returncode == 0 else "failed"
        return ExecutionResult(status=status, output={"exit_code": proc.returncode}, logs=logs)


class ArgoWorkflowExecutor(StepExecutor):
    """Submit + monitor one self-contained Argo Workflow (P2).

    config:
      manifest: full Workflow manifest dict (metadata.generateName ignored —
                name is derived from the idempotency key), OR
      manifest_file: path relative to NIGHTORDER_MANIFESTS_DIR — the worker loads
                the Workflow YAML at execution time (keeps specs small), OR
      workflow_template_ref: name of a WorkflowTemplate installed on cluster
      attach_to: name of an ALREADY-RUNNING Workflow — skip submission and
                just watch it to completion (re-poll after a watch was lost)
      namespace: override (default from settings)
      parameters: {name: value} Argo parameters
      poll_seconds / timeout_seconds

    Idempotency: workflow name = nightorder-<sha of idempotency key>. A 409 on
    create means this attempt already submitted — we re-attach and monitor.
    """

    async def execute(self, ctx: StepContext, config: dict[str, Any]) -> ExecutionResult:
        config = _render(config, ctx)
        work = asyncio.create_task(asyncio.to_thread(self._execute_sync, ctx, config))
        # Heartbeat from the event loop (heartbeating from the poll thread is
        # silently dropped by the SDK). Keeps unlimited watches alive.
        while True:
            done, _ = await asyncio.wait({work}, timeout=30)
            if done:
                return work.result()
            try:
                from temporalio import activity as temporal_activity

                temporal_activity.heartbeat(f"watching argo for {ctx.step_id}")
            except Exception:
                pass  # not inside an activity (tests / direct use)

    def _wf_name(self, ctx: StepContext) -> str:
        digest = hashlib.sha256(ctx.idempotency_key.encode()).hexdigest()[:12]
        return f"nightorder-{digest}"

    def _execute_sync(self, ctx: StepContext, config: dict[str, Any]) -> ExecutionResult:
        import time

        from kubernetes import client as k8s_client, config as k8s_config
        from kubernetes.client.rest import ApiException

        cfg = settings()
        kube_context = config.get("kube_context") or cfg.kube_context or None
        try:
            k8s_config.load_kube_config(context=kube_context)
        except Exception:
            k8s_config.load_incluster_config()
        api = k8s_client.CustomObjectsApi()
        namespace = config.get("namespace", cfg.argo_namespace)
        group, version, plural = "argoproj.io", "v1alpha1", "workflows"

        attach_to = (config.get("attach_to") or "").strip()
        if attach_to:
            # Re-poll mode: watch an existing workflow, never submit anything.
            try:
                api.get_namespaced_custom_object(group, version, namespace, plural, attach_to)
            except ApiException as e:
                return ExecutionResult(
                    status="failed",
                    logs=f"attach_to: workflow {namespace}/{attach_to} not found ({e.status})")
            return self._watch(api, namespace, attach_to, config, ctx)

        name = self._wf_name(ctx)

        parameters = [
            {"name": k, "value": str(v)} for k, v in (config.get("parameters") or {}).items()
        ]
        if config.get("manifest"):
            manifest = dict(config["manifest"])
        elif config.get("manifest_file"):
            import os

            import yaml as pyyaml

            base = os.path.realpath(cfg.manifests_dir)
            path = os.path.realpath(os.path.join(base, config["manifest_file"]))
            if not path.startswith(base + os.sep):
                return ExecutionResult(status="failed",
                                       logs=f"argo executor: manifest_file escapes manifests dir: {config['manifest_file']}")
            if not os.path.isfile(path):
                return ExecutionResult(status="failed",
                                       logs=f"argo executor: manifest_file not found: {path} "
                                            f"(NIGHTORDER_MANIFESTS_DIR={cfg.manifests_dir})")
            with open(path) as f:
                manifest = pyyaml.safe_load(f)
            if not isinstance(manifest, dict) or manifest.get("kind") != "Workflow":
                return ExecutionResult(status="failed",
                                       logs=f"argo executor: {path} is not an Argo Workflow manifest")
        elif config.get("workflow_template_ref"):
            manifest = {
                "apiVersion": "argoproj.io/v1alpha1",
                "kind": "Workflow",
                "spec": {"workflowTemplateRef": {"name": config["workflow_template_ref"]}},
            }
        else:
            return ExecutionResult(status="failed", logs="argo executor: need manifest or workflow_template_ref")

        manifest.setdefault("metadata", {})
        manifest["metadata"].pop("generateName", None)
        manifest["metadata"]["name"] = name
        manifest["metadata"].setdefault("labels", {}).update(
            {
                "nightorder/project": ctx.project,
                "nightorder/run-id": ctx.run_id[:63],
                "nightorder/step-id": ctx.step_id[:63],
                "app.kubernetes.io/managed-by": "nightorder",
            }
        )
        if parameters:
            manifest["spec"].setdefault("arguments", {}).setdefault("parameters", [])
            declared = manifest["spec"]["arguments"]["parameters"]
            by_name = {p["name"]: p for p in declared}
            for p in parameters:
                if p["name"] in by_name:
                    by_name[p["name"]]["value"] = p["value"]  # fill/override declared param
                else:
                    declared.append(p)

        try:
            api.create_namespaced_custom_object(group, version, namespace, plural, manifest)
        except ApiException as e:
            if e.status != 409:  # 409 = already submitted (idempotent re-attach)
                return ExecutionResult(status="failed", logs=f"argo submit failed: {e.status} {e.reason} {e.body}")

        return self._watch(api, namespace, name, config, ctx)

    def _watch(self, api, namespace: str, name: str, config: dict[str, Any], ctx: StepContext) -> ExecutionResult:
        import time

        group, version, plural = "argoproj.io", "v1alpha1", "workflows"
        poll = int(config.get("poll_seconds", 5))
        # timeout_seconds == 0 → no deadline: watch for as long as the Argo
        # workflow runs. Liveness comes from heartbeats (the workflow sets a
        # heartbeat timeout for unlimited steps).
        timeout = int(config.get("timeout_seconds", 0))
        deadline = time.monotonic() + timeout if timeout > 0 else None
        phase = "Pending"
        wf: dict = {}
        while deadline is None or time.monotonic() < deadline:
            wf = api.get_namespaced_custom_object(group, version, namespace, plural, name)
            phase = (wf.get("status") or {}).get("phase", "Pending")
            if phase in ("Succeeded", "Failed", "Error"):
                break
            time.sleep(poll)

        nodes = (wf.get("status") or {}).get("nodes", {}) or {}
        messages = [
            f"{n.get('displayName')}: {n.get('phase')} {n.get('message', '')}".strip()
            for n in nodes.values()
        ]
        logs = redact("\n".join(messages)[-8000:])
        if phase == "Succeeded":
            return ExecutionResult(status="succeeded", output={"phase": phase}, logs=logs, external_ref=f"{namespace}/{name}")
        if phase in ("Failed", "Error"):
            return ExecutionResult(status="failed", output={"phase": phase}, logs=logs, external_ref=f"{namespace}/{name}")
        return ExecutionResult(
            status="failed", output={"phase": phase}, logs=f"timed out after {timeout}s in phase {phase}\n{logs}",
            external_ref=f"{namespace}/{name}",
        )


class RemoteExecExecutor(StepExecutor):
    """SSH a command onto a non-K8s host (legacy VMs, DB servers).
    config: {host, user, command, port, timeout_seconds}. Auth via the
    worker's SSH agent / default keys (never passwords in specs)."""

    async def execute(self, ctx: StepContext, config: dict[str, Any]) -> ExecutionResult:
        import asyncssh

        config = _render(config, ctx)
        host, command = config.get("host"), config.get("command")
        if not host or not command:
            return ExecutionResult(status="failed", logs="remote_exec: need host and command")
        timeout = int(config.get("timeout_seconds", 600)) or None  # 0 = no timeout
        # Idempotency guard: a sentinel file keyed by the idempotency key.
        key_digest = hashlib.sha256(ctx.idempotency_key.encode()).hexdigest()[:16]
        sentinel = f"/tmp/nightorder-{key_digest}.done"
        guarded = f"if [ -e {sentinel} ]; then echo NIGHTORDER_ALREADY_RAN; else ({command}) && touch {sentinel}; fi"
        try:
            async with asyncssh.connect(
                host,
                port=int(config.get("port", 22)),
                username=config.get("user"),
                known_hosts=None,
                connect_timeout=30,
            ) as conn:
                result = await asyncio.wait_for(conn.run(guarded), timeout=timeout)
        except Exception as e:
            return ExecutionResult(status="failed", logs=f"remote_exec error: {e}")
        logs = redact(((result.stdout or "") + (result.stderr or ""))[-8000:])
        status = "succeeded" if result.exit_status == 0 else "failed"
        return ExecutionResult(status=status, output={"exit_code": result.exit_status}, logs=logs, external_ref=host)
