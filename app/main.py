import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.events import bus
from app.runtime import Runtime
from app.telegram.accounts import LoginError
from app.web import routes_accounts, routes_chats, routes_dashboard, routes_files, routes_sse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("telethon").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Partial downloads live only as long as the process: whatever is left here on
    # shutdown is garbage, and the queue re-downloads it from scratch on the next start.
    with tempfile.TemporaryDirectory(prefix="tghoard-") as tmp:
        runtime = Runtime(settings, bus, parts_dir=Path(tmp))
        app.state.runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()


app = FastAPI(title="tghoard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")
app.include_router(routes_dashboard.router)
app.include_router(routes_accounts.router)
app.include_router(routes_chats.router)
app.include_router(routes_files.router)
app.include_router(routes_sse.router)


@app.exception_handler(LoginError)
async def login_error_handler(request: Request, exc: LoginError):
    return RedirectResponse(f"/accounts?error={quote(str(exc))}", status_code=303)


if __name__ == "__main__":
    uvicorn.run(app, host=settings.host, port=settings.port)
