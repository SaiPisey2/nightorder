"""Extension-point contracts — the platform's public API for consuming teams.

Semantic versioning applies: breaking a signature here is a major-version
change. Teams implement these ABCs in their own packages and register them
via Python entry points (see registry.py). The kernel never needs to know
about a team's package beyond `pip install`.

Extension points:
  StepExecutor      — new execution backends (Argo, SSH, SaaS API, dbt, ...)
  PreflightCheck    — deterministic readiness checks (P7)
  ParameterResolver — computed parameters with provenance
  GateChannel       — gate delivery (Teams/Slack/webhook/log)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

CONTRACT_VERSION = "1.0"


@dataclass
class StepContext:
    """Everything an extension may know about the step it serves."""

    project: str
    pipeline: str
    run_id: str
    step_id: str
    unit: str | None  # fan-out unit (e.g. a locale) or None
    attempt: int
    idempotency_key: str
    params: dict[str, Any]  # resolved pipeline parameters


@dataclass
class ExecutionResult:
    status: str  # "succeeded" | "failed"
    output: dict[str, Any] = field(default_factory=dict)
    logs: str = ""  # tail of logs; full logs belong in object storage
    external_ref: str = ""  # e.g. Argo workflow name — audit link


@dataclass
class CheckResult:
    passed: bool
    evidence: str  # concrete evidence (P6) — file path, count, URL, queue depth


@dataclass
class ResolvedValue:
    value: Any
    provenance: str  # where the value came from — recorded in the run


@dataclass
class GateNotification:
    gate_id: str
    gate_type: str
    prompt: str
    approve_url: str
    reject_url: str
    context: dict[str, Any]


class StepExecutor(ABC):
    """Execute one self-contained unit of work. Must be idempotent per
    ctx.idempotency_key: re-invocation with the same key must not duplicate
    the side effect (P5)."""

    @abstractmethod
    async def execute(self, ctx: StepContext, config: dict[str, Any]) -> ExecutionResult: ...


class PreflightCheck(ABC):
    """Deterministic check: typed params in → pass/fail + evidence out.
    Never raises for a failing condition — a failing condition is
    CheckResult(passed=False); exceptions mean the check itself broke."""

    @abstractmethod
    async def run(self, ctx: StepContext, params: dict[str, Any]) -> CheckResult: ...


class ParameterResolver(ABC):
    """Compute a parameter from external state (newest file in a bucket,
    a row count, ...). Must be read-only."""

    @abstractmethod
    async def resolve(self, ctx: StepContext, params: dict[str, Any]) -> ResolvedValue: ...


class GateChannel(ABC):
    """Deliver a gate request to humans. Content only — action tokens are
    minted by the kernel, never by the channel (or any LLM)."""

    @abstractmethod
    async def deliver(self, notification: GateNotification, params: dict[str, Any]) -> None: ...
