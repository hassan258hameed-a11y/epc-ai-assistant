"""Messages an administrator writes and sends from Console → Telegram → Send.

Four places a message can go:

* the **channel** — one post everyone following it sees;
* the **community group** — one post in the group chat;
* **one student** — their private chat with the bot (and, if chosen, their
  in-app notifications too);
* **all connected students** — every student's private bot chat, sent on a
  background thread at Telegram's pace so the page answers at once.

Everything goes through :mod:`telegram_service`, so each chat's delivery is
logged, a blocked bot is noticed and a 429 is honoured exactly as for every
other message the bot sends. A student who switched Telegram off in their own
settings is skipped, never messaged.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from AI.models import (
    AccountStatus,
    NotificationCategory,
    NotificationPreference,
    Role,
    TelegramAccount,
    TelegramDeliveryStatus,
    TelegramMessage,
    TelegramMessageStatus,
    TelegramMessageTarget,
)
from AI.services import telegram_service

logger = logging.getLogger("AI.telegram")

#: Longest message body the composer accepts. Telegram's own limit is 4,096
#: characters after formatting; the title, the sign-off and escaping need room.
MAX_BODY = 3500
MAX_TITLE = 140
MAX_BUTTON_LABEL = 40

#: Seconds between private messages in a send to everyone: 20 a second,
#: inside Telegram's 30-a-second limit for a bot.
PACE_SECONDS = 0.05

#: How long a bot may still delete its own channel or group post.
DELETE_WINDOW_HOURS = 48


class MessageError(Exception):
    """Something the administrator can fix; the text says what."""


# ---------------------------------------------------------------------------
# Who can be reached
# ---------------------------------------------------------------------------
def reachable_students():
    """Linked, private, unblocked student chats whose owner kept Telegram on."""
    accounts = (
        TelegramAccount.objects.filter(
            is_linked=True, blocked_at__isnull=True, telegram_id__isnull=False,
            user__is_active=True, user__status=AccountStatus.ACTIVE,
        )
        .exclude(user__role=Role.ADMIN).exclude(user__is_superuser=True)
        .exclude(user__notification_preference__telegram_enabled=False)
        .select_related("user")
        .order_by("-linked_at", "-pk")
    )
    return [account for account in accounts if telegram_service.can_receive_private(account)]


def destinations() -> dict:
    """What each destination tile shows: whether it can be used, and why not."""
    configured = telegram_service.is_configured()
    students = reachable_students() if configured else []
    return {
        "configured": configured,
        "channel": {
            "ready": telegram_service.channel_configured(),
            "name": telegram_service.channel_title() or "Your channel",
            "url": telegram_service.channel_url(),
        },
        "group": {
            "ready": telegram_service.group_configured(),
            "name": telegram_service.group_title() or "Community group",
            "url": telegram_service.group_url(),
        },
        "students": students,
        "student_count": len(students),
    }


# ---------------------------------------------------------------------------
# The message itself
# ---------------------------------------------------------------------------
def clean_button(label: str, url: str) -> tuple[str, str]:
    """Validate the optional link button. Returns ``("", "")`` for none."""
    label, url = (label or "").strip(), (url or "").strip()
    if not label and not url:
        return "", ""
    if not label or not url:
        raise MessageError("A button needs both a label and a link — or leave both empty.")
    if len(label) > MAX_BUTTON_LABEL:
        raise MessageError(f"Keep the button label under {MAX_BUTTON_LABEL} characters.")
    if url.startswith("/"):
        return label, url  # a page on this site; made absolute when sent
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise MessageError("The button link must start with https:// (or / for a page on this site).")
    return label, url


def render_text(message: TelegramMessage, *, private: bool) -> str:
    """The message as Telegram shows it: a bold title, then the text."""
    esc = telegram_service.escape_html
    parts = []
    if message.title:
        parts.append(f"<b>{esc(message.title)}</b>")
    parts.append(esc(message.body.strip()))
    text = "\n\n".join(parts)
    if private:
        text += f"\n\n<i>From the {esc(settings.BRAND['full_name'])} team</i>"
    return text


def _buttons(message: TelegramMessage):
    if not message.button_label or not message.button_url:
        return None
    return [(message.button_label, telegram_service.absolute_url(message.button_url))]


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def create(*, created_by, target: str, title: str, body: str, button_label: str = "",
           button_url: str = "", recipient=None, in_app: bool = False) -> TelegramMessage:
    """Check what was written, then send it (or start sending it).

    Raises :class:`MessageError` before anything goes out if the message or
    its destination is not usable.
    """
    if not telegram_service.is_configured():
        raise MessageError("The Telegram bot isn't set up yet. Add TELEGRAM_BOT_TOKEN first.")
    title, body = (title or "").strip(), (body or "").strip()
    if not body:
        raise MessageError("Write a message first.")
    if len(body) > MAX_BODY:
        raise MessageError(f"That message is {len(body):,} characters; Telegram fits {MAX_BODY:,}.")
    if len(title) > MAX_TITLE:
        raise MessageError(f"Keep the title under {MAX_TITLE} characters.")
    button_label, button_url = clean_button(button_label, button_url)

    if target == TelegramMessageTarget.CHANNEL and not telegram_service.channel_configured():
        raise MessageError("No channel is set up. Add TELEGRAM_CHANNEL_ID first.")
    if target == TelegramMessageTarget.GROUP and not telegram_service.group_configured():
        raise MessageError("No community group is set up. Add TELEGRAM_GROUP_ID first.")
    if target == TelegramMessageTarget.STUDENT:
        if recipient is None:
            raise MessageError("Choose the student to send it to.")
        if recipient.is_admin_user:
            raise MessageError("That account is staff, not a student.")
    if target not in TelegramMessageTarget.values:
        raise MessageError("Choose where to send it.")

    message = TelegramMessage.objects.create(
        created_by=created_by, target=target, title=title, body=body,
        button_label=button_label, button_url=button_url,
        recipient=recipient if target == TelegramMessageTarget.STUDENT else None,
        in_app=bool(in_app) and target == TelegramMessageTarget.STUDENT,
    )
    if target in (TelegramMessageTarget.CHANNEL, TelegramMessageTarget.GROUP):
        _post(message)
    elif target == TelegramMessageTarget.STUDENT:
        _send_to_one(message)
    else:
        _queue_everyone(message)
    return message


def _finish(message: TelegramMessage, **fields) -> None:
    for key, value in fields.items():
        setattr(message, key, value)
    message.finished_at = timezone.now()
    message.save(update_fields=[*fields, "finished_at"])


def _post(message: TelegramMessage) -> None:
    post = telegram_service.post_to_channel if message.target == TelegramMessageTarget.CHANNEL \
        else telegram_service.post_to_group
    delivery = post(render_text(message, private=False), buttons=_buttons(message), kind="message")
    ok = delivery.status == TelegramDeliveryStatus.SENT
    _finish(
        message, recipients=1, sent=int(ok), failed=int(not ok),
        status=TelegramMessageStatus.SENT if ok else TelegramMessageStatus.FAILED,
        error=("" if ok else delivery.error or delivery.get_status_display())[:255],
        telegram_message_id=delivery.telegram_message_id or "",
    )


def _send_to_one(message: TelegramMessage) -> None:
    from AI.services import notification_service

    user = message.recipient
    if message.in_app:
        # In the app whatever happens on Telegram, so the message is never lost.
        notification_service.notify(
            user, message.title or f"Message from the {settings.BRAND['full_name']} team", message.body,
            category=NotificationCategory.SYSTEM,
            action_url=message.button_url, action_label=message.button_label,
        )

    account = telegram_service.linked_account(user)
    prefs = NotificationPreference.objects.filter(user=user).first()
    if account is None or not telegram_service.can_receive_private(account):
        reason = ("They blocked the bot." if account is not None and account.blocked_at
                  else "They haven't connected Telegram.")
        _finish(message, recipients=1, skipped=1, status=TelegramMessageStatus.FAILED, error=reason)
        return
    if prefs is not None and not prefs.telegram_enabled:
        _finish(message, recipients=1, skipped=1, status=TelegramMessageStatus.FAILED,
                error="They turned Telegram messages off in their settings.")
        return

    delivery = telegram_service.deliver(
        account.telegram_id, render_text(message, private=True), user=user, account=account,
        kind="message", buttons=_buttons(message),
    )
    ok = delivery.status == TelegramDeliveryStatus.SENT
    _finish(
        message, recipients=1, sent=int(ok), failed=int(not ok),
        status=TelegramMessageStatus.SENT if ok else TelegramMessageStatus.FAILED,
        error=("" if ok else delivery.error or delivery.get_status_display())[:255],
    )


def _queue_everyone(message: TelegramMessage) -> None:
    from AI.services import notification_service

    accounts = reachable_students()
    if not accounts:
        _finish(message, status=TelegramMessageStatus.FAILED,
                error="No student has connected Telegram yet.")
        return
    user_ids = [account.user_id for account in accounts]
    message.recipients = len(user_ids)
    message.save(update_fields=["recipients"])
    # After the row is committed, so the background thread can see it.
    transaction.on_commit(lambda: notification_service._spawn(send_to_students, message.pk, user_ids))


def send_to_students(message_id: int, user_ids: list[int]) -> dict:
    """Deliver one message to each student's bot chat, paced. Runs in the background.

    Each student is checked again at send time: they may have unlinked,
    blocked the bot or switched Telegram off since the send began.
    """
    counts = {"sent": 0, "failed": 0, "skipped": 0}
    message = TelegramMessage.objects.filter(pk=message_id).first()
    if message is None:
        return counts
    text, buttons = render_text(message, private=True), _buttons(message)
    for index, user_id in enumerate(user_ids):
        account = TelegramAccount.objects.filter(user_id=user_id, is_linked=True).select_related("user").first()
        prefs = NotificationPreference.objects.filter(user_id=user_id).first()
        if account is None or not telegram_service.can_receive_private(account) or (
            prefs is not None and not prefs.telegram_enabled
        ):
            counts["skipped"] += 1
        else:
            delivery = telegram_service.deliver(
                account.telegram_id, text, user=account.user, account=account, kind="message",
                buttons=buttons, max_retry_wait=30,
            )
            counts["sent" if delivery.status == TelegramDeliveryStatus.SENT else "failed"] += 1
            time.sleep(PACE_SECONDS)
        if index % 25 == 24:  # the history shows progress on a long send
            TelegramMessage.objects.filter(pk=message_id).update(**counts)

    if counts["sent"] == 0:
        status = TelegramMessageStatus.FAILED
    elif counts["failed"] or counts["skipped"]:
        status = TelegramMessageStatus.PARTLY
    else:
        status = TelegramMessageStatus.SENT
    TelegramMessage.objects.filter(pk=message_id).update(
        **counts, status=status, finished_at=timezone.now(),
        error="" if counts["sent"] else "No chat accepted the message.",
    )
    logger.info("Telegram message %s sent to students: %s", message_id, counts)
    return counts


# ---------------------------------------------------------------------------
# Taking a post back
# ---------------------------------------------------------------------------
def can_delete(message: TelegramMessage) -> bool:
    """A channel or group post the bot may still delete (Telegram allows 48 hours)."""
    return (
        message.target in (TelegramMessageTarget.CHANNEL, TelegramMessageTarget.GROUP)
        and message.status == TelegramMessageStatus.SENT
        and bool(message.telegram_message_id)
        and message.created_at >= timezone.now() - timezone.timedelta(hours=DELETE_WINDOW_HOURS)
    )


def delete_post(message: TelegramMessage) -> None:
    if not can_delete(message):
        raise MessageError("Only a channel or group post from the last 48 hours can be deleted from here.")
    remove = telegram_service.delete_channel_message if message.target == TelegramMessageTarget.CHANNEL \
        else telegram_service.delete_group_message
    deleted, error = remove(message.telegram_message_id)
    if not deleted:
        raise MessageError(f"Telegram didn't delete it: {error or 'unknown error'}. Remove it in Telegram.")
    message.status = TelegramMessageStatus.DELETED
    message.save(update_fields=["status"])
