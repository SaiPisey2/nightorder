"""Declarative pipeline spec — the platform's public API (semver'd).

A pipeline is data: steps, dependencies, parameters (with resolvers), gates,
preflight checks, fan-out, quotas. One generic Temporal workflow interprets it
(Workflow Definition Model (b)). Escape hatch: any registered step executor.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "1.0"


class ParameterSpec(BaseModel):
    """Pipeline-level parameter with a resolver and recorded provenance."""

    name: str
    resolver: Literal["static", "expression", "activity"] = "static"
    value: Any = None  # static
    expression: str | None = None  # expression: python-like expr over `params`
    activity: str | None = None  # activity: registered resolver name
    params: dict[str, Any] = Field(default_factory=dict)  # resolver params

    @model_validator(mode="after")
    def _check(self) -> "ParameterSpec":
        if self.resolver == "expression" and not self.expression:
            raise ValueError(f"parameter {self.name}: expression resolver needs 'expression'")
        if self.resolver == "activity" and not self.activity:
            raise ValueError(f"parameter {self.name}: activity resolver needs 'activity'")
        return self


class PreflightSpec(BaseModel):
    """Reference to a registered check in the check catalog (P7)."""

    check: str
    params: dict[str, Any] = Field(default_factory=dict)
    on_fail: Literal["block", "warn_gate"] = "block"


class GateSpec(BaseModel):
    """Human gate (P4): signal + bounded timeout + escalation + audit."""

    type: Literal["sign_off", "budget_approval", "manual_step", "remediation_approval"] = "sign_off"
    prompt: str = ""
    channel: str = "log"  # registered gate delivery channel
    channel_params: dict[str, Any] = Field(default_factory=dict)
    timeout_minutes: int = 24 * 60
    on_timeout: Literal["reject", "approve", "escalate"] = "reject"
    approvers: list[str] = Field(default_factory=list)


class RepeatSpec(BaseModel):
    """Designed re-run (P8): repeat step until a deterministic condition holds."""

    until_check: str
    params: dict[str, Any] = Field(default_factory=dict)
    max_iterations: int = 3
    delay_seconds: int = 30


class FanOutSpec(BaseModel):
    """Fan out over a list parameter as child workflows, with ordering tiers."""

    over_param: str
    max_concurrent: int = 4
    # Optional shared quota pool (declared at pipeline level) instead of
    # a private max_concurrent semaphore.
    quota_pool: str | None = None
    # Optional ordering: earlier tiers complete before later tiers start.
    # Units not listed in any tier run in a final implicit tier.
    tiers: list[list[str]] = Field(default_factory=list)


class RetrySpec(BaseModel):
    maximum_attempts: int = 3
    initial_interval_seconds: int = 10
    backoff_coefficient: float = 2.0


class NotifySpec(BaseModel):
    """Step-level Teams notifications (beyond gates and global alerts)."""

    on: list[Literal["started", "succeeded", "failed", "rerun"]] = Field(
        default_factory=lambda: ["started", "succeeded", "failed"]
    )
    mentions: list[str] = Field(default_factory=list)  # emails to tag
    webhook_url: str = ""  # override; default NIGHTORDER_TEAMS_WEBHOOK_URL


class StepSpec(BaseModel):
    id: str
    name: str = ""
    executor: str = "script"  # registered step-executor name; "manual" = ManualStep
    config: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    preflight: list[PreflightSpec] = Field(default_factory=list)
    gate: GateSpec | None = None  # gate blocks before execution
    fan_out: FanOutSpec | None = None
    repeat: RepeatSpec | None = None
    retry: RetrySpec = Field(default_factory=RetrySpec)
    notify: NotifySpec | None = None
    # 0 = no timeout: watch the external work for as long as it runs (the
    # executor heartbeats; a dead worker resumes watching via idempotent
    # re-attach). Any positive value is a hard deadline.
    timeout_minutes: int = Field(default=12 * 60, ge=0)


class QuotaSpec(BaseModel):
    name: str
    max_concurrent: int


class ScheduleSpec(BaseModel):
    """Calendar-anchored kickoff (Temporal Schedule)."""

    cron: str  # e.g. "0 6 8 * *" = 06:00 on the 8th of every month


class PipelineSpec(BaseModel):
    api_version: str = Field(default="nightorder/v1", alias="apiVersion")
    kind: Literal["Pipeline"] = "Pipeline"
    name: str
    project: str
    description: str = ""
    schema_version: str = SCHEMA_VERSION
    parameters: list[ParameterSpec] = Field(default_factory=list)
    quotas: list[QuotaSpec] = Field(default_factory=list)
    schedule: ScheduleSpec | None = None
    steps: list[StepSpec]

    model_config = {"populate_by_name": True}
