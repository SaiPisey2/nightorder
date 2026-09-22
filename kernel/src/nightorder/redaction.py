"""Redaction of secrets in captured output.

Step logs are persisted to Postgres, rendered in the UI, and included in the
incident bundle sent to the advisory model. Anything missed here leaves the
machine, so this runs on every captured stream.

Two families of rule:

* **Labelled** — a word like `password` or `authorization` followed by a value.
  This catches secrets whose shape is unknown but whose label is not, including
  `Authorization: Bearer <jwt>`, where the value sits behind a scheme word, and
  quoted values containing spaces.
* **Shaped** — tokens recognisable on their own, with no label at all. A bare
  provider key pasted into a log has nothing to match on except its shape.

Redaction is best-effort and always will be: it cannot recognise a secret that
is neither labelled nor distinctively shaped. Treat it as a safety net, not a
guarantee.
"""
from __future__ import annotations

import re

REDACTED = "[REDACTED]"

_LABEL = r"(?:api[_-]?key|apikey|auth|authorization|token|password|passwd|pwd|secret|credential|private[_-]?key|access[_-]?key)"
_SCHEME = r"(?:Bearer|Basic|Token|ApiKey|Digest)"

_RULES: list[tuple[str, re.Pattern[str], str]] = [
    # Authorization: Bearer <value>  /  auth = Basic <value>
    (
        "labelled-scheme",
        re.compile(rf"(?i)\b({_LABEL}\s*[:=]\s*{_SCHEME}\s+)([^\s\"']+)"),
        rf"\1{REDACTED}",
    ),
    # password: 'value with spaces'  /  secret="..."
    (
        "labelled-quoted",
        re.compile(rf"(?i)\b({_LABEL}\s*[:=]\s*)(['\"])(?:(?!\2).)*\2"),
        rf"\1\2{REDACTED}\2",
    ),
    # api_key=abcdefgh  /  token: abcdefgh
    (
        "labelled-bare",
        re.compile(rf"(?i)\b({_LABEL}[\"']?\s*[:=]\s*)(?!{_SCHEME}\b)([^\s\"'&]{{6,}})"),
        rf"\1{REDACTED}",
    ),
    # Bearer <value> with no label in front
    (
        "scheme-only",
        re.compile(rf"(?i)\b({_SCHEME}\s+)([A-Za-z0-9\-._~+/]{{16,}}=*)"),
        rf"\1{REDACTED}",
    ),
    # https://user:password@host
    (
        "url-userinfo",
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s@/]+)(@)"),
        rf"\1{REDACTED}\3",
    ),
    # Webhook URLs: the path itself is the credential.
    (
        "webhook-url",
        re.compile(
            r"(?i)\bhttps://[^\s]*?(?:hooks\.slack\.com|discord(?:app)?\.com/api/webhooks|"
            r"webhook\.office\.com|logic\.azure\.com|office\.com/webhookb2)[^\s\"']*"
        ),
        REDACTED,
    ),
    # JSON Web Tokens.
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"),
        REDACTED,
    ),
    # Provider tokens recognisable without a label.
    (
        "provider-token",
        re.compile(
            r"(?i)\b(?:"
            r"sk-ant-[A-Za-z0-9_-]{8,}"          # Anthropic
            r"|sk-[A-Za-z0-9]{20,}"              # OpenAI-style
            r"|gh[pousr]_[A-Za-z0-9]{16,}"       # GitHub
            r"|github_pat_[A-Za-z0-9_]{20,}"
            r"|xox[baprs]-[A-Za-z0-9-]{10,}"     # Slack
            r"|AKIA[0-9A-Z]{16}"                 # AWS access key id
            r"|ASIA[0-9A-Z]{16}"
            r"|AIza[A-Za-z0-9_-]{30,}"           # Google
            r"|glpat-[A-Za-z0-9_-]{16,}"         # GitLab
            r")\b"
        ),
        REDACTED,
    ),
    # PEM private key blocks, header to footer.
    (
        "private-key-block",
        re.compile(
            r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        REDACTED,
    ),
]


def redact(text: str) -> str:
    """Replace recognisable secrets in `text`. Safe on None and non-strings."""
    if not text:
        return text
    if not isinstance(text, str):
        return text
    for _name, pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def rule_names() -> list[str]:
    """The rules applied, in order — used by the tests to keep them in step."""
    return [name for name, _, _ in _RULES]
