"""The generic spec interpreter (Workflow Definition Model (b)).

One workflow interprets every pipeline spec. It owns all orchestration state
(P1): dependency order, gate waits (Signals + bounded timers), fan-out child
workflows, quota semaphores, designed re-runs. It never touches the DB or any
external system directly — all side effects go through Activities.

Only stdlib + temporalio imports here (workflow sandbox determinism).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

# Activities are invoked by name via string references to keep this module
# import-clean for the sandbox.
_ACT_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=2))
_DB_TIMEOUT = timedelta(seconds=60)


@dataclass
class RunInput:
    run_id: str
    project: str
    pipeline: str
    spec: dict
    param_overrides: dict = field(default_factory=dict)
    correlation_id: str = ""


@dataclass
class UnitInput:
    run_id: str
    project: str
    pipeline: str
    step: dict
    unit: str
    params: dict
    correlation_id: str = ""


def _base(inp: RunInput | UnitInput, **extra: Any) -> dict:
    d = {
        "run_id": inp.run_id,
        "project": inp.project,
        "pipeline": inp.pipeline,
        "correlation_id": inp.correlation_id,
    }
    d.update(extra)
    return d


async def _execute_with_retry_and_repeat(
    inp: RunInput | UnitInput, step: dict, params: dict, unit: str | None
) -> dict:
    """Shared step-body: execute (with audited retries), then designed
    re-runs (P8) until the declared condition holds."""
    unit_key = unit or ""
    retry = step.get("retry") or {}
    max_attempts = int(retry.get("maximum_attempts", 3))
    interval = int(retry.get("initial_interval_seconds", 10))
    backoff = float(retry.get("backoff_coefficient", 2.0))
    repeat = step.get("repeat")
    max_iterations = int(repeat.get("max_iterations", 3)) if repeat else 1
    step_timeout_minutes = int(step.get("timeout_minutes", 720))
    unlimited = step_timeout_minutes == 0
    # Unlimited steps: effectively-infinite deadline, liveness enforced by
    # executor heartbeats instead. If the worker dies mid-watch, the heartbeat
    # timeout fires and the unlimited retry policy re-runs the activity, which
    # re-attaches to the same external workload (idempotency key) and resumes
    # watching — the external work is never resubmitted or abandoned.
    exec_timeout = timedelta(days=365) if unlimited else timedelta(minutes=step_timeout_minutes)
    exec_heartbeat = timedelta(minutes=5) if unlimited else None
    exec_retry = (
        RetryPolicy(maximum_attempts=0, initial_interval=timedelta(seconds=10),
                    maximum_interval=timedelta(minutes=2))
        if unlimited else RetryPolicy(maximum_attempts=1)
    )

    last: dict = {"status": "failed", "logs": "", "external_ref": "", "attempt": 1}
    for iteration in range(1, max_iterations + 1):
        if iteration > 1:
            await workflow.execute_activity(
                "record_step",
                _base(inp, step_id=step["id"], unit=unit_key, attempt=iteration,
                      status="running", event="StepRerunTriggered", executor=step.get("executor", ""),
                      notify=step.get("notify")),
                start_to_close_timeout=_DB_TIMEOUT, retry_policy=_ACT_RETRY,
            )
            await asyncio.sleep(int(repeat.get("delay_seconds", 30)))

        succeeded = False
        for try_num in range(1, max_attempts + 1):
            if try_num > 1:
                await workflow.execute_activity(
                    "record_step",
                    _base(inp, step_id=step["id"], unit=unit_key, attempt=iteration,
                          status="running", event="StepRetried"),
                    start_to_close_timeout=_DB_TIMEOUT, retry_policy=_ACT_RETRY,
                )
                await asyncio.sleep(interval * (backoff ** (try_num - 2)))
            idempotency_key = f"{inp.run_id}:{step['id']}:{unit_key}:{iteration}:{try_num}"
            try:
                last = await workflow.execute_activity(
                    "execute_step",
                    _base(inp, step_id=step["id"], unit=unit, attempt=iteration,
                          executor=step.get("executor", "script"), config=step.get("config", {}),
                          params=params, idempotency_key=idempotency_key,
                          step_timeout_minutes=step_timeout_minutes),
                    start_to_close_timeout=exec_timeout,
                    heartbeat_timeout=exec_heartbeat,
                    retry_policy=exec_retry,
                )
            except Exception as e:  # activity/executor infrastructure error
                last = {"status": "failed", "logs": f"executor error: {e}", "external_ref": ""}
            last["attempt"] = iteration
            if last["status"] == "succeeded":
                succeeded = True
                break
        if not succeeded:
            return last

        if repeat:
            condition = await workflow.execute_activity(
                "run_check",
                _base(inp, step_id=step["id"], unit=unit, check=repeat["until_check"],
                      check_params=repeat.get("params", {}), params=params, phase="repeat_until"),
                start_to_close_timeout=timedelta(minutes=10), retry_policy=_ACT_RETRY,
            )
            if condition["passed"]:
                return last
            # else: loop into next designed re-run iteration
        else:
            return last
    # repeat declared but condition never held within max_iterations
    last["status"] = "failed"
    last["logs"] = (last.get("logs", "") + "\nrepeat_until condition not met within max_iterations").strip()
    return last


@workflow.defn
class StepUnitWorkflow:
    """One fan-out unit (e.g. one locale) — independent retry/re-run/status."""

    @workflow.run
    async def run(self, inp: UnitInput) -> dict:
        params = dict(inp.params)
        params["item"] = inp.unit
        step = inp.step
        await workflow.execute_activity(
            "record_step",
            _base(inp, step_id=step["id"], unit=inp.unit, attempt=1, status="running",
                  event="StepStarted", executor=step.get("executor", ""), notify=step.get("notify")),
            start_to_close_timeout=_DB_TIMEOUT, retry_policy=_ACT_RETRY,
        )
        result = await _execute_with_retry_and_repeat(inp, step, params, inp.unit)
        await workflow.execute_activity(
            "record_step",
            _base(inp, step_id=step["id"], unit=inp.unit,
                  attempt=result.get("attempt", 1),
                  status=result["status"],
                  event="StepCompleted" if result["status"] == "succeeded" else "StepFailed",
                  notify=step.get("notify"),
                  logs_tail=result.get("logs", ""), external_ref=result.get("external_ref", ""),
                  error="" if result["status"] == "succeeded" else result.get("logs", "")[-2000:]),
            start_to_close_timeout=_DB_TIMEOUT, retry_policy=_ACT_RETRY,
        )
        return result


@dataclass
class RemediationInput:
    incident_id: str
    project: str
    run_id: str
    step_id: str
    playbook_name: str
    image: str
    command: list
    env: dict = field(default_factory=dict)
    namespace: str = ""
    timeout_seconds: int = 600
    approved_by: str = ""
    correlation_id: str = ""


@workflow.defn
class RemediationWorkflow:
    """Phase 3: approved playbook → hardened sandbox Job → result + audit.
    Launched only after a remediation_approval gate is granted; runs only
    registry playbooks (image + fixed command), never free-form AI output."""

    @workflow.run
    async def run(self, inp: RemediationInput) -> dict:
        base = {"incident_id": inp.incident_id, "project": inp.project,
                "run_id": inp.run_id, "step_id": inp.step_id,
                "correlation_id": inp.correlation_id}
        await workflow.execute_activity(
            "record_remediation",
            {**base, "status": "executing", "event": "SandboxJobStarted",
             "payload": {"playbook": inp.playbook_name, "approved_by": inp.approved_by}},
            start_to_close_timeout=_DB_TIMEOUT, retry_policy=_ACT_RETRY,
        )
        try:
            result = await workflow.execute_activity(
                "execute_sandbox_job",
                {"idempotency_key": f"remediation:{inp.incident_id}",
                 "image": inp.image, "command": inp.command, "env": inp.env,
                 "namespace": inp.namespace or None, "timeout_seconds": inp.timeout_seconds},
                start_to_close_timeout=timedelta(seconds=inp.timeout_seconds + 120),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except Exception as e:
            result = {"status": "failed", "logs": f"sandbox activity error: {e}", "job": ""}
        succeeded = result.get("status") == "succeeded"
        await workflow.execute_activity(
            "record_remediation",
            {**base,
             "status": "executed" if succeeded else "failed",
             "outcome": result.get("logs", "")[-2000:],
             "event": "SandboxJobCompleted",
             "payload": {"playbook": inp.playbook_name, "job": result.get("job", ""),
                         "status": result.get("status")}},
            start_to_close_timeout=_DB_TIMEOUT, retry_policy=_ACT_RETRY,
        )
        await workflow.execute_activity(
            "record_remediation",
            {**base, "status": "",
             "event": "RemediationExecuted" if succeeded else "RemediationFailed",
             "payload": {"playbook": inp.playbook_name, "job": result.get("job", "")}},
            start_to_close_timeout=_DB_TIMEOUT, retry_policy=_ACT_RETRY,
        )
        return result


@workflow.defn
class PipelineRunWorkflow:
    """Interprets a pipeline spec: params → DAG → per-step
    preflight → gate → execute/fan-out → repeat — with full event audit."""

    def __init__(self) -> None:
        self._gate_decisions: dict[str, dict] = {}
        self._step_status: dict[str, str] = {}

    # ---- signals / queries -------------------------------------------------
    @workflow.signal
    def gate_decision(self, payload: dict) -> None:
        """{gate_id, decision: approve|reject, actor, note} — idempotent:
        first terminal decision wins, later signals ignored."""
        gate_id = payload.get("gate_id", "")
        if gate_id and gate_id not in self._gate_decisions:
            self._gate_decisions[gate_id] = payload

    @workflow.query
    def status(self) -> dict:
        return dict(self._step_status)

    # ---- helpers -----------------------------------------------------------
    async def _wait_gate(self, inp: RunInput, step: dict, gate: dict, params: dict) -> bool:
        """Open a gate and wait for a signal or timeout. True = approved."""
        if gate.get("prompt"):
            prompt = gate["prompt"]
            for name, value in params.items():
                prompt = prompt.replace("{{params." + name + "}}", str(value))
            gate = {**gate, "prompt": prompt}
        opened = await workflow.execute_activity(
            "open_gate",
            _base(inp, step_id=step["id"], gate=gate,
                  context={"pipeline": inp.pipeline, "params": {k: str(v) for k, v in params.items()}}),
            start_to_close_timeout=timedelta(minutes=5), retry_policy=_ACT_RETRY,
        )
        gate_id = opened["gate_id"]
        timeout = timedelta(minutes=int(gate.get("timeout_minutes", 1440)))
        try:
            await workflow.wait_condition(lambda: gate_id in self._gate_decisions, timeout=timeout)
        except asyncio.TimeoutError:
            on_timeout = gate.get("on_timeout", "reject")
            status = {"reject": "expired", "approve": "approved", "escalate": "escalated"}[on_timeout]
            await workflow.execute_activity(
                "close_gate",
                _base(inp, gate_id=gate_id, step_id=step["id"], status=status),
                start_to_close_timeout=_DB_TIMEOUT, retry_policy=_ACT_RETRY,
            )
            return on_timeout == "approve"
        decision = self._gate_decisions[gate_id]["decision"]
        return decision == "approve"

    async def _run_step(self, inp: RunInput, step: dict, params: dict,
                        quota_semaphores: dict[str, asyncio.Semaphore]) -> None:
        step_id = step["id"]
        deps = step.get("depends_on", [])
        # Wait for dependencies; propagate skip on any non-success.
        await workflow.wait_condition(
            lambda: all(self._step_status.get(d) in ("succeeded", "failed", "skipped") for d in deps)
        )
        if any(self._step_status.get(d) != "succeeded" for d in deps):
            self._step_status[step_id] = "skipped"
            await workflow.execute_activity(
                "record_step",
                _base(inp, step_id=step_id, unit="", attempt=1, status="skipped",
                      event="StepSkipped", error="upstream dependency did not succeed"),
                start_to_close_timeout=_DB_TIMEOUT, retry_policy=_ACT_RETRY,
            )
            return

        self._step_status[step_id] = "running"
        await workflow.execute_activity(
            "record_step",
            _base(inp, step_id=step_id, unit="", attempt=1, status="running",
                  event="StepStarted", executor=step.get("executor", ""), notify=step.get("notify")),
            start_to_close_timeout=_DB_TIMEOUT, retry_policy=_ACT_RETRY,
        )

        async def fail(reason: str, attempt: int = 1, external_ref: str = "") -> None:
            self._step_status[step_id] = "failed"
            await workflow.execute_activity(
                "record_step",
                _base(inp, step_id=step_id, unit="", attempt=attempt, status="failed",
                      event="StepFailed", error=reason[-2000:], logs_tail=reason,
                      external_ref=external_ref, notify=step.get("notify")),
                start_to_close_timeout=_DB_TIMEOUT, retry_policy=_ACT_RETRY,
            )

        # ---- preflight checks (P7) — failed check never silently proceeds
        for check in step.get("preflight", []):
            result = await workflow.execute_activity(
                "run_check",
                _base(inp, step_id=step_id, check=check["check"],
                      check_params=check.get("params", {}), params=params, phase="preflight"),
                start_to_close_timeout=timedelta(minutes=10), retry_policy=_ACT_RETRY,
            )
            if not result["passed"]:
                if check.get("on_fail", "block") == "warn_gate":
                    warn_gate = {
                        "type": "sign_off", "channel": step.get("gate", {}).get("channel", "log") if step.get("gate") else "log",
                        "prompt": f"Preflight check '{check['check']}' FAILED for step {step_id}: "
                                  f"{result['evidence']}. Approve to proceed anyway.",
                        "timeout_minutes": 1440, "on_timeout": "reject",
                    }
                    if not await self._wait_gate(inp, step, warn_gate, params):
                        await fail(f"preflight '{check['check']}' failed and warn-gate not approved: {result['evidence']}")
                        return
                else:
                    await fail(f"preflight '{check['check']}' failed: {result['evidence']}")
                    return

        # ---- human gate before execution (P4)
        if step.get("gate"):
            if not await self._wait_gate(inp, step, step["gate"], params):
                await fail("gate rejected or expired")
                return

        # ---- ManualStep: human does the work; completion via a gate
        if step.get("executor") == "manual":
            manual_gate = {
                "type": "manual_step",
                "channel": (step.get("config") or {}).get("channel", "log"),
                "channel_params": (step.get("config") or {}).get("channel_params", {}),
                "prompt": (step.get("config") or {}).get("instructions", f"Manual step {step_id}: confirm completion."),
                "timeout_minutes": int(step.get("timeout_minutes", 1440)),
                "on_timeout": "reject",
            }
            if await self._wait_gate(inp, step, manual_gate, params):
                self._step_status[step_id] = "succeeded"
                await workflow.execute_activity(
                    "record_step",
                    _base(inp, step_id=step_id, unit="", attempt=1, status="succeeded",
                          event="StepCompleted", executor="manual", notify=step.get("notify")),
                    start_to_close_timeout=_DB_TIMEOUT, retry_policy=_ACT_RETRY,
                )
            else:
                await fail("manual step not confirmed (rejected or expired)")
            return

        # ---- fan-out (child workflows per unit, tiers + quota semaphore)
        fan_out = step.get("fan_out")
        if fan_out:
            units = params.get(fan_out["over_param"]) or []
            if not isinstance(units, list):
                await fail(f"fan_out.over_param '{fan_out['over_param']}' is not a list")
                return
            tiers: list[list[str]] = [
                [u for u in tier if u in units] for tier in fan_out.get("tiers", [])
            ]
            tiered = {u for tier in tiers for u in tier}
            remainder = [u for u in units if u not in tiered]
            if remainder:
                tiers.append(remainder)

            pool = fan_out.get("quota_pool")
            semaphore = (
                quota_semaphores[pool] if pool and pool in quota_semaphores
                else asyncio.Semaphore(int(fan_out.get("max_concurrent", 4)))
            )

            async def run_unit(unit: str) -> dict:
                async with semaphore:
                    return await workflow.execute_child_workflow(
                        StepUnitWorkflow.run,
                        UnitInput(run_id=inp.run_id, project=inp.project, pipeline=inp.pipeline,
                                  step=step, unit=unit, params=params, correlation_id=inp.correlation_id),
                        id=f"{workflow.info().workflow_id}--{step_id}--{unit}",
                    )

            failures: list[str] = []
            for tier in tiers:  # tier N completes before tier N+1 starts
                results = await asyncio.gather(*(run_unit(u) for u in tier), return_exceptions=True)
                for unit, res in zip(tier, results):
                    if isinstance(res, BaseException) or res.get("status") != "succeeded":
                        failures.append(unit)
            if failures:
                await fail(f"fan-out units failed: {failures}")
                return
            self._step_status[step_id] = "succeeded"
            await workflow.execute_activity(
                "record_step",
                _base(inp, step_id=step_id, unit="", attempt=1, status="succeeded",
                      event="StepCompleted", notify=step.get("notify")),
                start_to_close_timeout=_DB_TIMEOUT, retry_policy=_ACT_RETRY,
            )
            return

        # ---- plain execution (with retries + designed re-runs)
        result = await _execute_with_retry_and_repeat(inp, step, params, None)
        if result["status"] == "succeeded":
            self._step_status[step_id] = "succeeded"
            await workflow.execute_activity(
                "record_step",
                _base(inp, step_id=step_id, unit="", attempt=result.get("attempt", 1),
                      status="succeeded",
                      event="StepCompleted", logs_tail=result.get("logs", ""),
                      notify=step.get("notify"),
                      external_ref=result.get("external_ref", "")),
                start_to_close_timeout=_DB_TIMEOUT, retry_policy=_ACT_RETRY,
            )
        else:
            await fail(result.get("logs", "step failed"), attempt=result.get("attempt", 1),
                       external_ref=result.get("external_ref", ""))

    # ---- main --------------------------------------------------------------
    @workflow.run
    async def run(self, inp: RunInput) -> dict:
        spec = inp.spec
        if inp.run_id in ("", "scheduled"):
            # Schedule-created run: derive a unique run id from Temporal.
            inp.run_id = workflow.info().run_id
        await workflow.execute_activity(
            "record_run_status",
            _base(inp, status="running", workflow_id=workflow.info().workflow_id),
            start_to_close_timeout=_DB_TIMEOUT, retry_policy=_ACT_RETRY,
        )

        # 1. Resolve parameters in declaration order (later may reference earlier).
        params: dict[str, Any] = {}
        for param in spec.get("parameters", []):
            name = param["name"]
            effective = dict(param)
            if name in (inp.param_overrides or {}):
                effective = {"name": name, "resolver": "override", "value": inp.param_overrides[name]}
            resolved = await workflow.execute_activity(
                "resolve_parameter",
                _base(inp, param=effective, params=params),
                start_to_close_timeout=timedelta(minutes=10), retry_policy=_ACT_RETRY,
            )
            params[name] = resolved["value"]

        # 2. Quota pools shared across steps of this run.
        quota_semaphores = {
            q["name"]: asyncio.Semaphore(int(q["max_concurrent"]))
            for q in spec.get("quotas", [])
        }

        # 3. Launch every step; each waits on its dependencies.
        steps = spec.get("steps", [])
        await asyncio.gather(*(self._run_step(inp, step, params, quota_semaphores) for step in steps))

        failed = [s["id"] for s in steps if self._step_status.get(s["id"]) != "succeeded"]
        status = "failed" if failed else "completed"
        await workflow.execute_activity(
            "record_run_status",
            _base(inp, status=status, error=f"steps not succeeded: {failed}" if failed else ""),
            start_to_close_timeout=_DB_TIMEOUT, retry_policy=_ACT_RETRY,
        )
        return {"status": status, "steps": dict(self._step_status), "params": {k: str(v) for k, v in params.items()}}
