"""Read-only public links to a conversation.

A link shows the conversation as it was when it was shared — up to the last
message at that moment — so carrying on chatting never publishes more than the
student chose to. Sharing again moves the snapshot forward; revoking kills the
link at once. Attachments and images are never included: a public page is not
a place for a photographed exam paper.
"""
from __future__ import annotations

from django.db.models import F, Max

from AI.models import Conversation, ConversationShare, MessageRole
from AI.services import settings_service


class ShareError(Exception):
    def __init__(self, message: str, *, code: str = "invalid"):
        self.friendly_message = message
        self.code = code
        super().__init__(message)


def sharing_enabled() -> bool:
    return bool(settings_service.get("allow_chat_sharing", True))


def _own(user, conversation_id) -> Conversation:
    conversation = Conversation.objects.filter(pk=conversation_id, user=user).first()
    if conversation is None:
        raise ShareError("Conversation not found.", code="not_found")
    return conversation


def share(user, conversation_id) -> ConversationShare:
    """Create the link, or move an existing one forward to the latest message."""
    if not sharing_enabled():
        raise ShareError("Sharing conversations is turned off on this platform.", code="disabled")
    conversation = _own(user, conversation_id)
    last = conversation.messages.aggregate(last=Max("id"))["last"] or 0
    if not last:
        raise ShareError("There is nothing in this conversation to share yet.", code="empty")
    link, _ = ConversationShare.objects.get_or_create(conversation=conversation)
    link.last_message_id = last
    link.is_active = True
    link.save()
    return link


def revoke(user, conversation_id) -> None:
    conversation = _own(user, conversation_id)
    ConversationShare.objects.filter(conversation=conversation).update(is_active=False)


def status(user, conversation_id) -> ConversationShare | None:
    conversation = _own(user, conversation_id)
    return ConversationShare.objects.filter(conversation=conversation, is_active=True).first()


def open_link(token: str):
    """The shared snapshot for a public visitor, or None. Counts the view."""
    if not sharing_enabled() or not token:
        return None, []
    link = (
        ConversationShare.objects.select_related("conversation", "conversation__user")
        .filter(token=token, is_active=True, conversation__deleted_at__isnull=True)
        .first()
    )
    if link is None:
        return None, []
    ConversationShare.objects.filter(pk=link.pk).update(view_count=F("view_count") + 1)
    messages = list(
        link.conversation.messages.filter(
            id__lte=link.last_message_id,
            role__in=[MessageRole.USER, MessageRole.ASSISTANT],
        ).order_by("created_at", "id")
    )
    return link, messages
