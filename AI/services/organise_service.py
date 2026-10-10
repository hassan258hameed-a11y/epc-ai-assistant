"""Folders of chats and saved answers — the rules, in one place.

Every function takes the acting student and checks ownership itself: a folder,
a conversation and a message are all looked up *through* the student, so no
request can file somebody else's chat or bookmark somebody else's answer.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Max, Q

from AI.models import (
    FOLDER_COLOURS,
    ChatFolder,
    Conversation,
    Message,
    MessageBookmark,
    MessageRole,
)

MAX_FOLDERS = 30
MAX_INSTRUCTIONS = 900
MAX_BOOKMARKS = 500


class OrganiseError(Exception):
    def __init__(self, message: str, *, code: str = "invalid"):
        self.friendly_message = message
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------
def folders_for(user):
    return list(
        ChatFolder.objects.filter(user=user)
        .annotate(count=Count("conversations", distinct=True))
        .order_by("sort_order", "name")
    )


def folder_payload(folder: ChatFolder) -> dict:
    return {
        "id": folder.pk,
        "name": folder.name,
        "colour": folder.colour,
        "hex": folder.hex,
        "instructions": folder.instructions,
        "count": getattr(folder, "count", None),
    }


def _clean(name: str, colour: str, instructions: str) -> tuple[str, str, str]:
    name = " ".join((name or "").split())[:60]
    if not name:
        raise OrganiseError("Give the folder a name.")
    colour = colour if colour in FOLDER_COLOURS else "indigo"
    instructions = (instructions or "").strip()
    if len(instructions) > MAX_INSTRUCTIONS:
        raise OrganiseError(f"Keep folder instructions under {MAX_INSTRUCTIONS} characters.")
    return name, colour, instructions


def _own_folder(user, folder_id) -> ChatFolder:
    folder = ChatFolder.objects.filter(pk=folder_id, user=user).first()
    if folder is None:
        raise OrganiseError("That folder could not be found.", code="not_found")
    return folder


def create_folder(user, *, name: str, colour: str = "", instructions: str = "") -> ChatFolder:
    name, colour, instructions = _clean(name, colour, instructions)
    with transaction.atomic():
        if ChatFolder.objects.filter(user=user).count() >= MAX_FOLDERS:
            raise OrganiseError(f"You can have up to {MAX_FOLDERS} folders.", code="limit")
        if ChatFolder.objects.filter(user=user, name__iexact=name).exists():
            raise OrganiseError("You already have a folder with that name.", code="duplicate")
        last = ChatFolder.objects.filter(user=user).aggregate(m=Max("sort_order"))["m"] or 0
        return ChatFolder.objects.create(
            user=user, name=name, colour=colour, instructions=instructions, sort_order=last + 1,
        )


def update_folder(user, folder_id, *, name: str, colour: str = "", instructions: str = "") -> ChatFolder:
    folder = _own_folder(user, folder_id)
    name, colour, instructions = _clean(name, colour, instructions)
    if ChatFolder.objects.filter(user=user, name__iexact=name).exclude(pk=folder.pk).exists():
        raise OrganiseError("You already have a folder with that name.", code="duplicate")
    folder.name, folder.colour, folder.instructions = name, colour, instructions
    folder.save()
    return folder


def delete_folder(user, folder_id) -> int:
    """Remove the folder. Its chats are kept, and simply become unfiled."""
    folder = _own_folder(user, folder_id)
    moved = Conversation.objects.filter(folder=folder).count()
    folder.delete()
    return moved


def move_conversation(user, conversation_id, folder_id) -> Conversation:
    """File a chat in a folder, or take it out with ``folder_id`` of None."""
    conversation = Conversation.objects.filter(pk=conversation_id, user=user).first()
    if conversation is None:
        raise OrganiseError("Conversation not found.", code="not_found")
    folder = _own_folder(user, folder_id) if folder_id else None
    conversation.folder = folder
    # update_fields keeps updated_at as it was: filing a chat is not activity
    # in it, and must not move it to the top of "Today".
    Conversation.objects.filter(pk=conversation.pk).update(folder=folder)
    return conversation


def folder_instructions_for(conversation) -> str:
    """The instructions of the folder this chat is in *now*.

    Read from the database rather than from the object passed in: a chat can
    be moved between folders while a stale copy of it is still in hand, and
    the answer must follow where the chat actually is.
    """
    if conversation is None or not getattr(conversation, "pk", None):
        return ""
    text = (
        Conversation.objects.filter(pk=conversation.pk)
        .values_list("folder__instructions", flat=True)
        .first()
    )
    return (text or "").strip()


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------
def toggle_bookmark(user, message_id) -> bool:
    """Save or unsave one of the student's own replies. Returns the new state."""
    message = Message.objects.filter(
        pk=message_id, conversation__user=user, role=MessageRole.ASSISTANT,
    ).first()
    if message is None:
        raise OrganiseError("That answer could not be found.", code="not_found")
    deleted, _ = MessageBookmark.objects.filter(user=user, message=message).delete()
    if deleted:
        return False
    if MessageBookmark.objects.filter(user=user).count() >= MAX_BOOKMARKS:
        raise OrganiseError(f"You can save up to {MAX_BOOKMARKS} answers.", code="limit")
    MessageBookmark.objects.create(user=user, message=message)
    return True


def remove_bookmark(user, message_id) -> bool:
    """Unsave an answer. Safe to repeat: removing twice leaves it removed."""
    deleted, _ = MessageBookmark.objects.filter(user=user, message_id=message_id).delete()
    return bool(deleted)


def bookmarked_ids(user, message_ids) -> set[int]:
    return set(
        MessageBookmark.objects.filter(user=user, message_id__in=list(message_ids))
        .values_list("message_id", flat=True)
    )


def bookmarks_for(user, *, query: str = ""):
    rows = (
        # A chat in Recently deleted takes its saved answers out of view with
        # it; restoring the chat brings them back.
        MessageBookmark.objects.filter(user=user, message__conversation__deleted_at__isnull=True)
        .select_related("message", "message__conversation", "message__conversation__folder")
    )
    if query:
        rows = rows.filter(
            Q(message__content__icontains=query) | Q(note__icontains=query)
            | Q(message__conversation__title__icontains=query)
        )
    return rows
