from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)


def _apply_pragmas(dbapi_conn, _record) -> None:
    cursor = dbapi_conn.cursor()
    for pragma in PRAGMAS:
        cursor.execute(pragma)
    cursor.close()


def create_engine(url: str | None = None) -> AsyncEngine:
    if url is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url or settings.db_url)
    sa.event.listen(engine.sync_engine, "connect", _apply_pragmas)
    return engine


engine = create_engine()
session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]:
    """One short write transaction: commits on success, rolls back on error."""
    async with session_factory() as session:
        async with session.begin():
            yield session
