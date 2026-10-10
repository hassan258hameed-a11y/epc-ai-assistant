"""Pull readable text out of an attached document.

The assistant cannot open a file, so whatever a student attaches has to become
text in the prompt. That text costs tokens, which shapes every decision here:

* a hard character budget, because a 200-page PDF would empty a balance in one
  message,
* page and sheet markers, so the model can cite where something came from,
* an honest note when content was truncated, so neither the student nor the
  model is misled about having seen the whole thing.

Extraction never raises on a malformed file. A document we cannot read returns
an explanatory note instead, because failing the whole message over one bad
attachment is worse than answering without it.
"""
from __future__ import annotations

import csv
import io
import json
import logging

logger = logging.getLogger("AI.extract")

#: Roughly 4 characters per token, so this is about 6,000 tokens of prompt.
#: Large enough for a report or a problem set, small enough that one attachment
#: cannot silently drain a balance.
MAX_CHARS = 24_000

#: Per-format structural caps, applied before the character budget.
MAX_PDF_PAGES = 40
MAX_SHEET_ROWS = 300
MAX_CSV_ROWS = 300


class ExtractionResult:
    def __init__(self, text: str, *, pages: int = 0, truncated: bool = False, note: str = ""):
        self.text = text
        self.pages = pages
        self.truncated = truncated
        self.note = note

    def __bool__(self) -> bool:
        return bool(self.text.strip())

    @property
    def char_count(self) -> int:
        return len(self.text)


def _clip(text: str) -> tuple[str, bool]:
    """Trim to the budget on a line boundary where possible."""
    if len(text) <= MAX_CHARS:
        return text, False
    cut = text[:MAX_CHARS]
    boundary = cut.rfind("\n")
    if boundary > MAX_CHARS * 0.6:
        cut = cut[:boundary]
    return cut, True


# ---------------------------------------------------------------------------
# Per-format readers
# ---------------------------------------------------------------------------
def _read_pdf(raw: bytes) -> ExtractionResult:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover — dependency is pinned
        return ExtractionResult("", note="PDF reading is not available on this server.")

    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception:
        logger.info("Unreadable PDF attachment")
        return ExtractionResult("", note="This PDF could not be opened — it may be corrupt.")

    if getattr(reader, "is_encrypted", False):
        # An empty password unlocks many "protected" PDFs; if it does not, say so.
        try:
            if reader.decrypt("") == 0:
                return ExtractionResult(
                    "", note="This PDF is password protected, so its text could not be read."
                )
        except Exception:
            return ExtractionResult(
                "", note="This PDF is password protected, so its text could not be read."
            )

    total_pages = len(reader.pages)
    chunks: list[str] = []
    for index, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
        try:
            body = (page.extract_text() or "").strip()
        except Exception:
            body = ""
        if body:
            chunks.append(f"--- Page {index} ---\n{body}")

    if not chunks:
        # Almost always a scan: images of text, with no text layer to read.
        return ExtractionResult(
            "",
            pages=total_pages,
            note=(
                "This PDF has no readable text layer — it looks like a scan or photos. "
                "Try attaching the pages as images instead."
            ),
        )

    text, clipped = _clip("\n\n".join(chunks))
    truncated = clipped or total_pages > MAX_PDF_PAGES
    note = ""
    if truncated:
        shown = min(total_pages, MAX_PDF_PAGES)
        note = f"Only the first {shown} of {total_pages} pages were read."
    return ExtractionResult(text, pages=total_pages, truncated=truncated, note=note)


def _read_docx(raw: bytes) -> ExtractionResult:
    try:
        import docx
    except ImportError:  # pragma: no cover
        return ExtractionResult("", note="Word reading is not available on this server.")

    try:
        document = docx.Document(io.BytesIO(raw))
    except Exception:
        logger.info("Unreadable DOCX attachment")
        return ExtractionResult("", note="This Word document could not be opened.")

    parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]

    # Tables carry the data in most coursework documents, so they come too.
    for number, table in enumerate(document.tables, start=1):
        rows = []
        for row in table.rows[:MAX_SHEET_ROWS]:
            cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            parts.append(f"--- Table {number} ---\n" + "\n".join(rows))

    if not parts:
        return ExtractionResult("", note="This Word document appears to be empty.")

    text, truncated = _clip("\n\n".join(parts))
    return ExtractionResult(
        text, truncated=truncated,
        note="The document was longer than the reading limit and was cut short." if truncated else "",
    )


def _read_xlsx(raw: bytes) -> ExtractionResult:
    try:
        from openpyxl import load_workbook
    except ImportError:  # pragma: no cover
        return ExtractionResult("", note="Spreadsheet reading is not available on this server.")

    try:
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception:
        logger.info("Unreadable XLSX attachment")
        return ExtractionResult("", note="This workbook could not be opened.")

    parts, truncated = [], False
    try:
        for sheet in workbook.worksheets:
            rows = []
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                if index >= MAX_SHEET_ROWS:
                    truncated = True
                    break
                cells = ["" if value is None else str(value) for value in row]
                if any(cell.strip() for cell in cells):
                    rows.append(" | ".join(cells))
            if rows:
                parts.append(f"--- Sheet: {sheet.title} ---\n" + "\n".join(rows))
    finally:
        workbook.close()

    if not parts:
        return ExtractionResult("", note="This workbook has no data in it.")

    text, clipped = _clip("\n\n".join(parts))
    truncated = truncated or clipped
    return ExtractionResult(
        text, truncated=truncated,
        note=f"Only the first {MAX_SHEET_ROWS} rows of each sheet were read." if truncated else "",
    )


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return ""


def _read_csv(raw: bytes) -> ExtractionResult:
    body = _decode(raw)
    if not body.strip():
        return ExtractionResult("", note="This CSV file is empty.")

    try:
        reader = csv.reader(io.StringIO(body))
        rows = []
        truncated = False
        for index, row in enumerate(reader):
            if index >= MAX_CSV_ROWS:
                truncated = True
                break
            rows.append(" | ".join(cell.strip() for cell in row))
    except csv.Error:
        # Not really CSV — fall back to showing it as plain text.
        text, clipped = _clip(body)
        return ExtractionResult(text, truncated=clipped)

    text, clipped = _clip("\n".join(rows))
    truncated = truncated or clipped
    return ExtractionResult(
        text, truncated=truncated,
        note=f"Only the first {MAX_CSV_ROWS} rows were read." if truncated else "",
    )


def _read_json(raw: bytes) -> ExtractionResult:
    body = _decode(raw)
    try:
        # Re-dump so the model sees consistent, readable indentation.
        pretty = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        pretty = body
    text, truncated = _clip(pretty)
    return ExtractionResult(
        text, truncated=truncated,
        note="The file was longer than the reading limit and was cut short." if truncated else "",
    )


def _read_text(raw: bytes) -> ExtractionResult:
    body = _decode(raw)
    if not body.strip():
        return ExtractionResult("", note="This file is empty.")
    text, truncated = _clip(body)
    return ExtractionResult(
        text, truncated=truncated,
        note="The file was longer than the reading limit and was cut short." if truncated else "",
    )


READERS = {
    "pdf": _read_pdf,
    "docx": _read_docx,
    "xlsx": _read_xlsx,
    "pptx": lambda raw: ExtractionResult(
        "", note="PowerPoint files cannot be read yet. Export the slides as a PDF and attach that."
    ),
    "csv": _read_csv,
    "json": _read_json,
    "txt": _read_text,
    "md": _read_text,
}


def extract_text(raw: bytes, ext: str) -> ExtractionResult:
    """Read a document's text. Never raises — an unreadable file returns a note."""
    reader = READERS.get((ext or "").lower())
    if reader is None:
        return ExtractionResult("", note=f"Files of type .{ext} cannot be read.")
    try:
        return reader(raw)
    except Exception:  # pragma: no cover — a reader bug must not break a message
        logger.exception("Extraction failed for a .%s attachment", ext)
        return ExtractionResult("", note="This file could not be read.")


def as_prompt_block(name: str, result: ExtractionResult) -> str:
    """Format one document for the prompt, clearly fenced and labelled.

    The boundary markers matter: without them a model can mistake document
    content for the student's own instructions.
    """
    if not result:
        return f'[Attached file "{name}": {result.note or "no readable content."}]'

    header = f'--- BEGIN ATTACHED FILE: {name} ---'
    footer = f"--- END ATTACHED FILE: {name} ---"
    note = f"\n[{result.note}]" if result.note else ""
    return f"{header}\n{result.text}{note}\n{footer}"
