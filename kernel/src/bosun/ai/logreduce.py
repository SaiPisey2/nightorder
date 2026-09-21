"""Log Reduction Pipeline (Phase 2).

Score and rank rather than hard-delete: the raw log is always retained by the
caller (DB logs_tail / object storage); this module produces a reduced *view*
that surfaces anomalies with context and down-ranks routine noise.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Signals worth surfacing, with weights. Kubernetes failure modes get top rank.
_SIGNALS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"OOMKilled|OutOfMemory|Killed process|oom-kill", re.I), 100),
    (re.compile(r"ImagePullBackOff|ErrImagePull|CrashLoopBackOff", re.I), 100),
    (re.compile(r"Evicted|NodeAffinity|FailedScheduling|Insufficient (cpu|memory)", re.I), 90),
    (re.compile(r"PersistentVolumeClaim|PVC|No space left on device|disk full", re.I), 90),
    (re.compile(r"NetworkPolicy|connection refused|connection reset|i/o timeout|TLS handshake", re.I), 70),
    (re.compile(r"ResourceQuota|forbidden|Unauthorized|permission denied|AccessDenied", re.I), 70),
    (re.compile(r"Traceback \(most recent call last\)|^\s+File \"", re.M), 80),
    (re.compile(r"\b(panic|FATAL|CRITICAL)\b", re.I), 80),
    (re.compile(r"\bfail(ed|ure)?\b", re.I), 40),
    (re.compile(r"\b(ERROR|ERR)\b"), 50),
    (re.compile(r"\bexception\b", re.I), 60),
    (re.compile(r"exit code [1-9]\d*|non-zero exit", re.I), 60),
    (re.compile(r"\bWARN(ING)?\b"), 15),
    (re.compile(r"\bretry(ing)?\b|\btimed? ?out\b", re.I), 30),
]

# Routine noise to down-rank (never delete — just score 0 unless also anomalous).
_NOISE = re.compile(r"health.?check|liveness|readiness|heartbeat|ping|GET /health|keepalive", re.I)


@dataclass
class ScoredLine:
    index: int
    score: int
    text: str


def score_line(line: str) -> int:
    score = 0
    for pattern, weight in _SIGNALS:
        if pattern.search(line):
            score = max(score, weight)
    if score < 50 and _NOISE.search(line):
        return 0
    return score


def reduce_log(raw: str, max_lines: int = 120, context: int = 3, min_score: int = 30) -> str:
    """Reduced view: anomalous lines (score >= min_score) plus `context`
    surrounding lines each, ordered by position, elided gaps marked. Head and
    tail of the log are always kept (crashes live at the end, config at the
    start)."""
    lines = raw.splitlines()
    if not lines:
        return ""
    scored = [ScoredLine(i, score_line(l), l) for i, l in enumerate(lines)]

    keep: set[int] = set(range(min(3, len(lines))))  # head
    keep |= set(range(max(0, len(lines) - 5), len(lines)))  # tail
    anomalies = [s for s in scored if s.score >= min_score]
    # Highest-scoring anomalies first until budget, each with context window.
    for s in sorted(anomalies, key=lambda x: -x.score):
        if len(keep) >= max_lines:
            break
        keep |= set(range(max(0, s.index - context), min(len(lines), s.index + context + 1)))

    out: list[str] = []
    prev = -1
    for i in sorted(keep):
        if prev >= 0 and i > prev + 1:
            out.append(f"    ... [{i - prev - 1} lines elided] ...")
        out.append(lines[i])
        prev = i
    return "\n".join(out)
