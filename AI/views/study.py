"""Study sets: the Study panel, making a set, taking it.

Making a set is an AI request, so it runs through the same limits and error
shapes as a chat reply — the composer's error handling works unchanged.
Taking one is free.
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from AI.models import Conversation, MessageRole, StudyKind, StudySet
from AI.services import study_service
from AI.services.ai_service import AIError
from AI.services.chat_service import ChatBlocked
from AI.services.model_service import ModelUnavailable
from AI.utils import ratelimit
from AI.utils.decorators import login_required_student
from AI.utils.pagination import page_range, paginate
from AI.utils.ratelimit import RateLimited
from AI.utils.request import json_error, json_ok
from AI.views.chat import _balance_for, _blocked_response, _requested_model
from AI.views.panels import render_panel

logger = logging.getLogger("AI.views.study")


def _panel_context(request) -> dict:
    user = request.user
    kind = request.GET.get("kind", "")
    sets = study_service.sets_for(user)
    counts = {
        "all": sets.count(),
        "flashcards": sets.filter(kind=StudyKind.FLASHCARDS).count(),
        "quiz": sets.filter(kind=StudyKind.QUIZ).count(),
    }
    if kind in StudyKind.values:
        sets = sets.filter(kind=kind)
    page = paginate(request, sets, per_page=18)
    preference = getattr(user, "chat_preference", None)
    return {
        "page": page,
        "page_numbers": page_range(page),
        "sets": page.object_list,
        "kind": kind if kind in StudyKind.values else "",
        "counts": counts,
        "today": study_service.today(user, goal=getattr(preference, "daily_goal", 0) or 0),
        # Chats worth studying from: ones with at least one answer, newest first.
        "sources": list(
            Conversation.objects.filter(user=user, is_archived=False, messages__role=MessageRole.ASSISTANT)
            .distinct().order_by("-updated_at").only("id", "title")[:40]
        ),
        "study_counts": study_service.COUNTS,
        "default_count": study_service.DEFAULT_COUNT,
    }


@login_required_student
def study_panel(request):
    """Every set the student has made, and a way to make another."""
    if request.method == "POST" and request.POST.get("action") == "delete":
        study_set = StudySet.objects.filter(pk=request.POST.get("set_id") or 0, user=request.user).first()
        if study_set:
            title = study_set.title
            study_set.delete()
            messages.success(request, f"“{title}” deleted.")
    return render_panel(request, "study", _panel_context(request))


@login_required_student
@require_POST
def study_generate(request):
    user = request.user
    try:
        ratelimit.enforce("ai_request", str(user.pk))
    except RateLimited as exc:
        return json_error(exc.friendly_message, status=429, code="rate_limited")

    conversation = Conversation.objects.filter(pk=request.POST.get("conversation_id") or 0, user=user).first()
    if conversation is None:
        return json_error("That conversation is not available.", status=404, code="not_found")
    try:
        count = int(request.POST.get("count") or study_service.DEFAULT_COUNT)
    except ValueError:
        count = study_service.DEFAULT_COUNT

    try:
        study_set = study_service.generate(
            user, conversation, request.POST.get("kind", ""), count=count, model_key=_requested_model(request),
        )
    except study_service.StudyError as exc:
        return json_error(exc.friendly_message, status=400, code=exc.code)
    except ModelUnavailable as exc:
        return json_error(exc.friendly_message, status=409, code=exc.code)
    except ChatBlocked as exc:
        return _blocked_response(exc, user, request)
    except AIError as exc:
        return json_error(exc.friendly_message, status=502, code=exc.code, retryable=exc.retryable,
                          **_balance_for(request, user))
    except Exception:  # pragma: no cover — never leak a traceback to the UI
        logger.exception("Study set failed for user=%s", user.pk)
        return json_error("Something went wrong. Please try again.", status=500, code="server_error")

    return json_ok(set=study_service.payload(study_set, shuffle_options=True), **_balance_for(request, user))


@login_required_student
def study_detail(request, pk: int):
    study_set = get_object_or_404(StudySet, pk=pk, user=request.user)
    return json_ok(set=study_service.payload(study_set, shuffle_options=True))


@login_required_student
@require_POST
def study_attempt(request, pk: int):
    study_set = get_object_or_404(StudySet, pk=pk, user=request.user)
    try:
        score = int(request.POST.get("score") or 0)
    except ValueError:
        score = 0
    study_service.record_attempt(study_set, score)
    return json_ok(best_score=study_set.best_score, attempts=study_set.attempts)
