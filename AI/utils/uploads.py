"""Secure image upload handling.

User supplied filenames and Content-Type headers are never trusted.  Files are
verified by decoding them with Pillow, re-encoded to strip metadata and any
embedded payload, and stored under a random server-generated name.
"""
from __future__ import annotations

import base64
import io

from django.conf import settings
from django.core.files.uploadedfile import InMemoryUploadedFile
from PIL import Image, UnidentifiedImageError

# Pillow format -> (extension, mime)
ALLOWED_FORMATS = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
    "GIF": ("gif", "image/gif"),
}


class InvalidUpload(Exception):
    """Raised with a message safe to show to the end user."""


class ValidatedImage:
    def __init__(self, file: InMemoryUploadedFile, fmt: str, mime: str, ext: str, width: int, height: int, size: int):
        self.file = file
        self.format = fmt
        self.mime = mime
        self.ext = ext
        self.width = width
        self.height = height
        self.size = size

    def as_data_url(self) -> str:
        self.file.seek(0)
        encoded = base64.b64encode(self.file.read()).decode("ascii")
        self.file.seek(0)
        return f"data:{self.mime};base64,{encoded}"


def validate_image(uploaded) -> ValidatedImage:
    """Validate and normalise an uploaded image.

    Raises :class:`InvalidUpload` with a friendly message on any problem.
    """
    if uploaded is None:
        raise InvalidUpload("No image was received.")

    max_bytes = settings.UPLOAD_MAX_IMAGE_BYTES
    if uploaded.size and uploaded.size > max_bytes:
        raise InvalidUpload(f"Image is too large. Maximum size is {max_bytes // (1024 * 1024)} MB.")

    raw = uploaded.read()
    uploaded.seek(0)
    if not raw:
        raise InvalidUpload("The image file is empty.")
    if len(raw) > max_bytes:
        raise InvalidUpload(f"Image is too large. Maximum size is {max_bytes // (1024 * 1024)} MB.")

    # Decode twice: verify() invalidates the object, so re-open for processing.
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        raise InvalidUpload("That file is not a valid image.")

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise InvalidUpload("That image could not be read.")

    fmt = (image.format or "").upper()
    if fmt not in ALLOWED_FORMATS:
        raise InvalidUpload("Unsupported format. Please upload a JPG, PNG, WEBP or GIF image.")

    ext, mime = ALLOWED_FORMATS[fmt]

    # Re-encode as PNG/JPEG to strip EXIF, ICC and any appended payload.
    max_dim = settings.UPLOAD_MAX_IMAGE_DIMENSION
    if max(image.size) > max_dim:
        image.thumbnail((max_dim, max_dim), Image.LANCZOS)

    buffer = io.BytesIO()
    if fmt in {"JPEG", "GIF"}:
        image = image.convert("RGB")
        image.save(buffer, format="JPEG", quality=85, optimize=True)
        ext, mime, fmt = "jpg", "image/jpeg", "JPEG"
    else:
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA")
        image.save(buffer, format="PNG", optimize=True)
        ext, mime, fmt = "png", "image/png", "PNG"

    buffer.seek(0)
    size = buffer.getbuffer().nbytes
    cleaned = InMemoryUploadedFile(
        buffer, field_name="image", name=f"upload.{ext}", content_type=mime, size=size, charset=None
    )
    return ValidatedImage(cleaned, fmt, mime, ext, image.width, image.height, size)


def validate_spreadsheet(uploaded, *, max_bytes: int = 4 * 1024 * 1024) -> tuple[bytes, str]:
    """Validate a CSV/XLSX upload by magic bytes, not by filename."""
    if uploaded is None:
        raise InvalidUpload("No file was received.")
    if uploaded.size and uploaded.size > max_bytes:
        raise InvalidUpload(f"File is too large. Maximum size is {max_bytes // (1024 * 1024)} MB.")

    raw = uploaded.read()
    uploaded.seek(0)
    if not raw:
        raise InvalidUpload("The file is empty.")

    if raw[:2] == b"PK":  # XLSX is a zip container
        return raw, "xlsx"

    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            raw.decode(encoding)
            return raw, "csv"
        except UnicodeDecodeError:
            continue

    raise InvalidUpload("Unsupported file. Upload a .csv or .xlsx file.")


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
#: Extension -> (label, mime, magic prefixes). A file is accepted only when its
#: *content* matches one of the prefixes; the filename is a hint, never proof.
#: An empty prefix tuple means "plain text", checked by attempting a decode.
DOCUMENT_TYPES = {
    "pdf":  ("PDF document",   "application/pdf", (b"%PDF-",)),
    "docx": ("Word document",  "application/vnd.openxmlformats-officedocument.wordprocessingml.document", (b"PK",)),
    "xlsx": ("Excel workbook", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", (b"PK",)),
    "pptx": ("PowerPoint deck", "application/vnd.openxmlformats-officedocument.presentationml.presentation", (b"PK",)),
    "csv":  ("CSV file",       "text/csv", ()),
    "txt":  ("Text file",      "text/plain", ()),
    "md":   ("Markdown file",  "text/markdown", ()),
    "json": ("JSON file",      "application/json", ()),
}

#: Never accepted, whatever the content looks like. Listed explicitly so the
#: refusal message can be specific rather than a generic "unsupported".
DANGEROUS_EXTENSIONS = {
    "exe", "dll", "bat", "cmd", "com", "scr", "msi", "ps1", "sh", "bash", "zsh",
    "js", "mjs", "vbs", "jar", "app", "apk", "deb", "rpm", "php", "py", "rb", "pl",
    "html", "htm", "svg", "xhtml",   # can carry script when served back
}


class ValidatedDocument:
    def __init__(self, file, *, ext: str, mime: str, label: str, size: int, name: str):
        self.file = file
        self.ext = ext
        self.mime = mime
        self.label = label
        self.size = size
        self.name = name

    @property
    def is_text(self) -> bool:
        return self.ext in {"csv", "txt", "md", "json"}


def safe_display_name(filename: str, *, fallback: str = "attachment") -> str:
    """A filename safe to show and to store as a label.

    Strips any path, control characters and anything that could be read as
    markup. The stored file always gets a random server-generated name; this is
    only ever displayed.
    """
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char.isprintable() and char not in '<>:"|?*')
    name = name.strip().strip(".")
    return name[:120] or fallback


def validate_document(uploaded, *, max_bytes: int | None = None) -> ValidatedDocument:
    """Validate an uploaded document by its content.

    Raises :class:`InvalidUpload` with a message safe to show to the student.
    """
    if uploaded is None:
        raise InvalidUpload("No file was received.")

    max_bytes = max_bytes or getattr(settings, "UPLOAD_MAX_DOCUMENT_BYTES", 10 * 1024 * 1024)
    limit_mb = max_bytes // (1024 * 1024)

    if uploaded.size and uploaded.size > max_bytes:
        raise InvalidUpload(f"That file is too large. The maximum is {limit_mb} MB.")

    display = safe_display_name(getattr(uploaded, "name", ""))
    ext = display.rsplit(".", 1)[-1].lower() if "." in display else ""

    if ext in DANGEROUS_EXTENSIONS:
        raise InvalidUpload("That file type cannot be uploaded for security reasons.")
    if ext not in DOCUMENT_TYPES:
        allowed = ", ".join(sorted(DOCUMENT_TYPES)).upper()
        raise InvalidUpload(f"Unsupported file type. Allowed: {allowed}.")

    raw = uploaded.read()
    uploaded.seek(0)
    if not raw:
        raise InvalidUpload("That file is empty.")
    if len(raw) > max_bytes:
        raise InvalidUpload(f"That file is too large. The maximum is {limit_mb} MB.")

    label, mime, prefixes = DOCUMENT_TYPES[ext]

    if prefixes:
        if not any(raw.startswith(prefix) for prefix in prefixes):
            raise InvalidUpload(f"That file does not look like a valid {label.lower()}.")
    else:
        # Text-ish formats: prove it decodes rather than trusting the extension.
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise InvalidUpload("That file could not be read as text.")

    uploaded.seek(0)
    return ValidatedDocument(
        uploaded, ext=ext, mime=mime, label=label, size=len(raw), name=display
    )
