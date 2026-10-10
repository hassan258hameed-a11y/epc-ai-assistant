"""What administrators keep for themselves: notes on students, saved replies.

Neither is ever shown to a student. Notes are a timeline rather than one text
box, so "who said what, when" survives several people working the same
account; saved replies are the answers support gives often enough to type once.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class StudentNote(models.Model):
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="admin_notes",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    body = models.TextField(max_length=2000)
    is_pinned = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-is_pinned", "-created_at"]

    def __str__(self) -> str:
        return f"note:{self.student_id}"


class CannedReply(models.Model):
    title = models.CharField(max_length=80)
    body = models.TextField(max_length=4000)
    use_count = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-use_count", "title"]

    def __str__(self) -> str:
        return self.title
