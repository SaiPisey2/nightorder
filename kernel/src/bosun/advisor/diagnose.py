"""Read-only AI failure advisor (Phase 2 seed).

Builds a minimal incident bundle for a failed step (execution record, logs
tail, preflight history, resolved parameters) and asks Claude for a root-cause
analysis with evidence links. Advisory only (P6): the model never decides
pass/fail and never executes anything. Requires ANTHROPIC_API_KEY in the
API process env; endpoint returns 503 without it.
"""
from __future__ import annotations

import os
from typing import Any

from bosun.config import settings

SYSTEM = """You are a pipeline-failure analyst inside an orchestration platform.
You receive an incident bundle for one failed step. Produce:
1. Probable root cause (cite specific log lines / check evidence — no invented facts).
2. What to verify next (concrete commands or checks).
3. Suggested remediation (advisory only; a human will decide).
Be concise. If evidence is insufficient, say exactly what is missing."""


def advisor_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


async def diagnose(bundle: dict[str, Any]) -> dict[str, Any]:
    import anthropic

    client = anthropic.AsyncAnthropic()  # key from env
    import json

    message = await client.messages.create(
        model=settings().anthropic_model,
        max_tokens=1500,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"Incident bundle:\n```json\n{json.dumps(bundle, default=str, indent=2)[:30000]}\n```"}],
    )
    text = "".join(b.text for b in message.content if b.type == "text")
    return {"analysis": text, "model": settings().anthropic_model, "advisory_only": True}
