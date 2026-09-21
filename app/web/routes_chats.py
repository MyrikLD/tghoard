from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.models import MEDIA_KINDS, FileStatus
from app.store import accounts as accounts_store
from app.store import chats as chats_store
from app.store import files as files_store
from app.store.files import FileFilter
from app.web.deps import RuntimeDep, SessionDep, templates

router = APIRouter(prefix="/chats")


@router.get("")
async def chats_page(request: Request, session: SessionDep, runtime: RuntimeDep):
    chats = await chats_store.list_chats(session)
    accounts = await accounts_store.list_accounts(session)
    connected = [a for a in accounts if runtime.accounts.is_connected(a["id"])]
    stats = {c["id"]: await files_store.count_by_status(session, c["id"]) for c in chats}
    return templates.TemplateResponse(
        request,
        "chats.html",
        {
            "request": request,
            "chats": chats,
            "accounts": connected,
            "stats": stats,
            "media_kinds": MEDIA_KINDS,
            "scanning": {c["id"]: runtime.scanner.is_scanning(c["id"]) for c in chats},
        },
    )


@router.get("/dialogs")
async def dialogs_partial(
    request: Request,
    session: SessionDep,
    runtime: RuntimeDep,
    account_id: int,
    q: str = "",
    refresh: bool = False,
):
    dialogs = await runtime.accounts.get_dialogs(account_id, refresh=refresh)
    known = {c["tg_chat_id"] for c in await chats_store.list_chats(session, account_id)}
    needle = q.lower()
    items = [
        d
        for d in dialogs
        if d.tg_chat_id not in known and (not needle or needle in d.title.lower())
    ]
    return templates.TemplateResponse(
        request,
        "partials/dialogs.html",
        {"request": request, "dialogs": items[:200], "account_id": account_id},
    )


@router.post("/add")
async def add_chat(
    session: SessionDep,
    account_id: Annotated[int, Form()],
    tg_chat_id: Annotated[int, Form()],
    title: Annotated[str, Form()],
    kind: Annotated[str, Form()],
):
    existing = await chats_store.get_chat_by_tg_id(session, account_id, tg_chat_id)
    if existing is None:
        await chats_store.create_chat(session, account_id, tg_chat_id, title, kind)
        await session.commit()
    return RedirectResponse("/chats", status_code=303)


@router.post("/{chat_id}/settings")
async def update_settings(
    request: Request,
    session: SessionDep,
    runtime: RuntimeDep,
    chat_id: int,
    min_size_mb: Annotated[str, Form()] = "",
    max_size_mb: Annotated[str, Form()] = "",
):
    form = await request.form()
    kinds = [k for k in MEDIA_KINDS if form.get(f"kind_{k}")]
    if not kinds:
        kinds = ["video"]
    chat = await chats_store.get_chat(session, chat_id)
    if chat is None:
        return RedirectResponse("/chats", status_code=303)
    watch = bool(form.get("watch"))
    await chats_store.update_chat_settings(
        session,
        chat_id,
        media_kinds=kinds,
        min_size=int(float(min_size_mb) * 1024 * 1024) if min_size_mb.strip() else None,
        max_size=int(float(max_size_mb) * 1024 * 1024) if max_size_mb.strip() else None,
        watch=watch,
        auto_queue=bool(form.get("auto_queue")),
        enabled=bool(form.get("enabled")),
    )
    await session.commit()
    if watch != chat["watch"] and runtime.accounts.is_connected(chat["account_id"]):
        await runtime.scanner.refresh_watch(chat["account_id"])
    return RedirectResponse("/chats", status_code=303)


@router.post("/{chat_id}/scan")
async def scan(runtime: RuntimeDep, chat_id: int):
    runtime.scanner.start_scan(chat_id)
    return RedirectResponse("/chats", status_code=303)


@router.post("/{chat_id}/rescan")
async def rescan(session: SessionDep, runtime: RuntimeDep, chat_id: int):
    if not runtime.scanner.is_scanning(chat_id):
        await chats_store.reset_scan(session, chat_id)
        await session.commit()
        runtime.scanner.start_scan(chat_id)
    return RedirectResponse("/chats", status_code=303)


@router.post("/{chat_id}/queue-all")
async def queue_all(session: SessionDep, runtime: RuntimeDep, chat_id: int):
    await files_store.queue_all(
        session, FileFilter(chat_id=chat_id), [FileStatus.NEW, FileStatus.FAILED]
    )
    await session.commit()
    runtime.downloader.wake()
    return RedirectResponse("/chats", status_code=303)


@router.post("/{chat_id}/delete")
async def delete_chat(session: SessionDep, runtime: RuntimeDep, chat_id: int):
    chat = await chats_store.get_chat(session, chat_id)
    if chat is not None:
        await chats_store.delete_chat(session, chat_id)
        await session.commit()
        if chat["watch"] and runtime.accounts.is_connected(chat["account_id"]):
            await runtime.scanner.refresh_watch(chat["account_id"])
    return RedirectResponse("/chats", status_code=303)
