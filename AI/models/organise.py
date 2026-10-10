"""How a student organises their work: folders of chats, and saved answers.

* :class:`ChatFolder` — a named, coloured group of conversations. A folder can
  carry its own instructions ("these chats are for my biology course"), added
  to every request made from a conversation inside it, the way a *project*
  works on other assistants.
* :class:`MessageBookmark` — one reply the student wants to find again.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

#: The swatches offered for a folder. Stored as a key, drawn as a colour.
FOLDER_COLOURS = {
    "indigo": "#4F46E5",
    "sky": "#0EA5E9",
    "emerald": "#059669",
    "amber": "#D97706",
    "rose": "#E11D48",
    "violet": "#7C3AED",
    "slate": "#475569",
}


class ChatFolder(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chat_folders")
    name = models.CharField(max_length=60)
    colour = models.CharField(max_length=12, default="indigo")
    instructions = models.CharField(
        max_length=900, blank=True,
        help_text="Added to every answer in this folder's chats.",
    )
    sort_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name"]
        indexes = [models.Index(fields=["user", "sort_order"])]

    def __str__(self) -> str:
        return self.name

    @property
    def hex(self) -> str:
        return FOLDER_COLOURS.get(self.colour, FOLDER_COLOURS["indigo"])


class MessageBookmark(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bookmarks")
    message = models.ForeignKey("AI.Message", on_delete=models.CASCADE, related_name="bookmarks")
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "message"], name="one_bookmark_per_message"),
        ]
