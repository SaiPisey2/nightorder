"""PostgreSQL system of record.

Everything project-scoped (logical multi-project isolation on shared infra).
Events use the transactional outbox pattern: event row + outbox row written in
one transaction; a relay (Kafka in a later phase) marks outbox rows published.
Temporal remains the sole orchestration authority (P1) — these tables are the
queryable record, never the state machine.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # slug
    display_name: Mapped[str] = mapped_column(String(255), default="")
    api_key: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PipelineSpecRecord(Base):
    __tablename__ = "pipeline_specs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    version: Mapped[int] = mapped_column(Integer)
    spec: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("project", "name", "version"),)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    pipeline: Mapped[str] = mapped_column(String(255), index=True)
    # Nullable: schedule-created runs bootstrap their own row on first activity.
    spec_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_specs.id"), nullable=True)
    spec_version: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    temporal_workflow_id: Mapped[str] = mapped_column(String(255), default="")
    param_overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StepExecution(Base):
    """One row per (step, unit, attempt) — the timing history product feature."""

    __tablename__ = "step_executions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    step_id: Mapped[str] = mapped_column(String(255), index=True)
    unit: Mapped[str] = mapped_column(String(255), default="")  # fan-out unit
    attempt: Mapped[int] = mapped_column(Integer, default=1)  # designed-rerun iteration (P8)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    executor: Mapped[str] = mapped_column(String(128), default="")
    external_ref: Mapped[str] = mapped_column(String(512), default="")
    logs_tail: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("run_id", "step_id", "unit", "attempt"),
        Index("ix_step_exec_run_step", "run_id", "step_id"),
    )


class ResolvedParameter(Base):
    __tablename__ = "resolved_parameters"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    value: Mapped[dict] = mapped_column(JSON)  # {"value": ...} wrapper — any JSON type
    provenance: Mapped[str] = mapped_column(Text, default="")
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("run_id", "name"),)


class Gate(Base):
    __tablename__ = "gates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    step_id: Mapped[str] = mapped_column(String(255))
    project: Mapped[str] = mapped_column(String(64), index=True)
    gate_type: Mapped[str] = mapped_column(String(64))
    prompt: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)  # pending|approved|rejected|expired|escalated
    decided_by: Mapped[str] = mapped_column(String(255), default="")
    decision_note: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GateTokenNonce(Base):
    """Single-use nonce store for signed gate action tokens (replay protection)."""

    __tablename__ = "gate_token_nonces"
    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    gate_id: Mapped[str] = mapped_column(String(36), index=True)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    schema_version: Mapped[str] = mapped_column(String(16), default="1.0")
    project: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    step_id: Mapped[str] = mapped_column(String(255), default="")
    correlation_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Incident(Base):
    """Phase 2: one incident per troubleshoot invocation on a failed step.
    Holds the bundle + reduced log view + AI analysis. Advisory only —
    never workflow state."""

    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    step_id: Mapped[str] = mapped_column(String(255))
    bundle: Mapped[dict] = mapped_column(JSON, default=dict)
    reduced_log: Mapped[str] = mapped_column(Text, default="")
    rca: Mapped[str] = mapped_column(Text, default="")  # root-cause analysis w/ evidence
    remediation_proposal: Mapped[str] = mapped_column(Text, default="")
    similar_incidents: Mapped[dict] = mapped_column(JSON, default=dict)  # retrieval hits
    gate_id: Mapped[str] = mapped_column(String(36), default="")  # remediation-approval gate
    status: Mapped[str] = mapped_column(String(32), default="analyzed", index=True)
    # analyzed | remediation_proposed | approved | rejected | executed | failed
    outcome: Mapped[str] = mapped_column(Text, default="")  # Phase 2.5 learning capture
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeRecord(Base):
    """Phase 2: system-of-record row for semantic memory. Embeddings are
    indexed into Qdrant asynchronously by the outbox relay — never a
    synchronous dual write."""

    __tablename__ = "knowledge_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # runbook|incident|fix|doc
    title: Mapped[str] = mapped_column(String(512))
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(512), default="")  # url / incident id
    indexed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRecord(Base):
    """Agent Registry: which AI agents exist, what they may do."""

    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    allowed_tools: Mapped[dict] = mapped_column(JSON, default=dict)
    execution_limits: Mapped[dict] = mapped_column(JSON, default=dict)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Playbook(Base):
    """Phase 3: pre-approved, versioned remediation playbooks. Autonomous /
    approved execution may ONLY run these — never free-form AI output (P4)."""

    __tablename__ = "playbooks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(Text, default="")
    image: Mapped[str] = mapped_column(String(512))  # immutable image ref
    command: Mapped[dict] = mapped_column(JSON)  # ["sh","-c","..."] — fixed, params via env
    allowed_params: Mapped[dict] = mapped_column(JSON, default=dict)  # {name: description}
    approved_by: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("project", "name", "version"),)


class Outbox(Base):
    """Transactional outbox — written in the same transaction as the event."""

    __tablename__ = "outbox"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), index=True)
    published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
