import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate
from telethon.tl import types

from app.config import Settings
from app.db import transaction
from app.events import EventBus
from app.models import AccountStatus
from app.store import accounts as accounts_store

log = logging.getLogger(__name__)

ReadyHook = Callable[[int, TelegramClient], Awaitable[None]]
GoneHook = Callable[[int], Awaitable[None]]


class DialogInfo(BaseModel):
    tg_chat_id: int
    title: str
    kind: str
    username: str | None


class LoginError(Exception):
    pass


def _proxy_kwargs(proxy: str | None) -> dict:
    if not proxy:
        return {}
    url = urlparse(proxy)
    if url.scheme == "mtproxy":
        secret = url.path.strip("/")
        return {
            "connection": ConnectionTcpMTProxyRandomizedIntermediate,
            "proxy": (url.hostname, url.port, secret),
        }
    if url.scheme in ("socks5", "socks4", "http"):
        return {
            "proxy": {
                "proxy_type": url.scheme,
                "addr": url.hostname,
                "port": url.port,
                "username": url.username,
                "password": url.password,
                "rdns": True,
            }
        }
    raise ValueError(f"Unsupported proxy scheme: {url.scheme}")


def _dialog_kind(entity) -> str:
    if isinstance(entity, types.User):
        return "user"
    if isinstance(entity, types.Channel):
        return "channel" if entity.broadcast else "group"
    return "group"


class AccountManager:
    """Owns one connected TelegramClient per active account."""

    def __init__(self, settings: Settings, bus: EventBus) -> None:
        self._settings = settings
        self._bus = bus
        self._clients: dict[int, TelegramClient] = {}
        self._phone_code_hash: dict[int, str] = {}
        self._dialog_cache: dict[int, list[DialogInfo]] = {}
        self._lock = asyncio.Lock()
        self.ready_hooks: list[ReadyHook] = []
        self.gone_hooks: list[GoneHook] = []

    # ---- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        self._settings.sessions_dir.mkdir(parents=True, exist_ok=True)
        async with transaction() as s:
            rows = await accounts_store.list_accounts(s)
        for row in rows:
            if row["status"] == AccountStatus.ACTIVE:
                try:
                    await self._connect_active(row["id"], row["phone"])
                except Exception as e:  # noqa: BLE001 — one broken account must not block others
                    log.exception("account %s failed to start", row["id"])
                    await self._set_status(row["id"], AccountStatus.ERROR, error=str(e))

    async def stop(self) -> None:
        for client in list(self._clients.values()):
            await client.disconnect()
        self._clients.clear()

    def client(self, account_id: int) -> TelegramClient:
        try:
            return self._clients[account_id]
        except KeyError:
            raise LoginError(f"account {account_id} is not connected") from None

    def is_connected(self, account_id: int) -> bool:
        client = self._clients.get(account_id)
        return client is not None and client.is_connected()

    def _session_path(self, account_id: int) -> Path:
        return self._settings.sessions_dir / f"{account_id}"

    def _make_client(self, account_id: int) -> TelegramClient:
        return TelegramClient(
            str(self._session_path(account_id)),
            self._settings.api_id,
            self._settings.api_hash,
            flood_sleep_threshold=60,
            request_retries=5,
            connection_retries=None,
            retry_delay=2,
            auto_reconnect=True,
            **_proxy_kwargs(self._settings.proxy),
        )

    async def _connect_active(self, account_id: int, phone: str) -> None:
        client = self._make_client(account_id)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise LoginError("session is no longer authorized, remove and re-add the account")
        await self._activate(account_id, client)

    async def _activate(self, account_id: int, client: TelegramClient) -> None:
        me = await client.get_me()
        name = " ".join(filter(None, [me.first_name, me.last_name])) or me.username or ""
        self._clients[account_id] = client
        await self._set_status(account_id, AccountStatus.ACTIVE, display_name=name)
        for hook in self.ready_hooks:
            await hook(account_id, client)

    async def _set_status(
        self,
        account_id: int,
        status: str,
        error: str | None = None,
        display_name: str | None = None,
    ) -> None:
        async with transaction() as s:
            await accounts_store.set_account_status(s, account_id, status, error, display_name)
        self._bus.publish("account.status", {"account_id": account_id, "status": status})

    # ---- login flow ------------------------------------------------------------------

    async def begin_login(self, phone: str) -> int:
        async with self._lock:
            async with transaction() as s:
                existing = await accounts_store.get_account_by_phone(s, phone)
                if existing is not None and existing["status"] == AccountStatus.ACTIVE:
                    raise LoginError("this phone is already logged in")
                if existing is None:
                    account_id = await accounts_store.create_account(s, phone)
                else:
                    account_id = existing["id"]
                    await accounts_store.set_account_status(
                        s, account_id, AccountStatus.PENDING_CODE
                    )
            client = self._clients.get(account_id) or self._make_client(account_id)
            try:
                if not client.is_connected():
                    await client.connect()
                sent = await client.send_code_request(phone)
            except Exception:
                await client.disconnect()
                self._clients.pop(account_id, None)
                if existing is None:
                    async with transaction() as s:
                        await accounts_store.delete_account(s, account_id)
                raise
            self._clients[account_id] = client
            self._phone_code_hash[account_id] = sent.phone_code_hash
            return account_id

    async def submit_code(self, account_id: int, code: str) -> str:
        """Returns the resulting account status."""
        client = self.client(account_id)
        async with transaction() as s:
            row = await accounts_store.get_account(s, account_id)
        if row is None:
            raise LoginError("unknown account")
        try:
            await client.sign_in(
                row["phone"], code, phone_code_hash=self._phone_code_hash.get(account_id)
            )
        except SessionPasswordNeededError:
            await self._set_status(account_id, AccountStatus.PENDING_PASSWORD)
            return AccountStatus.PENDING_PASSWORD
        self._phone_code_hash.pop(account_id, None)
        await self._activate(account_id, client)
        return AccountStatus.ACTIVE

    async def submit_password(self, account_id: int, password: str) -> str:
        client = self.client(account_id)
        await client.sign_in(password=password)
        self._phone_code_hash.pop(account_id, None)
        await self._activate(account_id, client)
        return AccountStatus.ACTIVE

    async def remove(self, account_id: int) -> None:
        client = self._clients.pop(account_id, None)
        self._phone_code_hash.pop(account_id, None)
        self._dialog_cache.pop(account_id, None)
        for hook in self.gone_hooks:
            await hook(account_id)
        if client is not None:
            try:
                await client.log_out()
            except Exception:  # noqa: BLE001 — session may already be dead server-side
                await client.disconnect()
        async with transaction() as s:
            await accounts_store.delete_account(s, account_id)
        for suffix in ("", "-journal"):
            path = Path(f"{self._session_path(account_id)}.session{suffix}")
            path.unlink(missing_ok=True)

    # ---- dialogs ---------------------------------------------------------------------

    async def get_dialogs(self, account_id: int, refresh: bool = False) -> list[DialogInfo]:
        """Dialog list, fetched from Telegram once per process unless `refresh` is set."""
        cached = self._dialog_cache.get(account_id)
        if cached is not None and not refresh:
            return cached
        client = self.client(account_id)
        result = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            result.append(
                DialogInfo(
                    tg_chat_id=dialog.id,
                    title=dialog.name or str(dialog.id),
                    kind=_dialog_kind(entity),
                    username=getattr(entity, "username", None),
                )
            )
        self._dialog_cache[account_id] = result
        return result
