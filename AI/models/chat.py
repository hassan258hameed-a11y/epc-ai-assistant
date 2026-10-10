"""Conversations, messages and the raw AI usage log."""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


def chat_image_path(instance, filename: str) -> str:
    """Store uploads under a per-user, date-partitioned, randomised name.

    The original filename is never reused — it is attacker controlled.
    """
    ext = (instance.image_format or "png").lower()
    stamp = timezone.now().strftime("%Y/%m")
    return f"uploads/chat/{stamp}/{uuid.uuid4().hex}.{ext}"


class ConversationQuerySet(models.QuerySet):
    def live(self):
        return self.filter(deleted_at__isnull=True)

    def trashed(self):
        return self.filter(deleted_at__isnull=False)


class LiveConversationManager(models.Manager.from_queryset(ConversationQuerySet)):
    """Hides chats in the trash from everything that doesn't ask for them.

    It is the default manager, so ``user.conversations`` and every existing
    query leave trashed chats out without having to remember to; the trash
    itself and the purge use ``Conversation.all_objects``.
    """

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class Conversation(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations"
    )
    title = models.CharField(max_length=160, default="New chat")
    is_archived = models.BooleanField(default=False)
    is_pinned = models.BooleanField(default=False)
    folder = models.ForeignKey(
        "AI.ChatFolder", null=True, blank=True, on_delete=models.SET_NULL, related_name="conversations",
    )
    message_count = models.PositiveIntegerField(default=0)
    total_tokens = models.BigIntegerField(default=0)

    #: What the model is told about the part of this conversation that no
    #: longer fits the context window. Re-sending forty turns to answer a
    #: question about the last one is the single largest avoidable cost in the
    #: product, and past a point it makes the answers worse, not better.
    summary = models.TextField(
        blank=True,
        help_text="Rolling summary of the messages that no longer fit the context window.",
    )
    #: Newest message the summary covers, so nothing is ever summarised twice.
    summary_upto_id = models.BigIntegerField(
        default=0,
        help_text="Id of the newest message the summary covers. Nothing above this is summarised twice.",
    )
    #: When the student moved this chat to the trash. It can be restored
    #: until it is purged, 30 days later (``send_reminders``).
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    objects = LiveConversationManager()
    all_objects = ConversationQuerySet.as_manager()

    class Meta:
        base_manager_name = "all_objects"
        ordering = ["-is_pinned", "-updated_at"]
        indexes = [
            models.Index(fields=["user", "-updated_at"]),
            models.Index(fields=["user", "is_archived", "-updated_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.user_id})"

    def touch(self, *, tokens: int = 0, messages: int = 0) -> None:
        Conversation.objects.filter(pk=self.pk).update(
            updated_at=timezone.now(),
            total_tokens=models.F("total_tokens") + tokens,
            message_count=models.F("message_count") + messages,
        )


class MessageRole(models.TextChoices):
    USER = "user", "User"
    ASSISTANT = "assistant", "Assistant"
    SYSTEM = "system", "System"


class MessageStatus(models.TextChoices):
    OK = "ok", "Delivered"
    FAILED = "failed", "Failed"
    PENDING = "pending", "Pending"


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=12, choices=MessageRole.choices)
    content = models.TextField(blank=True)

    image = models.ImageField(upload_to=chat_image_path, blank=True, null=True)
    image_format = models.CharField(max_length=8, blank=True)
    image_bytes = models.PositiveIntegerField(default=0)

    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)

    model = models.CharField(max_length=64, blank=True)
    #: Canonical catalogue key, stable even when the provider echoes back a
    #: dated snapshot name such as ``gpt-4o-mini-2024-07-18``.
    model_key = models.CharField(max_length=64, blank=True, db_index=True)
    request_id = models.CharField(max_length=120, blank=True)
    #: Sent by the browser with each question. A retry after a dropped
    #: connection carries the same id, so it gets the saved answer back
    #: instead of asking (and charging) a second time.
    client_id = models.CharField(max_length=64, blank=True, db_index=True)
    status = models.CharField(max_length=12, choices=MessageStatus.choices, default=MessageStatus.OK)
    error_message = models.CharField(max_length=255, blank=True)
    latency_ms = models.PositiveIntegerField(default=0)
    #: Follow-up questions the model suggested with this reply, shown as
    #: one-tap chips under the newest answer. Never part of ``content``.
    suggestions = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["conversation", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.role}: {self.content[:40]}"

    @property
    def is_user(self) -> bool:
        return self.role == MessageRole.USER

    @property
    def image_prompt(self) -> str:
        """What was actually drawn, for a generated picture's own actions.

        The endpoint often rewrites a prompt, and the rewrite is what produced
        the picture on screen — so re-drawing from what the student typed would
        quietly produce something else. Empty for anything that is not a
        generated image.
        """
        request = next(iter(self.image_requests.all()), None)
        if request is None:
            return ""
        return request.prompt or ""

    @property
    def document_attachments(self):
        """Non-image files, for the template's file chips."""
        return [item for item in self.attachments.all() if item.kind != "image"]


class AIUsageLog(models.Model):
    """Per-request record of every call made to the AI provider.

    Kept separate from :class:`Message` because failed calls produce no assistant
    message but still need to be visible in analytics and audits.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_usage")
    conversation = models.ForeignKey(
        Conversation, null=True, blank=True, on_delete=models.SET_NULL, related_name="usage_logs"
    )
    message = models.ForeignKey(
        Message, null=True, blank=True, on_delete=models.SET_NULL, related_name="usage_logs"
    )

    model = models.CharField(max_length=64)
    model_key = models.CharField(max_length=64, blank=True, db_index=True)
    provider = models.CharField(max_length=32, default="openai")
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)
    tokens_charged = models.PositiveIntegerField(default=0)
    reasoning_tokens = models.PositiveIntegerField(
        default=0, help_text="Hidden thinking tokens, already included in output_tokens."
    )

    request_id = models.CharField(max_length=120, blank=True)
    success = models.BooleanField(default=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.CharField(max_length=255, blank=True)
    latency_ms = models.PositiveIntegerField(default=0)
    had_image = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["success", "-created_at"]),
            models.Index(fields=["model_key", "-created_at"]),
            models.Index(fields=["-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} {self.model} {self.total_tokens}t"


def attachment_path(instance, filename: str) -> str:
    """Random, server-generated storage name. The original is attacker controlled."""
    ext = (instance.ext or "bin").lower()[:8]
    stamp = timezone.now().strftime("%Y/%m")
    return f"uploads/attachments/{stamp}/{uuid.uuid4().hex}.{ext}"


class AttachmentKind(models.TextChoices):
    IMAGE = "image", "Image"
    DOCUMENT = "document", "Document"


class MessageAttachment(models.Model):
    """One file sent with a message.

    Images are passed to the model as pictures where it supports vision.
    Documents are read once, at upload time, and their text is kept here so a
    later regenerate or follow-up question still has the content without
    re-parsing the file.
    """

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="attachments")
    kind = models.CharField(max_length=12, choices=AttachmentKind.choices)

    file = models.FileField(upload_to=attachment_path)
    name = models.CharField(max_length=160, help_text="Display name, sanitised at upload.")
    ext = models.CharField(max_length=8, blank=True)
    mime = models.CharField(max_length=80, blank=True)
    size = models.PositiveIntegerField(default=0)

    #: Text pulled out of a document. Empty for images.
    extracted_text = models.TextField(blank=True)
    extraction_note = models.CharField(max_length=255, blank=True)
    pages = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["message", "id"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.kind})"

    @property
    def is_image(self) -> bool:
        return self.kind == AttachmentKind.IMAGE

    @property
    def readable(self) -> bool:
        return bool(self.extracted_text.strip())

    @property
    def icon(self) -> str:
        """Sprite id for this file type."""
        return {
            "pdf": "i-doc", "docx": "i-doc", "txt": "i-doc", "md": "i-doc",
            "xlsx": "i-chart", "csv": "i-chart",
            "json": "i-code", "pptx": "i-doc",
        }.get(self.ext, "i-doc")


class MessageEmbedding(models.Model):
    """A message's meaning as a vector, for "search by meaning".

    One row per indexed message (an answer, embedded together with the
    question before it). The vector is float32, little-endian, stored as
    bytes; similarity is computed in the application (numpy), so no vector
    database is needed at this scale. Deleted with the message.
    """

    message = models.OneToOneField(Message, on_delete=models.CASCADE, related_name="embedding")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="message_embeddings")
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="embeddings")
    #: Which embedding model made the vector — vectors from different models
    #: are not comparable, so a change of model means re-indexing.
    model_name = models.CharField(max_length=64)
    dimensions = models.PositiveSmallIntegerField()
    vector = models.BinaryField()
    #: Hash of the text that was embedded, so an edited message is re-indexed.
    text_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["user", "model_name"])]

    def __str__(self) -> str:
        return f"embedding:{self.message_id}"

