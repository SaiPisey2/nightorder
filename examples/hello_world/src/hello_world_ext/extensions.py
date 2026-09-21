"""A consuming team's extensions — written against the public contracts only.

This file demonstrates every extension kind a team typically needs:
a step executor (custom backend), a preflight check, a parameter resolver.
It imports nothing from the kernel except `bosun.contracts`.
"""
from __future__ import annotations

from typing import Any

from bosun.contracts import (
    CheckResult,
    ExecutionResult,
    ParameterResolver,
    PreflightCheck,
    ResolvedValue,
    StepContext,
    StepExecutor,
)


class HelloBackendExecutor(StepExecutor):
    """A make-believe execution backend (stands in for a SaaS call, a dbt
    run, an Airflow trigger...). config: {message}."""

    async def execute(self, ctx: StepContext, config: dict[str, Any]) -> ExecutionResult:
        message = config.get("message", "hello")
        greeting = ctx.params.get("greeting", "")
        return ExecutionResult(
            status="succeeded",
            output={"echo": f"{greeting} {message}".strip(), "unit": ctx.unit},
            logs=f"hello_backend executed for step={ctx.step_id} unit={ctx.unit} attempt={ctx.attempt}",
        )


class HelloReadyCheck(PreflightCheck):
    """params: {ready: bool} — trivially deterministic check."""

    async def run(self, ctx: StepContext, params: dict[str, Any]) -> CheckResult:
        ready = bool(params.get("ready", True))
        return CheckResult(passed=ready, evidence=f"hello_ready reported ready={ready}")


class GreetingResolver(ParameterResolver):
    """params: {name} — computes a value with provenance."""

    async def resolve(self, ctx: StepContext, params: dict[str, Any]) -> ResolvedValue:
        name = params.get("name", "world")
        return ResolvedValue(value=f"hello-{name}", provenance=f"hello_greeting(name={name})")
