import asyncio
import logging
from collections.abc import Callable

from telethon import TelegramClient, events

from app.db import transaction
from app.events import EventBus
from app.models import ScanStatus
from app.store import chats as chats_store
from app.store import files as files_store
from app.telegram.accounts import AccountManager
from app.telegram.media import KIND_FILTERS, FileMeta, extract, passes_filters

log = logging.getLogger(__name__)

BATCH = 200


class Scanner:
    """Fills the `files` table: history scans on demand, new messages via watch."""

    def __init__(self, accounts: AccountManager, bus: EventBus) -> None:
        self._accounts = accounts
        self._bus = bus
        self._tasks: dict[int, asyncio.Task] = {}
        self._watchers: dict[int, tuple[TelegramClient, Callable]] = {}
        self.on_files_added: Callable[[int], None] | None = None
        accounts.ready_hooks.append(self.refresh_watch)
        accounts.gone_hooks.append(self._drop_watch)

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    # ---- history scan --------------------------------------------------------------

    def is_scanning(self, chat_id: int) -> bool:
        task = self._tasks.get(chat_id)
        return task is not None and not task.done()

    def start_scan(self, chat_id: int) -> bool:
        if self.is_scanning(chat_id):
            return False
        task = asyncio.create_task(self._scan(chat_id), name=f"scan:{chat_id}")
        self._tasks[chat_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(chat_id, None))
        return True

    async def _scan(self, chat_id: int) -> None:
        async with transaction() as s:
            chat = await chats_store.get_chat(s, chat_id)
            if chat is None:
                return
            await chats_store.set_scan_status(s, chat_id, ScanStatus.SCANNING)
        self._publish_scan(chat_id, ScanStatus.SCANNING, 0)
        found = 0
        try:
            client = self._accounts.client(chat["account_id"])
            entity = await client.get_input_entity(chat["tg_chat_id"])
            kinds: list[str] = chat["media_kinds"]
            start_id = chat["last_scanned_msg_id"]
            # With several media kinds each pass walks a different subset of messages, so
            # the checkpoint can only move once every pass has finished.
            checkpoint_each_batch = len(kinds) == 1
            max_seen = start_id
            for kind in kinds:
                batch: list[FileMeta] = []
                async for message in client.iter_messages(
                    entity, filter=KIND_FILTERS[kind](), min_id=start_id, reverse=True
                ):
                    max_seen = max(max_seen, message.id)
                    meta = extract(message)
                    if meta is None or not passes_filters(
                        meta, kinds, chat["min_size"], chat["max_size"]
                    ):
                        continue
                    batch.append(meta)
                    if len(batch) >= BATCH:
                        found += await self._flush(
                            chat, batch, max_seen if checkpoint_each_batch else None
                        )
                        batch = []
                        self._publish_scan(chat_id, ScanStatus.SCANNING, found)
                found += await self._flush(chat, batch, max_seen if checkpoint_each_batch else None)
            async with transaction() as s:
                await files_store.fill_album_captions(s, chat_id)
                await chats_store.set_last_scanned(s, chat_id, max_seen)
                await chats_store.set_scan_status(s, chat_id, ScanStatus.IDLE)
            self._publish_scan(chat_id, ScanStatus.IDLE, found)
        except asyncio.CancelledError:
            async with transaction() as s:
                await chats_store.set_scan_status(s, chat_id, ScanStatus.IDLE)
            raise
        except Exception as e:  # noqa: BLE001 — surfaced in the UI as scan_error
            log.exception("scan of chat %s failed", chat_id)
            async with transaction() as s:
                await chats_store.set_scan_status(s, chat_id, ScanStatus.ERROR, str(e))
            self._publish_scan(chat_id, ScanStatus.ERROR, found, str(e))

    async def _flush(self, chat, batch: list[FileMeta], checkpoint: int | None) -> int:
        if not batch and checkpoint is None:
            return 0
        async with transaction() as s:
            inserted = await files_store.insert_files(s, chat["id"], batch, chat["auto_queue"])
            if checkpoint is not None:
                await chats_store.set_last_scanned(s, chat["id"], checkpoint)
        if inserted and chat["auto_queue"] and self.on_files_added:
            self.on_files_added(chat["account_id"])
        return inserted

    def _publish_scan(
        self, chat_id: int, status: str, found: int, error: str | None = None
    ) -> None:
        self._bus.publish(
            "chat.scan", {"chat_id": chat_id, "status": status, "found": found, "error": error}
        )

    # ---- watch ---------------------------------------------------------------------

    async def refresh_watch(self, account_id: int, client: TelegramClient | None = None) -> None:
        """(Re)register the NewMessage handler with the current set of watched chats."""
        client = client or self._accounts.client(account_id)
        await self._drop_watch(account_id)
        async with transaction() as s:
            watched = await chats_store.list_watched_chats(s, account_id)
        if not watched:
            return
        tg_ids = [c["tg_chat_id"] for c in watched]

        async def handler(event: events.NewMessage.Event) -> None:
            await self._on_new_message(account_id, event)

        client.add_event_handler(handler, events.NewMessage(chats=tg_ids))
        self._watchers[account_id] = (client, handler)
        log.info("watching %d chats on account %s", len(tg_ids), account_id)

    async def _drop_watch(self, account_id: int) -> None:
        entry = self._watchers.pop(account_id, None)
        if entry:
            client, handler = entry
            client.remove_event_handler(handler)

    async def _on_new_message(self, account_id: int, event: events.NewMessage.Event) -> None:
        meta = extract(event.message)
        if meta is None:
            return
        async with transaction() as s:
            chat = await chats_store.get_chat_by_tg_id(s, account_id, event.chat_id)
            if chat is None or not chat["watch"] or not chat["enabled"]:
                return
            if not passes_filters(meta, chat["media_kinds"], chat["min_size"], chat["max_size"]):
                return
            inserted = await files_store.insert_files(s, chat["id"], [meta], chat["auto_queue"])
            if inserted and meta.grouped_id is not None:
                await files_store.fill_album_captions(s, chat["id"])
        if inserted:
            self._bus.publish("chat.new_file", {"chat_id": chat["id"], "name": meta.name})
            if chat["auto_queue"] and self.on_files_added:
                self.on_files_added(account_id)
