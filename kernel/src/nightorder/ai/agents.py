"""LangGraph multi-agent layer (Phase 2). Stateless reasoning (P3):
invoked per-incident, holds no workflow state, produces advisory text with
evidence references. All routing flows through the Supervisor node; agents
never call each other directly.

Deterministic guardrails (P6):
- Knowledge Retrieval queries Qdrant deterministically (no LLM).
- Troubleshooting/Remediation output is advisory; no pass/fail, no execution.
- No confidence scores — evidence references only.
"""
from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from nightorder.config import settings


class IncidentState(TypedDict, total=False):
    bundle: dict
    active_agents: list[str]
    retrieval: list[dict]  # similar incidents / runbook hits from Qdrant
    rca: str
    remediation_proposal: str
    gate_prompt: str
    visited: list[str]


async def _claude(system: str, user: str, max_tokens: int = 1200) -> str:
    import anthropic

    client = anthropic.AsyncAnthropic()
    msg = await client.messages.create(
        model=settings().anthropic_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if b.type == "text")


def _bundle_excerpt(bundle: dict, limit: int = 14000) -> str:
    return json.dumps(bundle, default=str, indent=1)[:limit]


# ---- agents -------------------------------------------------------------------
async def knowledge_retrieval(state: IncidentState) -> dict:
    """Deterministic Qdrant lookup — no LLM involved."""
    from nightorder.ai import knowledge

    bundle = state["bundle"]
    query = f"{bundle.get('step_id', '')} {bundle.get('executions', [{}])[-1].get('error', '')} {bundle.get('reduced_log', '')[:500]}"
    try:
        hits = await knowledge.search(bundle["project"], query, limit=5)
    except Exception:
        hits = []
    return {"retrieval": hits, "visited": state.get("visited", []) + ["knowledge_retrieval"]}


async def troubleshooting(state: IncidentState) -> dict:
    """RCA over the incident bundle + retrieved knowledge. Evidence links only."""
    hits = "\n".join(f"- [{h['kind']}] {h['title']}: {h['snippet'][:200]}" for h in state.get("retrieval", []))
    rca = await _claude(
        system=(
            "You are the Troubleshooting agent in an orchestration platform. "
            "Analyze the incident bundle and determine the probable root cause. "
            "Cite specific evidence (log lines, check evidence, k8s events, parameter values) for every claim. "
            "Never invent facts, never output confidence percentages. "
            "If similar past incidents are provided, say whether they match and why."
        ),
        user=f"Incident bundle:\n{_bundle_excerpt(state['bundle'])}\n\nSimilar past incidents/runbooks:\n{hits or '(none found)'}",
    )
    return {"rca": rca, "visited": state.get("visited", []) + ["troubleshooting"]}


async def remediation_planning(state: IncidentState) -> dict:
    """Proposes remediation. MAY NOT execute; output goes behind a human gate."""
    proposal = await _claude(
        system=(
            "You are the Remediation Planning agent. Based on the root-cause analysis, "
            "propose a concrete remediation plan as numbered steps. Each step must be "
            "specific (exact commands/resources) and reversible where possible. "
            "You cannot execute anything — a human will review. If a pre-approved playbook "
            "obviously applies (from the similar-incidents list), name it. "
            "End with 'Verification:' — how to confirm the fix worked deterministically."
        ),
        user=f"RCA:\n{state.get('rca', '')}\n\nBundle excerpt:\n{_bundle_excerpt(state['bundle'], 6000)}",
    )
    return {"remediation_proposal": proposal, "visited": state.get("visited", []) + ["remediation_planning"]}


async def human_interaction(state: IncidentState) -> dict:
    """Drafts the gate-request *content* only — action tokens/routing are
    minted by the deterministic kernel, never here."""
    prompt = await _claude(
        system=(
            "You are the Human Interaction agent. Draft a concise approval-request message "
            "(<150 words) for an on-call engineer: what failed, probable cause in one line, "
            "proposed remediation in 2-3 lines, what approving will do (record approval; "
            "execution only via pre-approved playbooks). Plain text."
        ),
        user=f"Step: {state['bundle'].get('step_id')}\nRCA:\n{state.get('rca', '')[:2000]}\n\nProposal:\n{state.get('remediation_proposal', '')[:2000]}",
        max_tokens=400,
    )
    return {"gate_prompt": prompt, "visited": state.get("visited", []) + ["human_interaction"]}


AGENT_NODES = {
    "knowledge_retrieval": knowledge_retrieval,
    "troubleshooting": troubleshooting,
    "remediation_planning": remediation_planning,
    "human_interaction": human_interaction,
}

_ORDER = ["knowledge_retrieval", "troubleshooting", "remediation_planning", "human_interaction"]


def supervisor(state: IncidentState) -> dict:
    return state  # routing happens in the conditional edge


def _route(state: IncidentState) -> str:
    active = state.get("active_agents") or _ORDER
    visited = set(state.get("visited", []))
    for agent in _ORDER:
        if agent in active and agent not in visited:
            return agent
    return END


def build_incident_graph():
    graph = StateGraph(IncidentState)
    graph.add_node("supervisor", supervisor)
    for name, fn in AGENT_NODES.items():
        graph.add_node(name, fn)
        graph.add_edge(name, "supervisor")  # agents always return to supervisor
    graph.add_conditional_edges("supervisor", _route, {**{n: n for n in AGENT_NODES}, END: END})
    graph.set_entry_point("supervisor")
    return graph.compile()


async def analyze_incident(bundle: dict, active_agents: list[str] | None = None) -> IncidentState:
    graph = build_incident_graph()
    result: IncidentState = await graph.ainvoke(
        {"bundle": bundle, "active_agents": active_agents or _ORDER, "visited": []}
    )
    return result


# Default Agent Registry entries (Phase 1 scaffold, Phase 2 activation).
DEFAULT_AGENTS = [
    {"id": "supervisor", "name": "Supervisor", "capabilities": {"routing": True}, "requires_approval": False},
    {"id": "knowledge_retrieval", "name": "Knowledge Retrieval", "capabilities": {"qdrant_search": True}, "requires_approval": False},
    {"id": "troubleshooting", "name": "Troubleshooting", "capabilities": {"rca": True}, "requires_approval": False},
    {"id": "remediation_planning", "name": "Remediation Planning", "capabilities": {"propose_only": True}, "requires_approval": True},
    {"id": "human_interaction", "name": "Human Interaction", "capabilities": {"draft_content": True}, "requires_approval": False},
    {"id": "argo_operations", "name": "Argo Operations", "capabilities": {"explain_status": True}, "requires_approval": False},
    {"id": "qa_analysis", "name": "QA Analysis", "capabilities": {"explain_failures": True, "may_decide_pass_fail": False}, "requires_approval": False},
    {"id": "learning", "name": "Learning", "capabilities": {"capture_outcomes": True}, "requires_approval": False},
]
