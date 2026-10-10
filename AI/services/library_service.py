"""Answer feedback and the prompt library — the rules, in one place.

Views only translate HTTP into calls here. Every function takes the acting user
and checks ownership itself, so a view that forgets to cannot rate another
student's reply or delete another student's prompt.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from AI.models import (
    AIModel,
    FeedbackReason,
    FeedbackStatus,
    Message,
    MessageFeedback,
    MessageRole,
    SavedPrompt,
)

#: How many prompts one student may keep. A library, not a notebook.
MAX_PROMPTS_PER_STUDENT = 50
MAX_TITLE = 80
MAX_BODY = 4000


class LibraryError(Exception):
    """A request this module refuses, with a message safe to show."""

    def __init__(self, message: str, *, code: str = "invalid"):
        self.friendly_message = message
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------
def rate_message(user, message_id: int, rating: int, *, reason: str = "", comment: str | None = None):
    """Record, change or clear a student's verdict on one of their replies.

    ``rating`` is 1, -1 or 0; 0 removes the verdict. Only an assistant reply in
    the student's own conversation can be rated — anything else is reported as
    not found, so the endpoint never confirms another student's message exists.
    """
    message = (
        Message.objects.select_related("conversation")
        .filter(pk=message_id, conversation__user=user, role=MessageRole.ASSISTANT)
        .first()
    )
    if message is None:
        raise LibraryError("That reply could not be found.", code="not_found")

    try:
        rating = int(rating)
    except (TypeError, ValueError):
        raise LibraryError("Choose helpful or not helpful.")
    if rating not in (MessageFeedback.UP, MessageFeedback.DOWN, 0):
        raise LibraryError("Choose helpful or not helpful.")

    if rating == 0:
        MessageFeedback.objects.filter(message=message).delete()
        return None

    reason = reason if reason in FeedbackReason.values else ""
    if rating > 0:
        reason = ""        # a reason only explains a thumbs down
    key = (message.model_key or "").strip()
    defaults = {
        "user": user,
        "rating": rating,
        "reason": reason,
        "model_key": key,
        "ai_model": AIModel.objects.filter(key=key).first() if key else None,
        # A changed verdict is new information: it goes back into the review
        # queue, however the last one was handled.
        "status": FeedbackStatus.OPEN,
        "reviewed_by": None,
        "reviewed_at": None,
    }
    # Picking a reason after the thumb is a second request without a comment;
    # it must not wipe a comment the student already wrote.
    if comment is not None or rating > 0:
        defaults["comment"] = (comment or "").strip()[:500] if rating < 0 else ""
    feedback, _ = MessageFeedback.objects.update_or_create(message=message, defaults=defaults)
    return feedback


def ratings_for(user, message_ids) -> dict[int, dict]:
    """``{message_id: {"rating": 1, "reason": ""}}`` for a transcript's replies."""
    rows = MessageFeedback.objects.filter(user=user, message_id__in=list(message_ids))
    return {row.message_id: {"rating": row.rating, "reason": row.reason} for row in rows}


def quality_report(days: int = 30) -> dict:
    """Satisfaction per model over the last ``days``, for the console.

    A model nobody rated is still listed, with no percentage rather than 0%:
    "no opinion yet" and "everybody hated it" must never look the same.
    """
    since = timezone.now() - timedelta(days=days)
    recent = MessageFeedback.objects.filter(updated_at__gte=since)

    per_model = {
        row["model_key"]: row
        for row in recent.values("model_key").annotate(
            up=Count("id", filter=Q(rating__gt=0)),
            down=Count("id", filter=Q(rating__lt=0)),
        )
    }
    names = dict(AIModel.objects.values_list("key", "name"))
    models = list(AIModel.objects.order_by("sort_order", "name"))
    keys = [m.key for m in models] + [k for k in per_model if k not in names]

    rows = []
    for key in keys:
        counts = per_model.get(key, {"up": 0, "down": 0})
        total = counts["up"] + counts["down"]
        rows.append({
            "key": key,
            "name": names.get(key, key or "Unknown"),
            "up": counts["up"],
            "down": counts["down"],
            "total": total,
            "score": round(counts["up"] / total * 100) if total else None,
        })

    up = sum(r["up"] for r in rows)
    down = sum(r["down"] for r in rows)
    total = up + down

    reasons = [
        {"key": row["reason"], "label": FeedbackReason(row["reason"]).label, "count": row["n"]}
        for row in recent.filter(rating__lt=0).exclude(reason="")
        .values("reason").annotate(n=Count("id")).order_by("-n")
    ]
    reason_total = sum(r["count"] for r in reasons)
    for row in reasons:
        row["percent"] = round(row["count"] / reason_total * 100) if reason_total else 0

    return {
        "days": days,
        "rows": rows,
        "up": up,
        "down": down,
        "total": total,
        "score": round(up / total * 100) if total else None,
        "raters": recent.values("user_id").distinct().count(),
        "reasons": reasons,
    }


def recent_negative(days: int = 30):
    since = timezone.now() - timedelta(days=days)
    return (
        MessageFeedback.objects.filter(rating__lt=0, updated_at__gte=since)
        .select_related("user", "message", "message__conversation", "ai_model", "reviewed_by")
        .order_by("-updated_at")
    )


def review_counts(days: int = 30) -> dict[str, int]:
    """Thumbs-down in the window, by where they are in review."""
    rows = recent_negative(days).values("status").annotate(n=Count("id"))
    counts = {value: 0 for value in FeedbackStatus.values}
    counts.update({row["status"]: row["n"] for row in rows})
    counts["all"] = sum(counts[value] for value in FeedbackStatus.values)
    return counts


def review_feedback(admin, feedback: MessageFeedback, *, status: str, note: str = "") -> MessageFeedback:
    """An administrator's call on one thumbs-down."""
    if status not in FeedbackStatus.values:
        raise LibraryError("Choose a review status.")
    feedback.status = status
    feedback.admin_note = (note or "").strip()[:500]
    if status == FeedbackStatus.OPEN:
        feedback.reviewed_by, feedback.reviewed_at = None, None
    else:
        feedback.reviewed_by, feedback.reviewed_at = admin, timezone.now()
    # Not ``save()``: auto_now would move updated_at, and that is the time the
    # student rated — reviewing must not re-date their complaint.
    MessageFeedback.objects.filter(pk=feedback.pk).update(
        status=feedback.status, admin_note=feedback.admin_note,
        reviewed_by=feedback.reviewed_by, reviewed_at=feedback.reviewed_at,
    )
    return feedback


# ---------------------------------------------------------------------------
# Prompt library
# ---------------------------------------------------------------------------
def prompts_for(user) -> dict:
    """The featured prompts every student sees, then this student's own."""
    featured = list(SavedPrompt.objects.filter(user=None, is_featured=True, is_active=True))
    mine = list(SavedPrompt.objects.filter(user=user, is_active=True).order_by("-updated_at"))
    return {"featured": featured, "mine": mine}


def prompt_payload(prompt: SavedPrompt, user=None) -> dict:
    return {
        "id": prompt.pk,
        "title": prompt.title,
        "body": prompt.body,
        "category": prompt.category,
        "featured": prompt.user_id is None,
        "mine": user is not None and prompt.user_id == getattr(user, "pk", None),
    }


def _clean(title: str, body: str, category: str) -> tuple[str, str, str]:
    title = " ".join((title or "").split())[:MAX_TITLE]
    body = (body or "").strip()
    category = " ".join((category or "").split())[:40]
    if not title:
        raise LibraryError("Give the prompt a short title.")
    if not body:
        raise LibraryError("Write the prompt itself.")
    if len(body) > MAX_BODY:
        raise LibraryError(f"Keep the prompt under {MAX_BODY:,} characters.")
    return title, body, category


def create_prompt(user, *, title: str, body: str, category: str = "") -> SavedPrompt:
    title, body, category = _clean(title, body, category)
    with transaction.atomic():
        if SavedPrompt.objects.filter(user=user).count() >= MAX_PROMPTS_PER_STUDENT:
            raise LibraryError(
                f"You can keep up to {MAX_PROMPTS_PER_STUDENT} prompts. Delete one to add another.",
                code="limit",
            )
        return SavedPrompt.objects.create(
            user=user, title=title, body=body, category=category, created_by=user,
        )


def update_prompt(user, prompt_id, *, title: str, body: str, category: str = "") -> SavedPrompt:
    """Rewrite one of the student's own prompts. Featured ones are not theirs to edit."""
    prompt = SavedPrompt.objects.filter(pk=prompt_id, user=user).first()
    if prompt is None:
        raise LibraryError("That prompt could not be found.", code="not_found")
    prompt.title, prompt.body, prompt.category = _clean(title, body, category)
    prompt.save(update_fields=["title", "body", "category", "updated_at"])
    return prompt


def delete_prompt(user, prompt_id) -> None:
    deleted, _ = SavedPrompt.objects.filter(pk=prompt_id, user=user).delete()
    if not deleted:
        raise LibraryError("That prompt could not be found.", code="not_found")


def record_use(user, prompt_id) -> SavedPrompt:
    """Count one use of a prompt the student can see. Returns it for inserting."""
    prompt = (
        SavedPrompt.objects.filter(pk=prompt_id, is_active=True)
        .filter(Q(user=user) | Q(user=None, is_featured=True))
        .first()
    )
    if prompt is None:
        raise LibraryError("That prompt could not be found.", code="not_found")
    SavedPrompt.objects.filter(pk=prompt.pk).update(use_count=F("use_count") + 1)
    return prompt


# -- administrator ----------------------------------------------------------
def save_featured(admin, *, title: str, body: str, category: str = "",
                  sort_order: int = 100, prompt: SavedPrompt | None = None) -> SavedPrompt:
    title, body, category = _clean(title, body, category)
    if prompt is None:
        prompt = SavedPrompt(user=None, is_featured=True, created_by=admin)
    prompt.title, prompt.body, prompt.category = title, body, category
    prompt.sort_order = max(0, min(int(sort_order or 0), 32000))
    prompt.save()
    return prompt


def featured_queryset():
    return SavedPrompt.objects.filter(user=None, is_featured=True)


def library_stats() -> dict:
    student_prompts = SavedPrompt.objects.exclude(user=None)
    return {
        "featured": featured_queryset().filter(is_active=True).count(),
        "student_prompts": student_prompts.count(),
        "students_with_prompts": student_prompts.values("user_id").distinct().count(),
        "uses": sum(featured_queryset().values_list("use_count", flat=True))
        + sum(student_prompts.values_list("use_count", flat=True)),
    }
