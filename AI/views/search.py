"""Search by meaning — the JSON endpoint behind the palette's "by meaning" results.

``GET /api/chat/search/meaning/?q=…&archived=1&days=7|30|90|365``

Always answers ``200`` with ``{"ok": true, "enabled", "reason", "code",
"results"}`` except when the student is going too fast (``429``, the same
shape every other rate-limited endpoint uses). A switched-off or unconfigured
feature, a query too short to mean anything and a provider outage are all
ordinary answers with no results and a ``reason`` — never a 500.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.views.decorators.http import require_GET

from AI.services import embedding_service
from AI.services.embedding_service import EmbeddingError
from AI.utils import ratelimit
from AI.utils.decorators import login_required_student
from AI.utils.ratelimit import RateLimited
from AI.utils.request import json_error, json_ok

logger = logging.getLogger("AI.views.search")

#: Its own scope, so searching never eats into the student's chat allowance.
#: Override with RATE_LIMITS["semantic_search"] = (limit, window_seconds).
RATE_SCOPE = "semantic_search"
DEFAULT_RATE = (30, 60)
DAY_CHOICES = {"7", "30", "90", "365"}
RESULT_LIMIT = 8


def _answer(*, enabled: bool, reason: str = "", code: str = "", results=None, query: str = ""):
    return json_ok(enabled=enabled, reason=reason, code=code, query=query, results=results or [])


@login_required_student
@require_GET
def search_meaning(request):
    """The student's own chats that answer ``q`` by meaning, one result per chat."""
    query = " ".join((request.GET.get("q") or "").split())[: embedding_service.MAX_QUERY_CHARS]
    include_archived = request.GET.get("archived") == "1"
    days_param = (request.GET.get("days") or "").strip()
    days = int(days_param) if days_param in DAY_CHOICES else None

    enabled, reason, code = embedding_service.availability()
    if not enabled:
        return _answer(enabled=False, reason=reason, code=code, query=query)
    if len(query) < embedding_service.MIN_QUERY_CHARS:
        return _answer(enabled=True, code="too_short", query=query)

    limit, window = getattr(settings, "RATE_LIMITS", {}).get(RATE_SCOPE, DEFAULT_RATE)
    try:
        ratelimit.enforce(RATE_SCOPE, str(request.user.pk), limit=int(limit), window=int(window))
    except RateLimited as exc:
        return json_error(exc.friendly_message, status=429, code="rate_limited", retry_after=exc.retry_after)

    try:
        results = embedding_service.search(
            request.user, query, limit=RESULT_LIMIT, include_archived=include_archived, days=days,
        )
    except EmbeddingError as exc:
        logger.warning("Search by meaning failed for user %s (%s): %s", request.user.pk, exc.code, exc)
        return _answer(enabled=True, reason=exc.friendly_message, code=exc.code, query=query)
    except Exception:
        logger.exception("Search by meaning crashed for user %s", request.user.pk)
        return _answer(enabled=True, reason=EmbeddingError.DEFAULT_FRIENDLY, code="error", query=query)
    return _answer(enabled=True, results=results, query=query)
