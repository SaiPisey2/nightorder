"""Incident Bundle (Phase 2): everything known about a failed step, normalized
into one document. All troubleshooting agents operate on this — never on raw
scattered sources. Secrets are redacted before persistence."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from nightorder.builtins.executors import redact
from nightorder.db import Event, ResolvedParameter, Run, StepExecution, get_session
from nightorder.ai.logreduce import reduce_log


async def _kubernetes_context(external_ref: str) -> dict[str, Any]:
    """Best-effort K8s context for argo steps (events + pod phases). Failure
    to reach the cluster degrades the bundle, never the caller."""
    if "/" not in external_ref:
        return {}
    namespace, name = external_ref.split("/", 1)
    try:
        import asyncio

        def _fetch() -> dict:
            from kubernetes import client as k8s_client, config as k8s_config

            from nightorder.config import settings

            k8s_config.load_kube_config(context=settings().kube_context or None)
            core = k8s_client.CoreV1Api()
            events = core.list_namespaced_event(
                namespace, field_selector=f"involvedObject.name={name}", limit=20
            )
            return {
                "k8s_events": [
                    {"reason": e.reason, "message": e.message, "type": e.type}
                    for e in events.items
                ]
            }

        return await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=20)
    except Exception as e:
        return {"k8s_context_error": str(e)[:200]}


async def build_incident_bundle(run_id: str, step_id: str) -> dict[str, Any]:
    async with get_session() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise ValueError(f"run {run_id} not found")
        steps = (
            await session.execute(
                select(StepExecution)
                .where(StepExecution.run_id == run_id, StepExecution.step_id == step_id)
                .order_by(StepExecution.attempt)
            )
        ).scalars().all()
        if not steps:
            raise ValueError(f"no executions for step {step_id} in run {run_id}")
        events = (
            await session.execute(
                select(Event)
                .where(Event.run_id == run_id, Event.step_id == step_id)
                .order_by(Event.created_at)
            )
        ).scalars().all()
        params = (
            await session.execute(select(ResolvedParameter).where(ResolvedParameter.run_id == run_id))
        ).scalars().all()

    raw_logs = "\n".join(s.logs_tail or s.error for s in steps if (s.logs_tail or s.error))
    last = steps[-1]
    bundle: dict[str, Any] = {
        "project": run.project,
        "pipeline": run.pipeline,
        "run_id": run_id,
        "step_id": step_id,
        "executor": last.executor,
        "external_ref": last.external_ref,
        "executions": [
            {
                "unit": s.unit, "attempt": s.attempt, "status": s.status,
                "error": redact(s.error), "started_at": str(s.started_at),
                "completed_at": str(s.completed_at),
            }
            for s in steps
        ],
        # Preflight history + step lifecycle for this step (P7 evidence).
        "events": [
            {"type": e.event_type, "payload": e.payload, "at": str(e.created_at)}
            for e in events
        ],
        "resolved_parameters": [
            {"name": p.name, "value": p.value.get("value"), "provenance": p.provenance}
            for p in params
        ],
        "reduced_log": reduce_log(redact(raw_logs)),
        "raw_log_length": len(raw_logs),
    }
    if last.executor == "argo" and last.external_ref:
        bundle.update(await _kubernetes_context(last.external_ref))
    return bundle
