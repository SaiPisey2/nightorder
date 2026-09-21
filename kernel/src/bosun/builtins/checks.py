"""Built-in preflight checks (P7). Each returns pass/fail + concrete evidence."""
from __future__ import annotations

import asyncio
import os
import shlex
from typing import Any

import httpx

from bosun.contracts.base import CheckResult, PreflightCheck, StepContext
from bosun.builtins.executors import _render, redact


class FileExistsCheck(PreflightCheck):
    """params: {path}"""

    async def run(self, ctx: StepContext, params: dict[str, Any]) -> CheckResult:
        path = _render(params.get("path", ""), ctx)
        exists = os.path.exists(path)
        return CheckResult(passed=exists, evidence=f"path {path} exists={exists}")


class HttpOkCheck(PreflightCheck):
    """params: {url, expect_status=200}"""

    async def run(self, ctx: StepContext, params: dict[str, Any]) -> CheckResult:
        url = _render(params.get("url", ""), ctx)
        expect = int(params.get("expect_status", 200))
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
            return CheckResult(passed=resp.status_code == expect, evidence=f"GET {url} -> {resp.status_code} (expected {expect})")
        except Exception as e:
            return CheckResult(passed=False, evidence=f"GET {url} error: {e}")


class CommandSucceedsCheck(PreflightCheck):
    """params: {command} — exit 0 = pass. Evidence = output tail."""

    async def run(self, ctx: StepContext, params: dict[str, Any]) -> CheckResult:
        command = _render(params.get("command", ""), ctx)
        if isinstance(command, str):
            command = shlex.split(command)
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=int(params.get("timeout_seconds", 120)))
        out = redact(stdout.decode(errors="replace")[-2000:])
        return CheckResult(passed=proc.returncode == 0, evidence=f"exit={proc.returncode} output: {out}")


class AlwaysPassCheck(PreflightCheck):
    async def run(self, ctx: StepContext, params: dict[str, Any]) -> CheckResult:
        return CheckResult(passed=True, evidence="always_pass")


class AlwaysFailCheck(PreflightCheck):
    async def run(self, ctx: StepContext, params: dict[str, Any]) -> CheckResult:
        return CheckResult(passed=False, evidence=params.get("reason", "always_fail"))
