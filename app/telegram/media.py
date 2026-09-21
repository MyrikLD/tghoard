import mimetypes
import re
from datetime import datetime

from pydantic import BaseModel, Field
from telethon.tl import types
from telethon.tl.custom.message import Message

KIND_FILTERS = {
    "video": types.InputMessagesFilterVideo,
    "document": types.InputMessagesFilterDocument,
    "photo": types.InputMessagesFilterPhotos,
    "audio": types.InputMessagesFilterMusic,
}

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class FileMeta(BaseModel):
    message_id: int
    doc_id: int = Field(description="Telegram document/photo id, stable across chats")
    kind: str
    name: str
    mime: str | None
    size: int
    date: datetime
    caption: str = ""
    grouped_id: int | None = Field(default=None, description="Album id shared by its messages")


def safe_name(name: str, limit: int = 180) -> str:
    name = _UNSAFE.sub("_", name).strip(" .")
    if len(name) > limit:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 8:
            name = stem[: limit - len(ext) - 1] + "." + ext
        else:
            name = name[:limit]
    return name or "file"


def _document_name(doc: types.Document, message_id: int) -> str:
    for attr in doc.attributes:
        if isinstance(attr, types.DocumentAttributeFilename) and attr.file_name:
            return attr.file_name
    ext = mimetypes.guess_extension(doc.mime_type or "") or ".bin"
    if ext == ".jpe":
        ext = ".jpg"
    return f"{message_id}_{doc.id}{ext}"


def _document_kind(doc: types.Document) -> str:
    for attr in doc.attributes:
        if isinstance(attr, types.DocumentAttributeVideo):
            return "video"
        if isinstance(attr, types.DocumentAttributeAudio):
            return "audio"
    mime = doc.mime_type or ""
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "document"


def extract(message: Message) -> FileMeta | None:
    """Metadata for the downloadable media of a message, or None if it has none."""
    media = message.media
    if isinstance(media, types.MessageMediaDocument) and isinstance(media.document, types.Document):
        doc = media.document
        return FileMeta(
            message_id=message.id,
            doc_id=doc.id,
            kind=_document_kind(doc),
            name=safe_name(_document_name(doc, message.id)),
            mime=doc.mime_type,
            size=doc.size,
            date=message.date,
            caption=message.message or "",
            grouped_id=message.grouped_id,
        )
    if isinstance(media, types.MessageMediaPhoto) and isinstance(media.photo, types.Photo):
        photo = media.photo
        sizes = [
            s for s in photo.sizes if isinstance(s, types.PhotoSize | types.PhotoSizeProgressive)
        ]
        if not sizes:
            return None
        largest = max(sizes, key=lambda s: s.w * s.h)
        size = largest.size if isinstance(largest, types.PhotoSize) else max(largest.sizes)
        return FileMeta(
            message_id=message.id,
            doc_id=photo.id,
            kind="photo",
            name=f"{message.id}_{photo.id}.jpg",
            mime="image/jpeg",
            size=size,
            date=message.date,
            caption=message.message or "",
            grouped_id=message.grouped_id,
        )
    return None


def passes_filters(
    meta: FileMeta, kinds: list[str], min_size: int | None, max_size: int | None
) -> bool:
    if meta.kind not in kinds:
        return False
    if min_size is not None and meta.size < min_size:
        return False
    if max_size is not None and meta.size > max_size:
        return False
    return True
