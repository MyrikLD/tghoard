import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class AccountStatus:
    PENDING_CODE = "pending_code"
    PENDING_PASSWORD = "pending_password"
    ACTIVE = "active"
    ERROR = "error"


class ScanStatus:
    IDLE = "idle"
    SCANNING = "scanning"
    ERROR = "error"


class FileStatus:
    NEW = "new"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    DUPLICATE = "duplicate"

    ALL = (NEW, QUEUED, DOWNLOADING, DONE, FAILED, SKIPPED, DUPLICATE)


MEDIA_KINDS = ("video", "document", "photo", "audio")


class Account(Base):
    __tablename__ = "accounts"

    id = sa.Column(sa.Integer, primary_key=True)
    phone = sa.Column(sa.String, nullable=False, unique=True)
    display_name = sa.Column(sa.String, nullable=True)
    status = sa.Column(sa.String, nullable=False, default=AccountStatus.PENDING_CODE)
    error = sa.Column(sa.Text, nullable=True)
    paused = sa.Column(sa.Boolean, nullable=False, default=False)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


class Chat(Base):
    __tablename__ = "chats"
    __table_args__ = (sa.UniqueConstraint("account_id", "tg_chat_id"),)

    id = sa.Column(sa.Integer, primary_key=True)
    account_id = sa.Column(sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    tg_chat_id = sa.Column(sa.BigInteger, nullable=False)
    title = sa.Column(sa.String, nullable=False)
    kind = sa.Column(sa.String, nullable=False)
    enabled = sa.Column(sa.Boolean, nullable=False, default=True)
    watch = sa.Column(sa.Boolean, nullable=False, default=False)
    auto_queue = sa.Column(sa.Boolean, nullable=False, default=False)
    media_kinds = sa.Column(sa.JSON, nullable=False, default=lambda: ["video"])
    min_size = sa.Column(sa.BigInteger, nullable=True)
    max_size = sa.Column(sa.BigInteger, nullable=True)
    last_scanned_msg_id = sa.Column(sa.Integer, nullable=False, default=0)
    scan_status = sa.Column(sa.String, nullable=False, default=ScanStatus.IDLE)
    scan_error = sa.Column(sa.Text, nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        sa.UniqueConstraint("chat_id", "message_id"),
        sa.Index("ix_files_status_queued", "status", "queued_at"),
        sa.Index("ix_files_doc_id", "doc_id"),
        sa.Index("ix_files_chat_grouped", "chat_id", "grouped_id"),
    )

    id = sa.Column(sa.Integer, primary_key=True)
    chat_id = sa.Column(sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    message_id = sa.Column(sa.Integer, nullable=False)
    doc_id = sa.Column(sa.BigInteger, nullable=False)
    kind = sa.Column(sa.String, nullable=False)
    name = sa.Column(sa.String, nullable=False)
    mime = sa.Column(sa.String, nullable=True)
    size = sa.Column(sa.BigInteger, nullable=False)
    msg_date = sa.Column(sa.DateTime(timezone=True), nullable=False)
    caption = sa.Column(sa.Text, nullable=False, default="", server_default="")
    grouped_id = sa.Column(sa.BigInteger, nullable=True)
    status = sa.Column(sa.String, nullable=False, default=FileStatus.NEW)
    path = sa.Column(sa.String, nullable=True)
    downloaded_bytes = sa.Column(sa.BigInteger, nullable=False, default=0)
    attempts = sa.Column(sa.Integer, nullable=False, default=0)
    error = sa.Column(sa.Text, nullable=True)
    queued_at = sa.Column(sa.DateTime(timezone=True), nullable=True)
    finished_at = sa.Column(sa.DateTime(timezone=True), nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
