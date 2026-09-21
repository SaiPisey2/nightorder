"""Troubleshooting workflow (Phase 2, read-only):

failure → Incident Bundle → Qdrant retrieval → LangGraph analysis → RCA with
evidence → remediation proposal → remediation-approval gate (Teams/webhook/log).

Nothing here executes anything. Approval is recorded; execution (Phase 3) only
happens via pre-approved playbooks through the sandbox path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from nightorder import events as ev
from nightorder.ai.agents import analyze_incident
from nightorder.ai.bundle import build_incident_bundle
from nightorder.api.security import mint_gate_token
from nightorder.config import settings
from nightorder.contracts import load_registry
from nightorder.contracts.base import GateNotification
from nightorder.db import AgentRecord, Gate, Incident, Run, get_session
from nightorder.temporal.activities import _emit


async def _active_agents() -> list[str]:
    async with get_session() as session:
        rows = (await session.execute(select(AgentRecord).where(AgentRecord.active))).scalars().all()
    return [r.id for r in rows] or []


async def run_troubleshooting(run_id: str, step_id: str, gate_channel: str = "log",
                              channel_params: dict | None = None) -> dict:
    bundle = await build_incident_bundle(run_id, step_id)
    project = bundle["project"]

    # Persist incident + IncidentBundleCreated before analysis (bundle survives
    # even if the LLM layer is down).
    async with get_session() as session:
        incident = Incident(project=project, run_id=run_id, step_id=step_id,
                            bundle=bundle, reduced_log=bundle.get("reduced_log", ""))
        session.add(incident)
        await session.flush()
        incident_id = incident.id
        await _emit(session, event_type=ev.INCIDENT_BUNDLE_CREATED, project=project,
                    run_id=run_id, step_id=step_id, payload={"incident_id": incident_id})
        await session.commit()

    active = await _active_agents()
    result = await analyze_incident(bundle, active_agents=active or None)

    # Remediation-approval gate: content drafted by AI, mechanics by kernel.
    gate_prompt = result.get("gate_prompt") or (
        f"Remediation proposed for {bundle['pipeline']}/{step_id} (run {run_id}). Review incident {incident_id}."
    )
    async with get_session() as session:
        gate = Gate(run_id=run_id, step_id=step_id, project=project,
                    gate_type="remediation_approval",
                    prompt=gate_prompt,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=24))
        session.add(gate)
        await session.flush()
        gate_id = gate.id
        incident = await session.get(Incident, incident_id)
        incident.rca = result.get("rca", "")
        incident.remediation_proposal = result.get("remediation_proposal", "")
        incident.similar_incidents = {"hits": result.get("retrieval", [])}
        incident.gate_id = gate_id
        incident.status = "remediation_proposed"
        await _emit(session, event_type=ev.REMEDIATION_SUGGESTED, project=project,
                    run_id=run_id, step_id=step_id,
                    payload={"incident_id": incident_id, "gate_id": gate_id})
        await _emit(session, event_type=ev.GATE_REQUESTED, project=project,
                    run_id=run_id, step_id=step_id,
                    payload={"gate_id": gate_id, "gate_type": "remediation_approval"})
        await session.commit()

    base = settings().api_base_url.rstrip("/")
    notification = GateNotification(
        gate_id=gate_id, gate_type="remediation_approval", prompt=gate_prompt,
        approve_url=f"{base}/gate-action?token={mint_gate_token(gate_id, 'approve')}",
        reject_url=f"{base}/gate-action?token={mint_gate_token(gate_id, 'reject')}",
        context={"incident_id": incident_id},
    )
    channel = load_registry().gate_channels.get(gate_channel)
    if channel is not None:
        try:
            await channel.deliver(notification, channel_params or {})
        except Exception:
            pass  # gate stays resolvable via API

    return {
        "incident_id": incident_id,
        "gate_id": gate_id,
        "rca": result.get("rca", ""),
        "remediation_proposal": result.get("remediation_proposal", ""),
        "similar_incidents": result.get("retrieval", []),
        "agents_visited": result.get("visited", []),
        "advisory_only": True,
    }
