"""Temporal Activities — the only place side effects happen.

Each activity is idempotent (P5): DB writes upsert on natural keys, event
emission tolerates replays (duplicate events are acceptable in the audit
stream and deduped downstream by event consumers via event_id).
Extensions run *inside* activities, so a broken extension surfaces as a
step/check failure with a timeout — never as interpreter corruption.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from temporalio import activity

from nightorder import events as ev
from nightorder.config import settings
from nightorder.contracts import StepContext, load_registry
from nightorder.db import Event, Gate, Outbox, ResolvedParameter, Run, StepExecution, get_session

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _emit(session, *, event_type: str, project: str, run_id: str, step_id: str = "",
                correlation_id: str = "", payload: dict | None = None) -> None:
    """Event + outbox row in the caller's transaction (transactional outbox)."""
    event = Event(
        event_type=event_type, project=project, run_id=run_id, step_id=step_id,
        correlation_id=correlation_id, payload=payload or {}, schema_version=ev.SCHEMA_VERSION,
    )
    session.add(event)
    await session.flush()
    session.add(Outbox(event_id=event.id))


def _ctx(input: dict, unit: str | None = None, attempt: int = 1) -> StepContext:
    return StepContext(
        project=input["project"],
        pipeline=input.get("pipeline", ""),
        run_id=input["run_id"],
        step_id=input.get("step_id", ""),
        unit=unit if unit is not None else input.get("unit"),
        attempt=attempt or input.get("attempt", 1),
        idempotency_key=input.get("idempotency_key", ""),
        params=input.get("params", {}),
    )


@activity.defn
async def record_run_status(input: dict) -> None:
    """{run_id, project, status, error?, correlation_id}"""
    async with get_session() as session:
        run = await session.get(Run, input["run_id"])
        if run is None:
            # Schedule-created runs bootstrap their own record.
            run = Run(id=input["run_id"], project=input["project"],
                      pipeline=input.get("pipeline", ""), temporal_workflow_id=input.get("workflow_id", ""))
            session.add(run)
        status = input["status"]
        run.status = status
        if status == "running" and run.started_at is None:
            run.started_at = _now()
        if status in ("completed", "failed"):
            run.completed_at = _now()
            run.error = input.get("error", "")
        event_type = {
            "running": ev.WORKFLOW_STARTED,
            "completed": ev.WORKFLOW_COMPLETED,
            "failed": ev.WORKFLOW_FAILED,
        }.get(status)
        if event_type:
            await _emit(session, event_type=event_type, project=input["project"], run_id=run.id,
                        correlation_id=input.get("correlation_id", ""), payload={"error": input.get("error", "")})
        await session.commit()


@activity.defn
async def record_step(input: dict) -> None:
    """Upsert step_executions on (run_id, step_id, unit, attempt) + emit event.
    {run_id, project, step_id, unit, attempt, status, executor?, error?,
     logs_tail?, external_ref?, event?, correlation_id}"""
    async with get_session() as session:
        row = (
            await session.execute(
                select(StepExecution).where(
                    StepExecution.run_id == input["run_id"],
                    StepExecution.step_id == input["step_id"],
                    StepExecution.unit == input.get("unit", ""),
                    StepExecution.attempt == input.get("attempt", 1),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = StepExecution(
                run_id=input["run_id"], step_id=input["step_id"],
                unit=input.get("unit", ""), attempt=input.get("attempt", 1),
            )
            session.add(row)
        status = input["status"]
        row.status = status
        row.executor = input.get("executor", row.executor)
        row.external_ref = input.get("external_ref", row.external_ref)
        if input.get("logs_tail"):
            row.logs_tail = input["logs_tail"]
        if input.get("error"):
            row.error = input["error"]
        if status == "running" and row.started_at is None:
            row.started_at = _now()
        if status in ("succeeded", "failed", "skipped"):
            row.completed_at = _now()
        if input.get("event"):
            payload = {"unit": input.get("unit", ""), "attempt": input.get("attempt", 1),
                       "status": status, "error": input.get("error", "")}
            if input.get("notify"):
                # Step-level notification config rides in the event payload;
                # the outbox relay delivers the Teams card.
                payload["notify"] = input["notify"]
                payload["pipeline"] = input.get("pipeline", "")
            await _emit(
                session, event_type=input["event"], project=input["project"], run_id=input["run_id"],
                step_id=input["step_id"], correlation_id=input.get("correlation_id", ""),
                payload=payload,
            )
        await session.commit()


@activity.defn
async def resolve_parameter(input: dict) -> dict:
    """{run_id, project, pipeline, param: ParameterSpec dict, params: resolved-so-far, correlation_id}
    -> {value, provenance}. Records provenance + emits ParameterResolved."""
    param = input["param"]
    name = param["name"]
    resolver = param.get("resolver", "static")
    if resolver == "static":
        value, provenance = param.get("value"), "static"
    elif resolver == "override":
        value, provenance = param.get("value"), f"run override by {param.get('actor', 'api')}"
    elif resolver == "expression":
        # Deterministic expression over previously resolved params; no builtins.
        value = eval(param["expression"], {"__builtins__": {}}, {"params": dict(input.get("params", {}))})  # noqa: S307
        provenance = f"expression: {param['expression']}"
    elif resolver == "activity":
        registry = load_registry()
        impl = registry.param_resolvers.get(param["activity"])
        if impl is None:
            raise RuntimeError(f"unregistered parameter resolver '{param['activity']}'")
        resolved = await impl.resolve(_ctx(input), param.get("params", {}))
        value, provenance = resolved.value, resolved.provenance
    else:
        raise RuntimeError(f"unknown resolver kind '{resolver}'")

    async with get_session() as session:
        existing = (
            await session.execute(
                select(ResolvedParameter).where(
                    ResolvedParameter.run_id == input["run_id"], ResolvedParameter.name == name
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(ResolvedParameter(run_id=input["run_id"], name=name,
                                          value={"value": value}, provenance=provenance))
        else:
            existing.value, existing.provenance = {"value": value}, provenance
        await _emit(session, event_type=ev.PARAMETER_RESOLVED, project=input["project"],
                    run_id=input["run_id"], correlation_id=input.get("correlation_id", ""),
                    payload={"name": name, "value": value, "provenance": provenance})
        await session.commit()
    return {"value": value, "provenance": provenance}


@activity.defn
async def run_check(input: dict) -> dict:
    """{run_id, project, step_id, unit?, check, check_params, params, phase, correlation_id}
    -> {passed, evidence}. Emits PreflightCheckPassed/Failed."""
    registry = load_registry()
    impl = registry.preflight_checks.get(input["check"])
    if impl is None:
        raise RuntimeError(f"unregistered check '{input['check']}'")
    result = await impl.run(_ctx(input), input.get("check_params", {}))
    async with get_session() as session:
        await _emit(
            session,
            event_type=ev.PREFLIGHT_PASSED if result.passed else ev.PREFLIGHT_FAILED,
            project=input["project"], run_id=input["run_id"], step_id=input["step_id"],
            correlation_id=input.get("correlation_id", ""),
            payload={"check": input["check"], "phase": input.get("phase", "preflight"),
                     "evidence": result.evidence, "unit": input.get("unit") or ""},
        )
        await session.commit()
    return {"passed": result.passed, "evidence": result.evidence}


@activity.defn
async def open_gate(input: dict) -> dict:
    """Create gate row, mint signed single-use action tokens, deliver via
    channel, emit GateRequested.
    {run_id, project, step_id, gate: GateSpec dict, context, correlation_id}
    -> {gate_id}"""
    from nightorder.api.security import mint_gate_token  # kernel mints tokens, not channels/LLMs

    gate_spec = input["gate"]
    cfg = settings()
    async with get_session() as session:
        gate = Gate(
            run_id=input["run_id"], step_id=input["step_id"], project=input["project"],
            gate_type=gate_spec.get("type", "sign_off"), prompt=gate_spec.get("prompt", ""),
            expires_at=_now() + timedelta(minutes=int(gate_spec.get("timeout_minutes", 1440))),
        )
        session.add(gate)
        await session.flush()
        gate_id = gate.id
        await _emit(session, event_type=ev.GATE_REQUESTED, project=input["project"],
                    run_id=input["run_id"], step_id=input["step_id"],
                    correlation_id=input.get("correlation_id", ""),
                    payload={"gate_id": gate_id, "gate_type": gate.gate_type,
                             "channel": gate_spec.get("channel", "log")})
        await session.commit()

    approve = mint_gate_token(gate_id, "approve")
    reject = mint_gate_token(gate_id, "reject")
    base = cfg.api_base_url.rstrip("/")
    registry = load_registry()
    channel = registry.gate_channels.get(gate_spec.get("channel", "log"))
    if channel is None:
        raise RuntimeError(f"unregistered gate channel '{gate_spec.get('channel')}'")
    from nightorder.contracts.base import GateNotification

    notification = GateNotification(
        gate_id=gate_id, gate_type=gate.gate_type, prompt=gate.prompt,
        approve_url=f"{base}/gate-action?token={approve}",
        reject_url=f"{base}/gate-action?token={reject}",
        context=input.get("context", {}),
    )
    try:
        await channel.deliver(notification, gate_spec.get("channel_params", {}))
    except Exception:
        # Delivery failure must not lose the gate — it stays resolvable via API.
        log.exception("gate delivery failed for %s (channel %s)", gate_id, gate_spec.get("channel"))
    return {"gate_id": gate_id}


@activity.defn
async def close_gate(input: dict) -> None:
    """Terminal-state a gate from the workflow side (timeout/escalation).
    {gate_id, project, run_id, step_id, status, correlation_id}"""
    event_type = {
        "approved": ev.GATE_GRANTED, "rejected": ev.GATE_REJECTED,
        "expired": ev.GATE_EXPIRED, "escalated": ev.GATE_ESCALATED,
    }[input["status"]]
    async with get_session() as session:
        gate = await session.get(Gate, input["gate_id"])
        if gate and gate.status == "pending":
            gate.status = input["status"]
            gate.resolved_at = _now()
            gate.decided_by = input.get("decided_by", "system:timeout")
        await _emit(session, event_type=event_type, project=input["project"], run_id=input["run_id"],
                    step_id=input.get("step_id", ""), correlation_id=input.get("correlation_id", ""),
                    payload={"gate_id": input["gate_id"], "decided_by": input.get("decided_by", "system:timeout")})
        await session.commit()


@activity.defn
async def execute_step(input: dict) -> dict:
    """Dispatch to the registered step executor.
    {run_id, project, pipeline, step_id, unit?, attempt, executor, config,
     params, idempotency_key, step_timeout_minutes} -> ExecutionResult dict."""
    registry = load_registry()
    impl = registry.step_executors.get(input["executor"])
    if impl is None:
        raise RuntimeError(f"unregistered step executor '{input['executor']}'")
    ctx = _ctx(input, unit=input.get("unit"), attempt=input.get("attempt", 1))
    config = dict(input.get("config", {}))
    # Executors that poll external systems inherit the step's timeout unless
    # the spec sets its own — a private short default must never out-vote the
    # step timeout (a 1h poll budget once failed a healthy 5.5h Argo run).
    # step_timeout_minutes == 0 means unlimited: watch as long as the external
    # work runs (timeout_seconds=0 → no deadline in the executor).
    if "timeout_seconds" not in config and "step_timeout_minutes" in input:
        minutes = int(input["step_timeout_minutes"])
        config["timeout_seconds"] = 0 if minutes == 0 else max(60, minutes * 60 - 300)
    result = await impl.execute(ctx, config)
    return {
        "status": result.status, "output": result.output,
        "logs": result.logs[-8000:], "external_ref": result.external_ref,
    }


@activity.defn
async def execute_sandbox_job(input: dict) -> dict:
    """Phase 3: run one pre-approved playbook inside the hardened sandbox.
    {idempotency_key, image, command, env, namespace?, timeout_seconds}"""
    from nightorder.sandbox import run_sandbox_job

    return await run_sandbox_job(
        idempotency_key=input["idempotency_key"], image=input["image"],
        command=input["command"], env=input.get("env", {}),
        namespace=input.get("namespace"), timeout_seconds=int(input.get("timeout_seconds", 600)),
    )


@activity.defn
async def record_remediation(input: dict) -> None:
    """Update incident status + emit remediation/sandbox events.
    {incident_id, project, run_id, step_id, status, event, payload}"""
    from nightorder.db import Incident

    async with get_session() as session:
        incident = await session.get(Incident, input["incident_id"])
        if incident is not None and input.get("status"):
            incident.status = input["status"]
            if input.get("outcome"):
                incident.outcome = input["outcome"]
        await _emit(session, event_type=input["event"], project=input["project"],
                    run_id=input["run_id"], step_id=input.get("step_id", ""),
                    correlation_id=input.get("correlation_id", ""),
                    payload=input.get("payload", {}))
        await session.commit()


ALL_ACTIVITIES = [
    record_run_status,
    record_step,
    resolve_parameter,
    run_check,
    open_gate,
    close_gate,
    execute_step,
    execute_sandbox_job,
    record_remediation,
]
