from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from telethon.errors import RPCError

from app.models import AccountStatus
from app.store import accounts as accounts_store
from app.web.deps import RuntimeDep, SessionDep, templates

router = APIRouter(prefix="/accounts")


def _redirect(error: Exception | None = None) -> RedirectResponse:
    url = f"/accounts?error={quote(str(error))}" if error else "/accounts"
    return RedirectResponse(url, status_code=303)


@router.get("")
async def accounts_page(
    request: Request, session: SessionDep, runtime: RuntimeDep, error: str | None = None
):
    rows = await accounts_store.list_accounts(session)
    accounts = [{**row, "connected": runtime.accounts.is_connected(row["id"])} for row in rows]
    pending = [
        a
        for a in accounts
        if a["status"] in (AccountStatus.PENDING_CODE, AccountStatus.PENDING_PASSWORD)
    ]
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {"request": request, "accounts": accounts, "pending": pending, "error": error},
    )


@router.post("/login")
async def login(runtime: RuntimeDep, phone: Annotated[str, Form()]):
    try:
        await runtime.accounts.begin_login(phone.strip())
    except RPCError as e:
        return _redirect(e)
    return _redirect()


@router.post("/{account_id}/code")
async def submit_code(runtime: RuntimeDep, account_id: int, code: Annotated[str, Form()]):
    try:
        await runtime.accounts.submit_code(account_id, code.strip())
    except RPCError as e:
        return _redirect(e)
    return _redirect()


@router.post("/{account_id}/password")
async def submit_password(runtime: RuntimeDep, account_id: int, password: Annotated[str, Form()]):
    try:
        await runtime.accounts.submit_password(account_id, password)
    except RPCError as e:
        return _redirect(e)
    return _redirect()


@router.post("/{account_id}/delete")
async def delete_account(runtime: RuntimeDep, account_id: int):
    await runtime.accounts.remove(account_id)
    return RedirectResponse("/accounts", status_code=303)


@router.post("/{account_id}/pause")
async def toggle_pause(session: SessionDep, runtime: RuntimeDep, account_id: int):
    row = await accounts_store.get_account(session, account_id)
    if row is not None:
        paused = not row["paused"]
        await accounts_store.set_account_paused(session, account_id, paused)
        await session.commit()
        worker = runtime.downloader.worker(account_id)
        if worker:
            worker.set_paused(paused)
    return RedirectResponse("/", status_code=303)
