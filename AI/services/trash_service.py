"""Recently deleted chats.

Deleting a chat moves it to the trash (``Conversation.deleted_at``). From
there it can be restored for ``TRASH_DAYS`` days, or deleted for good — which
is the only irreversible step, and the only one that asks first. The default
``Conversation.objects`` manager already hides trashed chats everywhere else,
so nothing outside this module has to remember to.

Deleting for good removes the stored files as well as the rows: an uploaded
photo of someone's homework should not outlive the chat it was sent in.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from AI.models import Conversation, MessageAttachment

logger = logging.getLogger("AI.trash")

TRASH_DAYS = 30


def _own(user, conversation_id):
    return Conversation.all_objects.filter(pk=conversation_id, user=user).first()


def trash(user, conversation_id) -> Conversation | None:
    """Move a chat to the trash. Returns it, or ``None`` if it isn't the user's."""
    conversation = _own(user, conversation_id)
    if conversation is None:
        return None
    if conversation.deleted_at is None:
        # .update() so the chat's "last active" time is left alone.
        Conversation.all_objects.filter(pk=conversation.pk).update(deleted_at=timezone.now())
        conversation.refresh_from_db()
    return conversation


def restore(user, conversation_id) -> Conversation | None:
    conversation = _own(user, conversation_id)
    if conversation is None or conversation.deleted_at is None:
        return conversation
    Conversation.all_objects.filter(pk=conversation.pk).update(deleted_at=None)
    conversation.refresh_from_db()
    return conversation


def _drop_file(field) -> None:
    try:
        if field:
            field.delete(save=False)
    except Exception:  # noqa: BLE001 - a file already gone is the goal anyway
        logger.warning("Could not remove %s", getattr(field, "name", "?"))


def purge(conversation: Conversation) -> None:
    """Delete a chat for good: its stored files first, then the rows."""
    files = [a.file for a in MessageAttachment.objects.filter(message__conversation=conversation)]
    files += [m.image for m in conversation.messages.exclude(image="").exclude(image__isnull=True)]
    with transaction.atomic():
        Conversation.all_objects.filter(pk=conversation.pk).delete()
    # Only once the rows are really gone: a failed delete must not leave a
    # chat pointing at files that no longer exist.
    for field in files:
        _drop_file(field)


def purge_for(user, conversation_id) -> bool:
    """Delete one trashed chat for good. Only chats already in the trash."""
    conversation = Conversation.all_objects.trashed().filter(pk=conversation_id, user=user).first()
    if conversation is None:
        return False
    purge(conversation)
    return True


def empty(user) -> int:
    count = 0
    for conversation in Conversation.all_objects.trashed().filter(user=user):
        purge(conversation)
        count += 1
    return count


def purge_expired(days: int = TRASH_DAYS) -> int:
    """Delete for good everything that has been in the trash for ``days``."""
    cutoff = timezone.now() - timedelta(days=days)
    count = 0
    for conversation in Conversation.all_objects.filter(deleted_at__lt=cutoff)[:500]:
        purge(conversation)
        count += 1
    return count


def days_left(conversation: Conversation) -> int:
    if conversation.deleted_at is None:
        return TRASH_DAYS
    remaining = conversation.deleted_at + timedelta(days=TRASH_DAYS) - timezone.now()
    return max(0, math.ceil(remaining.total_seconds() / 86400))


def items(user) -> list[dict]:
    rows = Conversation.all_objects.trashed().filter(user=user).select_related("folder").order_by("-deleted_at")
    return [{"conversation": c, "days_left": days_left(c)} for c in rows]


def count(user) -> int:
    return Conversation.all_objects.trashed().filter(user=user).count()
