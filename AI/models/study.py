"""Study sets: flashcards and quizzes made from a conversation.

A student who has just had something explained can turn that conversation into
something to revise from. The set is generated once — by the model the student
chose, billed like any other reply — and kept, so revising it again tomorrow
costs nothing. Items are stored as JSON rather than rows: a set is always read
and written whole, never queried card by card.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class StudyKind(models.TextChoices):
    FLASHCARDS = "flashcards", "Flashcards"
    QUIZ = "quiz", "Quiz"


class StudySet(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="study_sets")
    #: The chat it was made from. Kept when the chat is deleted: the cards are
    #: the student's, and still worth revising.
    conversation = models.ForeignKey(
        "AI.Conversation", null=True, blank=True, on_delete=models.SET_NULL, related_name="study_sets",
    )
    kind = models.CharField(max_length=12, choices=StudyKind.choices)
    title = models.CharField(max_length=160)
    #: Flashcards: ``[{"front", "back"}]``. Quiz: ``[{"question", "options", "answer", "why"}]``,
    #: where ``answer`` is the index of the right option.
    items = models.JSONField(default=list)
    model_key = models.CharField(max_length=64, blank=True)
    tokens_used = models.PositiveIntegerField(default=0)
    #: Quiz only: best percentage so far, and how many times it was taken.
    best_score = models.PositiveSmallIntegerField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_studied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.title}"

    @property
    def size(self) -> int:
        return len(self.items or [])
