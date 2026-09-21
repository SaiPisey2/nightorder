"""Built-in parameter resolvers. Every resolution records provenance."""
from __future__ import annotations

import asyncio
import os
import shlex
from datetime import datetime, timezone
from typing import Any

from nightorder.contracts.base import ParameterResolver, ResolvedValue, StepContext


class NowYearMonthResolver(ParameterResolver):
    """params: {offset_months=0, fmt="%Y%m"} — e.g. yearmonth / prev_yearmonth."""

    async def resolve(self, ctx: StepContext, params: dict[str, Any]) -> ResolvedValue:
        offset = int(params.get("offset_months", 0))
        now = datetime.now(timezone.utc)
        month = now.year * 12 + (now.month - 1) + offset
        y, m = divmod(month, 12)
        value = datetime(y, m + 1, 1, tzinfo=timezone.utc).strftime(params.get("fmt", "%Y%m"))
        return ResolvedValue(value=value, provenance=f"now_yearmonth(offset={offset}) at {now.isoformat()}")


class EnvResolver(ParameterResolver):
    """params: {var, default=""}"""

    async def resolve(self, ctx: StepContext, params: dict[str, Any]) -> ResolvedValue:
        var = params["var"]
        value = os.environ.get(var, params.get("default", ""))
        return ResolvedValue(value=value, provenance=f"worker env ${var}")


class CommandResolver(ParameterResolver):
    """params: {command, json=false} — stdout (stripped) becomes the value."""

    async def resolve(self, ctx: StepContext, params: dict[str, Any]) -> ResolvedValue:
        command = params["command"]
        if isinstance(command, str):
            command = shlex.split(command)
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=int(params.get("timeout_seconds", 120)))
        if proc.returncode != 0:
            raise RuntimeError(f"resolver command failed ({proc.returncode}): {stderr.decode(errors='replace')[-500:]}")
        raw = stdout.decode(errors="replace").strip()
        value: Any = raw
        if params.get("json"):
            import json

            value = json.loads(raw)
        return ResolvedValue(value=value, provenance=f"command: {' '.join(command)}")
