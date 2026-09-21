import logging
from pathlib import Path

from app.config import Settings
from app.events import EventBus
from app.telegram.accounts import AccountManager
from app.telegram.downloader import Downloader
from app.telegram.scanner import Scanner

log = logging.getLogger(__name__)


class Runtime:
    """All long-lived background components, wired together once per process."""

    def __init__(self, settings: Settings, bus: EventBus, parts_dir: Path) -> None:
        self.settings = settings
        self.bus = bus
        self.accounts = AccountManager(settings, bus)
        self.scanner = Scanner(self.accounts, bus)
        self.downloader = Downloader(settings, self.accounts, bus, parts_dir)
        self.scanner.on_files_added = self.downloader.wake

    async def start(self) -> None:
        self.settings.download_dir.mkdir(parents=True, exist_ok=True)
        await self.downloader.start()
        await self.accounts.start()

    async def stop(self) -> None:
        await self.scanner.stop()
        await self.downloader.stop()
        await self.accounts.stop()
