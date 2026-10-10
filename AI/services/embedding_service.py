"""Search by meaning: answers as vectors, found again by what they mean.

Each assistant answer is embedded together with the question that prompted it
and the chat's title ("Chat: …\\nQ: …\\nA: …", about 2,000 characters at most),
through any OpenAI-compatible ``/embeddings`` endpoint. Vectors are unit length,
stored as float32 bytes in :class:`AI.models.MessageEmbedding`, and compared
in-process with numpy — at this scale a matrix product over one student's
vectors is faster than a round trip to a vector database, and there is nothing
extra to run.

Rules that hold everywhere in here:

* **One student's vectors only.** Every read is filtered by the requesting
  user, twice (the row's ``user`` and its conversation's owner).
* **The trash is invisible.** Trashed chats are neither indexed nor returned;
  purging a chat deletes its vectors with its messages (CASCADE).
* **Indexing never breaks anything.** :func:`index_pending` logs and returns
  instead of raising, so a provider outage costs a search result, never a page.
* **The key never reaches a log.** Provider error bodies are redacted before
  they are logged or raised.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import timedelta
from typing import Iterable

import numpy as np
import requests
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F, Min, OuterRef, Q, Subquery
from django.utils import timezone

from AI.models import Conversation, Message, MessageEmbedding, MessageRole, MessageStatus

logger = logging.getLogger("AI.embeddings")

#: Inputs per request. OpenAI accepts far more, but a smaller batch keeps one
#: slow request short and one rejected input from sinking many others.
BATCH_SIZE = 64
#: What gets embedded per answer. Enough for the gist of a long answer; the
#: embedding of 2,000 characters says as much as the embedding of 20,000.
MAX_TEXT_CHARS = 2000
QUESTION_CHARS = 600
TITLE_CHARS = 160
#: Below this cosine similarity a match is noise, not meaning. Tuned for
#: text-embedding-3-small at 256 dimensions, where unrelated pairs sit around
#: 0.05-0.2 and a genuine match of a short query against a long answer at 0.3+.
MIN_SCORE = 0.25
#: Rows loaded per step when scoring, so a student with many thousands of
#: vectors never holds them all at once (2,000 x 256 x 4 bytes = 2 MB).
VECTOR_CHUNK = 2000
#: How many recently active chats are re-checked for edited or renamed text on
#: each ordinary indexing pass. The management command checks them all.
RECHECK_CONVERSATIONS = 20
RECHECK_MESSAGES = 600
QUERY_CACHE_SECONDS = 600
MIN_QUERY_CHARS = 3
MAX_QUERY_CHARS = 500
SNIPPET_WIDTH = 70
DEFAULT_TITLE = "new chat"

#: The real transport, captured at import. The test suite must never reach a
#: paid API by accident (the development .env carries a real key), so under
#: ``manage.py test`` a call goes out only when a test has replaced it.
_REAL_POST = requests.post


class EmbeddingError(Exception):
    """The embeddings endpoint could not produce vectors.

    ``str(exc)`` is for the log (already redacted); :attr:`friendly_message` is
    what a student may see.
    """

    FRIENDLY = {
        "not_configured": "Search by meaning hasn't been set up on this site yet.",
        "timeout": "Search by meaning took too long to answer. Please try again.",
        "network": "Search by meaning couldn't reach its service. Please try again in a moment.",
        "rate_limited": "Search by meaning is busy right now. Please try again in a moment.",
    }
    DEFAULT_FRIENDLY = "Search by meaning isn't available right now. Keyword search still works."

    def __init__(self, message: str, *, code: str = "error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    @property
    def friendly_message(self) -> str:
        return self.FRIENDLY.get(self.code, self.DEFAULT_FRIENDLY)


# ---------------------------------------------------------------------------
# Configuration — read at call time so a changed setting applies at once
# ---------------------------------------------------------------------------
def model_name() -> str:
    return (getattr(settings, "EMBEDDING_MODEL", "") or "text-embedding-3-small").strip()


def dimensions() -> int:
    return int(getattr(settings, "EMBEDDING_DIMENSIONS", 256) or 256)


def _api_key() -> str:
    return (getattr(settings, "EMBEDDING_API_KEY", "") or "").strip()


def _base_url() -> str:
    return (getattr(settings, "EMBEDDING_BASE_URL", "") or "https://api.openai.com/v1").rstrip("/")


def _timeout() -> int:
    return int(getattr(settings, "EMBEDDING_TIMEOUT", 20) or 20)


def is_configured() -> bool:
    """An API key is present. Never says which, or what it is."""
    return bool(_api_key())


def is_switched_on() -> bool:
    """The administrator's switch in the console settings."""
    from AI.services import settings_service

    try:
        return bool(settings_service.get("semantic_search_enabled", True))
    except Exception:  # pragma: no cover - settings table missing mid-migrate
        return True


def is_enabled() -> bool:
    return is_switched_on() and is_configured()


def availability() -> tuple[bool, str, str]:
    """``(enabled, reason, code)`` — the reason is written for a student."""
    if not is_switched_on():
        return False, "Search by meaning is switched off on this site.", "disabled"
    if not is_configured():
        return False, EmbeddingError.FRIENDLY["not_configured"], "not_configured"
    return True, "", ""


def _network_allowed() -> bool:
    if not getattr(settings, "TESTING", False):
        return True
    return requests.post is not _REAL_POST


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------
_KEYISH = re.compile(r"\b(?:sk|pk|rk|key)-[A-Za-z0-9_\-*.]{6,}", re.IGNORECASE)


def _redact(text) -> str:
    """Provider text made safe to log: the key, and anything shaped like one, removed."""
    text = str(text or "")
    key = _api_key()
    if key:
        text = text.replace(key, "[redacted]")
    return _KEYISH.sub("[redacted]", text)[:300]


def _provider_message(response) -> str:
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            return _redact(error.get("message") or error.get("code") or "")
        if isinstance(error, str):
            return _redact(error)
    except ValueError:
        pass
    return _redact(getattr(response, "text", "") or "")


def _normalise(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    if norm > 0 and np.isfinite(norm):
        vector = vector / norm
    return vector.astype(np.float32, copy=False)


def _fit(vector: np.ndarray, dims: int) -> np.ndarray:
    """The vector at the configured size, unit length.

    text-embedding-3 vectors keep their meaning when truncated (that is what the
    ``dimensions`` parameter does server-side), so an endpoint that ignores the
    parameter still gives usable vectors. A shorter vector cannot be fixed.
    """
    if vector.shape[0] < dims:
        raise EmbeddingError(
            f"The embeddings endpoint returned {vector.shape[0]} dimensions; "
            f"EMBEDDING_DIMENSIONS is {dims}.",
            code="bad_response",
        )
    return _normalise(vector[:dims])


def _embed_batch(batch: list[str]) -> list[np.ndarray]:
    model, dims = model_name(), dimensions()
    payload = {"model": model, "input": batch, "dimensions": dims}
    headers = {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}
    try:
        response = requests.post(
            f"{_base_url()}/embeddings", headers=headers, data=json.dumps(payload), timeout=_timeout(),
        )
    except requests.Timeout:
        raise EmbeddingError("The embeddings request timed out.", code="timeout", retryable=True) from None
    except requests.ConnectionError:
        raise EmbeddingError("Could not reach the embeddings endpoint.", code="network", retryable=True) from None
    except requests.RequestException as exc:
        raise EmbeddingError(f"Embeddings transport failure ({type(exc).__name__}).", code="transport") from None

    status = int(getattr(response, "status_code", 0) or 0)
    if status in (401, 403):
        raise EmbeddingError(
            f"The embeddings endpoint rejected the API key (HTTP {status}): {_provider_message(response)}",
            code="auth",
        )
    if status == 429:
        raise EmbeddingError(
            f"The embeddings endpoint is rate limiting or out of quota: {_provider_message(response)}",
            code="rate_limited", retryable=True,
        )
    if status >= 500:
        raise EmbeddingError(f"The embeddings endpoint failed (HTTP {status}).", code="provider_down", retryable=True)
    if status >= 400:
        raise EmbeddingError(
            f"The embeddings endpoint refused the request (HTTP {status}): {_provider_message(response)}",
            code="bad_request",
        )

    try:
        body = response.json()
    except ValueError:
        raise EmbeddingError("The embeddings endpoint returned an unreadable body.", code="bad_response") from None
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list) or len(rows) != len(batch) or not all(isinstance(r, dict) for r in rows):
        raise EmbeddingError(
            f"Expected {len(batch)} embeddings, got {len(rows) if isinstance(rows, list) else 'none'}.",
            code="bad_response",
        )

    ordered = sorted(enumerate(rows), key=lambda pair: (pair[1].get("index", pair[0]), pair[0]))
    vectors = []
    for _, row in ordered:
        values = row.get("embedding")
        try:
            vector = np.asarray(values, dtype=np.float32)
        except (TypeError, ValueError):
            raise EmbeddingError("An embedding was not a list of numbers.", code="bad_response") from None
        if vector.ndim != 1 or vector.size == 0 or not np.all(np.isfinite(vector)):
            raise EmbeddingError("An embedding was empty or malformed.", code="bad_response")
        vectors.append(_fit(vector, dims))
    return vectors


def embed_texts(texts: Iterable[str]) -> list[np.ndarray]:
    """One unit-length float32 vector per text, in order.

    Raises :class:`EmbeddingError` for anything that stops that happening —
    no key, no network, a rejected key, a quota, a malformed reply.
    """
    # Sent as given (the endpoint rejects an empty string): what is hashed is
    # exactly what is embedded.
    items = [(str(t or "").strip() or " ") for t in texts]
    if not items:
        return []
    if not is_configured():
        raise EmbeddingError("No embeddings API key is configured.", code="not_configured")
    if not _network_allowed():
        raise EmbeddingError("The test suite does not call the embeddings API.", code="offline")
    vectors: list[np.ndarray] = []
    for start in range(0, len(items), BATCH_SIZE):
        vectors.extend(_embed_batch(items[start:start + BATCH_SIZE]))
    return vectors


def to_bytes(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype="<f4").tobytes()


def from_bytes(blob) -> np.ndarray:
    return np.frombuffer(bytes(blob), dtype="<f4")


# ---------------------------------------------------------------------------
# What gets embedded
# ---------------------------------------------------------------------------
def _flat(text) -> str:
    return " ".join(str(text or "").split())


def compose_text(title: str, question: str, answer: str) -> str:
    """The text that stands for one answer: the chat, the question, the answer."""
    parts = []
    title = _flat(title)[:TITLE_CHARS]
    if title and title.lower() != DEFAULT_TITLE:
        parts.append(f"Chat: {title}")
    question = _flat(question)
    if question:
        parts.append(f"Q: {question[:QUESTION_CHARS].rstrip()}")
    head = "\n".join(parts)
    room = max(200, MAX_TEXT_CHARS - len(head) - 4)
    parts.append(f"A: {_flat(answer)[:room].rstrip()}")
    return "\n".join(parts)[:MAX_TEXT_CHARS]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _answers(user=None):
    """Every answer that can be searched: delivered, non-empty, not in the trash."""
    qs = Message.objects.filter(
        role=MessageRole.ASSISTANT,
        status=MessageStatus.OK,
        conversation__deleted_at__isnull=True,
        content__regex=r"\S",
    )
    if user is not None:
        qs = qs.filter(conversation__user=user)
    return qs


def _needs_vector(qs):
    """Answers with no vector, or one from another model or size."""
    return qs.filter(
        Q(embedding__isnull=True)
        | ~Q(embedding__model_name=model_name())
        | ~Q(embedding__dimensions=dimensions())
    )


def _with_question(qs):
    """Annotate each answer with the student's message just before it.

    Ties on ``created_at`` are broken by id: the clock is coarse on some
    platforms and a question and its answer can share a timestamp.
    """
    earlier = (
        Message.objects.filter(conversation_id=OuterRef("conversation_id"), role=MessageRole.USER)
        .filter(Q(created_at__lt=OuterRef("created_at")) | Q(created_at=OuterRef("created_at"), id__lt=OuterRef("id")))
        .order_by("-created_at", "-id")
        .values("content")[:1]
    )
    return qs.annotate(question_text=Subquery(earlier))


def _text_for(message) -> str:
    return compose_text(message.conversation.title, getattr(message, "question_text", "") or "", message.content)


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------
def stale_message_ids(user=None, *, recent_only: bool = True) -> list[int]:
    """Answers whose vector no longer matches their text (edited, renamed chat).

    ``recent_only`` looks only at the most recently active chats touched since
    they were indexed — cheap enough to run before every search. Without it,
    every chat with vectors is checked (the management command). Vectors of
    answers that are no longer searchable (emptied, failed) are dropped here.
    """
    conversations = Conversation.objects.all()
    if user is not None:
        conversations = conversations.filter(user=user)
    conversations = conversations.annotate(oldest_vector=Min("embeddings__created_at")).filter(
        oldest_vector__isnull=False
    )
    if recent_only:
        conversations = conversations.filter(updated_at__gte=F("oldest_vector"))
    ids = list(conversations.order_by("-updated_at", "-id").values_list("id", flat=True))
    if recent_only:
        ids = ids[:RECHECK_CONVERSATIONS]

    stale: list[int] = []
    orphaned: list[int] = []
    checked = 0
    for start in range(0, len(ids), 100):
        rows = (
            _with_question(Message.objects.filter(conversation_id__in=ids[start:start + 100], embedding__isnull=False))
            .select_related("conversation", "embedding")
            .order_by("-created_at", "-id")
        )
        if recent_only:
            rows = rows[: max(0, RECHECK_MESSAGES - checked)]
        for message in rows:
            checked += 1
            if (
                message.role != MessageRole.ASSISTANT
                or message.status != MessageStatus.OK
                or not (message.content or "").strip()
            ):
                orphaned.append(message.pk)
            elif text_hash(_text_for(message)) != message.embedding.text_hash:
                stale.append(message.pk)
        if recent_only and checked >= RECHECK_MESSAGES:
            break
    if orphaned:
        MessageEmbedding.objects.filter(message_id__in=orphaned).delete()
    return stale


def pending_ids(user=None, *, limit: int | None = None) -> list[int]:
    """Answers with no current vector, newest first."""
    qs = _needs_vector(_answers(user)).order_by("-created_at", "-id").values_list("id", flat=True)
    return list(qs[:limit] if limit is not None else qs)


def _store(chunk, vectors, model: str, dims: int) -> int:
    now = timezone.now()
    objs = [
        MessageEmbedding(
            message_id=message.pk,
            user_id=message.conversation.user_id,
            conversation_id=message.conversation_id,
            model_name=model,
            dimensions=dims,
            vector=to_bytes(vector),
            text_hash=digest,
            created_at=now,
        )
        for (message, _text, digest), vector in zip(chunk, vectors)
    ]
    try:
        with transaction.atomic():
            MessageEmbedding.objects.bulk_create(
                objs,
                update_conflicts=True,
                unique_fields=["message"],
                update_fields=["user", "conversation", "model_name", "dimensions", "vector", "text_hash", "created_at"],
            )
        return len(objs)
    except IntegrityError:
        # A message disappeared mid-batch (regenerated, deleted, purged).
        # Save the others one at a time and let the missing one go.
        saved = 0
        for obj in objs:
            try:
                with transaction.atomic():
                    MessageEmbedding.objects.update_or_create(
                        message_id=obj.message_id,
                        defaults={
                            "user_id": obj.user_id,
                            "conversation_id": obj.conversation_id,
                            "model_name": obj.model_name,
                            "dimensions": obj.dimensions,
                            "vector": obj.vector,
                            "text_hash": obj.text_hash,
                            "created_at": now,
                        },
                    )
                saved += 1
            except IntegrityError:
                continue
        return saved


def _index(message_ids: Iterable[int]) -> tuple[int, EmbeddingError | None]:
    """Embed and store these answers, batch by batch.

    Returns how many were saved and the error that stopped it, if one did —
    batches already saved stay saved.
    """
    ids = list(dict.fromkeys(int(pk) for pk in message_ids))
    if not ids:
        return 0, None
    messages = list(
        _with_question(_answers().filter(pk__in=ids)).select_related("conversation").order_by("-created_at", "-id")
    )
    items = []
    for message in messages:
        text = _text_for(message)
        items.append((message, text, text_hash(text)))

    model, dims = model_name(), dimensions()
    saved = 0
    for start in range(0, len(items), BATCH_SIZE):
        chunk = items[start:start + BATCH_SIZE]
        try:
            vectors = embed_texts([text for _message, text, _digest in chunk])
        except EmbeddingError as exc:
            return saved, exc
        saved += _store(chunk, vectors, model, dims)
    return saved, None


def index_messages(message_ids: Iterable[int]) -> int:
    """Embed these answers now. Raises :class:`EmbeddingError` on a provider failure."""
    saved, error = _index(message_ids)
    if error is not None:
        raise error
    return saved


def index_pending(user=None, limit: int = 64) -> int:
    """Embed up to ``limit`` answers that have no current vector. Never raises.

    Edited answers and renamed chats come first, then answers never indexed,
    newest first — so what a student just asked is findable straight away.
    Chats in the trash are skipped. Safe to call as often as you like: with
    nothing to do it is two small queries.
    """
    try:
        if limit <= 0 or not is_enabled() or not _network_allowed():
            return 0
        ids = stale_message_ids(user, recent_only=True)[:limit]
        if len(ids) < limit:
            seen = set(ids)
            for pk in pending_ids(user, limit=limit + len(seen)):
                if len(ids) >= limit:
                    break
                if pk not in seen:
                    ids.append(pk)
        saved, error = _index(ids)
        if error is not None:
            logger.warning("Search by meaning: indexing stopped (%s): %s", error.code, error)
        return saved
    except Exception:
        logger.exception("Search by meaning: indexing failed")
        return 0


def drop_foreign_vectors(user=None) -> int:
    """Delete vectors made by another model or at another size. Returns how many."""
    qs = MessageEmbedding.objects.exclude(model_name=model_name(), dimensions=dimensions())
    if user is not None:
        qs = qs.filter(user=user)
    deleted, _ = qs.delete()
    return deleted


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------
def _query_vector(query: str) -> np.ndarray:
    model, dims = model_name(), dimensions()
    key = "semsearch:q:" + hashlib.sha256(f"{model}|{dims}|{query.lower()}".encode("utf-8")).hexdigest()
    try:
        cached = cache.get(key)
    except Exception:  # pragma: no cover - a broken cache must not break search
        cached = None
    if isinstance(cached, (bytes, bytearray)) and len(cached) == dims * 4:
        return from_bytes(cached)
    vector = embed_texts([query])[0]
    try:
        cache.set(key, to_bytes(vector), QUERY_CACHE_SECONDS)
    except Exception:  # pragma: no cover
        pass
    return vector


_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
_QUOTE = re.compile(r"(?m)^\s*>\s?")
_EMPHASIS = re.compile(r"[`*]+")
_WORD = re.compile(r"\w{4,}")


def excerpt(text: str, query: str = "", width: int = SNIPPET_WIDTH) -> str:
    """About ``2 * width`` characters of an answer, as plain text.

    Centred on a word of the query when the answer uses one; a match by
    meaning often shares no words at all, and then it is the opening.
    """
    plain = _EMPHASIS.sub("", _QUOTE.sub("", _HEADING.sub("", text or "")))
    flat = _flat(plain)
    if len(flat) <= width * 2:
        return flat
    lowered = flat.lower()
    at, needle = -1, ""
    for word in sorted(set(_WORD.findall((query or "").lower())), key=len, reverse=True):
        at = lowered.find(word)
        if at >= 0:
            needle = word
            break
    if at < 0:
        return flat[: width * 2].rstrip() + "…"
    start = max(0, at - width)
    end = min(len(flat), at + len(needle) + width)
    # Cut at word boundaries: "…ants turn light" reads as a typo.
    if start:
        space = flat.find(" ", start, at)
        start = space + 1 if space != -1 else start
    if end < len(flat):
        space = flat.rfind(" ", at + len(needle), end)
        end = space if space != -1 else end
    return ("…" if start else "") + flat[start:end].strip() + ("…" if end < len(flat) else "")


def search(user, query: str, *, limit: int = 8, include_archived: bool = False, days: int | None = None) -> list[dict]:
    """The student's chats that answer ``query`` by meaning, best first.

    One result per chat — its best-matching answer. Raises
    :class:`EmbeddingError` when the query itself cannot be embedded, so the
    caller can say why there are no results.
    """
    query = _flat(query)[:MAX_QUERY_CHARS]
    if len(query) < MIN_QUERY_CHARS or user is None or not getattr(user, "pk", None) or not is_enabled():
        return []

    # So a chat from a minute ago is already findable.
    index_pending(user, limit=64)
    target = _query_vector(query)

    dims = dimensions()
    rows = MessageEmbedding.objects.filter(
        user=user,
        conversation__user=user,
        conversation__deleted_at__isnull=True,
        model_name=model_name(),
        dimensions=dims,
    )
    if not include_archived:
        rows = rows.filter(conversation__is_archived=False)
    if days:
        rows = rows.filter(message__created_at__gte=timezone.now() - timedelta(days=int(days)))

    best: dict[int, tuple[float, int]] = {}
    width = dims * 4
    last_pk = 0
    while True:
        chunk = list(
            rows.filter(pk__gt=last_pk).order_by("pk")
            .values_list("pk", "message_id", "conversation_id", "vector")[:VECTOR_CHUNK]
        )
        if not chunk:
            break
        last_pk = chunk[-1][0]
        usable = [row for row in chunk if row[3] is not None and len(row[3]) == width]
        if usable:
            matrix = np.frombuffer(b"".join(bytes(row[3]) for row in usable), dtype="<f4").reshape(len(usable), dims)
            scores = matrix @ target
            for index in np.nonzero(scores >= MIN_SCORE)[0].tolist():
                _pk, message_id, conversation_id, _vector = usable[index]
                score = float(scores[index])
                current = best.get(conversation_id)
                if current is None or (score, message_id) > current:
                    best[conversation_id] = (score, message_id)
        if len(chunk) < VECTOR_CHUNK:
            break

    ranked = sorted(best.items(), key=lambda item: (-item[1][0], -item[1][1]))[: max(1, int(limit))]
    if not ranked:
        return []
    messages = {
        message.pk: message
        for message in Message.objects.filter(
            pk__in=[message_id for _conversation, (_score, message_id) in ranked],
            conversation__user=user,
            conversation__deleted_at__isnull=True,
        ).select_related("conversation")
    }
    results = []
    for _conversation_id, (score, message_id) in ranked:
        message = messages.get(message_id)
        if message is None:
            continue
        conversation = message.conversation
        results.append({
            "conversation_id": conversation.pk,
            "message_id": message.pk,
            "title": conversation.title,
            "snippet": excerpt(message.content, query),
            "score": round(score, 3),
            "is_archived": conversation.is_archived,
            "created_at": timezone.localtime(message.created_at).isoformat(),
        })
    return results


# ---------------------------------------------------------------------------
# For the health page and the management command
# ---------------------------------------------------------------------------
def indexed_count(user=None) -> int:
    qs = MessageEmbedding.objects.filter(
        model_name=model_name(), dimensions=dimensions(), conversation__deleted_at__isnull=True,
    )
    if user is not None:
        qs = qs.filter(user=user)
    return qs.count()


def pending_count(user=None) -> int:
    return _needs_vector(_answers(user)).count()


def health() -> dict:
    """Database-only facts for the health page. No outbound call."""
    return {
        "switched_on": is_switched_on(),
        "configured": is_configured(),
        "model": model_name(),
        "dimensions": dimensions(),
        "indexed": indexed_count(),
        "pending": pending_count(),
    }
