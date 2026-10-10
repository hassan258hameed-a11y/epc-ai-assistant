"""A student's own data: take a copy of it, or close the account.

The privacy policy used to say "e-mail support" for both. Now a student can do
either from Settings, at any hour, without waiting on anybody.

**Closing is anonymising, not erasing.** Everything a student wrote — chats,
files, prompts, notes to the assistant, study sets — is deleted, files and all,
and every identifying field is blanked. What stays is what the business is
obliged to keep and cannot rebuild: payments and the token ledger, which now
belong to an account with no name, address or password.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from AI.models import (
    AccountStatus,
    AuditAction,
    ChatFolder,
    ChatPreference,
    Conversation,
    MessageAttachment,
    MessageBookmark,
    MessageFeedback,
    Notification,
    NotificationPreference,
    OTPCode,
    SavedPrompt,
    StudySet,
    SupportTicket,
    TelegramAccount,
    TicketReply,
)

logger = logging.getLogger("AI.privacy")

#: What the student must type to confirm. Deliberately a word, not a checkbox.
CONFIRM_WORD = "DELETE"


class PrivacyError(Exception):
    def __init__(self, message: str):
        self.friendly_message = message
        super().__init__(message)


def _when(value) -> str:
    return timezone.localtime(value).isoformat() if value else ""


def export_data(user) -> dict:
    """Everything the student put in or was given, as one JSON-ready dict."""
    preference = ChatPreference.objects.filter(user=user).first()
    conversations = (
        Conversation.all_objects.filter(user=user).order_by("created_at")
        .prefetch_related("messages__attachments")
    )
    return {
        "exported_at": _when(timezone.now()),
        "account": {
            "username": user.username,
            "full_name": user.full_name,
            "email": user.email,
            "phone": user.phone,
            "joined": _when(user.date_joined),
            "referral_code": user.referral_code,
        },
        "preferences": {
            "about_me": preference.about_me,
            "instructions": preference.instructions,
            "style": preference.style,
            "language": preference.language,
            "default_model": preference.default_model_key,
            "daily_goal": preference.daily_goal,
        } if preference else {},
        "wallets": [
            {"model": w.ai_model.name, "balance": w.balance, "allocated": w.allocated_tokens, "used": w.used_tokens}
            for w in user.ai_wallets.select_related("ai_model").order_by("ai_model__sort_order")
        ],
        "folders": [
            {"name": f.name, "instructions": f.instructions}
            for f in ChatFolder.objects.filter(user=user).order_by("sort_order", "name")
        ],
        "conversations": [
            {
                "title": c.title,
                "started": _when(c.created_at),
                "archived": c.is_archived,
                "messages": [
                    {
                        "role": m.role,
                        "content": m.content,
                        "model": m.model_key,
                        "tokens": m.total_tokens,
                        "sent": _when(m.created_at),
                        "files": [a.name for a in m.attachments.all()],
                    }
                    for m in c.messages.all().order_by("created_at", "id")
                ],
            }
            for c in conversations
        ],
        "saved_prompts": [
            {"title": p.title, "body": p.body, "category": p.category}
            for p in SavedPrompt.objects.filter(user=user)
        ],
        "saved_answers": [
            {"answer": b.message.content, "note": b.note, "saved": _when(b.created_at)}
            for b in MessageBookmark.objects.filter(user=user).select_related("message")
        ],
        "study_sets": [
            {"kind": s.kind, "title": s.title, "items": s.items, "best_score": s.best_score,
             "created": _when(s.created_at)}
            for s in StudySet.objects.filter(user=user)
        ],
        "payments": [
            {"reference": p.reference, "plan": getattr(p.plan, "name", ""), "amount": str(p.amount),
             "currency": p.currency, "method": p.method, "status": p.status, "created": _when(p.created_at),
             "paid": _when(p.paid_at)}
            for p in user.payments.select_related("plan").order_by("created_at")
        ],
        "support_tickets": [
            {
                "reference": t.reference, "subject": t.subject, "description": t.description,
                "status": t.status, "opened": _when(t.created_at),
                "replies": [
                    {"from": "support" if r.is_from_staff else "you", "body": r.body, "sent": _when(r.created_at)}
                    for r in t.replies.filter(is_internal=False).order_by("created_at")
                ],
            }
            for t in SupportTicket.objects.filter(user=user).prefetch_related("replies")
        ],
        "notifications": [
            {"title": n.title, "body": n.body, "read": n.is_read, "sent": _when(n.created_at)}
            for n in Notification.objects.filter(user=user).order_by("created_at")
        ],
    }


def _drop_file(field) -> None:
    """Remove a stored file, never letting a missing one stop the closure."""
    try:
        if field:
            field.delete(save=False)
    except Exception:  # noqa: BLE001 - a file already gone is the goal anyway
        logger.warning("Could not remove %s during account closure", getattr(field, "name", "?"))


def close_account(user, *, password: str, confirm: str, request=None) -> None:
    """Anonymise the account and delete everything personal. Irreversible."""
    if user.is_admin_user:
        raise PrivacyError("Administrator accounts are closed from the console, not here.")
    if (confirm or "").strip().upper() != CONFIRM_WORD:
        raise PrivacyError(f"Type {CONFIRM_WORD} to confirm.")
    if not user.check_password(password or ""):
        raise PrivacyError("That password is not right.")

    pk = user.pk
    with transaction.atomic():
        # Files first: once the rows are gone nothing points at them any more.
        for attachment in MessageAttachment.objects.filter(message__conversation__user=user):
            _drop_file(attachment.file)
        for conversation in Conversation.all_objects.filter(user=user).prefetch_related("messages"):
            for message in conversation.messages.all():
                _drop_file(message.image)
        for ticket in SupportTicket.objects.filter(user=user):
            _drop_file(ticket.attachment)
            for reply in ticket.replies.all():
                _drop_file(reply.attachment)
        TicketReply.objects.filter(ticket__user=user).exclude(is_from_staff=True).update(body="[removed]")
        SupportTicket.objects.filter(user=user).update(
            full_name="Closed account", email="", phone="", description="[removed]",
            attachment=None, attachment_name="",
        )

        Conversation.all_objects.filter(user=user).delete()
        StudySet.objects.filter(user=user).delete()
        for model in (ChatFolder, ChatPreference, MessageBookmark, MessageFeedback, SavedPrompt,
                      Notification, NotificationPreference, TelegramAccount, OTPCode):
            model.objects.filter(user=user).delete()

        user.username = f"closed-{pk}"
        user.full_name = ""
        user.first_name = ""
        user.last_name = ""
        user.email = ""
        user.phone = ""
        user.tags = []
        user.is_active = False
        user.status = AccountStatus.DISABLED
        user.disabled_at = timezone.now()
        user.disabled_reason = "Closed by the student"
        user.active_session_key = ""
        user.last_login_ip = None
        user.set_unusable_password()
        user.save()

    try:
        from AI.services import audit_service

        audit_service.record(
            AuditAction.USER_DISABLE, actor=user, request=request, target=user,
            description=f"Student closed their own account (#{pk}); personal data deleted",
        )
    except Exception:  # pragma: no cover
        logger.exception("Audit entry for account closure failed (user=%s)", pk)
    logger.info("Account %s closed by its owner", pk)
