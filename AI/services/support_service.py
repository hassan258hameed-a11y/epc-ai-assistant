"""Support ticket workflow.

Both sides of a ticket run through here so the audit trail, the notifications
and the status transitions stay consistent no matter which view triggered them.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from AI.models import (
    OPEN_STATUSES,
    SupportTicket,
    TicketPriority,
    TicketReply,
    TicketStatus,
)

logger = logging.getLogger("AI.support")

#: Categories that usually mean money is involved, so they start higher up the
#: queue. A student cannot set priority themselves — it would be meaningless if
#: everyone could mark their own ticket urgent.
ELEVATED_CATEGORIES = {"payment", "billing", "account"}


def create_ticket(user, *, data: dict, attachment=None) -> SupportTicket:
    """Record a new request and let the student know it landed."""
    category = data.get("category") or "other"

    ticket = SupportTicket(
        user=user,
        full_name=(data.get("full_name") or user.display_name)[:150],
        email=(data.get("email") or user.email)[:254],
        phone=(data.get("phone") or "")[:32],
        category=category,
        subject=(data.get("subject") or "")[:160],
        description=data.get("description") or "",
        status=TicketStatus.NEW,
        priority=TicketPriority.HIGH if category in ELEVATED_CATEGORIES else TicketPriority.NORMAL,
    )

    if attachment is not None:
        ticket.attachment = attachment.file
        ticket.attachment_name = attachment.name
        ticket.attachment_size = attachment.size

    ticket.save()

    _notify(
        user,
        "We have your request",
        f"Ticket {ticket.reference} — “{ticket.subject}”. We will reply by e-mail and here.",
        level="success",
        action_url=f"/help/tickets/{ticket.reference}/",
        action_label="View ticket",
    )
    logger.info("Support ticket %s opened by user=%s (%s)", ticket.reference, user.pk, category)
    return ticket


def add_reply(
    ticket: SupportTicket, author, body: str, *, internal: bool = False, from_staff: bool = False,
    attachment=None,
) -> TicketReply:
    """Append a message, and move the ticket to whoever now owes a response.

    ``attachment`` is a validated upload (see ``AI.utils.uploads``) — a
    screenshot of the error is often worth more than the words.
    """
    body = (body or "").strip()
    if not body and attachment is None:
        raise ValueError("A reply cannot be empty.")

    with transaction.atomic():
        reply = TicketReply(
            ticket=ticket, author=author, body=body,
            is_internal=internal, is_from_staff=from_staff,
        )
        if attachment is not None:
            reply.attachment = attachment.file
            reply.attachment_name = attachment.name
            reply.attachment_size = attachment.size
        reply.save()

        fields = ["updated_at"]
        # An internal note is bookkeeping — it must not change what the student
        # sees, or move the ticket along on their behalf.
        if not internal:
            if from_staff:
                if ticket.first_response_at is None:
                    ticket.first_response_at = timezone.now()
                    fields.append("first_response_at")
                if ticket.status in {TicketStatus.NEW, TicketStatus.OPEN}:
                    ticket.status = TicketStatus.WAITING_USER
                    fields.append("status")
            else:
                # The student came back, so the ball is with us again.
                if ticket.status in {TicketStatus.WAITING_USER, TicketStatus.RESOLVED}:
                    ticket.status = TicketStatus.OPEN
                    fields.append("status")
            ticket.save(update_fields=fields)
        else:
            ticket.save(update_fields=["updated_at"])

    if from_staff and not internal:
        _notify(
            ticket.user,
            f"Reply on ticket {ticket.reference}",
            (body or "Sent you a file.")[:160],
            level="info",
            action_url=f"/help/tickets/{ticket.reference}/",
            action_label="Read reply",
        )
        _email_reply(ticket, reply)
    return reply


def close_by_student(ticket: SupportTicket, user) -> SupportTicket:
    """The student says it is sorted. Only their own, and only if still open."""
    if ticket.user_id != user.pk or ticket.is_closed:
        return ticket
    return set_status(ticket, TicketStatus.CLOSED, actor=user)


def _email_reply(ticket: SupportTicket, reply: TicketReply) -> None:
    """The e-mail the help page promises. Sent to the address on the ticket —
    the one the student asked to be reached at — and never allowed to break
    the reply that triggered it."""
    try:
        from AI.models import EmailType
        from AI.services import mail_service

        mail_service.send_notice(
            ticket.user,
            email_type=EmailType.SUPPORT,
            subject=f"Re: {ticket.subject} [{ticket.reference}]",
            heading="We replied to your request",
            intro=f"here is our reply on ticket {ticket.reference} — “{ticket.subject}”.",
            body=reply.body or "We sent you a file on the ticket.",
            button_label="Open the ticket",
            button_path=f"/help/tickets/{ticket.reference}/",
            reason=f"because you opened support ticket {ticket.reference}.",
            recipient=ticket.email,
            trigger=f"ticket {ticket.reference} reply",
        )
    except Exception:  # pragma: no cover
        logger.exception("Support reply e-mail failed for ticket=%s", ticket.reference)


def set_status(ticket: SupportTicket, status: str, *, actor=None) -> SupportTicket:
    """Move a ticket, stamping resolution time and telling the student."""
    if status not in TicketStatus.values or status == ticket.status:
        return ticket

    previous = ticket.status
    ticket.status = status
    fields = ["status", "updated_at"]

    if status in {TicketStatus.RESOLVED, TicketStatus.CLOSED} and ticket.resolved_at is None:
        ticket.resolved_at = timezone.now()
        fields.append("resolved_at")
    elif status in OPEN_STATUSES:
        ticket.resolved_at = None
        fields.append("resolved_at")

    ticket.save(update_fields=fields)

    if status == TicketStatus.RESOLVED:
        _notify(
            ticket.user,
            f"Ticket {ticket.reference} resolved",
            f"“{ticket.subject}” has been marked resolved. Reply on the ticket if it is not sorted.",
            level="success",
            action_url=f"/help/tickets/{ticket.reference}/",
            action_label="View ticket",
        )
    logger.info("Ticket %s moved %s -> %s by user=%s", ticket.reference, previous, status, getattr(actor, "pk", None))
    return ticket


def visible_replies(ticket: SupportTicket, *, for_staff: bool):
    """Replies a given audience may read.

    Internal notes are filtered here, in the service, so no template change can
    ever leak them to a student.
    """
    queryset = ticket.replies.select_related("author")
    if not for_staff:
        queryset = queryset.filter(is_internal=False)
    return list(queryset)


def open_count() -> int:
    """Tickets still waiting on us — drives the console's nav badge."""
    return SupportTicket.objects.filter(status__in=OPEN_STATUSES).count()


def _notify(user, title, body, **extra) -> None:
    """Notifications must never break the ticket operation that triggered them.

    Mirrored to the student's Telegram (when linked and switched on): a reply
    from support is exactly the kind of thing worth a push, and the ticket
    reference in the others is what a student looks for later.
    """
    extra.setdefault("telegram", True)
    try:
        from AI.services import notification_service

        notification_service.notify(user, title, body, category="system", **extra)
    except Exception:  # pragma: no cover
        logger.exception("Ticket notification failed for user=%s", getattr(user, "pk", None))
