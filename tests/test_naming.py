from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from app.telegram.naming import TemplateError, build_context, render_path, validate_template


def ctx(**overrides):
    base = dict(
        chat_title="My: Channel",
        chat_id=-100123,
        msg_id=42,
        doc_id=7,
        date=datetime(2024, 5, 3, 12, 0, tzinfo=UTC),
        name="серия 1.mp4",
        kind="video",
        caption="Show — Season 2\nEpisode 1 of 10",
    )
    base.update(overrides)
    return build_context(**base)


def test_default_template_is_unique_per_message():
    assert render_path("{chat}/{msg_id}_{name}", ctx()) == PurePosixPath(
        "My_ Channel/42_серия 1.mp4"
    )


def test_caption_and_date_folders():
    path = render_path("{chat}/{date:%Y}/{caption_line} - {stem}.{ext}", ctx())
    assert path == PurePosixPath("My_ Channel/2024/Show — Season 2 - серия 1.mp4")


def test_empty_caption_leaves_no_empty_segment():
    path = render_path("{chat}/{caption_line}/{name}", ctx(caption=""))
    assert path == PurePosixPath("My_ Channel/серия 1.mp4")


def test_cannot_escape_download_root():
    path = render_path("../../{name}", ctx())
    assert path == PurePosixPath("серия 1.mp4")
    path = render_path("{caption_line}/{name}", ctx(caption="../etc"))
    assert path == PurePosixPath("_etc/серия 1.mp4")


@pytest.mark.parametrize(
    "template",
    ["", "static.mp4", "{nope}", "{name", "{date:%Y"],
)
def test_invalid_templates_rejected(template):
    with pytest.raises(TemplateError):
        validate_template(template)
    with pytest.raises(TemplateError):
        render_path(template, ctx())


def test_caption_is_collapsed_and_truncated():
    c = ctx(caption="a  b\n\nc" + "x" * 300)
    assert c.caption.startswith("a b c")
    assert len(c.caption) <= 100
    assert c.caption_line == "a b"
