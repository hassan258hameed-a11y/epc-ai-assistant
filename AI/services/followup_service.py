"""Suggested follow-up questions, produced inside the reply itself.

Asking a second request for suggestions would bill the student for a whole
extra call on every message. Instead the model is asked to end its answer with
a short marked block, which is cut off here before the answer is saved — so the
suggestions cost a few dozen output tokens, and the answer the student reads,
copies, shares or exports never contains them.

If the model leaves the block out, or writes it badly, the answer is kept
whole and simply has no suggestions. Nothing here can lose part of an answer
that was not the block.
"""
from __future__ import annotations

import re

from AI.services import settings_service

MARKER = "[[FOLLOWUPS]]"
MAX_SUGGESTIONS = 3
MAX_LENGTH = 140

#: Re-sent with every request, so kept as short as it can be while still being
#: followed. Greetings and thank-yous never see it (see question_profile).
INSTRUCTION = (
    "Then write a line with only " + MARKER + " and three questions the student "
    "might ask next, one per line starting \"- \", under 12 words each, in their "
    "voice and language. Don't mention this block; omit it after greetings, "
    "thanks or picture requests."
)

#: The marker as models actually write it: bolded, in backticks, a heading,
#: different case, with or without the hyphen.
_MARKER_RE = re.compile(
    r"(?:^|\n)[ \t>#*_`]*\[\[\s*follow[\s-]*ups?\s*\]\][ \t*_`:]*(?=\n|$)",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•–]|\d+[.)])\s*")


def enabled() -> bool:
    return bool(settings_service.get("ai_followups_enabled", True))


def split(content: str) -> tuple[str, list[str]]:
    """``(answer, suggestions)`` from a raw reply."""
    text = content or ""
    matches = list(_MARKER_RE.finditer(text))
    if not matches:
        return text.strip(), []
    last = matches[-1]
    answer = text[: last.start()].rstrip()
    # A horizontal rule the model put above the block belongs to the block.
    answer = re.sub(r"\n\s*(?:-{3,}|\*{3,}|_{3,})\s*$", "", answer).rstrip()
    if not answer:
        # A reply that is *only* a block is not an answer with suggestions;
        # keep what was written rather than returning nothing.
        return text.strip(), []

    suggestions: list[str] = []
    seen: set[str] = set()
    for line in text[last.end():].splitlines():
        cleaned = _BULLET_RE.sub("", line).strip().strip("*_`\"'“”").strip()
        if not cleaned:
            continue
        if len(cleaned) > MAX_LENGTH:
            cleaned = cleaned[: MAX_LENGTH - 1].rstrip() + "…"
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        suggestions.append(cleaned)
        if len(suggestions) >= MAX_SUGGESTIONS:
            break
    return answer, suggestions


_PARTIAL_MARKER_RE = re.compile(r"(?:^|\n)[ \t>#*_`]*\[\[[^\n]*$")


def trim_partial_marker(answer: str) -> str:
    """An answer stopped while the marker was being written, minus that line."""
    return _PARTIAL_MARKER_RE.sub("", answer or "").rstrip()


class StreamFilter:
    """Passes a streamed reply on, minus the suggestion block.

    The block comes last, so everything before its marker line is forwarded as
    it arrives. A line that could still turn out to be the marker — it starts
    with "[" or with nothing but markdown decoration so far — is held until
    its end is seen; after the marker, nothing more is forwarded. The saved
    answer is cut by :func:`split` as usual, so this only decides what the
    student sees while the answer is being written.
    """

    _DECORATION = " \t>#*_`"

    def __init__(self, emit):
        self._emit = emit
        self._buffer = ""
        self._sent = 0
        self._closed = False

    def feed(self, text: str) -> None:
        if self._closed or not text:
            return
        self._buffer += text
        found = _MARKER_RE.search(self._buffer, max(0, self._sent - 1))
        if found:
            self._forward(found.start())
            self._closed = True
            return
        line_start = self._buffer.rfind("\n") + 1
        tail = self._buffer[line_start:].lstrip(self._DECORATION)
        self._forward(line_start if (not tail or tail.startswith("[")) else len(self._buffer))

    def _forward(self, end: int) -> None:
        if end > self._sent:
            self._emit(self._buffer[self._sent:end])
            self._sent = end
