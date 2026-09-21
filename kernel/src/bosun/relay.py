"""Outbox relay (transactional-outbox consumer).

Tails the `outbox` table and dispatches events to downstream sinks:
- KnowledgeRecordCreated → index the record into Qdrant (async, never a
  synchronous dual-write from the request path).
- Everything else → marked published (Kafka slot-in point: replace
  `_publish_noop` with a Kafka producer without touching any producer code).

Run as its own process: `bosun-relay`.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from bosun.db import Event, KnowledgeRecord, Outbox, get_session, init_db

log = logging.getLogger("bosun.relay")

POLL_SECONDS = 2.0
BATCH = 50


def _alert_config() -> tuple[str, set[str], list[str]]:
    from bosun.config import settings

    cfg = settings()
    events = {e.strip() for e in cfg.teams_alert_events.split(",") if e.strip()}
    mentions = [m.strip() for m in cfg.teams_alert_mentions.split(",") if m.strip()]
    return cfg.teams_webhook_url, events, mentions


_NOTIFY_EVENT_MAP = {
    "StepStarted": ("started", "▶️ Step started", "accent"),
    "StepCompleted": ("succeeded", "✅ Step succeeded", "good"),
    "StepFailed": ("failed", "🔴 Step failed", "attention"),
    "StepRerunTriggered": ("rerun", "🔁 Step re-run triggered", "warning"),
}


async def _step_notification(event: Event) -> bool:
    """Per-step `notify:` config from the spec (rides in the event payload).
    Returns True if this event was handled as a step notification."""
    payload = event.payload or {}
    notify = payload.get("notify")
    mapping = _NOTIFY_EVENT_MAP.get(event.event_type)
    if not notify or not mapping:
        return False
    trigger, title, color = mapping
    if trigger not in (notify.get("on") or []):
        return True  # configured, but not for this event — swallow, no global fallback
    from bosun.config import settings

    cfg = settings()
    webhook_url = notify.get("webhook_url") or cfg.teams_webhook_url
    if not webhook_url:
        log.warning("step notify configured but no Teams webhook available (event %s)", event.id)
        return True
    from bosun.teams import build_adaptive_card, post_to_teams

    facts = {"pipeline": payload.get("pipeline", ""), "step": event.step_id,
             "run": event.run_id[:12], "project": event.project}
    if payload.get("unit"):
        facts["unit"] = payload["unit"]
    if payload.get("attempt", 1) > 1:
        facts["attempt"] = str(payload["attempt"])
    if payload.get("error"):
        facts["error"] = str(payload["error"])[:200]
    message = build_adaptive_card(
        title=title,
        body=f"**{event.step_id}** in pipeline **{payload.get('pipeline') or event.project}**.",
        facts={k: v for k, v in facts.items() if v},
        buttons=[{"title": "Open Bosun", "url": cfg.api_base_url.rstrip("/") + "/ui/"}],
        mentions=notify.get("mentions") or [],
        color=color if color != "accent" else "default",
    )
    await post_to_teams(webhook_url, message)
    log.info("teams step notification sent: %s %s (%s)", event.event_type, event.step_id, event.id)
    return True


async def _teams_alert(event: Event) -> None:
    """Forward selected events to the Teams channel as alert cards."""
    if await _step_notification(event):
        return  # step-level notify handled (or deliberately suppressed) it
    webhook_url, alert_events, mentions = _alert_config()
    if not webhook_url or event.event_type not in alert_events:
        return
    # Gates delivered via the teams channel already posted their own card
    # (with Approve/Reject buttons) — don't double-post.
    if event.event_type == "GateRequested" and (event.payload or {}).get("channel") == "teams":
        return
    from bosun.config import settings
    from bosun.teams import build_adaptive_card, post_to_teams

    titles = {
        "WorkflowFailed": ("🔴 Pipeline run failed", "attention"),
        "StepFailed": ("🔴 Step failed", "attention"),
        "GateRequested": ("🚧 Approval waiting", "warning"),
        "GateExpired": ("⏰ Gate expired without a decision", "attention"),
        "RemediationSuggested": ("🛠 AI remediation proposed", "warning"),
        "PreflightCheckFailed": ("⛔ Preflight check failed", "attention"),
    }
    title, color = titles.get(event.event_type, (event.event_type, "default"))
    payload = event.payload or {}
    facts = {"project": event.project, "run": event.run_id[:12]}
    if event.step_id:
        facts["step"] = event.step_id
    for key in ("gate_type", "error", "check", "evidence", "incident_id"):
        if payload.get(key):
            facts[key] = str(payload[key])[:200]
    ui = settings().api_base_url.rstrip("/") + "/ui/"
    message = build_adaptive_card(
        title=title,
        body=f"{event.event_type} in project **{event.project}**.",
        facts=facts,
        buttons=[{"title": "Open Bosun", "url": ui}],
        mentions=mentions,
        color=color,
    )
    await post_to_teams(webhook_url, message)
    log.info("teams alert sent for %s (%s)", event.event_type, event.id)


async def _index_knowledge(event: Event) -> None:
    from bosun.ai import knowledge

    record_id = event.payload.get("knowledge_record_id")
    if not record_id:
        return
    async with get_session() as session:
        record = await session.get(KnowledgeRecord, record_id)
    if record is None:
        return
    await knowledge.index_record(record.id, record.project, record.kind,
                                 record.title, record.content, record.source)
    async with get_session() as session:
        record = await session.get(KnowledgeRecord, record_id)
        record.indexed = True
        await session.commit()
    log.info("indexed knowledge record %s (%s)", record_id, record.title[:60])


async def process_once() -> int:
    """One outbox sweep; returns number of rows published. Importable for tests."""
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Outbox, Event).join(Event, Event.id == Outbox.event_id)
                .where(Outbox.published.is_(False)).order_by(Outbox.created_at).limit(BATCH)
            )
        ).all()
    done = 0
    for outbox_row, event in rows:
        try:
            if event.event_type == "KnowledgeRecordCreated":
                await _index_knowledge(event)
            try:
                await _teams_alert(event)  # best-effort: never blocks the outbox
            except Exception:
                log.exception("teams alert failed for %s — continuing", event.id)
            # Kafka producer goes here in a later phase
            async with get_session() as session:
                row = await session.get(Outbox, outbox_row.id)
                row.published = True
                from bosun.db.models import utcnow

                row.published_at = utcnow()
                await session.commit()
            done += 1
        except Exception:
            log.exception("relay failed for event %s (%s) — will retry", event.id, event.event_type)
    return done


async def run_relay() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    await init_db()
    log.info("outbox relay started")
    while True:
        try:
            processed = await process_once()
        except Exception:
            log.exception("relay sweep failed")
            processed = 0
        if processed < BATCH:
            await asyncio.sleep(POLL_SECONDS)


def main() -> None:
    asyncio.run(run_relay())


if __name__ == "__main__":
    main()
