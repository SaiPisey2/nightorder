"""Microsoft Teams delivery via a Power Automate incoming-webhook flow.

Posts Adaptive Cards (v1.4) wrapped in the Teams message envelope. Supports
@mentions through `msteams.entities` — pass a list of email addresses and the
card both renders `<at>Name</at>` and carries the mention entity so the person
actually gets notified.

Used by the `teams` gate channel and the relay's alert sink. The webhook URL
is configuration (env/channel_params) — never hardcoded, never logged.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("nightorder.teams")


def _display_name(email: str) -> str:
    local = email.split("@")[0]
    return " ".join(part.capitalize() for part in local.replace("_", ".").split("."))


def build_adaptive_card(
    *,
    title: str,
    body: str,
    facts: dict[str, str] | None = None,
    buttons: list[dict[str, str]] | None = None,  # [{title, url}]
    mentions: list[str] | None = None,  # email addresses
    color: str = "attention",
) -> dict[str, Any]:
    """Build the Teams message envelope containing one Adaptive Card."""
    mention_line = ""
    entities = []
    for email in mentions or []:
        name = _display_name(email)
        tag = f"<at>{name}</at>"
        mention_line += ("" if not mention_line else " ") + tag
        entities.append({
            "type": "mention",
            "text": tag,
            "mentioned": {"id": email, "name": name},
        })

    card_body: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": title, "weight": "bolder", "size": "medium",
         "color": color, "wrap": True},
        {"type": "TextBlock", "text": body, "wrap": True},
    ]
    if facts:
        card_body.append({
            "type": "FactSet",
            "facts": [{"title": k, "value": str(v)[:300]} for k, v in facts.items()],
        })
    if mention_line:
        card_body.append({"type": "TextBlock", "text": f"cc {mention_line}", "wrap": True})

    card: dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": card_body,
    }
    if buttons:
        card["actions"] = [
            {"type": "Action.OpenUrl", "title": b["title"], "url": b["url"]} for b in buttons
        ]
    if entities:
        card["msteams"] = {"entities": entities}

    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": card,
        }],
    }


async def post_to_teams(webhook_url: str, message: dict[str, Any]) -> None:
    """POST the message envelope to the Power Automate flow. Raises on failure
    so callers can decide whether delivery is best-effort or fatal."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(webhook_url, json=message)
        if resp.status_code >= 300:
            raise RuntimeError(f"teams webhook returned {resp.status_code}: {resp.text[:300]}")
