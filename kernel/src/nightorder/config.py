"""Kernel configuration — everything comes from environment variables."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# The built-in secret exists so `make infra` works with no setup. It is in
# the published source, so any deployment still using it can have its gate
# approval links forged by anyone.
DEV_GATE_TOKEN_SECRET = "dev-only-secret-change-me"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: _env(
            "NIGHTORDER_DATABASE_URL",
            "postgresql+asyncpg://nightorder:nightorder@localhost:5433/nightorder",
        )
    )
    temporal_address: str = field(default_factory=lambda: _env("TEMPORAL_ADDRESS", "localhost:7233"))
    temporal_namespace: str = field(default_factory=lambda: _env("TEMPORAL_NAMESPACE", "default"))
    api_base_url: str = field(default_factory=lambda: _env("NIGHTORDER_API_BASE_URL", "http://localhost:8400"))
    # HMAC secret for signed single-use gate action tokens.
    gate_token_secret: str = field(
        default_factory=lambda: _env("NIGHTORDER_GATE_TOKEN_SECRET", DEV_GATE_TOKEN_SECRET)
    )
    gate_token_ttl_seconds: int = field(
        default_factory=lambda: int(_env("NIGHTORDER_GATE_TOKEN_TTL_SECONDS", "86400"))
    )
    # Anthropic (advisor/agents). Key comes from env only — never persisted.
    anthropic_model: str = field(default_factory=lambda: _env("NIGHTORDER_ADVISOR_MODEL", "claude-sonnet-5"))
    # Qdrant semantic memory (Phase 2). Retrieval only — never workflow state.
    qdrant_url: str = field(default_factory=lambda: _env("NIGHTORDER_QDRANT_URL", "http://localhost:6333"))
    # Sandbox execution (Phase 3)
    sandbox_namespace: str = field(default_factory=lambda: _env("NIGHTORDER_SANDBOX_NAMESPACE", "nightorder-e2e"))
    # Teams (Power Automate webhook). Default channel for gates + relay alerts.
    teams_webhook_url: str = field(default_factory=lambda: _env("NIGHTORDER_TEAMS_WEBHOOK_URL", ""))
    # Which events the relay forwards to Teams as alerts (csv).
    teams_alert_events: str = field(
        default_factory=lambda: _env(
            "NIGHTORDER_TEAMS_ALERT_EVENTS",
            "WorkflowFailed,GateRequested,RemediationSuggested,GateExpired",
        )
    )
    # Emails to @mention on relay alerts (csv).
    teams_alert_mentions: str = field(default_factory=lambda: _env("NIGHTORDER_TEAMS_ALERT_MENTIONS", ""))
    # Kubernetes context for the Argo executor ("" = current context / in-cluster).
    kube_context: str = field(default_factory=lambda: _env("NIGHTORDER_KUBE_CONTEXT", ""))
    argo_namespace: str = field(default_factory=lambda: _env("NIGHTORDER_ARGO_NAMESPACE", "argo"))
    # Directory the argo executor resolves `manifest_file:` references against.
    # Keeps big workflow YAMLs out of pipeline specs; the worker loads them at
    # execution time.
    manifests_dir: str = field(default_factory=lambda: _env("NIGHTORDER_MANIFESTS_DIR", "manifests"))


def settings() -> Settings:
    """Fresh settings each call so env changes in tests take effect."""
    return Settings()


class InsecureConfiguration(RuntimeError):
    """Raised when an auth-enforcing deployment still uses development secrets."""


def auth_enabled() -> bool:
    return os.environ.get("NIGHTORDER_AUTH", "off").lower() == "on"


def check_runtime_config() -> list[str]:
    """Validate the security-relevant environment at process start.

    Raises InsecureConfiguration for a combination that is unsafe rather than
    merely permissive. Returns warnings for the open-platform default, which is
    deliberate but worth saying out loud on every boot.
    """
    current = settings()
    using_dev_secret = current.gate_token_secret == DEV_GATE_TOKEN_SECRET
    warnings: list[str] = []

    if auth_enabled():
        if using_dev_secret:
            raise InsecureConfiguration(
                "NIGHTORDER_AUTH=on but NIGHTORDER_GATE_TOKEN_SECRET is still the built-in "
                "development value. Gate approval links are signed with it, so anyone with "
                "the source can forge an approval. Generate one with:\n"
                "  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
            )
        if not os.environ.get("NIGHTORDER_ADMIN_KEY"):
            warnings.append(
                "NIGHTORDER_AUTH=on but NIGHTORDER_ADMIN_KEY is unset: project creation "
                "and agent toggles are unprotected."
            )
    else:
        warnings.append(
            "NIGHTORDER_AUTH=off: the control plane is open. Any caller that can reach it "
            "can register specs, start runs and resolve gates."
        )
        if using_dev_secret:
            warnings.append(
                "Using the built-in development gate-token secret. Set "
                "NIGHTORDER_GATE_TOKEN_SECRET before exposing this beyond localhost."
            )
    return warnings


def log_runtime_config() -> None:
    """check_runtime_config(), with the warnings emitted to the log."""
    for warning in check_runtime_config():
        log.warning("%s", warning)
