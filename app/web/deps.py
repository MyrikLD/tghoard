from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import session_factory
from app.runtime import Runtime


async def session_dep() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


def runtime_dep(request: Request) -> Runtime:
    return request.app.state.runtime


SessionDep = Annotated[AsyncSession, Depends(session_dep)]
RuntimeDep = Annotated[Runtime, Depends(runtime_dep)]


def human_size(n: int | float | None) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def relative_path(path: str | None) -> str:
    """Rows written before paths were stored relative still carry the DOWNLOAD_DIR prefix."""
    if not path:
        return ""
    prefix = f"{settings.download_dir}/"
    return path[len(prefix) :] if path.startswith(prefix) else path


templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["human_size"] = human_size
templates.env.filters["relative_path"] = relative_path
