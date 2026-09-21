"""Kernel configuration — everything comes from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


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
        default_factory=lambda: _env("NIGHTORDER_GATE_TOKEN_SECRET", "dev-only-secret-change-me")
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
