"""What students tell us about answers, and the prompts they reuse.

Two small tables, both owned by a student and both read by the console:

* :class:`MessageFeedback` — a thumbs up or down on one assistant reply, with an
  optional reason. One row per reply: changing your mind edits the row rather
  than adding a second vote. The model is copied onto the row so a model that
  is later renamed or retired still has its history.
* :class:`SavedPrompt` — a reusable prompt. A row with a ``user`` is that
  student's own; a row with no user and ``is_featured`` set is one an
  administrator published to everybody.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class FeedbackReason(models.TextChoices):
    INACCURATE = "inaccurate", "Inaccurate"
    UNHELPFUL = "unhelpful", "Not helpful"
    INCOMPLETE = "incomplete", "Incomplete"
    TOO_LONG = "too_long", "Too long"
    UNSAFE = "unsafe", "Inappropriate"
    OTHER = "other", "Other"


class FeedbackStatus(models.TextChoices):
    """Where a thumbs-down is in an administrator's review."""

    OPEN = "open", "To review"
    REVIEWED = "reviewed", "Reviewed"
    RESOLVED = "resolved", "Resolved"


class MessageFeedback(models.Model):
    UP = 1
    DOWN = -1
    RATINGS = ((UP, "Helpful"), (DOWN, "Not helpful"))

    message = models.OneToOneField(
        "AI.Message", on_delete=models.CASCADE, related_name="feedback",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="message_feedback",
    )
    ai_model = models.ForeignKey(
        "AI.AIModel", null=True, blank=True, on_delete=models.SET_NULL, related_name="feedback",
    )
    #: Copied from the message, so the rating keeps its model if the row goes.
    model_key = models.CharField(max_length=64, blank=True, db_index=True)
    rating = models.SmallIntegerField(choices=RATINGS)
    reason = models.CharField(max_length=16, choices=FeedbackReason.choices, blank=True)
    comment = models.CharField(max_length=500, blank=True)
    #: An administrator's review of a thumbs-down. Irrelevant to a thumbs-up.
    status = models.CharField(max_length=10, choices=FeedbackStatus.choices, default=FeedbackStatus.OPEN,
                              db_index=True)
    admin_note = models.CharField(max_length=500, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["model_key", "rating"])]

    def __str__(self) -> str:
        return f"{self.model_key} {'+' if self.rating > 0 else '-'} ({self.user_id})"

    @property
    def is_positive(self) -> bool:
        return self.rating > 0


class SavedPrompt(models.Model):
    #: Blank for a prompt an administrator published to everybody.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE,
        related_name="saved_prompts",
    )
    title = models.CharField(max_length=80)
    body = models.TextField(max_length=4000)
    category = models.CharField(max_length=40, blank=True)
    #: Published by an administrator: shown to every student, above their own.
    is_featured = models.BooleanField(default=False, db_index=True)
    is_active = models.BooleanField(default=True)
    use_count = models.PositiveIntegerField(default=0)
    sort_order = models.PositiveSmallIntegerField(default=100)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "-updated_at"]

    def __str__(self) -> str:
        return self.title

    @property
    def preview(self) -> str:
        text = " ".join((self.body or "").split())
        return text if len(text) <= 140 else text[:137].rstrip() + "…"
