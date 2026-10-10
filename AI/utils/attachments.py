"""Turn uploaded files into things the chat pipeline can use.

One place decides what an attachment *is* — an image the model can look at, or a
document whose text has to be read out — so the view, the service and the model
layer all agree without each re-sniffing the file.
"""
from __future__ import annotations

import base64
import logging

from django.conf import settings

from AI.models import AttachmentKind
from AI.utils import extract
from AI.utils.uploads import (
    DOCUMENT_TYPES,
    InvalidUpload,
    safe_display_name,
    validate_document,
    validate_image,
)

logger = logging.getLogger("AI.attachments")

IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}


class PreparedAttachment:
    """A validated file, ready to be stored and sent."""

    def __init__(self, *, kind, file, name, ext, mime, size,
                 text: str = "", note: str = "", pages: int = 0):
        self.kind = kind
        self.file = file
        self.name = name
        self.ext = ext
        self.mime = mime
        self.size = size
        self.text = text
        self.note = note
        self.pages = pages

    def as_data_url(self) -> str:
        """Images only — the inline form the vision API expects."""
        self.file.seek(0)
        encoded = base64.b64encode(self.file.read()).decode("ascii")
        self.file.seek(0)
        return f"data:{self.mime};base64,{encoded}"

    @property
    def is_image(self) -> bool:
        return self.kind == AttachmentKind.IMAGE


def looks_like_image(uploaded) -> bool:
    """Decide which validator to run. Both then verify the content properly."""
    mime = (getattr(uploaded, "content_type", "") or "").lower()
    if mime in IMAGE_MIMES:
        return True
    name = safe_display_name(getattr(uploaded, "name", ""))
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in IMAGE_EXTS


def prepare(uploaded) -> PreparedAttachment:
    """Validate one upload and read it if it is a document.

    Raises :class:`InvalidUpload` with a message safe to show the student.
    """
    if looks_like_image(uploaded):
        image = validate_image(uploaded)
        return PreparedAttachment(
            kind=AttachmentKind.IMAGE,
            file=image.file,
            name=safe_display_name(getattr(uploaded, "name", ""), fallback=f"image.{image.ext}"),
            ext=image.ext,
            mime=image.mime,
            size=image.size,
        )

    document = validate_document(uploaded)

    document.file.seek(0)
    raw = document.file.read()
    document.file.seek(0)

    result = extract.extract_text(raw, document.ext)
    if not result and not result.note:
        raise InvalidUpload(f"No readable text could be found in {document.name}.")

    return PreparedAttachment(
        kind=AttachmentKind.DOCUMENT,
        file=document.file,
        name=document.name,
        ext=document.ext,
        mime=document.mime,
        size=document.size,
        text=result.text,
        note=result.note,
        pages=result.pages,
    )


def prepare_many(files, *, limit: int) -> list[PreparedAttachment]:
    """Validate a whole upload set, failing on the first bad file.

    Failing the batch rather than silently dropping one file matters: a student
    who attached three files and got an answer about two would have no way to
    tell, and would be billed for the wrong question.
    """
    files = list(files or [])
    if not files:
        return []
    if len(files) > limit:
        raise InvalidUpload(f"You can attach up to {limit} files to one message.")

    total = 0
    prepared = []
    for uploaded in files:
        item = prepare(uploaded)
        total += item.size
        prepared.append(item)

    ceiling = getattr(settings, "UPLOAD_MAX_TOTAL_BYTES", 20 * 1024 * 1024)
    if total > ceiling:
        raise InvalidUpload(
            f"Those files come to more than {ceiling // (1024 * 1024)} MB in total. "
            "Send them across a few messages."
        )
    return prepared


def accepted_extensions() -> list[str]:
    """For the file picker's `accept` attribute."""
    return sorted(IMAGE_EXTS | set(DOCUMENT_TYPES))
