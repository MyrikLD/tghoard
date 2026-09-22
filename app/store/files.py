from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import RowMapping, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chat, File, FileStatus
from app.telegram.media import FileMeta

FILE_COLUMNS = (
    File.id,
    File.chat_id,
    File.message_id,
    File.doc_id,
    File.kind,
    File.name,
    File.mime,
    File.size,
    File.msg_date,
    File.caption,
    File.grouped_id,
    File.status,
    File.path,
    File.downloaded_bytes,
    File.attempts,
    File.error,
    File.queued_at,
    File.finished_at,
    File.created_at,
)

FILE_WITH_CHAT = (
    *FILE_COLUMNS,
    Chat.title.label("chat_title"),
    Chat.tg_chat_id,
    Chat.account_id,
)


def _now() -> datetime:
    return datetime.now(UTC)


async def insert_files(
    session: AsyncSession, chat_id: int, metas: Iterable[FileMeta], auto_queue: bool
) -> int:
    """Insert scanned files, skipping ones already known for this chat.

    A doc_id that is already downloaded from another chat is stored as `duplicate`
    so it is visible but never queued.
    """
    metas = list(metas)
    if not metas:
        return 0
    done_docs = set(
        await session.scalars(
            select(File.doc_id).where(
                File.doc_id.in_({m.doc_id for m in metas}), File.status == FileStatus.DONE
            )
        )
    )
    rows = []
    now = _now()
    for m in metas:
        if m.doc_id in done_docs:
            status, queued_at = FileStatus.DUPLICATE, None
        elif auto_queue:
            status, queued_at = FileStatus.QUEUED, now
        else:
            status, queued_at = FileStatus.NEW, None
        rows.append(
            {
                "chat_id": chat_id,
                "message_id": m.message_id,
                "doc_id": m.doc_id,
                "kind": m.kind,
                "name": m.name,
                "mime": m.mime,
                "size": m.size,
                "msg_date": m.date,
                "caption": m.caption,
                "grouped_id": m.grouped_id,
                "status": status,
                "queued_at": queued_at,
            }
        )
    stmt = (
        sqlite_insert(File)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["chat_id", "message_id"])
    )
    result = await session.execute(stmt)
    return result.rowcount


async def fill_album_captions(
    session: AsyncSession, chat_id: int, grouped_ids: Iterable[int] | None = None
) -> int:
    """Albums carry their caption on one message only; copy it to the rest of the group.

    `grouped_ids` limits the work to the albums just touched; None means the whole chat.
    """
    sibling = File.__table__.alias("sibling")
    caption_of_group = (
        select(sibling.c.caption)
        .where(
            sibling.c.chat_id == File.chat_id,
            sibling.c.grouped_id == File.grouped_id,
            sibling.c.caption != "",
        )
        .order_by(sibling.c.message_id)
        .limit(1)
        .scalar_subquery()
    )
    stmt = (
        update(File)
        .where(
            File.chat_id == chat_id,
            File.grouped_id.is_not(None),
            File.caption == "",
            caption_of_group.is_not(None),
        )
        .values(caption=caption_of_group)
    )
    if grouped_ids is not None:
        ids = list(grouped_ids)
        if not ids:
            return 0
        stmt = stmt.where(File.grouped_id.in_(ids))
    result = await session.execute(stmt)
    return result.rowcount


async def get_file(session: AsyncSession, file_id: int) -> RowMapping | None:
    stmt = select(*FILE_WITH_CHAT).join(Chat, Chat.id == File.chat_id).where(File.id == file_id)
    rows = await session.execute(stmt)
    return rows.mappings().first()


class FileFilter(BaseModel):
    chat_id: int | None = None
    status: str | None = None
    query: str | None = Field(default=None, description="Substring of the file name")
    min_size: int | None = Field(default=None, description="Bytes, inclusive")
    max_size: int | None = Field(default=None, description="Bytes, inclusive")
    date_from: datetime | None = Field(default=None, description="Message date, inclusive")
    date_to: datetime | None = Field(default=None, description="Message date, exclusive")

    def apply(self, stmt):
        if self.chat_id is not None:
            stmt = stmt.where(File.chat_id == self.chat_id)
        if self.status:
            stmt = stmt.where(File.status == self.status)
        if self.query:
            stmt = stmt.where(File.name.ilike(f"%{self.query}%"))
        if self.min_size is not None:
            stmt = stmt.where(File.size >= self.min_size)
        if self.max_size is not None:
            stmt = stmt.where(File.size <= self.max_size)
        if self.date_from is not None:
            stmt = stmt.where(File.msg_date >= self.date_from)
        if self.date_to is not None:
            stmt = stmt.where(File.msg_date < self.date_to)
        return stmt


SORT_COLUMNS = {
    "date": File.msg_date,
    "name": File.name,
    "chat": Chat.title,
    "size": File.size,
    "status": File.status,
}
DEFAULT_SORT = "date"


async def list_files(
    session: AsyncSession,
    flt: FileFilter,
    limit: int = 100,
    offset: int = 0,
    sort: str = DEFAULT_SORT,
    desc: bool = True,
) -> Sequence[RowMapping]:
    column = SORT_COLUMNS.get(sort, SORT_COLUMNS[DEFAULT_SORT])
    order = (column.desc(), File.id.desc()) if desc else (column.asc(), File.id.asc())
    stmt = flt.apply(select(*FILE_WITH_CHAT).join(Chat, Chat.id == File.chat_id))
    stmt = stmt.order_by(*order).limit(limit).offset(offset)
    rows = await session.execute(stmt)
    return rows.mappings().all()


async def count_files(session: AsyncSession, flt: FileFilter) -> int:
    return await session.scalar(flt.apply(select(func.count(File.id))))


async def count_by_status(session: AsyncSession, chat_id: int | None = None) -> dict[str, int]:
    stmt = select(File.status, func.count(File.id).label("n")).group_by(File.status)
    if chat_id is not None:
        stmt = stmt.where(File.chat_id == chat_id)
    rows = await session.execute(stmt)
    counts = dict.fromkeys(FileStatus.ALL, 0)
    counts.update({r["status"]: r["n"] for r in rows.mappings()})
    return counts


async def active_downloads(session: AsyncSession) -> Sequence[RowMapping]:
    stmt = (
        select(*FILE_WITH_CHAT)
        .join(Chat, Chat.id == File.chat_id)
        .where(File.status == FileStatus.DOWNLOADING)
        .order_by(File.id)
    )
    rows = await session.execute(stmt)
    return rows.mappings().all()


async def recent_failures(session: AsyncSession, limit: int = 20) -> Sequence[RowMapping]:
    stmt = (
        select(*FILE_WITH_CHAT)
        .join(Chat, Chat.id == File.chat_id)
        .where(File.status == FileStatus.FAILED)
        .order_by(File.finished_at.desc())
        .limit(limit)
    )
    rows = await session.execute(stmt)
    return rows.mappings().all()


async def queue_files(session: AsyncSession, file_ids: Iterable[int]) -> int:
    stmt = (
        update(File)
        .where(
            File.id.in_(list(file_ids)),
            File.status.in_([FileStatus.NEW, FileStatus.FAILED, FileStatus.SKIPPED]),
        )
        .values(status=FileStatus.QUEUED, queued_at=_now(), error=None, attempts=0)
    )
    result = await session.execute(stmt)
    return result.rowcount


async def queue_all(session: AsyncSession, flt: FileFilter, statuses: Sequence[str]) -> int:
    """Queue every file matching the filter whose status is one of `statuses`."""
    stmt = flt.model_copy(update={"status": None}).apply(update(File))
    stmt = stmt.where(File.status.in_(list(statuses))).values(
        status=FileStatus.QUEUED, queued_at=_now(), error=None, attempts=0
    )
    result = await session.execute(stmt)
    return result.rowcount


async def unqueue_files(session: AsyncSession, file_ids: Iterable[int]) -> int:
    """Return queued files to `new`. Running downloads are cancelled by the worker."""
    stmt = (
        update(File)
        .where(File.id.in_(list(file_ids)), File.status == FileStatus.QUEUED)
        .values(status=FileStatus.NEW, queued_at=None)
    )
    result = await session.execute(stmt)
    return result.rowcount


async def set_new(session: AsyncSession, file_id: int) -> None:
    """Cancelled download: back to `new`, nothing on disk."""
    await session.execute(
        update(File)
        .where(File.id == file_id)
        .values(status=FileStatus.NEW, queued_at=None, downloaded_bytes=0, error=None)
    )


async def claim_next(session: AsyncSession, account_id: int) -> RowMapping | None:
    """Atomically move the oldest queued file of this account to `downloading`."""
    next_id = (
        select(File.id)
        .join(Chat, Chat.id == File.chat_id)
        .where(
            File.status == FileStatus.QUEUED,
            File.queued_at <= _now(),
            Chat.account_id == account_id,
            Chat.enabled.is_(True),
        )
        .order_by(File.queued_at, File.id)
        .limit(1)
        .scalar_subquery()
    )
    stmt = (
        update(File)
        .where(File.id == next_id)
        .values(status=FileStatus.DOWNLOADING, error=None)
        .returning(File.id)
    )
    file_id = await session.scalar(stmt)
    if file_id is None:
        return None
    return await get_file(session, file_id)


async def set_progress(session: AsyncSession, file_id: int, downloaded_bytes: int) -> None:
    await session.execute(
        update(File).where(File.id == file_id).values(downloaded_bytes=downloaded_bytes)
    )


async def mark_done(session: AsyncSession, file_id: int, path: str, size: int) -> None:
    await session.execute(
        update(File)
        .where(File.id == file_id)
        .values(
            status=FileStatus.DONE,
            path=path,
            downloaded_bytes=size,
            error=None,
            finished_at=_now(),
        )
    )


async def mark_failed(
    session: AsyncSession, file_id: int, error: str, downloaded_bytes: int | None = None
) -> None:
    values = {"status": FileStatus.FAILED, "error": error, "finished_at": _now()}
    if downloaded_bytes is not None:
        values["downloaded_bytes"] = downloaded_bytes
    await session.execute(update(File).where(File.id == file_id).values(**values))


async def requeue(
    session: AsyncSession,
    file_id: int,
    error: str | None,
    count_attempt: bool,
    downloaded_bytes: int | None = None,
    delay: float = 0,
) -> None:
    """Put a file back to the end of the queue; `delay` seconds must pass before it is claimed."""
    queued_at = _now() + timedelta(seconds=delay)
    values = {"status": FileStatus.QUEUED, "queued_at": queued_at, "error": error}
    if count_attempt:
        values["attempts"] = File.attempts + 1
    if downloaded_bytes is not None:
        values["downloaded_bytes"] = downloaded_bytes
    await session.execute(update(File).where(File.id == file_id).values(**values))


async def reset_downloading(session: AsyncSession) -> int:
    """After a restart nothing is downloading; partial files resume from disk."""
    stmt = (
        update(File)
        .where(File.status == FileStatus.DOWNLOADING)
        .values(status=FileStatus.QUEUED, queued_at=func.coalesce(File.queued_at, _now()))
    )
    result = await session.execute(stmt)
    return result.rowcount


async def queued_count_by_account(session: AsyncSession) -> dict[int, int]:
    stmt = (
        select(Chat.account_id, func.count(File.id).label("n"))
        .join(Chat, Chat.id == File.chat_id)
        .where(File.status == FileStatus.QUEUED)
        .group_by(Chat.account_id)
    )
    rows = await session.execute(stmt)
    return {r["account_id"]: r["n"] for r in rows.mappings()}
