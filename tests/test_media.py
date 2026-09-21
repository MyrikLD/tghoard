from datetime import UTC, datetime
from types import SimpleNamespace

from telethon.tl import types

from app.telegram.media import extract, passes_filters, safe_name


def _message(media, msg_id: int = 42):
    return SimpleNamespace(
        id=msg_id,
        media=media,
        date=datetime(2024, 5, 1, tzinfo=UTC),
        message="caption text",
        grouped_id=None,
    )


def _document(attributes, mime="video/mp4", size=12345, doc_id=99):
    return types.Document(
        id=doc_id,
        access_hash=1,
        file_reference=b"",
        date=datetime(2024, 5, 1, tzinfo=UTC),
        mime_type=mime,
        size=size,
        dc_id=2,
        attributes=attributes,
        thumbs=None,
        video_thumbs=None,
    )


def test_video_document_with_filename():
    doc = _document(
        [
            types.DocumentAttributeVideo(duration=1, w=1, h=1),
            types.DocumentAttributeFilename("Clip: one?.mp4"),
        ]
    )
    meta = extract(_message(types.MessageMediaDocument(document=doc)))
    assert meta.kind == "video"
    assert meta.name == "Clip_ one_.mp4"
    assert meta.size == 12345
    assert meta.doc_id == 99
    assert meta.caption == "caption text"


def test_mp4_sent_as_plain_file_is_video_by_mime():
    doc = _document([], mime="video/mp4")
    meta = extract(_message(types.MessageMediaDocument(document=doc), msg_id=7))
    assert meta.kind == "video"
    assert meta.name == "7_99.mp4"


def test_photo_uses_largest_size():
    photo = types.Photo(
        id=5,
        access_hash=1,
        file_reference=b"",
        date=datetime(2024, 5, 1, tzinfo=UTC),
        sizes=[
            types.PhotoSize(type="s", w=10, h=10, size=100),
            types.PhotoSize(type="x", w=800, h=600, size=50000),
        ],
        dc_id=2,
    )
    meta = extract(_message(types.MessageMediaPhoto(photo=photo)))
    assert meta.kind == "photo"
    assert meta.size == 50000


def test_no_media():
    assert extract(_message(None)) is None


def test_filters():
    doc = _document([types.DocumentAttributeVideo(duration=1, w=1, h=1)], size=5_000_000)
    meta = extract(_message(types.MessageMediaDocument(document=doc)))
    assert passes_filters(meta, ["video"], None, None)
    assert not passes_filters(meta, ["photo"], None, None)
    assert not passes_filters(meta, ["video"], 10_000_000, None)
    assert not passes_filters(meta, ["video"], None, 1_000_000)


def test_safe_name_truncates_keeping_extension():
    name = safe_name("a" * 300 + ".mp4")
    assert name.endswith(".mp4")
    assert len(name) <= 180
