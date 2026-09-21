from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.models import FileStatus
from app.store import chats as chats_store
from app.store import files as files_store
from app.store.files import FileFilter
from app.telegram.naming import TemplateError, context_for, render_path
from app.web.deps import RuntimeDep, SessionDep, templates

router = APIRouter(prefix="/files")
PAGE = 100


MB = 1024 * 1024


def _parse_filter(
    chat_id: int | None,
    status: str | None,
    q: str | None,
    min_mb: str | None,
    max_mb: str | None,
    date_from: str | None,
    date_to: str | None,
) -> FileFilter:
    """Query-string values as typed on the form; blanks mean 'no bound'."""

    def mb(value: str | None) -> int | None:
        return int(float(value) * MB) if value and value.strip() else None

    def day(value: str | None, end: bool) -> datetime | None:
        if not value or not value.strip():
            return None
        d = datetime.fromisoformat(value.strip()).replace(tzinfo=UTC)
        return d + timedelta(days=1) if end else d

    return FileFilter(
        chat_id=chat_id,
        status=status if status in FileStatus.ALL else None,
        query=q or None,
        min_size=mb(min_mb),
        max_size=mb(max_mb),
        date_from=day(date_from, end=False),
        date_to=day(date_to, end=True),
    )


def _planned_path(row, default_template: str) -> str:
    try:
        return str(render_path(default_template, context_for(row)))
    except TemplateError as e:
        return f"template error: {e}"


def _query_string(form: dict[str, str], page: int = 1, **overrides: str) -> str:
    params = {k: v for k, v in {**form, **overrides}.items() if v}
    if page > 1:
        params["page"] = str(page)
    return "/files" + ("?" + urlencode(params) if params else "")


@router.get("")
async def files_page(
    request: Request,
    session: SessionDep,
    runtime: RuntimeDep,
    chat_id: str = "",
    status: str | None = None,
    q: str | None = None,
    min_mb: str | None = None,
    max_mb: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = files_store.DEFAULT_SORT,
    dir: str = "desc",
    page: int = 1,
):
    if sort not in files_store.SORT_COLUMNS:
        sort = files_store.DEFAULT_SORT
    desc = dir != "asc"
    form = {
        "chat_id": chat_id,
        "status": status or "",
        "q": q or "",
        "min_mb": min_mb or "",
        "max_mb": max_mb or "",
        "date_from": date_from or "",
        "date_to": date_to or "",
        "sort": sort if sort != files_store.DEFAULT_SORT else "",
        "dir": "asc" if not desc else "",
    }
    try:
        flt = _parse_filter(
            int(chat_id) if chat_id else None, status, q, min_mb, max_mb, date_from, date_to
        )
    except ValueError:
        return RedirectResponse("/files", status_code=303)
    total = await files_store.count_files(session, flt)
    rows = await files_store.list_files(session, flt, PAGE, (page - 1) * PAGE, sort, desc)
    chats = await chats_store.list_chats(session)
    counts = await files_store.count_by_status(session, flt.chat_id)
    targets = {r["id"]: _planned_path(r, runtime.settings.path_template) for r in rows}
    ctx = {
        "request": request,
        "files": rows,
        "chats": chats,
        "counts": counts,
        "form": form,
        "chat_id": flt.chat_id,
        "status": flt.status,
        "page": page,
        "pages": max(1, (total + PAGE - 1) // PAGE),
        "total": total,
        "statuses": FileStatus.ALL,
        "progress": runtime.downloader.progress,
        "targets": targets,
        "sort": sort,
        "desc": desc,
        "sort_links": {
            key: _query_string(form, sort=key, dir="asc" if (key == sort and desc) else "")
            for key in files_store.SORT_COLUMNS
        },
        "back": _query_string(form, page),
        "page_base": _query_string(form) + ("&" if any(form.values()) else "?"),
    }
    template = "partials/files_table.html" if request.headers.get("HX-Request") else "files.html"
    return templates.TemplateResponse(request, template, ctx)


@router.post("/queue")
async def queue_selected(
    request: Request,
    session: SessionDep,
    runtime: RuntimeDep,
    back: Annotated[str, Form()] = "/files",
):
    form = await request.form()
    ids = [int(v) for v in form.getlist("ids")]
    if ids:
        await files_store.queue_files(session, ids)
        await session.commit()
        runtime.downloader.wake()
    return RedirectResponse(back, status_code=303)


@router.post("/unqueue")
async def unqueue_selected(
    request: Request,
    session: SessionDep,
    runtime: RuntimeDep,
    back: Annotated[str, Form()] = "/files",
):
    form = await request.form()
    ids = [int(v) for v in form.getlist("ids")]
    if ids:
        await files_store.unqueue_files(session, ids)
        await session.commit()
        for file_id in ids:
            await runtime.downloader.cancel_file(file_id)
    return RedirectResponse(back, status_code=303)


@router.post("/queue-all")
async def queue_all(
    session: SessionDep,
    runtime: RuntimeDep,
    status: Annotated[str, Form()] = FileStatus.NEW,
    back: Annotated[str, Form()] = "/files",
    chat_id: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
    min_mb: Annotated[str, Form()] = "",
    max_mb: Annotated[str, Form()] = "",
    date_from: Annotated[str, Form()] = "",
    date_to: Annotated[str, Form()] = "",
):
    """Queue everything matching the current filter form (except the status field)."""
    requeueable = (FileStatus.NEW, FileStatus.FAILED, FileStatus.SKIPPED)
    statuses = [status] if status in requeueable else [FileStatus.NEW]
    try:
        flt = _parse_filter(
            int(chat_id) if chat_id else None, None, q, min_mb, max_mb, date_from, date_to
        )
    except ValueError:
        return RedirectResponse(back, status_code=303)
    await files_store.queue_all(session, flt, statuses)
    await session.commit()
    runtime.downloader.wake()
    return RedirectResponse(back, status_code=303)
