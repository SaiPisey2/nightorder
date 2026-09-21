from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from nightorder.config import settings
from nightorder.db.models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(settings().database_url, pool_size=10, max_overflow=5)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


@asynccontextmanager
async def get_session():
    get_engine()
    assert _sessionmaker is not None
    async with _sessionmaker() as session:
        yield session


async def init_db() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        # Advisory lock: worker and API may boot concurrently; only one
        # process runs DDL at a time.
        from sqlalchemy import text

        await conn.execute(text("SELECT pg_advisory_xact_lock(884203)"))
        await conn.run_sync(Base.metadata.create_all)
