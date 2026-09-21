import asyncio
import errno
import logging
import os
import shutil
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import RowMapping
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl import types

from app.config import Settings
from app.db import transaction
from app.events import EventBus
from app.store import files as files_store
from app.telegram.accounts import AccountManager
from app.telegram.naming import context_for, resolve_target

log = logging.getLogger(__name__)

PROGRESS_DB_INTERVAL = 3.0
IDLE_POLL = 5.0
MESSAGE_FETCH_INTERVAL = 0.7


class IncompleteDownload(Exception):
    """The stream ended before `expected` bytes were written; the .part is kept for resume."""


class Progress(BaseModel):
    file_id: int
    name: str
    chat_title: str
    total: int
    downloaded: int
    started_at: float
    speed: float = 0.0


# ---- disk logic: pure, no Telegram ----------------------------------------------------


def part_path(parts_dir: Path, file_id: int) -> Path:
    return parts_dir / f"{file_id}.part"


def resume_offset(part: Path, expected: int) -> int:
    """Bytes already on disk that can be kept. A part larger than expected is garbage."""
    if not part.exists():
        return 0
    size = part.stat().st_size
    if size >= expected:
        part.unlink()
        return 0
    return size


def unique_final_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    stem, dot, ext = name.rpartition(".")
    if not dot or len(ext) > 8:
        stem, ext = name, ""
    n = 1
    while candidate.exists():
        n += 1
        candidate = directory / (f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})")
    return candidate


async def write_stream(
    stream: AsyncIterator[bytes],
    part: Path,
    offset: int,
    expected: int,
    on_progress: Callable[[int], None],
) -> int:
    """Append the stream to `part` starting at `offset`; returns bytes now on disk.

    Raises IncompleteDownload when the stream ends short, so a truncated part can never be
    promoted to a final file.
    """
    part.parent.mkdir(parents=True, exist_ok=True)
    written = offset
    with open(part, "r+b" if offset else "wb") as f:
        if offset:
            f.seek(offset)
            f.truncate()
        async for chunk in stream:
            if not chunk:
                continue
            f.write(chunk)
            written += len(chunk)
            on_progress(written)
            if written > expected:
                raise ValueError(f"received {written} bytes, expected {expected}")
        f.flush()
        await asyncio.to_thread(os.fsync, f.fileno())
    if written != expected or written == 0:
        raise IncompleteDownload(f"got {written} of {expected} bytes")
    return written


def promote(part: Path, directory: Path, name: str, expected: int) -> Path:
    """Move a verified part into place. Never overwrites an existing file."""
    actual = part.stat().st_size
    if actual != expected:
        raise ValueError(f"part is {actual} bytes, expected {expected}")
    directory.mkdir(parents=True, exist_ok=True)
    final = unique_final_path(directory, name)
    try:
        os.replace(part, final)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        # tmp dir is on another filesystem: copy next to the destination first so the
        # final name still appears atomically.
        staging = final.with_name(final.name + ".copying")
        shutil.copyfile(part, staging)
        os.replace(staging, final)
        part.unlink()
    return final


# ---- worker ------------------------------------------------------------------------------


class AccountWorker:
    def __init__(
        self,
        account_id: int,
        client: TelegramClient,
        settings: Settings,
        bus: EventBus,
        progress: dict[int, Progress],
        promote_lock: asyncio.Lock,
        parts_dir: Path,
    ) -> None:
        self.account_id = account_id
        self._client = client
        self._settings = settings
        self._parts_dir = parts_dir
        self._bus = bus
        self._progress = progress
        self._promote_lock = promote_lock
        self._slots = asyncio.Semaphore(settings.concurrency_per_account)
        self._wake = asyncio.Event()
        self._tasks: dict[int, asyncio.Task] = {}
        self._cancelled: set[int] = set()
        # Message lookups are cheap but Telegram flood-limits them when several slots fire
        # at once; downloads themselves tolerate parallelism fine.
        self._fetch_lock = asyncio.Lock()
        self._last_fetch = 0.0
        self.paused = False
        self.flood_until: float | None = None
        self._loop_task = asyncio.create_task(self._loop(), name=f"worker:{account_id}")

    # -- control --

    def wake(self) -> None:
        self._wake.set()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.wake()

    @property
    def active_ids(self) -> list[int]:
        return list(self._tasks)

    async def cancel_file(self, file_id: int) -> bool:
        task = self._tasks.get(file_id)
        if task is None:
            return False
        self._cancelled.add(file_id)
        task.cancel()
        return True

    async def stop(self) -> None:
        self._loop_task.cancel()
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(self._loop_task, *self._tasks.values(), return_exceptions=True)

    # -- main loop --

    async def _wait(self, timeout: float | None) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except TimeoutError:
            pass

    async def _loop(self) -> None:
        while True:
            if self.paused:
                await self._wait(None)
                continue
            if self.flood_until and time.monotonic() < self.flood_until:
                await self._wait(self.flood_until - time.monotonic())
                continue
            self.flood_until = None
            await self._slots.acquire()
            try:
                async with transaction() as s:
                    row = await files_store.claim_next(s, self.account_id)
            except Exception:  # noqa: BLE001 — DB hiccup, retry shortly
                log.exception("claim failed for account %s", self.account_id)
                self._slots.release()
                await self._wait(IDLE_POLL)
                continue
            if row is None:
                self._slots.release()
                await self._wait(IDLE_POLL)
                continue
            task = asyncio.create_task(self._run(row), name=f"dl:{row['id']}")
            self._tasks[row["id"]] = task
            task.add_done_callback(lambda _t, fid=row["id"]: self._finished(fid))

    def _finished(self, file_id: int) -> None:
        self._tasks.pop(file_id, None)
        self._progress.pop(file_id, None)
        self._slots.release()
        self.wake()

    # -- one file --

    async def _run(self, row: RowMapping) -> None:
        file_id = row["id"]
        part = part_path(self._parts_dir, file_id)
        self._progress[file_id] = Progress(
            file_id=file_id,
            name=row["name"],
            chat_title=row["chat_title"],
            total=row["size"],
            downloaded=0,
            started_at=time.monotonic(),
        )
        self._publish_status(row, "downloading")
        try:
            final = await self._download(row, part)
        except asyncio.CancelledError:
            if file_id in self._cancelled:
                self._cancelled.discard(file_id)
                part.unlink(missing_ok=True)
                async with transaction() as s:
                    await files_store.set_new(s, file_id)
                self._publish_status(row, "new")
            raise
        except FloodWaitError as e:
            self.flood_until = time.monotonic() + e.seconds + 1
            request = type(e.request).__name__ if e.request is not None else "?"
            log.warning("account %s flood wait %ss on %s", self.account_id, e.seconds, request)
            async with transaction() as s:
                await files_store.requeue(
                    s, file_id, f"flood wait {e.seconds}s on {request}", count_attempt=False
                )
            self._bus.publish(
                "account.flood",
                {"account_id": self.account_id, "seconds": e.seconds},
            )
            self._publish_status(row, "queued")
        except Exception as e:  # noqa: BLE001 — any failure is recorded on the file row
            attempts = row["attempts"] + 1
            error = f"{type(e).__name__}: {e}"
            log.warning("file %s attempt %s failed: %s", file_id, attempts, error)
            downloaded = part.stat().st_size if part.exists() else 0
            async with transaction() as s:
                if attempts >= self._settings.max_attempts:
                    await files_store.mark_failed(s, file_id, error, downloaded)
                    status = "failed"
                else:
                    delay = min(2**attempts, 300)
                    await files_store.requeue(s, file_id, error, True, downloaded, delay=delay)
                    status = "queued"
            self._publish_status(row, status, error=error)
        else:
            async with transaction() as s:
                await files_store.mark_done(s, file_id, self._relative(final), row["size"])
            self._publish_status(row, "done", path=self._relative(final))

    def _relative(self, path: Path) -> str:
        """Stored path is relative to DOWNLOAD_DIR so the root can move between hosts."""
        return str(path.relative_to(self._settings.download_dir))

    def _target(self, row: RowMapping) -> tuple[Path, str]:
        return resolve_target(
            self._settings.download_dir, self._settings.path_template, context_for(row)
        )

    async def _download(self, row: RowMapping, part: Path) -> Path:
        directory, name = self._target(row)
        existing = directory / name
        if existing.exists() and existing.stat().st_size == row["size"]:
            part.unlink(missing_ok=True)
            return existing

        message = await self._fetch_message(row["tg_chat_id"], row["message_id"])
        if message is None or message.media is None:
            raise RuntimeError("message or its media no longer exists")
        media = message.media
        if isinstance(media, types.MessageMediaDocument):
            location = media.document
            size = media.document.size
        elif isinstance(media, types.MessageMediaPhoto):
            location = media.photo
            size = row["size"]
        else:
            raise RuntimeError(f"unsupported media {type(media).__name__}")
        if size != row["size"]:
            raise RuntimeError(f"size changed: db {row['size']}, telegram {size}")

        offset = resume_offset(part, size)
        progress = self._progress[row["id"]]
        last_db = time.monotonic()
        last_bytes = offset
        last_tick = last_db

        def on_progress(written: int) -> None:
            nonlocal last_bytes, last_tick
            progress.downloaded = written
            now = time.monotonic()
            if now - last_tick >= 1.0:
                progress.speed = (written - last_bytes) / (now - last_tick)
                last_bytes, last_tick = written, now
                self._bus.publish(
                    "file.progress",
                    {
                        "file_id": row["id"],
                        "downloaded": written,
                        "total": size,
                        "speed": progress.speed,
                    },
                )

        async def stream() -> AsyncIterator[bytes]:
            nonlocal last_db
            async for chunk in self._client.iter_download(
                location, offset=offset, request_size=self._settings.request_size
            ):
                yield chunk
                now = time.monotonic()
                if now - last_db >= PROGRESS_DB_INTERVAL:
                    last_db = now
                    async with transaction() as s:
                        await files_store.set_progress(s, row["id"], progress.downloaded)

        await write_stream(stream(), part, offset, size, on_progress)
        async with self._promote_lock:
            return await asyncio.to_thread(promote, part, directory, name, size)

    async def _fetch_message(self, chat_id: int, message_id: int):
        async with self._fetch_lock:
            wait = self._last_fetch + MESSAGE_FETCH_INTERVAL - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self._client.get_messages(chat_id, ids=message_id)
            finally:
                self._last_fetch = time.monotonic()

    def _publish_status(self, row: RowMapping, status: str, **extra) -> None:
        self._bus.publish(
            "file.status",
            {"file_id": row["id"], "chat_id": row["chat_id"], "status": status, **extra},
        )


# ---- facade ------------------------------------------------------------------------------


class Downloader:
    def __init__(
        self, settings: Settings, accounts: AccountManager, bus: EventBus, parts_dir: Path
    ) -> None:
        self._settings = settings
        self._accounts = accounts
        self._bus = bus
        self._parts_dir = parts_dir
        self._workers: dict[int, AccountWorker] = {}
        self._promote_lock = asyncio.Lock()
        self.progress: dict[int, Progress] = {}
        accounts.ready_hooks.append(self._on_account_ready)
        accounts.gone_hooks.append(self._on_account_gone)

    async def start(self) -> None:
        async with transaction() as s:
            n = await files_store.reset_downloading(s)
        if n:
            log.info("re-queued %d downloads interrupted by restart", n)
        self._parts_dir.mkdir(parents=True, exist_ok=True)

    async def stop(self) -> None:
        await asyncio.gather(*(w.stop() for w in self._workers.values()), return_exceptions=True)
        self._workers.clear()

    async def _on_account_ready(self, account_id: int, client: TelegramClient) -> None:
        old = self._workers.pop(account_id, None)
        if old:
            await old.stop()
        self._workers[account_id] = AccountWorker(
            account_id,
            client,
            self._settings,
            self._bus,
            self.progress,
            self._promote_lock,
            self._parts_dir,
        )

    async def _on_account_gone(self, account_id: int) -> None:
        worker = self._workers.pop(account_id, None)
        if worker:
            await worker.stop()

    def wake(self, account_id: int | None = None) -> None:
        for wid, worker in self._workers.items():
            if account_id is None or wid == account_id:
                worker.wake()

    def worker(self, account_id: int) -> AccountWorker | None:
        return self._workers.get(account_id)

    async def cancel_file(self, file_id: int) -> bool:
        for worker in self._workers.values():
            if await worker.cancel_file(file_id):
                return True
        return False

    def flood_until(self, account_id: int) -> datetime | None:
        worker = self._workers.get(account_id)
        if worker is None or worker.flood_until is None:
            return None
        remaining = worker.flood_until - time.monotonic()
        if remaining <= 0:
            return None
        return datetime.fromtimestamp(time.time() + remaining, UTC)
