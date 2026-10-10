"""Telegram Bot API integration.

A bot cannot open a conversation with a user — the user must press Start first.
The linking flow is therefore:

1. The student presses **Connect Telegram** in Settings. That mints a fresh,
   single-use ``link_token`` that expires after ``TELEGRAM_LINK_TTL_MINUTES``.
2. They open ``t.me/<bot>?start=<link_token>`` (a button, or the QR code).
3. Telegram delivers ``/start <link_token>`` to our webhook (or the poller).
4. We resolve the token to the account — only from a *private* chat, only
   while it is fresh — store the chat id, and immediately retire the token, so
   a link that leaks afterwards (a screenshot, a forwarded message) can never
   re-point the student's codes somewhere else.

Every message the bot sends goes through :func:`deliver`, which records a
:class:`TelegramDelivery` row (never the text — it may be a code) and turns
Telegram's answers into state: "blocked by the user" marks the account so we
stop trying, a rate limit is honoured once, and a network failure is logged
rather than raised.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from AI.models import AccountStatus, TelegramAccount, TelegramDelivery, TelegramDeliveryStatus

logger = logging.getLogger("AI.telegram")

API_ROOT = "https://api.telegram.org"

PRIVATE = "private"
CHANNEL = "channel"
GROUP = "group"
#: Telegram's chat types for a group: a small one, and one upgraded (or
#: created) as a supergroup — which gets a new "-100…" id.
GROUP_TYPES = {"group", "supergroup"}

#: Both the webhook registration and the poller ask for exactly these, so the
#: two ways of receiving updates can never disagree about what arrives.
#: ``my_chat_member`` is how Telegram says "the student blocked the bot" (and
#: "the bot was made an administrator of the channel").
ALLOWED_UPDATES = ["message", "my_chat_member"]

#: The command menu Telegram shows next to the message box. Registered with
#: ``setMyCommands`` from the console, or pasted into @BotFather /setcommands.
BOT_COMMANDS = [
    ("start", "Connect this chat to your account"),
    ("status", "Your token balances and plan"),
    ("help", "What this bot can do"),
    ("unlink", "Disconnect this chat from your account"),
]

#: Telegram's wording for "this chat can no longer hear from the bot". A 403 is
#: always one of these; "chat not found" arrives as a 400 but means the same
#: thing for a private chat — the account behind it is gone.
_BLOCKING_MARKERS = (
    "bot was blocked",
    "user is deactivated",
    "chat not found",
    "bot was kicked",
    "bot can't initiate conversation",
)

#: Where the console's "a channel was detected" hint is kept. An AppSetting
#: row rather than the cache, because the webhook and the console may run in
#: different processes, and the poller certainly does.
DETECTED_CHANNEL_KEY = "telegram_detected_channel"
DETECTED_GROUP_KEY = "telegram_detected_group"
#: The configured channel's and group's real titles, as Telegram last gave
#: them, so student pages name them without calling Telegram on every load.
CHANNEL_TITLE_KEY = "telegram_channel_title"
GROUP_TITLE_KEY = "telegram_group_title"

#: Longest a message may be. Telegram's own limit is 4,096 characters after
#: entity parsing; a margin keeps an escaped body safely under it.
MAX_TEXT = 3900


class TelegramError(Exception):
    """A failed Bot API call. Carries what the API said, for the caller to act on."""

    def __init__(self, message: str, *, error_code: int | None = None,
                 retry_after: int | None = None, network: bool = False, migrate_to=None):
        self.error_code = error_code
        self.retry_after = retry_after
        self.network = network
        #: A group upgraded to a supergroup answers with its new id here.
        self.migrate_to = migrate_to
        super().__init__(message)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def is_configured() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN)


def bot_username() -> str:
    return (getattr(settings, "TELEGRAM_BOT_USERNAME", "") or "").strip().lstrip("@")


def bot_url() -> str:
    """The bot's own chat, without a start token — for "open the bot" buttons."""
    name = bot_username()
    return f"https://t.me/{name}" if name else ""


def channel_id() -> str:
    return (getattr(settings, "TELEGRAM_CHANNEL_ID", "") or "").strip()


def channel_url() -> str:
    return (getattr(settings, "TELEGRAM_CHANNEL_URL", "") or "").strip()


def channel_configured() -> bool:
    return is_configured() and bool(channel_id())


def group_id() -> str:
    return (getattr(settings, "TELEGRAM_GROUP_ID", "") or "").strip()


def group_url() -> str:
    return (getattr(settings, "TELEGRAM_GROUP_URL", "") or "").strip()


def group_configured() -> bool:
    return is_configured() and bool(group_id())


def _is_chat(chat: dict, configured: str) -> bool:
    """Is this the chat the setting names — by its numeric id or its @username?"""
    if not configured or not chat:
        return False
    username = str(chat.get("username") or "")
    return configured == str(chat.get("id")) or (
        bool(username) and configured.lstrip("@").lower() == username.lower()
    )


def _store_title(key: str, configured: str, chat: dict | None) -> None:
    """Keep the configured chat's title, writing only when it changed."""
    from AI.services import settings_service

    title = str((chat or {}).get("title") or "").strip()[:80]
    if not title or not _is_chat(chat, configured):
        return
    value = json.dumps({"for": configured, "title": title})
    try:
        if settings_service.get(key, "") != value:
            settings_service.set_value(key, value)
    except Exception:  # pragma: no cover - a label is never worth failing a call
        logger.exception("Could not store the Telegram chat title")


def _stored_title(key: str, configured: str) -> str:
    """The stored title, but only while it still belongs to the configured chat."""
    from AI.services import settings_service

    if not configured:
        return ""
    try:
        value = json.loads(settings_service.get(key, "") or "{}")
    except (TypeError, ValueError):
        return ""
    if not isinstance(value, dict) or value.get("for") != configured:
        return ""
    return str(value.get("title") or "")


def channel_title() -> str:
    """The channel's name as Telegram shows it, or "" before it was ever checked."""
    return _stored_title(CHANNEL_TITLE_KEY, channel_id())


def group_title() -> str:
    return _stored_title(GROUP_TITLE_KEY, group_id())


def link_ttl_minutes() -> int:
    return max(1, int(getattr(settings, "TELEGRAM_LINK_TTL_MINUTES", 15) or 15))


def absolute_url(path: str) -> str:
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{settings.SITE_URL.rstrip('/')}/{path.lstrip('/')}"


def _redact(text: str) -> str:
    """Take the bot token out of anything that might be logged or stored.

    ``requests`` puts the full URL — token included — into its exception
    messages, and those end up in logs and on the console's delivery table.
    """
    token = settings.TELEGRAM_BOT_TOKEN
    text = str(text or "")
    return text.replace(token, "<token>") if token else text


# ---------------------------------------------------------------------------
# Low-level API
# ---------------------------------------------------------------------------
def _api(method: str, payload: dict, *, timeout: float | None = None):
    """Call one Bot API method and return its ``result``.

    Raises :class:`TelegramError` with Telegram's own error code, description
    and ``retry_after`` so :func:`deliver` can tell a block from a rate limit
    from a typo.
    """
    if not is_configured():
        raise TelegramError("Telegram bot is not configured.")
    url = f"{API_ROOT}/bot{settings.TELEGRAM_BOT_TOKEN}/{method}"
    try:
        response = requests.post(url, json=payload, timeout=timeout or settings.TELEGRAM_TIMEOUT)
    except requests.RequestException as exc:
        raise TelegramError(_redact(f"Telegram request failed: {exc}"), network=True) from None

    try:
        data = response.json()
    except ValueError:
        raise TelegramError("Telegram returned an unreadable response.") from None
    if not isinstance(data, dict):
        raise TelegramError("Telegram returned an unreadable response.")

    if not data.get("ok"):
        parameters = data.get("parameters") or {}
        raise TelegramError(
            _redact(str(data.get("description") or "Telegram rejected the request."))[:255],
            error_code=data.get("error_code") or getattr(response, "status_code", None),
            retry_after=parameters.get("retry_after"),
            migrate_to=parameters.get("migrate_to_chat_id"),
        )
    result = data.get("result")
    return {} if result is None else result


def escape_html(text) -> str:
    return (str(text) if text is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _usable_button_url(url: str) -> bool:
    """Telegram refuses inline buttons that point at localhost or a bare IP.

    One unusable button fails the whole message, so on a laptop (SITE_URL
    http://127.0.0.1:8000) buttons are dropped and the text still arrives.
    """
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not host or "." not in host:
        return False
    if host == "localhost" or host.endswith(".local") or host.startswith("127.") or host == "0.0.0.0":
        return False
    return not host.replace(".", "").isdigit()


def _reply_markup(buttons) -> dict | None:
    rows = [[{"text": str(label)[:64], "url": url}] for label, url in (buttons or []) if _usable_button_url(url)]
    return {"inline_keyboard": rows} if rows else None


def _message_payload(chat_id, text: str, buttons=None) -> dict:
    payload: dict = {
        "chat_id": str(chat_id),
        "text": text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    markup = _reply_markup(buttons)
    if markup:
        payload["reply_markup"] = markup
    return payload


def send_message(chat_id: str, text: str, *, buttons: list[tuple[str, str]] | None = None) -> dict:
    """Send an HTML message and return Telegram's result. Raises on failure.

    The unrecorded primitive. Everything that talks to a person uses
    :func:`deliver` instead, which logs the attempt and never raises.
    """
    return _api("sendMessage", _message_payload(chat_id, text, buttons))


# ---------------------------------------------------------------------------
# Who may receive what
# ---------------------------------------------------------------------------
def is_private_account(account: TelegramAccount | None) -> bool:
    """True only for a one-to-one chat with the student.

    Links made before ``chat_type`` was recorded have it blank. Telegram gives
    every private chat the user's own id, which is always positive, while
    group, supergroup and channel ids are always negative — so a blank type
    with a positive id is a private chat, and anything else is not.
    """
    if account is None or not account.telegram_id:
        return False
    if account.chat_type:
        return account.chat_type == PRIVATE
    return str(account.telegram_id).strip().isdigit()


def can_receive_private(account: TelegramAccount | None) -> bool:
    """May codes and account details be sent to this chat, right now?"""
    return bool(
        account is not None
        and account.is_linked
        and account.blocked_at is None
        and is_private_account(account)
    )


def linked_account(user) -> TelegramAccount | None:
    if user is None or not getattr(user, "pk", None):
        return None
    return TelegramAccount.objects.filter(user=user, is_linked=True).first()


def private_target(user) -> TelegramAccount | None:
    """The chat a private message to ``user`` may go to, or ``None``."""
    if not is_configured():
        return None
    account = linked_account(user)
    return account if can_receive_private(account) else None


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
def _is_blocking(exc: TelegramError) -> bool:
    description = str(exc).lower()
    return exc.error_code == 403 or any(marker in description for marker in _BLOCKING_MARKERS)


def _record(*, user, chat_id, target, kind, status, error="", message_id="") -> TelegramDelivery:
    return TelegramDelivery.objects.create(
        user=user if getattr(user, "pk", None) else None,
        chat_id=str(chat_id or "")[:64],
        target=target,
        kind=(kind or "alert")[:16],
        status=status,
        error=_redact(error)[:255],
        telegram_message_id=str(message_id or "")[:32],
    )


def mark_blocked(account: TelegramAccount, reason: str = "") -> None:
    now = timezone.now()
    TelegramAccount.objects.filter(pk=account.pk).update(blocked_at=now, last_error=_redact(reason)[:255])
    account.blocked_at, account.last_error = now, _redact(reason)[:255]


def deliver(
    chat_id,
    text: str,
    *,
    user=None,
    account: TelegramAccount | None = None,
    kind: str = "alert",
    target: str = PRIVATE,
    buttons: list[tuple[str, str]] | None = None,
    max_retry_wait: int = 3,
    timeout=None,
) -> TelegramDelivery:
    """Send one message and record what happened. Never raises for Telegram's reasons.

    * sent — ``last_delivery_at`` moves and the message id is kept (so a
      channel post can be deleted later);
    * blocked — the student blocked the bot, deleted their account, or the
      chat no longer exists: the account is marked and skipped from now on;
    * failed — anything else, including a 429 whose wait is longer than
      ``max_retry_wait`` seconds (one short wait is honoured, once) and
      network errors.
    """
    if account is None and target == PRIVATE and chat_id:
        account = TelegramAccount.objects.filter(telegram_id=str(chat_id)).first()
    if user is None and account is not None:
        user = account.user

    payload = _message_payload(chat_id, text, buttons)
    attempts = 0
    while True:
        attempts += 1
        try:
            result = _api("sendMessage", payload, timeout=timeout)
            break
        except TelegramError as exc:
            if exc.error_code == 429 and attempts == 1 and exc.retry_after is not None \
                    and int(exc.retry_after) <= max_retry_wait:
                time.sleep(max(0, int(exc.retry_after)))
                continue
            if exc.error_code == 400 and "reply_markup" in payload and attempts == 1 \
                    and ("button" in str(exc).lower() or "url" in str(exc).lower()):
                # A button Telegram does not accept must not cost the message.
                payload.pop("reply_markup", None)
                continue
            if exc.migrate_to and target == GROUP:
                # The group became a supergroup, with a new id. Remembered so
                # the console can show it — the setting must be updated.
                _remember_chat(DETECTED_GROUP_KEY, {"id": exc.migrate_to, "title": "", "type": "supergroup"},
                               "migrated", True)
            if target == PRIVATE and _is_blocking(exc):
                if account is not None:
                    mark_blocked(account, str(exc))
                logger.info("Telegram chat %s is unreachable (%s): %s", chat_id, kind, exc)
                return _record(user=user, chat_id=chat_id, target=target, kind=kind,
                               status=TelegramDeliveryStatus.BLOCKED, error=str(exc))
            logger.warning("Telegram %s delivery to %s failed: %s", kind, chat_id, exc)
            if account is not None:
                TelegramAccount.objects.filter(pk=account.pk).update(last_error=_redact(str(exc))[:255])
            return _record(user=user, chat_id=chat_id, target=target, kind=kind,
                           status=TelegramDeliveryStatus.FAILED, error=str(exc))
        except Exception as exc:  # pragma: no cover - never let a send break the caller
            logger.exception("Telegram %s delivery to %s crashed", kind, chat_id)
            return _record(user=user, chat_id=chat_id, target=target, kind=kind,
                           status=TelegramDeliveryStatus.FAILED, error=f"Unexpected error: {exc}")

    if account is not None:
        now = timezone.now()
        TelegramAccount.objects.filter(pk=account.pk).update(last_delivery_at=now, last_error="")
        account.last_delivery_at = now
    message_id = result.get("message_id", "") if isinstance(result, dict) else ""
    return _record(user=user, chat_id=chat_id, target=target, kind=kind,
                   status=TelegramDeliveryStatus.SENT, message_id=message_id)


def notify_user(user, text: str, *, buttons: list[tuple[str, str]] | None = None, kind: str = "alert") -> bool:
    """Best-effort delivery to a student's own linked, private chat. Never raises.

    Returns True only when Telegram accepted the message. A blocked or
    non-private link is recorded as skipped rather than attempted.
    """
    try:
        if not is_configured():
            return False
        account = linked_account(user)
        if account is None or not account.telegram_id:
            return False
        if not can_receive_private(account):
            _record(user=user, chat_id=account.telegram_id, target=PRIVATE, kind=kind,
                    status=TelegramDeliveryStatus.SKIPPED,
                    error="Blocked by the user" if account.blocked_at else "Not a private chat")
            return False
        delivery = deliver(account.telegram_id, text, user=user, account=account, kind=kind, buttons=buttons)
        return delivery.status == TelegramDeliveryStatus.SENT
    except Exception:  # pragma: no cover - notifications must never break their caller
        logger.exception("Telegram notify_user failed for user=%s", getattr(user, "pk", None))
        return False


def post_to_channel(text: str, *, buttons: list[tuple[str, str]] | None = None,
                    kind: str = "announcement") -> TelegramDelivery:
    """Post to the public channel. Returns the delivery row; never raises."""
    if not channel_configured():
        return _record(user=None, chat_id=channel_id(), target=CHANNEL, kind=kind,
                       status=TelegramDeliveryStatus.SKIPPED, error="Channel not configured")
    return deliver(channel_id(), text, target=CHANNEL, kind=kind, buttons=buttons)


def delete_channel_message(message_id) -> tuple[bool, str]:
    """Delete one of our channel posts. Returns ``(deleted, error)``; never raises.

    Telegram only lets a bot delete its channel posts for 48 hours, and only
    with "Post messages" (its own posts) or "Delete messages" (any post).
    """
    if not channel_configured() or not message_id:
        return False, "Nothing to delete."
    return _delete(channel_id(), message_id)


def post_to_group(text: str, *, buttons: list[tuple[str, str]] | None = None,
                  kind: str = "announcement") -> TelegramDelivery:
    """Post to the community group. Returns the delivery row; never raises.

    Only for things meant for everyone in it — an announcement, a test. Never
    a code or anything about one student's account.
    """
    if not group_configured():
        return _record(user=None, chat_id=group_id(), target=GROUP, kind=kind,
                       status=TelegramDeliveryStatus.SKIPPED, error="Group not configured")
    return deliver(group_id(), text, target=GROUP, kind=kind, buttons=buttons)


def delete_group_message(message_id) -> tuple[bool, str]:
    """Delete one of our group posts (a bot may delete its own for 48 hours)."""
    if not group_configured() or not message_id:
        return False, "Nothing to delete."
    return _delete(group_id(), message_id)


def _delete(chat_id: str, message_id) -> tuple[bool, str]:
    try:
        _api("deleteMessage", {"chat_id": chat_id, "message_id": int(message_id)})
        return True, ""
    except (TelegramError, ValueError) as exc:
        logger.info("Could not delete message %s in %s: %s", message_id, chat_id, exc)
        return False, str(exc)[:255]


# ---------------------------------------------------------------------------
# Linking
# ---------------------------------------------------------------------------
def get_or_create_link(user) -> TelegramAccount:
    """The student's Telegram row. Creating it does *not* make a usable link."""
    account, _ = TelegramAccount.objects.get_or_create(user=user)
    return account


def link_is_valid(account: TelegramAccount | None, now=None) -> bool:
    """Is the account's current token one that /start would accept?"""
    if account is None or not account.link_token or account.link_token_expires_at is None:
        return False
    return account.link_token_expires_at > (now or timezone.now())


def issue_link_token(user) -> TelegramAccount:
    """Mint a fresh single-use token, valid for ``TELEGRAM_LINK_TTL_MINUTES``.

    Only ever called from an explicit press of Connect (a POST), so merely
    opening Settings never creates something that could be used to link.
    """
    account = get_or_create_link(user)
    account.link_token = secrets.token_urlsafe(18)
    account.link_token_expires_at = timezone.now() + timezone.timedelta(minutes=link_ttl_minutes())
    account.save(update_fields=["link_token", "link_token_expires_at"])
    return account


def deep_link(account: TelegramAccount | None) -> str:
    """``t.me/<bot>?start=<token>`` — or "" when there is no live token."""
    name = bot_username()
    if not name or not link_is_valid(account):
        return ""
    return f"https://t.me/{name}?start={account.link_token}"


def link_seconds_left(account: TelegramAccount | None) -> int:
    if not link_is_valid(account):
        return 0
    return max(0, int((account.link_token_expires_at - timezone.now()).total_seconds()))


def _retire_token(account: TelegramAccount) -> None:
    """A used or abandoned token is replaced by one that cannot link anything."""
    account.link_token = secrets.token_urlsafe(18)
    account.link_token_expires_at = None


def link_from_start(token: str, chat: dict, sender: dict | None = None) -> tuple[TelegramAccount | None, str]:
    """Resolve a ``/start <token>`` payload. Returns ``(account, outcome)``.

    ``outcome`` is ``linked`` or why not: ``not_private``, ``invalid``,
    ``expired`` or ``disabled``. The token is consumed with a compare-and-swap
    update, so two presses racing each other cannot both succeed.
    """
    chat = chat or {}
    chat_id = str(chat.get("id") or "")
    if not token or not chat_id:
        return None, "invalid"
    if chat.get("type") != PRIVATE:
        return None, "not_private"

    account = TelegramAccount.objects.filter(link_token=token).select_related("user").first()
    if account is None or account.link_token_expires_at is None:
        return None, "invalid"
    now = timezone.now()
    if account.link_token_expires_at <= now:
        return None, "expired"
    # A brand-new account is still pending until its first code is entered,
    # and linking is how it chooses to get that code on Telegram. Only a
    # disabled (or deactivated) account is refused.
    user = account.user
    if not user.is_active or user.status == AccountStatus.DISABLED:
        return None, "disabled"

    who = sender or chat
    try:
        with transaction.atomic():
            # A Telegram identity may only be attached to one platform account:
            # whichever account held this chat before loses it.
            displaced = list(
                TelegramAccount.objects.filter(telegram_id=chat_id).exclude(pk=account.pk).select_related("user")
            )
            for other in displaced:
                _retire_token(other)
                other.telegram_id, other.is_linked, other.chat_type, other.blocked_at = None, False, "", None
                other.save(update_fields=["telegram_id", "is_linked", "chat_type", "blocked_at",
                                          "link_token", "link_token_expires_at"])
                _drop_unreachable_login_codes(other.user)
            claimed = TelegramAccount.objects.filter(
                pk=account.pk, link_token=token, link_token_expires_at__gt=now,
            ).update(
                telegram_id=chat_id,
                username=(who.get("username") or chat.get("username") or "")[:64],
                first_name=(who.get("first_name") or chat.get("first_name") or "")[:64],
                chat_type=PRIVATE,
                is_linked=True,
                linked_at=now,
                blocked_at=None,
                last_error="",
                link_token=secrets.token_urlsafe(18),
                link_token_expires_at=None,
            )
            if not claimed:
                # Somebody else used the token between the read and now; undo
                # the displacement above along with it.
                raise _LinkRaceLost
    except _LinkRaceLost:
        return None, "invalid"
    account.refresh_from_db()
    return account, "linked"


class _LinkRaceLost(Exception):
    pass


def _drop_unreachable_login_codes(user) -> bool:
    """Switch sign-in codes off when there is no longer anywhere to send them.

    Codes go to Telegram or to a verified e-mail. With neither, keeping the
    requirement would lock the student out of their own account.
    """
    if user.login_otp_enabled and not (user.email and user.is_verified):
        type(user).objects.filter(pk=user.pk).update(login_otp_enabled=False)
        user.login_otp_enabled = False
        return True
    return False


def unlink(account: TelegramAccount, *, tell_chat: bool = True) -> dict:
    """Disconnect a chat. Returns what else changed, for the caller to report.

    Sign-in codes need somewhere to go. If Telegram was the only place (no
    verified e-mail), the sign-in code requirement is switched off rather than
    locking the student out of their own account.
    """
    user = account.user
    chat_id = account.telegram_id
    if tell_chat and chat_id and can_receive_private(account) and is_configured():
        deliver(chat_id, "<b>Disconnected</b>\n\nThis chat is no longer connected to your "
                f"{escape_html(settings.BRAND['full_name'])} account. You will not get codes or alerts here.",
                user=user, account=account, kind="account")

    _retire_token(account)
    account.telegram_id = None
    account.is_linked = False
    account.chat_type = ""
    account.blocked_at = None
    account.save(update_fields=["telegram_id", "is_linked", "chat_type", "blocked_at",
                                "link_token", "link_token_expires_at"])

    return {"login_otp_disabled": _drop_unreachable_login_codes(user)}


def _set_blocked_for_chat(chat_id: str, blocked: bool) -> bool:
    account = TelegramAccount.objects.filter(telegram_id=str(chat_id), is_linked=True).first()
    if account is None:
        return False
    if blocked:
        mark_blocked(account, "Blocked by the user")
    elif account.blocked_at is not None:
        TelegramAccount.objects.filter(pk=account.pk).update(blocked_at=None, last_error="")
    return True


# ---------------------------------------------------------------------------
# Updates: commands and membership changes
# ---------------------------------------------------------------------------
def handle_update(update: dict) -> str:
    """Process one update from the webhook or the poller. Returns a short status."""
    if not isinstance(update, dict):
        return "ignored"
    if update.get("my_chat_member"):
        return _handle_member_update(update["my_chat_member"])
    message = update.get("message") or {}
    if message:
        return _handle_message(message)
    return "ignored"


def _handle_member_update(member: dict) -> str:
    """The bot's own membership changed in some chat.

    In a private chat, "kicked" is the student blocking the bot and "member"
    is them unblocking it (pressing Start/Restart). In a channel, becoming an
    administrator is remembered so the console can show the channel's id —
    the one fact a private channel's owner cannot easily look up.
    """
    chat = member.get("chat") or {}
    status = ((member.get("new_chat_member") or {}).get("status") or "").lower()
    chat_type = chat.get("type")
    if chat_type == PRIVATE and chat.get("id") is not None:
        if status == "kicked":
            _set_blocked_for_chat(chat["id"], True)
            return "blocked"
        if status == "member":
            _set_blocked_for_chat(chat["id"], False)
            return "unblocked"
        return "ignored"
    if chat_type == CHANNEL and chat.get("id") is not None:
        new = member.get("new_chat_member") or {}
        _remember_channel(chat, status, bool(new.get("can_post_messages")))
        return "channel_" + (status or "update")
    if chat_type in GROUP_TYPES and chat.get("id") is not None:
        new = member.get("new_chat_member") or {}
        can_post = status in {"member", "administrator", "creator"} or (
            status == "restricted" and bool(new.get("can_send_messages"))
        )
        _remember_chat(DETECTED_GROUP_KEY, chat, status, can_post)
        return "group_" + (status or "update")
    return "ignored"


def _remember_chat(key: str, chat: dict, status: str, can_post: bool) -> None:
    """Keep what Telegram just said about a channel or group the bot is in —
    the one fact (its id) a private chat's owner cannot easily look up."""
    from AI.services import settings_service

    detail = {
        "id": str(chat.get("id")),
        "title": str(chat.get("title") or "")[:80],
        "username": str(chat.get("username") or "")[:64],
        "type": str(chat.get("type") or "")[:16],
        "status": status,
        "can_post": can_post,
        "at": timezone.now().isoformat(timespec="seconds"),
    }
    try:
        settings_service.set_value(key, json.dumps(detail))
    except Exception:  # pragma: no cover - a hint is never worth failing an update
        logger.exception("Could not remember the detected Telegram chat")
    # When this is the configured chat, its title is fresh news for students too.
    if key == DETECTED_CHANNEL_KEY:
        _store_title(CHANNEL_TITLE_KEY, channel_id(), chat)
    elif key == DETECTED_GROUP_KEY:
        _store_title(GROUP_TITLE_KEY, group_id(), chat)
    logger.info("Telegram %s update: %s (%s) -> %s", detail["type"] or "chat", detail["title"], detail["id"], status)


def _remember_channel(chat: dict, status: str, can_post: bool) -> None:
    _remember_chat(DETECTED_CHANNEL_KEY, chat, status, can_post)


def _detected(key: str) -> dict | None:
    from AI.services import settings_service

    raw = settings_service.get(key, "")
    if not raw:
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def detected_channel() -> dict | None:
    return _detected(DETECTED_CHANNEL_KEY)


def detected_group() -> dict | None:
    return _detected(DETECTED_GROUP_KEY)


def _command(text: str) -> tuple[str, str]:
    """``/start@MyBot abc`` → ``("start", "abc")``."""
    head, _, rest = text.strip().partition(" ")
    name = head[1:].split("@", 1)[0].lower() if head.startswith("/") else ""
    return name, rest.strip()


def _reply(chat: dict, text: str, *, kind: str = "command", buttons=None) -> None:
    deliver(chat.get("id"), text, kind=kind, buttons=buttons,
            target=PRIVATE if chat.get("type") == PRIVATE else "group")


def _linked_here(chat: dict) -> TelegramAccount | None:
    if chat.get("type") != PRIVATE:
        return None
    return TelegramAccount.objects.filter(telegram_id=str(chat.get("id")), is_linked=True).select_related("user").first()


def _handle_message(message: dict) -> str:
    chat = message.get("chat") or {}
    if not chat or chat.get("id") is None:
        return "ignored"
    text = (message.get("text") or "").strip()
    name, argument = _command(text)
    private = chat.get("type") == PRIVATE

    # Anything the student sends from a chat marked blocked means they are
    # talking to the bot again.
    if private:
        account = _linked_here(chat)
        if account is not None and account.blocked_at is not None:
            TelegramAccount.objects.filter(pk=account.pk).update(blocked_at=None, last_error="")

    if name == "start":
        return _cmd_start(chat, argument, message.get("from") or {})
    if name == "help":
        _reply(chat, _help_text())
        return "help"
    if name in {"status", "unlink"} and not private:
        _reply(chat, "For your privacy, use this command in your private chat with the bot"
               + (f": {bot_url()}" if bot_url() else "."))
        return "not_private"
    if name == "status":
        return _cmd_status(chat)
    if name == "unlink":
        return _cmd_unlink(chat, argument)
    if private and text:
        _reply(chat, "I only understand a few commands. Send /help to see them.")
        return "hint"
    return "ignored"


def _brand() -> str:
    return escape_html(settings.BRAND["full_name"])


def _help_text() -> str:
    lines = [f"<b>{_brand()} bot</b>", ""]
    lines += [f"/{name} — {escape_html(description)}" for name, description in BOT_COMMANDS]
    lines += ["", "Codes and alerts only ever come to your private chat with this bot."]
    return "\n".join(lines)


_START_REFUSALS = {
    "not_private": (
        "<b>Link from a private chat</b>\n\nFor your security, accounts can only be connected in a "
        "private chat with the bot — never in a group or channel."
    ),
    "expired": (
        "<b>This link has expired</b>\n\nConnect links work for {ttl} minutes. Open "
        "<b>Settings → Telegram</b> on the website and press <b>Connect Telegram</b> for a new one."
    ),
    "invalid": (
        "<b>This link can't be used</b>\n\nEach connect link works once and expires after {ttl} minutes. "
        "Open <b>Settings → Telegram</b> on the website and press <b>Connect Telegram</b> for a fresh link."
    ),
    "disabled": "This account is disabled. Please contact support.",
}


def _cmd_start(chat: dict, token: str, sender: dict) -> str:
    if token:
        account, outcome = link_from_start(token, chat, sender)
        if account is not None:
            name = escape_html(account.user.display_name)
            if not account.user.is_verified:
                # Mid-sign-up: the code page is waiting for them, not the app.
                deliver(
                    chat["id"],
                    f"<b>Linked successfully</b>\n\nHi {name}, this chat is now connected to your new "
                    f"<b>{_brand()}</b> account.\n\n"
                    "Go back to the sign-up page, press <b>I've pressed Start</b>, then "
                    "<b>Send the code to Telegram instead</b>. Your verification code will arrive here.",
                    user=account.user, account=account, kind="account",
                )
                return "linked"
            deliver(
                chat["id"],
                f"<b>Linked successfully</b>\n\nHi {name}, this chat is now connected to your "
                f"<b>{_brand()}</b> account.\n\n"
                "You'll get alerts here, and verification codes if you choose Telegram for them. "
                "Send /status to see your balances.",
                user=account.user, account=account, kind="account",
                buttons=[("Open the assistant", absolute_url("/chat/"))],
            )
            return "linked"
        _reply(chat, _START_REFUSALS.get(outcome, _START_REFUSALS["invalid"]).format(ttl=link_ttl_minutes()),
               kind="account")
        return outcome

    if chat.get("type") != PRIVATE:
        _reply(chat, f"Open a private chat with me to connect your account"
               + (f": {bot_url()}" if bot_url() else "."))
        return "greeted"

    account = _linked_here(chat)
    if account is not None:
        _reply(chat, f"<b>Welcome back, {escape_html(account.user.display_name)}</b>\n\n"
                     "This chat is connected to your account. Send /status for your balances or /help "
                     "for everything else.")
        return "welcome_back"
    _reply(
        chat,
        f"<b>Welcome to {_brand()}</b>\n\n"
        "To get your alerts and codes here:\n"
        "1. Sign in on the website.\n"
        "2. Open <b>Settings → Telegram</b> and press <b>Connect Telegram</b>.\n"
        "3. Tap the button (or scan the QR code) and press <b>Start</b>.\n\n"
        f"Links work once and expire after {link_ttl_minutes()} minutes.",
        buttons=[("Open Settings", absolute_url("/app/settings/"))],
    )
    return "greeted"


def _cmd_status(chat: dict) -> str:
    from AI.services import wallet_service

    account = _linked_here(chat)
    if account is None:
        _reply(chat, "This chat is not connected to an account yet. Send /start to see how to connect it.")
        return "unlinked"

    user = account.user
    rows = [
        row for row in wallet_service.overview(user)
        if row["model"].is_active and (row["wallet"] is not None or row["balance"] > 0)
    ]
    lines = ["<b>Your token balances</b>", ""]
    if rows:
        for row in rows:
            note = "" if row["usable"] else " <i>(not available)</i>" if row["balance"] > 0 else ""
            lines.append(f"• {escape_html(row['model'].name)} — <b>{row['balance']:,}</b> tokens{note}")
    else:
        lines.append("You don't have any tokens yet.")

    subscription = user.active_subscription
    if subscription is not None:
        ends = timezone.localtime(subscription.ends_at).strftime("%d %b %Y")
        lines += ["", f"Plan: <b>{escape_html(subscription.plan.name)}</b> until {ends}"]

    _reply(chat, "\n".join(lines), kind="account", buttons=[
        ("Open the assistant", absolute_url("/chat/")),
        ("Plans and top-ups", absolute_url("/app/pricing/")),
    ])
    return "status"


def _cmd_unlink(chat: dict, argument: str) -> str:
    account = _linked_here(chat)
    if account is None:
        _reply(chat, "This chat is not connected to an account.")
        return "unlinked"
    if argument.lower() not in {"confirm", "yes"}:
        warning = ""
        if account.user.login_otp_enabled and not (account.user.email and account.user.is_verified):
            warning = ("\n\n<b>Note:</b> Telegram is where your sign-in codes go, and there is no verified e-mail on the "
                       "account, so sign-in codes will be switched off.")
        _reply(chat, f"Disconnect this chat from <b>{escape_html(account.user.display_name)}</b>?\n\n"
                     f"You'll stop getting alerts and codes here.{warning}\n\n"
                     "To confirm, send: <code>/unlink confirm</code>", kind="account")
        return "unlink_confirm"
    changes = unlink(account, tell_chat=True)
    if changes["login_otp_disabled"]:
        deliver(chat["id"], "Sign-in codes have been switched off for your account. You can turn them back "
                            "on in Settings → Security.", kind="account")
    return "unlinked_now"


# ---------------------------------------------------------------------------
# Bot administration
# ---------------------------------------------------------------------------
def webhook_url() -> str:
    return f"{settings.SITE_URL.rstrip('/')}/webhooks/telegram/"


def set_webhook(url: str) -> dict:
    payload = {"url": url, "allowed_updates": ALLOWED_UPDATES}
    if settings.TELEGRAM_WEBHOOK_SECRET:
        payload["secret_token"] = settings.TELEGRAM_WEBHOOK_SECRET
    return _api("setWebhook", payload)


def set_commands() -> dict:
    return _api("setMyCommands", {
        "commands": [{"command": name, "description": description} for name, description in BOT_COMMANDS],
    })


def get_updates(offset: int | None = None, timeout: int = 25) -> list[dict]:
    """Long-poll for updates — the development alternative to a webhook."""
    payload = {"timeout": timeout, "allowed_updates": ALLOWED_UPDATES}
    if offset is not None:
        payload["offset"] = offset
    # The HTTP timeout has to outlast the long poll, or every quiet poll
    # would be reported as a network error.
    result = _api("getUpdates", payload, timeout=timeout + max(5, settings.TELEGRAM_TIMEOUT))
    return result if isinstance(result, list) else []


def bot_status(*, timeout: float = 6) -> dict:
    """getMe and getWebhookInfo, for the console. Never raises."""
    status = {"configured": is_configured(), "me": None, "webhook": None, "error": ""}
    if not status["configured"]:
        return status
    try:
        status["me"] = _api("getMe", {}, timeout=timeout)
        status["webhook"] = _api("getWebhookInfo", {}, timeout=timeout)
    except TelegramError as exc:
        status["error"] = str(exc)
    return status


def group_status(*, bot_id=None, timeout: float = 6) -> dict:
    """getChat and the bot's membership in the group, for the console. Never raises."""
    status = {"configured": group_configured(), "chat": None, "member": None,
              "can_post": False, "can_delete": False, "error": ""}
    if not status["configured"]:
        return status
    try:
        status["chat"] = _api("getChat", {"chat_id": group_id()}, timeout=timeout)
        _store_title(GROUP_TITLE_KEY, group_id(), status["chat"])
        if bot_id is None:
            bot_id = (_api("getMe", {}, timeout=timeout) or {}).get("id")
        if bot_id:
            member = _api("getChatMember", {"chat_id": group_id(), "user_id": bot_id}, timeout=timeout)
            status["member"] = member
            role = member.get("status")
            status["can_post"] = role in {"creator", "administrator", "member"} or (
                role == "restricted" and bool(member.get("can_send_messages"))
            )
            status["can_delete"] = role == "creator" or bool(member.get("can_delete_messages"))
    except TelegramError as exc:
        status["error"] = str(exc)
        if exc.migrate_to:
            status["error"] = (f"This group became a supergroup; its new id is {exc.migrate_to}. "
                               "Put that in TELEGRAM_GROUP_ID.")
    return status


def channel_status(*, bot_id=None, timeout: float = 6) -> dict:
    """getChat and the bot's own rights there, for the console. Never raises."""
    status = {"configured": channel_configured(), "chat": None, "member": None,
              "can_post": False, "can_delete": False, "error": ""}
    if not status["configured"]:
        return status
    try:
        status["chat"] = _api("getChat", {"chat_id": channel_id()}, timeout=timeout)
        _store_title(CHANNEL_TITLE_KEY, channel_id(), status["chat"])
        if bot_id is None:
            bot_id = (_api("getMe", {}, timeout=timeout) or {}).get("id")
        if bot_id:
            member = _api("getChatMember", {"chat_id": channel_id(), "user_id": bot_id}, timeout=timeout)
            status["member"] = member
            is_owner = member.get("status") == "creator"
            status["can_post"] = is_owner or bool(member.get("can_post_messages"))
            status["can_delete"] = is_owner or bool(member.get("can_delete_messages"))
    except TelegramError as exc:
        status["error"] = str(exc)
    return status
