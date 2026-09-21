from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    api_id: int = Field(description="Telegram API id from my.telegram.org", alias="TELEGRAM_API_ID")
    api_hash: str = Field(
        description="Telegram API hash from my.telegram.org", alias="TELEGRAM_API_HASH"
    )
    data_dir: Path = Field(default=Path("data"), description="Database and session files")
    download_dir: Path = Field(default=Path("downloads"), description="Root for downloaded media")
    path_template: str = Field(
        default="{chat}/{msg_id}_{name}",
        description="str.format template of the file path under DOWNLOAD_DIR; "
        "chats can override it",
    )
    concurrency_per_account: int = Field(default=3, ge=1, le=10)
    request_size: int = Field(default=512 * 1024, description="Bytes per MTProto download request")
    max_attempts: int = Field(
        default=5, description="Failed attempts before a file is marked failed"
    )
    proxy: str | None = Field(
        default=None,
        description="socks5://user:pass@host:port, http://host:port or mtproxy://host:port/secret",
    )
    host: str = "0.0.0.0"
    port: int = 8000

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"


settings = Settings()
