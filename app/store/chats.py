from collections.abc import Sequence

from sqlalchemy import RowMapping, delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, Chat, ScanStatus

CHAT_COLUMNS = (
    Chat.id,
    Chat.account_id,
    Chat.tg_chat_id,
    Chat.title,
    Chat.kind,
    Chat.enabled,
    Chat.watch,
    Chat.auto_queue,
    Chat.media_kinds,
    Chat.min_size,
    Chat.max_size,
    Chat.last_scanned_msg_id,
    Chat.scan_status,
    Chat.scan_error,
    Chat.created_at,
)


async def list_chats(session: AsyncSession, account_id: int | None = None) -> Sequence[RowMapping]:
    stmt = select(*CHAT_COLUMNS, Account.phone.label("account_phone")).join(
        Account, Account.id == Chat.account_id
    )
    if account_id is not None:
        stmt = stmt.where(Chat.account_id == account_id)
    rows = await session.execute(stmt.order_by(Chat.id))
    return rows.mappings().all()


async def get_chat(session: AsyncSession, chat_id: int) -> RowMapping | None:
    rows = await session.execute(select(*CHAT_COLUMNS).where(Chat.id == chat_id))
    return rows.mappings().first()


async def get_chat_by_tg_id(
    session: AsyncSession, account_id: int, tg_chat_id: int
) -> RowMapping | None:
    stmt = select(*CHAT_COLUMNS).where(Chat.account_id == account_id, Chat.tg_chat_id == tg_chat_id)
    rows = await session.execute(stmt)
    return rows.mappings().first()


async def list_watched_chats(session: AsyncSession, account_id: int) -> Sequence[RowMapping]:
    stmt = select(*CHAT_COLUMNS).where(
        Chat.account_id == account_id, Chat.enabled.is_(True), Chat.watch.is_(True)
    )
    rows = await session.execute(stmt)
    return rows.mappings().all()


async def create_chat(
    session: AsyncSession,
    account_id: int,
    tg_chat_id: int,
    title: str,
    kind: str,
) -> int:
    stmt = (
        insert(Chat)
        .values(account_id=account_id, tg_chat_id=tg_chat_id, title=title, kind=kind)
        .returning(Chat.id)
    )
    return await session.scalar(stmt)


async def update_chat_settings(session: AsyncSession, chat_id: int, **values) -> None:
    await session.execute(update(Chat).where(Chat.id == chat_id).values(**values))


async def set_scan_status(
    session: AsyncSession, chat_id: int, status: str, error: str | None = None
) -> None:
    await session.execute(
        update(Chat).where(Chat.id == chat_id).values(scan_status=status, scan_error=error)
    )


async def set_last_scanned(session: AsyncSession, chat_id: int, message_id: int) -> None:
    await session.execute(
        update(Chat)
        .where(Chat.id == chat_id, Chat.last_scanned_msg_id < message_id)
        .values(last_scanned_msg_id=message_id)
    )


async def reset_scan(session: AsyncSession, chat_id: int) -> None:
    await session.execute(
        update(Chat)
        .where(Chat.id == chat_id)
        .values(last_scanned_msg_id=0, scan_status=ScanStatus.IDLE, scan_error=None)
    )


async def delete_chat(session: AsyncSession, chat_id: int) -> None:
    await session.execute(delete(Chat).where(Chat.id == chat_id))
