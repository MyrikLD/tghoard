import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test")

from app.db import create_engine  # noqa: E402
from app.models import Base  # noqa: E402
from app.store import accounts as accounts_store  # noqa: E402
from app.store import chats as chats_store  # noqa: E402
from app.telegram.media import FileMeta  # noqa: E402


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest.fixture
async def session(session_factory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        async with s.begin():
            yield s


@pytest.fixture
async def chat_id(session: AsyncSession) -> int:
    account_id = await accounts_store.create_account(session, "+100")
    return await chats_store.create_chat(session, account_id, -100123, "Test chat", "group")


def meta(
    message_id: int,
    doc_id: int | None = None,
    size: int = 1000,
    kind: str = "video",
    caption: str = "",
    grouped_id: int | None = None,
) -> FileMeta:
    return FileMeta(
        message_id=message_id,
        doc_id=doc_id or message_id * 10,
        kind=kind,
        name=f"{message_id}.mp4",
        mime="video/mp4",
        size=size,
        date=datetime(2024, 1, 1, tzinfo=UTC),
        caption=caption,
        grouped_id=grouped_id,
    )
