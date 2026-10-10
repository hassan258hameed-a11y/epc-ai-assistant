"""Support tickets.

A student raises a request, an administrator works it, and both sides see the
same thread. The reference number is what a student quotes when they follow up,
so it is generated once and never changes.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from AI.models.billing import generate_reference


def ticket_attachment_path(instance, filename: str) -> str:
    """Store under a random server-generated name — the original is attacker controlled."""
    suffix = (filename.rsplit(".", 1)[-1] if "." in filename else "bin").lower()[:8]
    stamp = timezone.now().strftime("%Y/%m")
    return f"uploads/support/{stamp}/{uuid.uuid4().hex}.{suffix}"


class TicketCategory(models.TextChoices):
    TECHNICAL = "technical", "Technical issue"
    PAYMENT = "payment", "Payment issue"
    ACCOUNT = "account", "Account issue"
    CHATBOT = "chatbot", "AI / chatbot issue"
    UPLOAD = "upload", "File upload issue"
    VOICE = "voice", "Voice input issue"
    BILLING = "billing", "Billing / invoice"
    OTHER = "other", "Something else"


class TicketStatus(models.TextChoices):
    NEW = "new", "New"
    OPEN = "open", "Open"
    IN_PROGRESS = "in_progress", "In progress"
    WAITING_USER = "waiting_user", "Waiting for user"
    RESOLVED = "resolved", "Resolved"
    CLOSED = "closed", "Closed"


#: Statuses where the ball is with us, used for the admin's "needs attention" count.
OPEN_STATUSES = {TicketStatus.NEW, TicketStatus.OPEN, TicketStatus.IN_PROGRESS}


class TicketPriority(models.TextChoices):
    LOW = "low", "Low"
    NORMAL = "normal", "Normal"
    HIGH = "high", "High"
    URGENT = "urgent", "Urgent"


class SupportTicket(models.Model):
    reference = models.CharField(max_length=32, unique=True, db_index=True)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="support_tickets"
    )
    # Captured at submission rather than read from the account, because a student
    # may want to be reached on a different number or address.
    full_name = models.CharField(max_length=150)
    email = models.EmailField()
    phone = models.CharField(max_length=32, blank=True)

    category = models.CharField(max_length=16, choices=TicketCategory.choices, db_index=True)
    subject = models.CharField(max_length=160)
    description = models.TextField()
    attachment = models.FileField(upload_to=ticket_attachment_path, blank=True, null=True)
    attachment_name = models.CharField(max_length=160, blank=True)
    attachment_size = models.PositiveIntegerField(default=0)

    status = models.CharField(
        max_length=16, choices=TicketStatus.choices, default=TicketStatus.NEW, db_index=True
    )
    priority = models.CharField(
        max_length=8, choices=TicketPriority.choices, default=TicketPriority.NORMAL
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True, on_delete=models.SET_NULL, related_name="assigned_tickets",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    first_response_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["category", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.reference} — {self.subject}"

    def save(self, *args, **kwargs):
        if not self.reference:
            for _ in range(8):
                candidate = generate_reference("TKT")
                if not SupportTicket.objects.filter(reference=candidate).exists():
                    self.reference = candidate
                    break
            else:  # pragma: no cover — astronomically unlikely
                self.reference = generate_reference("TKT")
        super().save(*args, **kwargs)

    # -- presentation ------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_closed(self) -> bool:
        return self.status in {TicketStatus.RESOLVED, TicketStatus.CLOSED}

    @property
    def status_tone(self) -> str:
        return {
            TicketStatus.NEW: "danger",
            TicketStatus.OPEN: "warning",
            TicketStatus.IN_PROGRESS: "info",
            TicketStatus.WAITING_USER: "brand",
            TicketStatus.RESOLVED: "success",
            TicketStatus.CLOSED: "neutral",
        }.get(self.status, "neutral")

    @property
    def priority_tone(self) -> str:
        return {
            TicketPriority.LOW: "neutral",
            TicketPriority.NORMAL: "info",
            TicketPriority.HIGH: "warning",
            TicketPriority.URGENT: "danger",
        }.get(self.priority, "neutral")

    @property
    def waiting_hours(self) -> int:
        """How long the student has been waiting for a first reply."""
        if self.first_response_at:
            return 0
        return int((timezone.now() - self.created_at).total_seconds() // 3600)


class TicketReply(models.Model):
    """One message on a ticket thread.

    Internal notes are for administrators only and are never rendered on the
    student's view of the ticket — that filtering happens in the view, not the
    template, so a template change cannot leak them.
    """

    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name="replies")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="ticket_replies",
    )
    body = models.TextField()
    is_internal = models.BooleanField(
        default=False, help_text="An internal note, never shown to the student."
    )
    is_from_staff = models.BooleanField(default=False)
    attachment = models.FileField(upload_to=ticket_attachment_path, blank=True, null=True)
    attachment_name = models.CharField(max_length=160, blank=True)
    attachment_size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["ticket", "created_at"])]

    def __str__(self) -> str:
        return f"{self.ticket_id}: {self.body[:40]}"
