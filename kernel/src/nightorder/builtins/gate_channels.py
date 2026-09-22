"""Built-in gate delivery channels.

`log`     — prints the gate + action URLs to worker logs (dev/demo).
`webhook` — POSTs an Adaptive-Card-shaped payload to a URL. Point it at a
            Microsoft Teams Incoming Webhook / Workflows URL for real Teams
            delivery; the payload is MessageCard-compatible. Action tokens in
            the URLs are minted by the kernel (signed, single-use) — the
            channel only carries them.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from nightorder.contracts.base import GateChannel, GateNotification
from nightorder.net import check_outbound_url

log = logging.getLogger("nightorder.gates")


class LogChannel(GateChannel):
    async def deliver(self, notification: GateNotification, params: dict[str, Any]) -> None:
        log.warning(
            "\n=== HUMAN GATE [%s] %s ===\n%s\nApprove: %s\nReject:  %s\n",
            notification.gate_type,
            notification.gate_id,
            notification.prompt,
            notification.approve_url,
            notification.reject_url,
        )


class TeamsChannel(GateChannel):
    """Teams via Power Automate webhook flow — Adaptive Card with Approve/
    Reject buttons (signed single-use links minted by the kernel) and
    @mentions.

    channel_params:
      url: webhook URL (default: NIGHTORDER_TEAMS_WEBHOOK_URL)
      mentions: [email, ...] — people to tag
    """

    async def deliver(self, notification: GateNotification, params: dict[str, Any]) -> None:
        from nightorder.config import settings
        from nightorder.teams import build_adaptive_card, post_to_teams

        url = params.get("url") or settings().teams_webhook_url
        if not url:
            raise ValueError("teams channel: no webhook url (channel_params.url or NIGHTORDER_TEAMS_WEBHOOK_URL)")
        context = notification.context or {}
        message = build_adaptive_card(
            title=f"Approval required — {notification.gate_type}",
            body=notification.prompt,
            facts={k: str(v) for k, v in list(context.items())[:6] if not isinstance(v, dict)},
            buttons=[
                {"title": "✅ Approve", "url": notification.approve_url},
                {"title": "❌ Reject", "url": notification.reject_url},
            ],
            mentions=params.get("mentions") or [],
            color="warning",
        )
        await post_to_teams(url, message)


class WebhookChannel(GateChannel):
    """params: {url} — Teams-compatible MessageCard payload."""

    async def deliver(self, notification: GateNotification, params: dict[str, Any]) -> None:
        url = params.get("url")
        if not url:
            raise ValueError("webhook gate channel requires channel_params.url")
        card = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "summary": f"Gate: {notification.gate_type}",
            "themeColor": "d97706",
            "title": f"Approval required — {notification.gate_type}",
            "text": notification.prompt,
            "potentialAction": [
                {"@type": "OpenUri", "name": "Approve", "targets": [{"os": "default", "uri": notification.approve_url}]},
                {"@type": "OpenUri", "name": "Reject", "targets": [{"os": "default", "uri": notification.reject_url}]},
            ],
        }
        check_outbound_url(url)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=card)
            resp.raise_for_status()
