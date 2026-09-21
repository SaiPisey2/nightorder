"""Retrieval-quality metric (Phase 2.5).

'Continuously improve' is unverifiable without a number. This computes
hit@k / precision@1 against labeled ground truth: queries (failure
signatures) mapped to the knowledge record that historically resolved them.
The seeded corpus (runbook 'what usually fails' + incident reports) provides
the initial ground truth; learning capture adds to it over time.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LabeledQuery:
    query: str  # failure signature / symptom text
    expected_record_id: str  # knowledge record that should be retrieved


@dataclass
class RetrievalReport:
    total: int
    hit_at_k: float  # fraction where expected doc appears in top k
    precision_at_1: float  # fraction where expected doc is rank 1
    k: int
    misses: list[str]


async def evaluate_retrieval(project: str, labeled: list[LabeledQuery], k: int = 3) -> RetrievalReport:
    from nightorder.ai import knowledge

    hits = 0
    top1 = 0
    misses: list[str] = []
    for item in labeled:
        results = await knowledge.search(project, item.query, limit=k)
        ids = [r["id"] for r in results]
        if item.expected_record_id in ids:
            hits += 1
            if ids and ids[0] == item.expected_record_id:
                top1 += 1
        else:
            misses.append(item.query[:80])
    n = len(labeled) or 1
    return RetrievalReport(total=len(labeled), hit_at_k=hits / n,
                           precision_at_1=top1 / n, k=k, misses=misses)
