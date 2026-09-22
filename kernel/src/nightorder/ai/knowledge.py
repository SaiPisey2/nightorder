"""Qdrant semantic memory (Phase 2). Retrieval only — never workflow state.

System of record is Postgres (`knowledge_records`); the outbox relay indexes
records into Qdrant asynchronously (KnowledgeRecordCreated events), so a
Qdrant outage never blocks a write. Embeddings are computed locally via
fastembed (BAAI/bge-small-en-v1.5) — no external embedding API.
"""
from __future__ import annotations

import asyncio
from typing import Any

from nightorder.config import settings

COLLECTION = "nightorder-knowledge"
_client = None


def _get_client():
    global _client
    if _client is None:
        from qdrant_client import QdrantClient

        _client = QdrantClient(url=settings().qdrant_url)
        _client.set_model("BAAI/bge-small-en-v1.5")
    return _client


def _index_sync(record_id: str, project: str, kind: str, title: str, content: str, source: str) -> None:
    client = _get_client()
    client.add(
        collection_name=COLLECTION,
        documents=[f"{title}\n\n{content}"],
        metadata=[{"project": project, "kind": kind, "title": title, "source": source}],
        ids=[record_id],
    )


def _search_sync(project: str, query: str, limit: int, kind: str | None) -> list[dict[str, Any]]:
    from qdrant_client import models

    client = _get_client()
    must = [models.FieldCondition(key="project", match=models.MatchValue(value=project))]
    if kind:
        must.append(models.FieldCondition(key="kind", match=models.MatchValue(value=kind)))
    try:
        # client.add()/client.query() are a matched pair from qdrant-client's
        # fastembed convenience API: add() names the vector after the model, and
        # query() knows that name. query() carries a deprecation notice, but
        # add() does not, so migrating only this half would search a vector name
        # the writer never used. Migrate both together or neither.
        hits = client.query(
            collection_name=COLLECTION,
            query_text=query,
            query_filter=models.Filter(must=must),
            limit=limit,
        )
    except Exception as e:
        if "doesn't exist" in str(e) or "Not found" in str(e):
            return []
        raise
    return [
        {
            "id": str(h.id),
            "score": round(h.score, 4),
            "title": h.metadata.get("title", ""),
            "kind": h.metadata.get("kind", ""),
            "source": h.metadata.get("source", ""),
            "snippet": (h.metadata.get("document") or h.document or "")[:600],
        }
        for h in hits
    ]


async def index_record(record_id: str, project: str, kind: str, title: str, content: str, source: str = "") -> None:
    await asyncio.to_thread(_index_sync, record_id, project, kind, title, content, source)


def _delete_sync(record_id: str) -> None:
    from qdrant_client import models

    client = _get_client()
    try:
        client.delete(collection_name=COLLECTION,
                      points_selector=models.PointIdsList(points=[record_id]))
    except Exception as e:
        if "doesn't exist" in str(e) or "Not found" in str(e):
            return
        raise


async def delete_record(record_id: str) -> None:
    """Remove a record's embedding from Qdrant (row deletion is the caller's)."""
    await asyncio.to_thread(_delete_sync, record_id)


def _delete_project_sync(project: str) -> None:
    from qdrant_client import models

    client = _get_client()
    try:
        client.delete(
            collection_name=COLLECTION,
            points_selector=models.FilterSelector(filter=models.Filter(must=[
                models.FieldCondition(key="project", match=models.MatchValue(value=project))
            ])),
        )
    except Exception as e:
        if "doesn't exist" in str(e) or "Not found" in str(e):
            return
        raise


async def delete_project_points(project: str) -> None:
    """Remove ALL of a project's embeddings from Qdrant."""
    await asyncio.to_thread(_delete_project_sync, project)


async def search(project: str, query: str, limit: int = 5, kind: str | None = None) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_search_sync, project, query, limit, kind)


def qdrant_available() -> bool:
    try:
        _get_client().get_collections()
        return True
    except Exception:
        return False
