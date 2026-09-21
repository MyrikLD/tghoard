import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from telethon.errors import FloodWaitError
from telethon.tl import types

from app.config import Settings
from app.events import EventBus
from app.models import FileStatus
from app.store import chats as chats_store
from app.store import files as files_store
from app.store.files import FileFilter
from app.telegram import downloader as dl
from tests.conftest import meta

DATA = b"x" * 3000


class FakeClient:
    def __init__(self, data: bytes = DATA, fail_after: int | None = None, error=None):
        self.data = data
        self.fail_after = fail_after
        self.error = error
        self.calls = 0

    async def get_messages(self, chat, ids):
        doc = types.Document(
            id=ids * 10,
            access_hash=1,
            file_reference=b"",
            date=datetime(2024, 1, 1, tzinfo=UTC),
            mime_type="video/mp4",
            size=len(self.data),
            dc_id=2,
            attributes=[],
            thumbs=None,
            video_thumbs=None,
        )
        return types.Message(
            id=ids,
            peer_id=types.PeerChat(1),
            date=datetime.now(UTC),
            message="",
            media=types.MessageMediaDocument(document=doc),
        )

    async def iter_download(self, location, offset, request_size):
        self.calls += 1
        if self.error and self.calls == 1:
            raise self.error
        sent = 0
        for i in range(offset, len(self.data), 1000):
            if self.fail_after is not None and sent >= self.fail_after and self.calls == 1:
                return
            yield self.data[i : i + 1000]
            sent += 1000


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        api_id=1,
        api_hash="x",
        data_dir=tmp_path / "data",
        download_dir=tmp_path / "dl",
        path_template="{chat}/{msg_id}_{name}",
        concurrency_per_account=2,
        max_attempts=3,
    )


@pytest.fixture
def patch_tx(session_factory, monkeypatch):
    @asynccontextmanager
    async def transaction():
        async with session_factory() as s:
            async with s.begin():
                yield s

    monkeypatch.setattr(dl, "transaction", transaction)
    return transaction


async def _wait_until(tx, file_id: int, pred, timeout: float = 5) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async with tx() as s:
            row = await files_store.get_file(s, file_id)
        if pred(row):
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"file {file_id} stuck in {row['status']}: {row['error']}")


async def _wait_status(tx, file_id: int, statuses: set[str], timeout: float = 5) -> dict:
    return await _wait_until(tx, file_id, lambda r: r["status"] in statuses, timeout)


async def _seed(session, chat_id, n=1) -> tuple[int, list[int]]:
    await files_store.insert_files(
        session, chat_id, [meta(i + 1, size=len(DATA)) for i in range(n)], auto_queue=True
    )
    chat = await chats_store.get_chat(session, chat_id)
    rows = await files_store.list_files(session, FileFilter(chat_id=chat_id))
    await session.commit()
    return chat["account_id"], [r["id"] for r in rows]


async def _run_worker(settings, account_id, client, tx):
    worker = dl.AccountWorker(
        account_id, client, settings, EventBus(), {}, asyncio.Lock(), settings.data_dir / "tmp"
    )
    return worker


async def test_worker_downloads_whole_file(session, chat_id, settings, patch_tx):
    account_id, (file_id,) = await _seed(session, chat_id)
    worker = await _run_worker(settings, account_id, FakeClient(), patch_tx)
    try:
        row = await _wait_status(patch_tx, file_id, {FileStatus.DONE, FileStatus.FAILED})
    finally:
        await worker.stop()
    assert row["status"] == FileStatus.DONE, row["error"]
    assert row["path"] == "Test chat/1_1.mp4"
    assert (settings.download_dir / row["path"]).read_bytes() == DATA
    assert not dl.part_path(settings.data_dir / "tmp", file_id).exists()


async def test_truncated_stream_requeues_then_resumes(session, chat_id, settings, patch_tx):
    account_id, (file_id,) = await _seed(session, chat_id)
    client = FakeClient(fail_after=1000)
    worker = await _run_worker(settings, account_id, client, patch_tx)
    try:
        row = await _wait_until(
            patch_tx, file_id, lambda r: r["attempts"] > 0 or r["status"] == FileStatus.DONE
        )
        assert row["status"] == FileStatus.QUEUED
        assert row["attempts"] == 1
        assert "IncompleteDownload" in row["error"]
        assert dl.part_path(settings.data_dir / "tmp", file_id).stat().st_size == 1000
        # backoff delay expired -> resumed from the part
        async with patch_tx() as s:
            await files_store.requeue(s, file_id, None, count_attempt=False)
        worker.wake()
        row = await _wait_status(patch_tx, file_id, {FileStatus.DONE, FileStatus.FAILED})
    finally:
        await worker.stop()
    assert row["status"] == FileStatus.DONE
    assert (settings.download_dir / row["path"]).read_bytes() == DATA
    assert client.calls == 2


async def test_flood_wait_pauses_account_without_counting_attempt(
    session, chat_id, settings, patch_tx
):
    account_id, (file_id,) = await _seed(session, chat_id)
    err = FloodWaitError(request=None, capture=30)
    worker = await _run_worker(settings, account_id, FakeClient(error=err), patch_tx)
    try:
        row = await _wait_until(patch_tx, file_id, lambda r: r["error"] is not None)
        assert row["status"] == FileStatus.QUEUED
        assert row["attempts"] == 0
        assert "flood wait 30s" in row["error"]
        assert worker.flood_until is not None
    finally:
        await worker.stop()


async def test_cancel_running_download_resets_to_new(session, chat_id, settings, patch_tx):
    account_id, (file_id,) = await _seed(session, chat_id)

    class SlowClient(FakeClient):
        async def iter_download(self, location, offset, request_size):
            yield self.data[:1000]
            await asyncio.sleep(10)

    worker = await _run_worker(settings, account_id, SlowClient(), patch_tx)
    try:
        await _wait_status(patch_tx, file_id, {FileStatus.DOWNLOADING})
        await asyncio.sleep(0.1)
        assert await worker.cancel_file(file_id)
        row = await _wait_status(patch_tx, file_id, {FileStatus.NEW})
    finally:
        await worker.stop()
    assert row["downloaded_bytes"] == 0
    assert not dl.part_path(settings.data_dir / "tmp", file_id).exists()
