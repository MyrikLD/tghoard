import errno
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.telegram.downloader import (
    IncompleteDownload,
    part_path,
    promote,
    resume_offset,
    unique_final_path,
    write_stream,
)


async def chunks(data: bytes, size: int = 4) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


async def test_full_download_promotes_to_final(tmp_path: Path):
    part = part_path(tmp_path, 1)
    data = b"0123456789abcdef"
    seen = []
    written = await write_stream(chunks(data), part, 0, len(data), seen.append)
    assert written == len(data)
    assert seen[-1] == len(data)
    final = promote(part, tmp_path / "chat", "video.mp4", len(data))
    assert final == tmp_path / "chat" / "video.mp4"
    assert final.read_bytes() == data
    assert not part.exists()


async def test_short_stream_keeps_part_and_never_promotes(tmp_path: Path):
    part = part_path(tmp_path, 2)
    with pytest.raises(IncompleteDownload):
        await write_stream(chunks(b"12345678"), part, 0, 100, lambda _: None)
    assert part.stat().st_size == 8
    with pytest.raises(ValueError):
        promote(part, tmp_path, "x.bin", 100)
    assert not (tmp_path / "x.bin").exists()


async def test_empty_stream_is_a_failure(tmp_path: Path):
    part = part_path(tmp_path, 3)
    with pytest.raises(IncompleteDownload):
        await write_stream(chunks(b""), part, 0, 0, lambda _: None)


async def test_resume_appends_after_existing_bytes(tmp_path: Path):
    part = part_path(tmp_path, 4)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"abcd")
    offset = resume_offset(part, 8)
    assert offset == 4
    await write_stream(chunks(b"efgh"), part, offset, 8, lambda _: None)
    assert part.read_bytes() == b"abcdefgh"


async def test_oversized_part_is_discarded(tmp_path: Path):
    part = part_path(tmp_path, 5)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"x" * 20)
    assert resume_offset(part, 10) == 0
    assert not part.exists()


async def test_overlong_stream_raises(tmp_path: Path):
    part = part_path(tmp_path, 6)
    with pytest.raises(ValueError):
        await write_stream(chunks(b"123456"), part, 0, 4, lambda _: None)


def test_unique_final_path_never_overwrites(tmp_path: Path):
    (tmp_path / "a.mp4").write_bytes(b"1")
    (tmp_path / "a (2).mp4").write_bytes(b"1")
    assert unique_final_path(tmp_path, "a.mp4") == tmp_path / "a (3).mp4"
    assert unique_final_path(tmp_path, "noext") == tmp_path / "noext"
    (tmp_path / "noext").write_bytes(b"1")
    assert unique_final_path(tmp_path, "noext") == tmp_path / "noext (2)"


def test_promote_across_filesystems_falls_back_to_copy(tmp_path: Path, monkeypatch):
    part = part_path(tmp_path / "tmp", 7)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"payload")
    real_replace = os.replace
    calls = []

    def fake_replace(src, dst):
        calls.append(Path(src).name)
        if Path(src) == part:
            raise OSError(errno.EXDEV, "cross-device")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fake_replace)
    final = promote(part, tmp_path / "dl", "v.mp4", 7)
    assert final.read_bytes() == b"payload"
    assert not part.exists()
    assert calls == ["7.part", "v.mp4.copying"]
    assert not (tmp_path / "dl" / "v.mp4.copying").exists()
