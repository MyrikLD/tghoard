from fastapi import APIRouter, Request

from app.store import accounts as accounts_store
from app.store import files as files_store
from app.web.deps import RuntimeDep, SessionDep, templates

router = APIRouter()


async def _context(request: Request, session: SessionDep, runtime: RuntimeDep) -> dict:
    counts = await files_store.count_by_status(session)
    active = await files_store.active_downloads(session)
    failures = await files_store.recent_failures(session, limit=10)
    accounts = await accounts_store.list_accounts(session)
    queued = await files_store.queued_count_by_account(session)
    account_rows = [
        {
            **row,
            "connected": runtime.accounts.is_connected(row["id"]),
            "flood_until": runtime.downloader.flood_until(row["id"]),
            "queued": queued.get(row["id"], 0),
        }
        for row in accounts
    ]
    return {
        "request": request,
        "counts": counts,
        "active": active,
        "progress": runtime.downloader.progress,
        "failures": failures,
        "accounts": account_rows,
    }


@router.get("/")
async def dashboard(request: Request, session: SessionDep, runtime: RuntimeDep):
    ctx = await _context(request, session, runtime)
    return templates.TemplateResponse(request, "dashboard.html", ctx)
