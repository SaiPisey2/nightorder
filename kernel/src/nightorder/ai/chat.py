"""Platform chat assistant (Phase 2 extension).

Claude with read-only platform tools in an agentic loop: run/pipeline status,
step details, events, incidents, knowledge search, component health, spec
validation. Deliberately cannot write: it proposes spec YAML / actions, the
human applies them through the UI or API (P4/P6 — humans decide, AI advises).

Stateless: the client sends the whole message history each turn.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from nightorder.config import settings
from nightorder.contracts import load_registry
from nightorder.db import (
    Event,
    Gate,
    Incident,
    PipelineSpecRecord,
    Playbook,
    ResolvedParameter,
    Run,
    StepExecution,
    get_session,
)

MAX_TOOL_ITERATIONS = 8

SYSTEM = """You are the Nightorder assistant — embedded in an enterprise workflow \
orchestration platform (declarative pipeline specs interpreted by Temporal; Argo/script/SSH \
executors; preflight checks; human gates; fan-out; designed re-runs; Qdrant knowledge base; \
sandboxed playbook remediation).

You have READ-ONLY tools. Use them — never guess platform state. Multiple tool calls are fine.

You help users:
- Check pipeline/run status, step failures, timings, gates, events, incidents.
- Diagnose problems: correlate step errors, preflight failures, events, similar past incidents.
- Answer "if X happens, what's the fix?" — ALWAYS call search_knowledge with the symptom \
first (the knowledge base holds runbooks, past incidents, and captured fixes); base your \
answer on the hits and cite record titles/sources. If nothing relevant is found, say so \
explicitly, then reason from the platform state instead.
- Fix or author pipeline spec YAML. ALWAYS lint a proposed spec with validate_pipeline_spec \
before presenting it. Present final YAML in a ```yaml block; the user applies it via the \
spec editor — you cannot register anything yourself, say so if asked.
- Explain platform concepts (spec fields, executors, checks, resolvers, gates, fan-out, quotas).

Spec schema quick reference: apiVersion: nightorder/v1, kind: Pipeline, name, project, \
parameters[{name, resolver: static|expression|activity, value/expression/activity+params}], \
quotas[{name,max_concurrent}], steps[{id, executor, config, depends_on[], \
preflight[{check,params,on_fail:block|warn_gate}], gate{type,prompt,channel,timeout_minutes,on_timeout}, \
fan_out{over_param,max_concurrent,quota_pool,tiers}, repeat{until_check,params,max_iterations,delay_seconds}, \
retry{maximum_attempts}, timeout_minutes}]. Templating: {{params.x}} and {{item}} in config.

Be concise. Cite concrete evidence (step ids, error text, event names). If a run failed, \
check the failing step's error and events before answering."""


# ---- tool implementations -------------------------------------------------------
async def _list_pipelines(project: str) -> Any:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(PipelineSpecRecord.name, PipelineSpecRecord.version)
                .where(PipelineSpecRecord.project == project)
                .order_by(PipelineSpecRecord.name, PipelineSpecRecord.version)
            )
        ).all()
    latest: dict[str, int] = {}
    for name, version in rows:
        latest[name] = max(latest.get(name, 0), version)
    return [{"name": n, "latest_version": v} for n, v in latest.items()]


async def _get_pipeline_spec(project: str, name: str, version: int | None = None) -> Any:
    async with get_session() as session:
        q = select(PipelineSpecRecord).where(
            PipelineSpecRecord.project == project, PipelineSpecRecord.name == name
        )
        q = q.where(PipelineSpecRecord.version == version) if version else q.order_by(
            PipelineSpecRecord.version.desc()).limit(1)
        record = (await session.execute(q)).scalars().first()
    if record is None:
        return {"error": f"pipeline '{name}' not found in project '{project}'"}
    return {"name": record.name, "version": record.version, "spec": record.spec}


async def _validate_pipeline_spec(spec_yaml: str) -> Any:
    import yaml as pyyaml

    from nightorder.spec.validate import SpecValidationError, validate_spec_dict

    try:
        raw = pyyaml.safe_load(spec_yaml)
        validate_spec_dict(raw, load_registry())
        return {"valid": True, "errors": []}
    except SpecValidationError as e:
        return {"valid": False, "errors": e.errors}
    except Exception as e:
        return {"valid": False, "errors": [f"parse error: {e}"]}


async def _list_runs(project: str, limit: int = 15) -> Any:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Run).where(Run.project == project).order_by(Run.created_at.desc()).limit(limit)
            )
        ).scalars().all()
    return [
        {"run_id": r.id, "pipeline": r.pipeline, "version": r.spec_version, "status": r.status,
         "started_at": str(r.started_at), "completed_at": str(r.completed_at), "error": r.error[:200]}
        for r in rows
    ]


async def _get_run(run_id: str) -> Any:
    async with get_session() as session:
        run = await session.get(Run, run_id)
        if run is None:
            return {"error": "run not found"}
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
            await session.execute(select(Gate).where(Gate.run_id == run_id))
        ).scalars().all()
    return {
        "run_id": run.id, "project": run.project, "pipeline": run.pipeline,
        "version": run.spec_version, "status": run.status, "error": run.error[:500],
        "steps": [
            {"step_id": s.step_id, "unit": s.unit, "attempt": s.attempt, "status": s.status,
             "executor": s.executor, "error": s.error[:400], "external_ref": s.external_ref,
             "started_at": str(s.started_at), "completed_at": str(s.completed_at)}
            for s in steps
        ],
        "parameters": [{"name": p.name, "value": p.value.get("value"), "provenance": p.provenance} for p in params],
        "gates": [{"gate_id": g.id, "step_id": g.step_id, "type": g.gate_type, "status": g.status,
                   "prompt": g.prompt[:200], "decided_by": g.decided_by} for g in gates],
    }


async def _get_run_events(run_id: str, limit: int = 50) -> Any:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Event).where(Event.run_id == run_id).order_by(Event.created_at.desc()).limit(limit)
            )
        ).scalars().all()
    return [{"type": e.event_type, "step_id": e.step_id, "payload": e.payload, "at": str(e.created_at)}
            for e in rows]


async def _list_gates(project: str, status: str = "pending") -> Any:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Gate).where(Gate.project == project, Gate.status == status)
                .order_by(Gate.created_at.desc()).limit(20)
            )
        ).scalars().all()
    return [{"gate_id": g.id, "run_id": g.run_id, "step_id": g.step_id, "type": g.gate_type,
             "prompt": g.prompt[:200], "expires_at": str(g.expires_at)} for g in rows]


async def _list_incidents(project: str, limit: int = 10) -> Any:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Incident).where(Incident.project == project)
                .order_by(Incident.created_at.desc()).limit(limit)
            )
        ).scalars().all()
    return [{"incident_id": i.id, "run_id": i.run_id, "step_id": i.step_id, "status": i.status,
             "rca_preview": i.rca[:300]} for i in rows]


async def _get_incident(incident_id: str) -> Any:
    async with get_session() as session:
        i = await session.get(Incident, incident_id)
    if i is None:
        return {"error": "incident not found"}
    return {"incident_id": i.id, "run_id": i.run_id, "step_id": i.step_id, "status": i.status,
            "rca": i.rca, "remediation_proposal": i.remediation_proposal,
            "similar_incidents": i.similar_incidents, "outcome": i.outcome,
            "reduced_log": i.reduced_log[-3000:]}


async def _search_knowledge(project: str, query: str) -> Any:
    from nightorder.ai import knowledge

    try:
        return await knowledge.search(project, query, limit=5)
    except Exception as e:
        return {"error": f"knowledge search unavailable: {e}"}


async def _get_component_health() -> Any:
    from nightorder.api.main import health_components

    return await health_components()


async def _get_catalog() -> Any:
    return load_registry().catalog()


async def _list_playbooks(project: str) -> Any:
    async with get_session() as session:
        rows = (
            await session.execute(select(Playbook).where(Playbook.project == project, Playbook.active))
        ).scalars().all()
    return [{"name": p.name, "version": p.version, "description": p.description,
             "allowed_params": p.allowed_params} for p in rows]


async def _get_timings(project: str, pipeline: str) -> Any:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(StepExecution, Run.id).join(Run, Run.id == StepExecution.run_id)
                .where(Run.project == project, Run.pipeline == pipeline,
                       StepExecution.completed_at.isnot(None))
                .order_by(StepExecution.started_at.desc()).limit(100)
            )
        ).all()
    return [
        {"run_id": r[1][:8], "step_id": r[0].step_id, "unit": r[0].unit, "status": r[0].status,
         "duration_seconds": (r[0].completed_at - r[0].started_at).total_seconds()
         if r[0].started_at and r[0].completed_at else None}
        for r in rows
    ]


TOOLS: list[dict] = [
    {"name": "list_pipelines", "description": "List pipelines (with latest spec version) in the project.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_pipeline_spec", "description": "Get a pipeline's spec document (latest or specific version).",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "version": {"type": "integer"}},
                      "required": ["name"]}},
    {"name": "validate_pipeline_spec", "description": "Lint a pipeline spec YAML against the schema + registered extensions. Use before proposing YAML to the user.",
     "input_schema": {"type": "object", "properties": {"spec_yaml": {"type": "string"}}, "required": ["spec_yaml"]}},
    {"name": "list_runs", "description": "Recent runs in the project with status.",
     "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "get_run", "description": "Full run detail: steps (per-unit, per-attempt) with errors, resolved parameters with provenance, gates.",
     "input_schema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}},
    {"name": "get_run_events", "description": "Event audit trail for a run (StepFailed, PreflightCheckFailed, Gate*, ...).",
     "input_schema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}},
    {"name": "list_gates", "description": "Gates in the project by status (pending/approved/rejected/expired).",
     "input_schema": {"type": "object", "properties": {"status": {"type": "string"}}}},
    {"name": "list_incidents", "description": "Recent AI-analyzed incidents in the project.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_incident", "description": "Full incident: RCA, remediation proposal, similar incidents, reduced log.",
     "input_schema": {"type": "object", "properties": {"incident_id": {"type": "string"}}, "required": ["incident_id"]}},
    {"name": "search_knowledge", "description": "Semantic search over the project's knowledge base (runbooks, incidents, fixes).",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_component_health", "description": "Live health of platform components (postgres, temporal, worker, relay, qdrant, advisor).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_catalog", "description": "Registered extensions: step executors, preflight checks, parameter resolvers, gate channels.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_playbooks", "description": "Pre-approved remediation playbooks in the project.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_timings", "description": "Step timing history across runs of a pipeline.",
     "input_schema": {"type": "object", "properties": {"pipeline": {"type": "string"}}, "required": ["pipeline"]}},
]


async def _dispatch(project: str, name: str, args: dict) -> Any:
    if name == "list_pipelines":
        return await _list_pipelines(project)
    if name == "get_pipeline_spec":
        return await _get_pipeline_spec(project, args["name"], args.get("version"))
    if name == "validate_pipeline_spec":
        return await _validate_pipeline_spec(args["spec_yaml"])
    if name == "list_runs":
        return await _list_runs(project, int(args.get("limit", 15)))
    if name == "get_run":
        return await _get_run(args["run_id"])
    if name == "get_run_events":
        return await _get_run_events(args["run_id"])
    if name == "list_gates":
        return await _list_gates(project, args.get("status", "pending"))
    if name == "list_incidents":
        return await _list_incidents(project)
    if name == "get_incident":
        return await _get_incident(args["incident_id"])
    if name == "search_knowledge":
        return await _search_knowledge(project, args["query"])
    if name == "get_component_health":
        return await _get_component_health()
    if name == "get_catalog":
        return await _get_catalog()
    if name == "list_playbooks":
        return await _list_playbooks(project)
    if name == "get_timings":
        return await _get_timings(project, args["pipeline"])
    return {"error": f"unknown tool {name}"}


async def chat(project: str, messages: list[dict]) -> dict:
    """Run one assistant turn with an agentic tool loop.

    messages: [{role: user|assistant, content: str}] — client-held history.
    Returns {reply, tool_trace: [{tool, args}]}.
    """
    import anthropic

    client = anthropic.AsyncAnthropic()
    convo: list[dict] = [{"role": m["role"], "content": m["content"]} for m in messages]
    trace: list[dict] = []

    for _ in range(MAX_TOOL_ITERATIONS):
        response = await client.messages.create(
            model=settings().anthropic_model,
            max_tokens=2500,
            system=SYSTEM + f"\n\nCurrent project: {project}",
            tools=TOOLS,
            messages=convo,
        )
        if response.stop_reason != "tool_use":
            reply = "".join(b.text for b in response.content if b.type == "text")
            return {"reply": reply, "tool_trace": trace}

        convo.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                trace.append({"tool": block.name, "args": block.input})
                try:
                    result = await _dispatch(project, block.name, dict(block.input))
                except Exception as e:
                    result = {"error": str(e)[:500]}
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str)[:25000],
                })
        convo.append({"role": "user", "content": results})

    return {"reply": "I hit my tool-call limit for one turn — ask me to continue.", "tool_trace": trace}
