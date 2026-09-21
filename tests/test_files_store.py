from datetime import UTC, datetime

import pytest

from app.models import FileStatus
from app.store import accounts as accounts_store
from app.store import chats as chats_store
from app.store import files as files_store
from app.store.files import FileFilter
from tests.conftest import meta


async def test_insert_skips_known_messages(session, chat_id):
    assert (
        await files_store.insert_files(session, chat_id, [meta(1), meta(2)], auto_queue=False) == 2
    )
    assert (
        await files_store.insert_files(session, chat_id, [meta(2), meta(3)], auto_queue=False) == 1
    )
    assert await files_store.count_files(session, FileFilter(chat_id=chat_id)) == 3


async def test_auto_queue_sets_queued(session, chat_id):
    await files_store.insert_files(session, chat_id, [meta(1)], auto_queue=True)
    counts = await files_store.count_by_status(session, chat_id)
    assert counts[FileStatus.QUEUED] == 1


async def test_same_document_downloaded_elsewhere_is_duplicate(session, chat_id):
    await files_store.insert_files(session, chat_id, [meta(1, doc_id=777)], auto_queue=False)
    (row,) = await files_store.list_files(session, FileFilter(chat_id=chat_id))
    await files_store.mark_done(session, row["id"], "/x/1.mp4", 1000)

    account_id = await accounts_store.create_account(session, "+200")
    other_chat = await chats_store.create_chat(session, account_id, -100999, "Other", "group")
    await files_store.insert_files(session, other_chat, [meta(5, doc_id=777)], auto_queue=True)
    (dup,) = await files_store.list_files(session, FileFilter(chat_id=other_chat))
    assert dup["status"] == FileStatus.DUPLICATE


async def test_claim_next_is_fifo_and_moves_to_downloading(session, chat_id):
    await files_store.insert_files(session, chat_id, [meta(1), meta(2)], auto_queue=False)
    rows = await files_store.list_files(session, FileFilter(chat_id=chat_id))
    ids = sorted(r["id"] for r in rows)
    await files_store.queue_files(session, [ids[1]])
    await files_store.queue_files(session, [ids[0]])
    account_id = await chats_store.get_chat(session, chat_id)
    account_id = account_id["account_id"]

    first = await files_store.claim_next(session, account_id)
    assert first["id"] == ids[1]
    assert first["status"] == FileStatus.DOWNLOADING
    second = await files_store.claim_next(session, account_id)
    assert second["id"] == ids[0]
    assert await files_store.claim_next(session, account_id) is None


async def test_claim_ignores_delayed_and_disabled(session, chat_id):
    await files_store.insert_files(session, chat_id, [meta(1)], auto_queue=True)
    chat = await chats_store.get_chat(session, chat_id)
    (row,) = await files_store.list_files(session, FileFilter(chat_id=chat_id))
    await files_store.requeue(session, row["id"], "boom", count_attempt=True, delay=600)
    assert await files_store.claim_next(session, chat["account_id"]) is None

    await files_store.requeue(session, row["id"], None, count_attempt=False, delay=0)
    await chats_store.update_chat_settings(session, chat_id, enabled=False)
    assert await files_store.claim_next(session, chat["account_id"]) is None

    await chats_store.update_chat_settings(session, chat_id, enabled=True)
    claimed = await files_store.claim_next(session, chat["account_id"])
    assert claimed is not None
    assert claimed["attempts"] == 1


async def test_reset_downloading_after_restart(session, chat_id):
    await files_store.insert_files(session, chat_id, [meta(1)], auto_queue=True)
    chat = await chats_store.get_chat(session, chat_id)
    claimed = await files_store.claim_next(session, chat["account_id"])
    assert claimed["status"] == FileStatus.DOWNLOADING
    assert await files_store.reset_downloading(session) == 1
    again = await files_store.claim_next(session, chat["account_id"])
    assert again["id"] == claimed["id"]


async def test_queue_files_only_touches_requeueable(session, chat_id):
    await files_store.insert_files(session, chat_id, [meta(1), meta(2)], auto_queue=False)
    rows = await files_store.list_files(session, FileFilter(chat_id=chat_id))
    await files_store.mark_done(session, rows[0]["id"], "/x", 1000)
    assert await files_store.queue_files(session, [r["id"] for r in rows]) == 1


@pytest.mark.parametrize("delay", [0, 5])
async def test_requeue_sets_future_queued_at(session, chat_id, delay):
    await files_store.insert_files(session, chat_id, [meta(1)], auto_queue=True)
    (row,) = await files_store.list_files(session, FileFilter(chat_id=chat_id))
    before = datetime.now(UTC)
    await files_store.requeue(session, row["id"], None, count_attempt=False, delay=delay)
    updated = await files_store.get_file(session, row["id"])
    queued_at = updated["queued_at"].replace(tzinfo=UTC)
    assert (queued_at - before).total_seconds() >= delay - 1


async def test_filter_by_size_and_date(session, chat_id):
    small = meta(1, size=100)
    big = meta(2, size=10_000)
    big.date = datetime(2024, 6, 15, tzinfo=UTC)
    await files_store.insert_files(session, chat_id, [small, big], auto_queue=False)

    rows = await files_store.list_files(session, FileFilter(min_size=1_000))
    assert [r["message_id"] for r in rows] == [2]
    rows = await files_store.list_files(session, FileFilter(max_size=1_000))
    assert [r["message_id"] for r in rows] == [1]
    rows = await files_store.list_files(
        session,
        FileFilter(
            date_from=datetime(2024, 6, 1, tzinfo=UTC), date_to=datetime(2024, 7, 1, tzinfo=UTC)
        ),
    )
    assert [r["message_id"] for r in rows] == [2]

    queued = await files_store.queue_all(session, FileFilter(min_size=1_000), [FileStatus.NEW])
    assert queued == 1
    counts = await files_store.count_by_status(session, chat_id)
    assert counts[FileStatus.QUEUED] == 1 and counts[FileStatus.NEW] == 1


async def test_album_caption_is_shared_with_siblings(session, chat_id):
    album = [
        meta(1, grouped_id=5),
        meta(2, grouped_id=5, caption="Show S02"),
        meta(3, grouped_id=5),
        meta(4, grouped_id=6),
        meta(5, caption="own caption"),
    ]
    await files_store.insert_files(session, chat_id, album, auto_queue=False)
    assert await files_store.fill_album_captions(session, chat_id) == 2
    rows = await files_store.list_files(session, FileFilter(chat_id=chat_id))
    captions = {r["message_id"]: r["caption"] for r in rows}
    assert captions == {1: "Show S02", 2: "Show S02", 3: "Show S02", 4: "", 5: "own caption"}
