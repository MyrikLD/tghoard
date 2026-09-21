from collections.abc import Sequence

from sqlalchemy import RowMapping, delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, AccountStatus

ACCOUNT_COLUMNS = (
    Account.id,
    Account.phone,
    Account.display_name,
    Account.status,
    Account.error,
    Account.paused,
    Account.created_at,
)


async def list_accounts(session: AsyncSession) -> Sequence[RowMapping]:
    rows = await session.execute(select(*ACCOUNT_COLUMNS).order_by(Account.id))
    return rows.mappings().all()


async def get_account(session: AsyncSession, account_id: int) -> RowMapping | None:
    rows = await session.execute(select(*ACCOUNT_COLUMNS).where(Account.id == account_id))
    return rows.mappings().first()


async def get_account_by_phone(session: AsyncSession, phone: str) -> RowMapping | None:
    rows = await session.execute(select(*ACCOUNT_COLUMNS).where(Account.phone == phone))
    return rows.mappings().first()


async def create_account(session: AsyncSession, phone: str) -> int:
    stmt = (
        insert(Account).values(phone=phone, status=AccountStatus.PENDING_CODE).returning(Account.id)
    )
    return await session.scalar(stmt)


async def set_account_status(
    session: AsyncSession,
    account_id: int,
    status: str,
    error: str | None = None,
    display_name: str | None = None,
) -> None:
    values = {"status": status, "error": error}
    if display_name is not None:
        values["display_name"] = display_name
    await session.execute(update(Account).where(Account.id == account_id).values(**values))


async def set_account_paused(session: AsyncSession, account_id: int, paused: bool) -> None:
    await session.execute(update(Account).where(Account.id == account_id).values(paused=paused))


async def delete_account(session: AsyncSession, account_id: int) -> None:
    await session.execute(delete(Account).where(Account.id == account_id))
