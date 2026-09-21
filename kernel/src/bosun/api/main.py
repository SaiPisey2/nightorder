"""FastAPI control plane.

Project-scoped auth via X-API-Key. Specs are registered (validated, versioned)
and runs are started here; Temporal owns execution. Gate resolution comes in
either as an authenticated API call or a signed single-use token link.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from temporalio.client import Client, Schedule, ScheduleActionStartWorkflow, ScheduleSpec as TScheduleSpec

from bosun import TASK_QUEUE, __version__
from bosun.api.security import TokenError, mint_gate_token, verify_gate_token
from bosun.config import settings
from bosun.contracts import load_registry
from bosun.db import (
    Event,
    Gate,
    GateTokenNonce,
    PipelineSpecRecord,
    Project,
    ResolvedParameter,
    Run,
    StepExecution,
    get_session,
    init_db,
)
from bosun.spec.validate import SpecValidationError, validate_spec_dict

log = logging.getLogger("bosun.api")

app = FastAPI(title="Bosun", version=__version__)

# CORS for the Vite dev server; production UI is same-origin (/ui).
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_temporal_client: Client | None = None


async def temporal_client() -> Client:
    global _temporal_client
    if _temporal_client is None:
        cfg = settings()
        _temporal_client = await Client.connect(cfg.temporal_address, namespace=cfg.temporal_namespace)
    return _temporal_client


@app.on_event("startup")
async def _startup() -> None:
    await init_db()
    # Seed the Agent Registry (scaffolded Phase 1, activated Phase 2).
    from bosun.ai.agents import DEFAULT_AGENTS
    from bosun.db import AgentRecord

    async with get_session() as session:
        for spec in DEFAULT_AGENTS:
            if await session.get(AgentRecord, spec["id"]) is None:
                session.add(AgentRecord(
                    id=spec["id"], name=spec["name"], capabilities=spec.get("capabilities", {}),
                    requires_approval=spec.get("requires_approval", True),
                ))
        await session.commit()


# ---- auth -------------------------------------------------------------------
# Open platform mode (default): anyone can view, validate, and act; projects
# are an organizational boundary, not an auth boundary. Set BOSUN_AUTH=on
# to enforce per-project API keys again.
def auth_enabled() -> bool:
    return os.environ.get("BOSUN_AUTH", "off").lower() == "on"


async def require_project(project: str, x_api_key: str = Header(default="")) -> Project:
    async with get_session() as session:
        row = await session.get(Project, project)
    if row is None:
        raise HTTPException(404, f"project '{project}' not found")
    if auth_enabled() and row.api_key != x_api_key:
        raise HTTPException(403, "invalid API key for project")
    return row


async def project_for_run(run_id: str, x_api_key: str = Header(default="")) -> Run:
    async with get_session() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        project = await session.get(Project, run.project)
    if auth_enabled() and (project is None or project.api_key != x_api_key):
        raise HTTPException(403, "invalid API key for run's project")
    return run


# ---- meta -------------------------------------------------------------------
@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/health/components")
async def health_components() -> dict:
    """Live health of every platform component (UI dashboard)."""
    import asyncio as aio
    from datetime import datetime, timezone

    from bosun import TASK_QUEUE
    from bosun.db import Outbox

    async def check_postgres() -> dict:
        try:
            async with get_session() as session:
                await session.execute(select(Project.id).limit(1))
            return {"ok": True, "detail": "connected"}
        except Exception as e:
            return {"ok": False, "detail": str(e)[:200]}

    async def check_temporal() -> dict:
        try:
            from temporalio.api.taskqueue.v1 import TaskQueue
            from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest

            client = await aio.wait_for(temporal_client(), timeout=5)
            info = await aio.wait_for(
                client.workflow_service.describe_task_queue(
                    DescribeTaskQueueRequest(
                        namespace=settings().temporal_namespace,
                        task_queue=TaskQueue(name=TASK_QUEUE),
                    )
                ),
                timeout=5,
            )
            pollers = len(info.pollers)
            return {"ok": True, "detail": "connected", "worker_pollers": pollers,
                    "worker_ok": pollers > 0}
        except Exception as e:
            return {"ok": False, "detail": str(e)[:200], "worker_pollers": 0, "worker_ok": False}

    async def check_relay() -> dict:
        try:
            async with get_session() as session:
                backlog = (
                    await session.execute(
                        select(Outbox).where(Outbox.published.is_(False))
                    )
                ).scalars().all()
            oldest_age = 0.0
            if backlog:
                oldest = min(o.created_at for o in backlog)
                oldest_age = (datetime.now(timezone.utc) - oldest).total_seconds()
            # Relay is unhealthy if the outbox is stuck (old unpublished rows).
            return {"ok": oldest_age < 120, "backlog": len(backlog),
                    "detail": f"backlog={len(backlog)}, oldest={int(oldest_age)}s"}
        except Exception as e:
            return {"ok": False, "detail": str(e)[:200]}

    async def check_qdrant() -> dict:
        try:
            from bosun.ai.knowledge import qdrant_available

            ok = await aio.wait_for(aio.to_thread(qdrant_available), timeout=5)
            return {"ok": ok, "detail": "connected" if ok else "unreachable"}
        except Exception as e:
            return {"ok": False, "detail": str(e)[:200]}

    postgres, temporal, relay, qdrant = await aio.gather(
        check_postgres(), check_temporal(), check_relay(), check_qdrant()
    )
    advisor = {"ok": bool(os.environ.get("ANTHROPIC_API_KEY")),
               "detail": "key configured" if os.environ.get("ANTHROPIC_API_KEY") else "ANTHROPIC_API_KEY not set"}
    worker = {"ok": temporal.pop("worker_ok", False),
              "detail": f"{temporal.pop('worker_pollers', 0)} poller(s) on {TASK_QUEUE}"}
    components = {"postgres": postgres, "temporal": temporal, "worker": worker,
                  "relay": relay, "qdrant": qdrant, "ai_advisor": advisor}
    return {"ok": all(c["ok"] for c in components.values()), "components": components,
            "version": __version__}


@app.get("/catalog")
async def catalog() -> dict:
    """Registered extension catalog (executors, checks, resolvers, channels)."""
    return load_registry().catalog()


# ---- projects -----------------------------------------------------------------
class ProjectCreate(BaseModel):
    id: str
    display_name: str = ""


@app.post("/projects", status_code=201)
async def create_project(body: ProjectCreate, x_admin_key: str = Header(default="")) -> dict:
    admin_key = os.environ.get("BOSUN_ADMIN_KEY", "")
    if admin_key and x_admin_key != admin_key:
        raise HTTPException(403, "invalid admin key")
    api_key = uuid.uuid4().hex
    async with get_session() as session:
        if await session.get(Project, body.id):
            raise HTTPException(409, f"project '{body.id}' exists")
        session.add(Project(id=body.id, display_name=body.display_name, api_key=api_key))
        await session.commit()
    return {"id": body.id, "api_key": api_key}


@app.delete("/projects/{project}")
async def delete_project(project: str, confirm: str = "", _: Project = Depends(require_project)) -> dict:
    """Delete a project and EVERYTHING in it: specs, runs, step history,
    gates, events, incidents, knowledge (Postgres + Qdrant), playbooks.
    Running Temporal workflows are terminated (best effort).
    Requires ?confirm=<project-id> — irreversible."""
    if confirm != project:
        raise HTTPException(422, f"pass ?confirm={project} to delete this project irreversibly")

    from sqlalchemy import delete as sql_delete

    from bosun.db import (
        Gate as GateT, GateTokenNonce as NonceT, Incident as IncidentT,
        KnowledgeRecord as KnowT, Outbox as OutboxT, Playbook as PlaybookT,
    )

    # Terminate live workflows first so nothing keeps writing rows.
    terminated = 0
    async with get_session() as session:
        running = (
            await session.execute(
                select(Run).where(Run.project == project, Run.status.in_(("pending", "running")))
            )
        ).scalars().all()
    try:
        client = await temporal_client()
        for run in running:
            if run.temporal_workflow_id:
                try:
                    await client.get_workflow_handle(run.temporal_workflow_id).terminate(
                        reason=f"project {project} deleted")
                    terminated += 1
                except Exception:
                    pass
    except Exception:
        log.warning("temporal unreachable while deleting project %s — skipping terminations", project)

    async with get_session() as session:
        run_ids = select(Run.id).where(Run.project == project)
        gate_ids = select(GateT.id).where(GateT.project == project)
        event_ids = select(Event.id).where(Event.project == project)
        await session.execute(sql_delete(NonceT).where(NonceT.gate_id.in_(gate_ids)))
        await session.execute(sql_delete(OutboxT).where(OutboxT.event_id.in_(event_ids)))
        await session.execute(sql_delete(Event).where(Event.project == project))
        await session.execute(sql_delete(GateT).where(GateT.project == project))
        await session.execute(sql_delete(IncidentT).where(IncidentT.project == project))
        await session.execute(sql_delete(KnowT).where(KnowT.project == project))
        await session.execute(sql_delete(PlaybookT).where(PlaybookT.project == project))
        await session.execute(sql_delete(ResolvedParameter).where(ResolvedParameter.run_id.in_(run_ids)))
        await session.execute(sql_delete(StepExecution).where(StepExecution.run_id.in_(run_ids)))
        await session.execute(sql_delete(Run).where(Run.project == project))
        await session.execute(sql_delete(PipelineSpecRecord).where(PipelineSpecRecord.project == project))
        row = await session.get(Project, project)
        if row is not None:
            await session.delete(row)
        await session.commit()

    qdrant = "removed"
    try:
        from bosun.ai import knowledge

        await knowledge.delete_project_points(project)
    except Exception as e:
        qdrant = f"warning, embeddings not removed: {e}"
    return {"deleted": project, "terminated_workflows": terminated, "qdrant": qdrant}


# ---- specs --------------------------------------------------------------------
async def _spec_from_request(request: Request) -> dict:
    body = await request.body()
    if request.headers.get("content-type", "").startswith(("application/yaml", "text/yaml", "text/plain")):
        return yaml.safe_load(body)
    import json

    return json.loads(body)


@app.post("/projects/{project}/specs/validate")
async def validate_spec(project: str, request: Request, _: Project = Depends(require_project)) -> dict:
    raw = await _spec_from_request(request)
    try:
        spec = validate_spec_dict(raw, load_registry())
    except SpecValidationError as e:
        return {"valid": False, "errors": e.errors}
    return {"valid": True, "errors": [], "pipeline": spec.name}


@app.post("/projects/{project}/specs", status_code=201)
async def register_spec(project: str, request: Request, _: Project = Depends(require_project)) -> dict:
    raw = await _spec_from_request(request)
    try:
        spec = validate_spec_dict(raw, load_registry())
    except SpecValidationError as e:
        raise HTTPException(422, detail={"errors": e.errors})
    if spec.project != project:
        raise HTTPException(422, f"spec.project '{spec.project}' does not match URL project '{project}'")
    async with get_session() as session:
        current = (
            await session.execute(
                select(PipelineSpecRecord.version)
                .where(PipelineSpecRecord.project == project, PipelineSpecRecord.name == spec.name)
                .order_by(PipelineSpecRecord.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        version = (current or 0) + 1
        record = PipelineSpecRecord(project=project, name=spec.name, version=version, spec=raw)
        session.add(record)
        await session.commit()
        return {"id": record.id, "name": spec.name, "version": version}


@app.get("/projects")
async def list_projects() -> list[dict]:
    """Project ids only — keys are never listed."""
    async with get_session() as session:
        rows = (await session.execute(select(Project).order_by(Project.created_at))).scalars().all()
    return [{"id": p.id, "display_name": p.display_name, "created_at": p.created_at} for p in rows]


@app.get("/projects/{project}/runs")
async def list_runs(project: str, limit: int = 50, _: Project = Depends(require_project)) -> list[dict]:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Run).where(Run.project == project)
                .order_by(Run.created_at.desc()).limit(limit)
            )
        ).scalars().all()
    return [
        {"run_id": r.id, "pipeline": r.pipeline, "status": r.status,
         "spec_version": r.spec_version, "started_at": r.started_at,
         "completed_at": r.completed_at, "created_at": r.created_at, "error": r.error[:200]}
        for r in rows
    ]


@app.get("/projects/{project}/incidents")
async def list_incidents(project: str, limit: int = 50, _: Project = Depends(require_project)) -> list[dict]:
    from bosun.db import Incident

    async with get_session() as session:
        rows = (
            await session.execute(
                select(Incident).where(Incident.project == project)
                .order_by(Incident.created_at.desc()).limit(limit)
            )
        ).scalars().all()
    return [
        {"id": i.id, "run_id": i.run_id, "step_id": i.step_id, "status": i.status,
         "gate_id": i.gate_id, "created_at": i.created_at,
         "rca_preview": i.rca[:200]}
        for i in rows
    ]


@app.get("/projects/{project}/knowledge")
async def list_knowledge(project: str, limit: int = 100, _: Project = Depends(require_project)) -> list[dict]:
    from bosun.db import KnowledgeRecord

    async with get_session() as session:
        rows = (
            await session.execute(
                select(KnowledgeRecord).where(KnowledgeRecord.project == project)
                .order_by(KnowledgeRecord.created_at.desc()).limit(limit)
            )
        ).scalars().all()
    return [
        {"id": k.id, "kind": k.kind, "title": k.title, "source": k.source,
         "indexed": k.indexed, "created_at": k.created_at,
         "content_preview": k.content[:300]}
        for k in rows
    ]


@app.get("/projects/{project}/specs/{name}")
async def get_spec(project: str, name: str, version: int | None = None,
                   _: Project = Depends(require_project)) -> dict:
    async with get_session() as session:
        q = select(PipelineSpecRecord).where(
            PipelineSpecRecord.project == project, PipelineSpecRecord.name == name
        )
        q = q.where(PipelineSpecRecord.version == version) if version else q.order_by(PipelineSpecRecord.version.desc()).limit(1)
        record = (await session.execute(q)).scalars().first()
    if record is None:
        raise HTTPException(404, "spec not found")
    return {"name": record.name, "version": record.version, "spec": record.spec,
            "created_at": record.created_at}


@app.get("/projects/{project}/specs")
async def list_specs(project: str, _: Project = Depends(require_project)) -> list[dict]:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(PipelineSpecRecord).where(PipelineSpecRecord.project == project)
                .order_by(PipelineSpecRecord.name, PipelineSpecRecord.version)
            )
        ).scalars().all()
    return [{"name": r.name, "version": r.version, "id": r.id, "created_at": r.created_at} for r in rows]


# ---- runs -------------------------------------------------------------------
class RunCreate(BaseModel):
    version: int | None = None  # default latest
    params: dict = {}


@app.post("/projects/{project}/pipelines/{pipeline}/runs", status_code=201)
async def start_run(project: str, pipeline: str, body: RunCreate, _: Project = Depends(require_project)) -> dict:
    async with get_session() as session:
        q = select(PipelineSpecRecord).where(
            PipelineSpecRecord.project == project, PipelineSpecRecord.name == pipeline
        )
        q = q.where(PipelineSpecRecord.version == body.version) if body.version else q.order_by(PipelineSpecRecord.version.desc()).limit(1)
        record = (await session.execute(q)).scalars().first()
        if record is None:
            raise HTTPException(404, f"pipeline '{pipeline}' (version={body.version or 'latest'}) not found")
        run = Run(project=project, pipeline=pipeline, spec_id=record.id,
                  spec_version=record.version, param_overrides=body.params)
        session.add(run)
        await session.flush()
        run_id = run.id
        workflow_id = f"bosun-run-{run_id}"
        run.temporal_workflow_id = workflow_id
        await session.commit()

    from bosun.temporal.workflows import PipelineRunWorkflow, RunInput

    client = await temporal_client()
    correlation_id = uuid.uuid4().hex
    spec_dict = validate_spec_dict(record.spec).model_dump(by_alias=False)
    await client.start_workflow(
        PipelineRunWorkflow.run,
        RunInput(run_id=run_id, project=project, pipeline=pipeline, spec=spec_dict,
                 param_overrides=body.params, correlation_id=correlation_id),
        id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    return {"run_id": run_id, "workflow_id": workflow_id, "spec_version": record.version,
            "correlation_id": correlation_id}


@app.get("/runs/{run_id}")
async def get_run(run_id: str, run: Run = Depends(project_for_run)) -> dict:
    async with get_session() as session:
        steps = (
            await session.execute(
                select(StepExecution).where(StepExecution.run_id == run_id)
                .order_by(StepExecution.started_at.nulls_last())
            )
        ).scalars().all()
        params = (
            await session.execute(select(ResolvedParameter).where(ResolvedParameter.run_id == run_id))
        ).scalars().all()
        gates = (
            await session.execute(select(Gate).where(Gate.run_id == run_id).order_by(Gate.created_at))
        ).scalars().all()
    return {
        "run_id": run.id, "project": run.project, "pipeline": run.pipeline,
        "spec_version": run.spec_version, "status": run.status, "error": run.error,
        "temporal_workflow_id": run.temporal_workflow_id,
        "started_at": run.started_at, "completed_at": run.completed_at,
        "steps": [
            {
                "step_id": s.step_id, "unit": s.unit, "attempt": s.attempt, "status": s.status,
                "executor": s.executor, "external_ref": s.external_ref, "error": s.error,
                "started_at": s.started_at, "completed_at": s.completed_at,
                "duration_seconds": (s.completed_at - s.started_at).total_seconds()
                if s.started_at and s.completed_at else None,
            }
            for s in steps
        ],
        "parameters": [{"name": p.name, "value": p.value.get("value"), "provenance": p.provenance} for p in params],
        "gates": [
            {"gate_id": g.id, "step_id": g.step_id, "type": g.gate_type, "status": g.status,
             "prompt": g.prompt, "decided_by": g.decided_by, "expires_at": g.expires_at}
            for g in gates
        ],
    }


@app.get("/runs/{run_id}/events")
async def run_events(run_id: str, run: Run = Depends(project_for_run)) -> list[dict]:
    async with get_session() as session:
        rows = (
            await session.execute(select(Event).where(Event.run_id == run_id).order_by(Event.created_at))
        ).scalars().all()
    return [
        {"event_id": e.id, "type": e.event_type, "step_id": e.step_id, "payload": e.payload,
         "correlation_id": e.correlation_id, "at": e.created_at, "schema_version": e.schema_version}
        for e in rows
    ]


@app.get("/projects/{project}/pipelines/{pipeline}/timings")
async def pipeline_timings(project: str, pipeline: str, _: Project = Depends(require_project)) -> list[dict]:
    """Timing history across runs — replaces hand-maintained run logs."""
    async with get_session() as session:
        rows = (
            await session.execute(
                select(StepExecution, Run.started_at.label("run_started"))
                .join(Run, Run.id == StepExecution.run_id)
                .where(Run.project == project, Run.pipeline == pipeline)
                .order_by(StepExecution.started_at.nulls_last())
            )
        ).all()
    return [
        {
            "run_id": s.StepExecution.run_id, "step_id": s.StepExecution.step_id,
            "unit": s.StepExecution.unit, "attempt": s.StepExecution.attempt,
            "status": s.StepExecution.status,
            "started_at": s.StepExecution.started_at, "completed_at": s.StepExecution.completed_at,
            "duration_seconds": (s.StepExecution.completed_at - s.StepExecution.started_at).total_seconds()
            if s.StepExecution.started_at and s.StepExecution.completed_at else None,
        }
        for s in rows
    ]


# ---- gates ------------------------------------------------------------------
class GateResolve(BaseModel):
    decision: str  # approve | reject
    actor: str = "api"
    note: str = ""


async def _apply_gate_decision(gate_id: str, decision: str, actor: str, note: str) -> dict:
    if decision not in ("approve", "reject"):
        raise HTTPException(422, "decision must be approve|reject")
    async with get_session() as session:
        gate = await session.get(Gate, gate_id)
        if gate is None:
            raise HTTPException(404, "gate not found")
        if gate.status != "pending":
            return {"gate_id": gate_id, "status": gate.status, "already_resolved": True}
        gate.status = "approved" if decision == "approve" else "rejected"
        gate.decided_by = actor
        gate.decision_note = note
        gate.resolved_at = datetime.now(timezone.utc)
        run = await session.get(Run, gate.run_id)
        # Gate decision event (transactional with the status change)
        from bosun.temporal.activities import _emit
        from bosun import events as ev

        await _emit(session, event_type=ev.GATE_GRANTED if decision == "approve" else ev.GATE_REJECTED,
                    project=gate.project, run_id=gate.run_id, step_id=gate.step_id,
                    payload={"gate_id": gate_id, "decided_by": actor, "note": note})
        await session.commit()
        workflow_id = run.temporal_workflow_id if run else ""

    # Signal the waiting workflow. Advisory gates (e.g. remediation approval on
    # an already-finished run) have no live workflow — decision is still
    # recorded and audited above.
    if workflow_id:
        try:
            client = await temporal_client()
            handle = client.get_workflow_handle(workflow_id)
            await handle.signal("gate_decision", {"gate_id": gate_id, "decision": decision, "actor": actor, "note": note})
        except Exception:
            log.info("gate %s resolved with no live workflow to signal (%s)", gate_id, workflow_id)

    # Remediation-approval bookkeeping (Phase 2/3)
    async with get_session() as session:
        gate = await session.get(Gate, gate_id)
        if gate and gate.gate_type == "remediation_approval":
            from bosun.db import Incident
            incident = (
                await session.execute(select(Incident).where(Incident.gate_id == gate_id))
            ).scalars().first()
            if incident:
                incident.status = "approved" if decision == "approve" else "rejected"
                if decision == "approve":
                    from bosun.temporal.activities import _emit as emit_event
                    from bosun import events as ev
                    await emit_event(session, event_type=ev.REMEDIATION_APPROVED,
                                     project=gate.project, run_id=gate.run_id, step_id=gate.step_id,
                                     payload={"incident_id": incident.id, "approved_by": actor})
                await session.commit()
    return {"gate_id": gate_id, "status": "approved" if decision == "approve" else "rejected"}


@app.get("/projects/{project}/gates")
async def list_gates(project: str, status: str = "pending", _: Project = Depends(require_project)) -> list[dict]:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Gate).where(Gate.project == project, Gate.status == status).order_by(Gate.created_at)
            )
        ).scalars().all()
    return [
        {"gate_id": g.id, "run_id": g.run_id, "step_id": g.step_id, "type": g.gate_type,
         "prompt": g.prompt, "created_at": g.created_at, "expires_at": g.expires_at}
        for g in rows
    ]


@app.post("/gates/{gate_id}/resolve")
async def resolve_gate(gate_id: str, body: GateResolve, x_api_key: str = Header(default="")) -> dict:
    async with get_session() as session:
        gate = await session.get(Gate, gate_id)
        if gate is None:
            raise HTTPException(404, "gate not found")
        project = await session.get(Project, gate.project)
    if auth_enabled() and (project is None or project.api_key != x_api_key):
        raise HTTPException(403, "invalid API key for gate's project")
    return await _apply_gate_decision(gate_id, body.decision, body.actor, body.note)


@app.get("/gate-action", response_class=HTMLResponse)
async def gate_action(token: str) -> str:
    """Signed single-use token path (the links in Teams cards / logs)."""
    try:
        action = verify_gate_token(token)
    except TokenError as e:
        raise HTTPException(403, str(e))
    # Burn the nonce — single use, replay-protected.
    async with get_session() as session:
        if await session.get(GateTokenNonce, action.nonce):
            raise HTTPException(409, "token already used")
        session.add(GateTokenNonce(nonce=action.nonce, gate_id=action.gate_id))
        await session.commit()
    result = await _apply_gate_decision(action.gate_id, action.decision, actor="token-link", note="")
    return f"<h2>Gate {action.gate_id}</h2><p>Decision recorded: <b>{result['status']}</b></p>"


# ---- schedules ----------------------------------------------------------------
@app.post("/projects/{project}/pipelines/{pipeline}/schedule", status_code=201)
async def create_schedule(project: str, pipeline: str, _: Project = Depends(require_project)) -> dict:
    """Calendar-anchored kickoff from the spec's `schedule.cron`."""
    async with get_session() as session:
        record = (
            await session.execute(
                select(PipelineSpecRecord)
                .where(PipelineSpecRecord.project == project, PipelineSpecRecord.name == pipeline)
                .order_by(PipelineSpecRecord.version.desc()).limit(1)
            )
        ).scalars().first()
    if record is None:
        raise HTTPException(404, "pipeline not found")
    spec = validate_spec_dict(record.spec)
    if spec.schedule is None:
        raise HTTPException(422, "spec has no schedule.cron")

    from bosun.temporal.workflows import PipelineRunWorkflow, RunInput

    schedule_id = f"bosun-sched-{project}-{pipeline}"
    client = await temporal_client()
    # Note: scheduled runs get run rows created lazily by the workflow start —
    # Phase 1 keeps schedule-created runs visible in Temporal UI; DB row is
    # created on first activity. For simplicity we schedule with a fixed run id
    # placeholder replaced per execution by Temporal's schedule action ids.
    await client.create_schedule(
        schedule_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                PipelineRunWorkflow.run,
                RunInput(run_id="scheduled", project=project, pipeline=pipeline,
                         spec=spec.model_dump(by_alias=False), param_overrides={}),
                id=f"bosun-sched-run-{project}-{pipeline}",
                task_queue=TASK_QUEUE,
            ),
            spec=TScheduleSpec(cron_expressions=[spec.schedule.cron]),
        ),
    )
    return {"schedule_id": schedule_id, "cron": spec.schedule.cron}


@app.delete("/projects/{project}/pipelines/{pipeline}/schedule")
async def delete_schedule(project: str, pipeline: str, _: Project = Depends(require_project)) -> dict:
    client = await temporal_client()
    handle = client.get_schedule_handle(f"bosun-sched-{project}-{pipeline}")
    await handle.delete()
    return {"deleted": True}


# ---- Phase 2: knowledge (Qdrant semantic memory) ------------------------------
class KnowledgeCreate(BaseModel):
    kind: str  # runbook | incident | fix | doc
    title: str
    content: str
    source: str = ""


@app.post("/projects/{project}/knowledge", status_code=201)
async def add_knowledge(project: str, body: KnowledgeCreate, _: Project = Depends(require_project)) -> dict:
    """Write to Postgres + outbox; the relay indexes into Qdrant async."""
    from bosun.db import KnowledgeRecord
    from bosun.temporal.activities import _emit as emit_event
    from bosun import events as ev

    async with get_session() as session:
        record = KnowledgeRecord(project=project, kind=body.kind, title=body.title,
                                 content=body.content, source=body.source)
        session.add(record)
        await session.flush()
        await emit_event(session, event_type=ev.KNOWLEDGE_RECORD_CREATED, project=project,
                         run_id="", payload={"knowledge_record_id": record.id, "kind": body.kind})
        await session.commit()
        return {"id": record.id, "indexed": False, "note": "relay indexes into Qdrant asynchronously"}


@app.get("/projects/{project}/knowledge/{record_id}/detail")
async def get_knowledge(project: str, record_id: str, _: Project = Depends(require_project)) -> dict:
    from bosun.db import KnowledgeRecord

    async with get_session() as session:
        record = await session.get(KnowledgeRecord, record_id)
    if record is None or record.project != project:
        raise HTTPException(404, "knowledge record not found")
    return {"id": record.id, "kind": record.kind, "title": record.title,
            "content": record.content, "source": record.source,
            "indexed": record.indexed, "created_at": record.created_at}


@app.delete("/projects/{project}/knowledge/{record_id}")
async def delete_knowledge(project: str, record_id: str, _: Project = Depends(require_project)) -> dict:
    """Delete from Postgres AND drop the embedding from Qdrant."""
    from bosun.ai import knowledge
    from bosun.db import KnowledgeRecord

    async with get_session() as session:
        record = await session.get(KnowledgeRecord, record_id)
        if record is None or record.project != project:
            raise HTTPException(404, "knowledge record not found")
        await session.delete(record)
        await session.commit()
    try:
        await knowledge.delete_record(record_id)
    except Exception as e:
        # Row is gone; the vector may linger in Qdrant until it's reachable
        # again — surfaced so the caller knows.
        return {"deleted": True, "qdrant": f"warning, embedding not removed: {e}"}
    return {"deleted": True, "qdrant": "removed"}


@app.get("/projects/{project}/knowledge/search")
async def search_knowledge(project: str, q: str, kind: str | None = None, limit: int = 5,
                           _: Project = Depends(require_project)) -> list[dict]:
    from bosun.ai import knowledge

    return await knowledge.search(project, q, limit=limit, kind=kind)


# ---- Phase 2: agent registry ---------------------------------------------------
@app.get("/agents")
async def list_agents() -> list[dict]:
    from bosun.db import AgentRecord

    async with get_session() as session:
        rows = (await session.execute(select(AgentRecord))).scalars().all()
    return [
        {"id": a.id, "name": a.name, "capabilities": a.capabilities,
         "requires_approval": a.requires_approval, "active": a.active}
        for a in rows
    ]


class AgentToggle(BaseModel):
    active: bool


@app.post("/agents/{agent_id}/toggle")
async def toggle_agent(agent_id: str, body: AgentToggle, x_admin_key: str = Header(default="")) -> dict:
    admin_key = os.environ.get("BOSUN_ADMIN_KEY", "")
    if admin_key and x_admin_key != admin_key:
        raise HTTPException(403, "invalid admin key")
    from bosun.db import AgentRecord

    async with get_session() as session:
        agent = await session.get(AgentRecord, agent_id)
        if agent is None:
            raise HTTPException(404, "agent not found")
        agent.active = body.active
        await session.commit()
    return {"id": agent_id, "active": body.active}


# ---- Phase 2: troubleshooting + incidents --------------------------------------
class TroubleshootRequest(BaseModel):
    gate_channel: str = "log"
    channel_params: dict = {}


@app.post("/runs/{run_id}/steps/{step_id}/troubleshoot")
async def troubleshoot_step(run_id: str, step_id: str, body: TroubleshootRequest | None = None,
                            run: Run = Depends(project_for_run)) -> dict:
    """Full Phase-2 flow: bundle → retrieval → LangGraph RCA → remediation
    proposal → remediation-approval gate. Read-only; nothing executes."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY not set")
    from bosun.ai.troubleshoot import run_troubleshooting

    body = body or TroubleshootRequest()
    try:
        return await run_troubleshooting(run_id, step_id, body.gate_channel, body.channel_params)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: str, x_api_key: str = Header(default="")) -> dict:
    from bosun.db import Incident

    async with get_session() as session:
        incident = await session.get(Incident, incident_id)
        if incident is None:
            raise HTTPException(404, "incident not found")
        project = await session.get(Project, incident.project)
    if auth_enabled() and (project is None or project.api_key != x_api_key):
        raise HTTPException(403, "invalid API key")
    return {
        "id": incident.id, "project": incident.project, "run_id": incident.run_id,
        "step_id": incident.step_id, "status": incident.status, "rca": incident.rca,
        "remediation_proposal": incident.remediation_proposal,
        "similar_incidents": incident.similar_incidents, "gate_id": incident.gate_id,
        "outcome": incident.outcome, "reduced_log": incident.reduced_log,
        "created_at": incident.created_at,
    }


# ---- Phase 2.5: learning capture ------------------------------------------------
class OutcomeReport(BaseModel):
    outcome: str  # what actually fixed it / what happened
    success: bool


@app.post("/incidents/{incident_id}/outcome")
async def report_outcome(incident_id: str, body: OutcomeReport, x_api_key: str = Header(default="")) -> dict:
    """Learning capture: successful remediation → knowledge record →
    (via outbox relay) Qdrant embedding. Failure signature + fix, reusable
    by future retrievals."""
    from bosun.db import Incident, KnowledgeRecord
    from bosun.temporal.activities import _emit as emit_event
    from bosun import events as ev

    async with get_session() as session:
        incident = await session.get(Incident, incident_id)
        if incident is None:
            raise HTTPException(404, "incident not found")
        project = await session.get(Project, incident.project)
        if auth_enabled() and (project is None or project.api_key != x_api_key):
            raise HTTPException(403, "invalid API key")
        incident.outcome = body.outcome
        incident.status = "executed" if body.success else "failed"
        record_id = ""
        if body.success:
            failure_sig = (incident.bundle.get("executions") or [{}])[-1].get("error", "")[:500]
            record = KnowledgeRecord(
                project=incident.project, kind="fix",
                title=f"Fix: {incident.step_id} — {failure_sig[:80] or 'failure'}",
                content=(f"Pipeline: {incident.bundle.get('pipeline')}\nStep: {incident.step_id}\n"
                         f"Failure signature: {failure_sig}\n\nRoot cause:\n{incident.rca[:1500]}\n\n"
                         f"What fixed it:\n{body.outcome}"),
                source=f"incident:{incident.id}",
            )
            session.add(record)
            await session.flush()
            record_id = record.id
            await emit_event(session, event_type=ev.KNOWLEDGE_RECORD_CREATED, project=incident.project,
                             run_id=incident.run_id, step_id=incident.step_id,
                             payload={"knowledge_record_id": record_id, "kind": "fix"})
        await session.commit()
    return {"incident_id": incident_id, "status": "executed" if body.success else "failed",
            "knowledge_record_id": record_id}


# ---- Phase 3: playbooks + sandboxed execution ------------------------------------
class PlaybookCreate(BaseModel):
    name: str
    description: str = ""
    image: str
    command: list[str]
    allowed_params: dict = {}
    approved_by: str


@app.post("/projects/{project}/playbooks", status_code=201)
async def register_playbook(project: str, body: PlaybookCreate, _: Project = Depends(require_project)) -> dict:
    from bosun.db import Playbook

    async with get_session() as session:
        current = (
            await session.execute(
                select(Playbook.version).where(Playbook.project == project, Playbook.name == body.name)
                .order_by(Playbook.version.desc()).limit(1)
            )
        ).scalar_one_or_none()
        playbook = Playbook(project=project, name=body.name, version=(current or 0) + 1,
                            description=body.description, image=body.image, command=body.command,
                            allowed_params=body.allowed_params, approved_by=body.approved_by)
        session.add(playbook)
        await session.commit()
        return {"id": playbook.id, "name": body.name, "version": playbook.version}


@app.get("/projects/{project}/playbooks")
async def list_playbooks(project: str, _: Project = Depends(require_project)) -> list[dict]:
    from bosun.db import Playbook

    async with get_session() as session:
        rows = (
            await session.execute(select(Playbook).where(Playbook.project == project, Playbook.active))
        ).scalars().all()
    return [{"name": p.name, "version": p.version, "image": p.image,
             "description": p.description, "allowed_params": p.allowed_params,
             "approved_by": p.approved_by} for p in rows]


class ExecuteRemediation(BaseModel):
    playbook: str
    params: dict = {}
    namespace: str = ""
    timeout_seconds: int = 600


@app.post("/incidents/{incident_id}/execute", status_code=202)
async def execute_remediation(incident_id: str, body: ExecuteRemediation,
                              x_api_key: str = Header(default="")) -> dict:
    """Phase 3 execution path. Requires: remediation gate APPROVED, playbook
    registered+active, params within the playbook's allow-list. Runs the
    playbook (immutable image + fixed command) in the hardened sandbox via
    RemediationWorkflow — never free-form AI output."""
    from bosun.db import Incident, Playbook

    async with get_session() as session:
        incident = await session.get(Incident, incident_id)
        if incident is None:
            raise HTTPException(404, "incident not found")
        project = await session.get(Project, incident.project)
        if auth_enabled() and (project is None or project.api_key != x_api_key):
            raise HTTPException(403, "invalid API key")
        gate = await session.get(Gate, incident.gate_id) if incident.gate_id else None
        if gate is None or gate.status != "approved":
            raise HTTPException(409, "remediation gate is not approved")
        playbook = (
            await session.execute(
                select(Playbook).where(Playbook.project == incident.project,
                                       Playbook.name == body.playbook, Playbook.active)
                .order_by(Playbook.version.desc()).limit(1)
            )
        ).scalars().first()
        if playbook is None:
            raise HTTPException(404, f"no active playbook '{body.playbook}' in project")
        illegal = set(body.params) - set(playbook.allowed_params)
        if illegal:
            raise HTTPException(422, f"params not in playbook allow-list: {sorted(illegal)}")
        approved_by = gate.decided_by

    from bosun.temporal.workflows import RemediationWorkflow, RemediationInput
    from bosun import TASK_QUEUE

    client = await temporal_client()
    env = {f"PARAM_{k.upper()}": str(v) for k, v in body.params.items()}
    handle = await client.start_workflow(
        RemediationWorkflow.run,
        RemediationInput(incident_id=incident_id, project=incident.project,
                         run_id=incident.run_id, step_id=incident.step_id,
                         playbook_name=f"{playbook.name}@v{playbook.version}",
                         image=playbook.image, command=playbook.command, env=env,
                         namespace=body.namespace, timeout_seconds=body.timeout_seconds,
                         approved_by=approved_by, correlation_id=uuid.uuid4().hex),
        id=f"bosun-remediation-{incident_id}",
        task_queue=TASK_QUEUE,
    )
    return {"workflow_id": handle.id, "playbook": f"{playbook.name}@v{playbook.version}",
            "approved_by": approved_by}


# ---- Chat assistant --------------------------------------------------------------
class ChatMessage(BaseModel):
    role: str  # user | assistant
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]


@app.post("/projects/{project}/chat")
async def project_chat(project: str, body: ChatRequest, _: Project = Depends(require_project)) -> dict:
    """Platform assistant: read-only tools over runs/specs/incidents/knowledge/
    health + spec linting. Proposes YAML; humans apply it."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY not set")
    if not body.messages or body.messages[-1].role != "user":
        raise HTTPException(422, "last message must be from the user")
    from bosun.ai.chat import chat as run_chat

    return await run_chat(project, [m.model_dump() for m in body.messages])


# ---- AI advisor (read-only, Phase 2 seed) -------------------------------------
@app.post("/runs/{run_id}/steps/{step_id}/diagnose")
async def diagnose_step(run_id: str, step_id: str, run: Run = Depends(project_for_run)) -> dict:
    from bosun.advisor.diagnose import advisor_available, diagnose

    if not advisor_available():
        raise HTTPException(503, "advisor unavailable: ANTHROPIC_API_KEY not set")
    async with get_session() as session:
        steps = (
            await session.execute(
                select(StepExecution).where(StepExecution.run_id == run_id, StepExecution.step_id == step_id)
            )
        ).scalars().all()
        events = (
            await session.execute(
                select(Event).where(Event.run_id == run_id, Event.step_id == step_id).order_by(Event.created_at)
            )
        ).scalars().all()
        params = (
            await session.execute(select(ResolvedParameter).where(ResolvedParameter.run_id == run_id))
        ).scalars().all()
    if not steps:
        raise HTTPException(404, "no executions recorded for this step")
    bundle = {
        "pipeline": run.pipeline, "run_id": run_id, "step_id": step_id,
        "executions": [
            {"unit": s.unit, "attempt": s.attempt, "status": s.status, "executor": s.executor,
             "error": s.error, "logs_tail": s.logs_tail[-4000:],
             "started_at": s.started_at, "completed_at": s.completed_at}
            for s in steps
        ],
        "events": [{"type": e.event_type, "payload": e.payload, "at": e.created_at} for e in events],
        "resolved_parameters": [{"name": p.name, "value": p.value.get("value"), "provenance": p.provenance} for p in params],
    }
    return await diagnose(bundle)


# ---- UI (built SPA served same-origin at /ui) ----------------------------------
_UI_DIST = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "ui", "dist")
if os.path.isdir(_UI_DIST):
    from fastapi.staticfiles import StaticFiles

    app.mount("/ui", StaticFiles(directory=_UI_DIST, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    async def _root_redirect():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/ui/")


def main() -> None:
    import uvicorn

    uvicorn.run("bosun.api.main:app", host="0.0.0.0", port=int(os.environ.get("BOSUN_API_PORT", "8400")))


if __name__ == "__main__":
    main()
