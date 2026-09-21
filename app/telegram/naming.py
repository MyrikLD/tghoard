import re
import string
from datetime import datetime
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from app.telegram.media import safe_name

CAPTION_LIMIT = 100
_WS = re.compile(r"\s+")


class PathContext(BaseModel):
    """Everything a path template may reference."""

    chat: str = Field(description="Chat title, filesystem-safe")
    chat_id: int
    msg_id: int
    doc_id: int
    date: datetime = Field(description="Message date; supports {date:%Y-%m}")
    name: str = Field(description="File name with extension")
    stem: str
    ext: str = Field(description="Extension without the dot")
    kind: str
    caption: str = Field(description="Whole caption, whitespace collapsed, truncated")
    caption_line: str = Field(description="First line of the caption")


PLACEHOLDERS = tuple(PathContext.model_fields)


class TemplateError(ValueError):
    pass


def _clean(text: str, limit: int) -> str:
    text = _WS.sub(" ", text).strip()
    return safe_name(text, limit) if text else ""


def build_context(
    chat_title: str,
    chat_id: int,
    msg_id: int,
    doc_id: int,
    date: datetime,
    name: str,
    kind: str,
    caption: str,
) -> PathContext:
    stem, dot, ext = name.rpartition(".")
    if not dot or len(ext) > 8:
        stem, ext = name, ""
    first_line = caption.strip().splitlines()[0] if caption.strip() else ""
    return PathContext(
        chat=safe_name(chat_title, 80),
        chat_id=chat_id,
        msg_id=msg_id,
        doc_id=doc_id,
        date=date,
        name=name,
        stem=stem,
        ext=ext,
        kind=kind,
        caption=_clean(caption, CAPTION_LIMIT),
        caption_line=_clean(first_line, CAPTION_LIMIT),
    )


def context_for(row) -> PathContext:
    """PathContext from a `files` row joined with its chat (see store.files.FILE_WITH_CHAT)."""
    return build_context(
        chat_title=row["chat_title"],
        chat_id=row["tg_chat_id"],
        msg_id=row["message_id"],
        doc_id=row["doc_id"],
        date=row["msg_date"],
        name=row["name"],
        kind=row["kind"],
        caption=row["caption"],
    )


def validate_template(template: str) -> None:
    """Raise TemplateError for syntax errors or unknown placeholders."""
    if not template.strip():
        raise TemplateError("template is empty")
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(template) if f is not None]
    except ValueError as e:
        raise TemplateError(str(e)) from None
    unknown = sorted({f.split(".")[0].split("[")[0] for f in fields} - set(PLACEHOLDERS))
    if unknown:
        raise TemplateError(f"unknown placeholder(s): {', '.join(unknown)}")
    if not fields:
        raise TemplateError("template has no placeholders, every file would get the same path")


def render_path(template: str, ctx: PathContext) -> PurePosixPath:
    """Relative path under the download root. Never escapes it, never yields empty segments."""
    validate_template(template)
    try:
        rendered = template.format(**ctx.model_dump())
    except (KeyError, ValueError, AttributeError, IndexError) as e:
        raise TemplateError(f"cannot render template: {e}") from None
    raw = [seg.strip() for seg in rendered.replace("\\", "/").split("/")]
    segments = [safe_name(seg) for seg in raw if seg and seg not in (".", "..")]
    if not segments:
        raise TemplateError("template rendered to an empty path")
    return PurePosixPath(*segments)


def resolve_target(root: Path, template: str, ctx: PathContext) -> tuple[Path, str]:
    """(directory, file name) for a file under `root`."""
    rel = render_path(template, ctx)
    return root / rel.parent, rel.name
