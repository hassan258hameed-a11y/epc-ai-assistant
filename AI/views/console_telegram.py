"""Telegram — the bot, the channel, the group, and every message they sent.

One page answers the questions an administrator has about Telegram: is it
set up (and if not, what exactly is missing — with the steps to fix it on the
same page), is it working (the bot, the webhook, the channel and the
community group and the bot's rights in each, straight from Telegram), which
automatic alerts go out there, who has connected, and did a given message
arrive (the delivery log, which never holds the text itself — some of it is
codes).

Status comes from live Bot API calls, cached for a minute so the page stays
quick and Telegram is not asked the same question on every reload; "Check"
refreshes it on demand. Nothing here ever shows the bot token or the webhook
secret — only whether they are set.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from AI.models import (
    AuditAction,
    OTPPurpose,
    TelegramAccount,
    TelegramDelivery,
    Role,
    TelegramDeliveryStatus,
    TelegramMessage,
    TelegramMessageStatus,
    TelegramMessageTarget,
    User,
)
from AI.services import (
    audit_service,
    otp_service,
    settings_service,
    staff_permissions,
    telegram_message_service,
    telegram_service,
)
from AI.utils import qr, ratelimit
from AI.utils.decorators import admin_required
from AI.utils.pagination import clean_int, page_range, paginate
from AI.utils.ratelimit import RateLimited
from AI.views.console_insights import _context

#: v2 holds the group's status too; an older cached value is simply ignored.
STATUS_CACHE_KEY = "console:telegram:status:v2"
STATUS_CACHE_SECONDS = 60

#: The switches deciding which automatic alerts reach students' Telegram
#: chats. The first is the master; labels and help texts live in
#: settings_service.DEFAULTS, so the Settings page and this one agree.
ALERT_MASTER = "telegram_alerts_enabled"
ALERT_CATEGORIES = (
    "telegram_send_payment",
    "telegram_send_tokens",
    "telegram_send_expiry",
    "telegram_send_system",
    "telegram_send_referral",
)
ALERT_KEYS = (ALERT_MASTER, *ALERT_CATEGORIES)

#: Linked chats listed per page under "Students on Telegram".
LINKED_PER_PAGE = 20

KIND_CHOICES = [
    ("otp", "Verification codes"),
    ("alert", "Alerts"),
    ("payment", "Payments"),
    ("announcement", "Announcements"),
    ("message", "Messages from the team"),
    ("account", "Account and linking"),
    ("command", "Bot replies"),
    ("test", "Tests"),
]


def _live_status(*, refresh: bool = False) -> dict:
    """getMe, getWebhookInfo, and getChat plus the bot's rights in the channel
    and the group — cached."""
    if not telegram_service.is_configured():
        return {"bot": telegram_service.bot_status(), "channel": telegram_service.channel_status(),
                "group": telegram_service.group_status(), "checked_at": None}
    if not refresh:
        cached = cache.get(STATUS_CACHE_KEY)
        if cached is not None:
            return cached
    bot = telegram_service.bot_status()
    bot_id = (bot.get("me") or {}).get("id")
    # With the bot itself unreachable, asking about its chats would only
    # repeat the same error twice more.
    reachable = bool(bot_id) or not bot["error"]
    channel = telegram_service.channel_status(bot_id=bot_id) if reachable else _unchecked(
        telegram_service.channel_configured())
    group = telegram_service.group_status(bot_id=bot_id) if reachable else _unchecked(
        telegram_service.group_configured())
    status = {"bot": bot, "channel": channel, "group": group, "checked_at": timezone.now()}
    cache.set(STATUS_CACHE_KEY, status, STATUS_CACHE_SECONDS)
    return status


def _unchecked(configured: bool) -> dict:
    return {"configured": configured, "chat": None, "member": None,
            "can_post": False, "can_delete": False, "error": ""}


def _detected_group_hint(group_id: str) -> dict | None:
    """The group the bot was last added to (or that migrated), when it is not
    the one the setting already names — the id to paste into .env."""
    detected = telegram_service.detected_group()
    if not detected or not detected.get("id"):
        return None
    same_id = str(detected["id"]) == group_id
    username = (detected.get("username") or "").lower()
    same_name = bool(username) and username == group_id.lstrip("@").lower()
    if same_id or same_name:
        return None
    return {**detected, "migrated": detected.get("status") == "migrated",
            "removed": detected.get("status") in {"left", "kicked"}}


def _alert_switches() -> list[dict]:
    """The master switch and the five categories, with the labels students'
    alerts are described by on the Settings page."""
    rows = []
    for key in ALERT_KEYS:
        spec = settings_service.DEFAULTS.get(key)
        rows.append({
            "key": key,
            "label": spec[3] if spec else key,
            "help": spec[4] if spec else "",
            "on": bool(settings_service.get(key, True)),
        })
    return rows


def _checklist(bot: dict, channel: dict, group: dict | None = None) -> list[dict]:
    """The .env keys, each as set / missing — never their secret values."""
    site = settings.SITE_URL.rstrip("/")
    me = bot.get("me") or {}
    username = telegram_service.bot_username()
    return [
        {"key": "TELEGRAM_BOT_TOKEN", "ok": telegram_service.is_configured(),
         "value": "set (hidden)" if telegram_service.is_configured() else "missing",
         "note": "From @BotFather. Keep it secret."},
        {"key": "TELEGRAM_BOT_USERNAME", "ok": bool(username) and (not me or me.get("username", "").lower() == username.lower()),
         "value": f"@{username}" if username else "missing",
         "note": "Must match the bot's real username" + (f" (@{me['username']})" if me.get("username") else "") + "."},
        {"key": "TELEGRAM_WEBHOOK_SECRET", "ok": bool(settings.TELEGRAM_WEBHOOK_SECRET),
         "value": "set (hidden)" if settings.TELEGRAM_WEBHOOK_SECRET else "missing",
         "note": "Required in production: without it the webhook refuses every call."},
        {"key": "TELEGRAM_CHANNEL_ID", "ok": bool(telegram_service.channel_id()) and not channel.get("error"),
         "value": telegram_service.channel_id() or "missing",
         "note": "@username of a public channel, or -100… id of a private one."},
        {"key": "TELEGRAM_CHANNEL_URL", "ok": bool(telegram_service.channel_url()),
         "value": telegram_service.channel_url() or "optional",
         "note": "The link students see under Settings → Telegram.", "optional": True},
        {"key": "TELEGRAM_GROUP_ID",
         "ok": bool(telegram_service.group_id()) and not (group or {}).get("error"),
         "value": telegram_service.group_id() or "optional",
         "note": "The community group's -100… id — copy it from “Detected group” once the bot is in it.",
         "optional": not telegram_service.group_id()},
        {"key": "TELEGRAM_GROUP_URL", "ok": bool(telegram_service.group_url()),
         "value": telegram_service.group_url() or "optional",
         "note": "The group's invite link, shown to students as “Join our Telegram group”.", "optional": True},
        {"key": "SITE_URL", "ok": site.startswith("https://"),
         "value": site,
         "note": "Must be https:// for Telegram to call the webhook and show buttons."},
    ]


@admin_required
def telegram(request):
    refresh = request.GET.get("refresh") == "1"
    live = _live_status(refresh=refresh)
    bot, channel, group = live["bot"], live["channel"], live["group"]

    deliveries = TelegramDelivery.objects.select_related("user")
    status_filter = (request.GET.get("status") or "").strip()
    if status_filter in TelegramDeliveryStatus.values:
        deliveries = deliveries.filter(status=status_filter)
    else:
        status_filter = ""
    kind_filter = (request.GET.get("kind") or "").strip()
    if kind_filter:
        deliveries = deliveries.filter(kind=kind_filter)
    page = paginate(request, deliveries.order_by("-created_at", "-id"), per_page=30)

    week_ago = timezone.now() - timedelta(days=7)
    week = TelegramDelivery.objects.filter(created_at__gte=week_ago).aggregate(
        sent=Count("id", filter=Q(status=TelegramDeliveryStatus.SENT)),
        failed=Count("id", filter=Q(status=TelegramDeliveryStatus.FAILED)),
        blocked=Count("id", filter=Q(status=TelegramDeliveryStatus.BLOCKED)),
    )
    linked = TelegramAccount.objects.filter(is_linked=True)
    # The tiles say "students", so staff who linked their own chat (to test,
    # or for sign-in codes) are left out of them; the list below shows everyone.
    linked_students = linked.exclude(user__role=Role.ADMIN).exclude(user__is_superuser=True)
    counts = {
        "linked": linked_students.count(),
        "blocked": linked_students.filter(blocked_at__isnull=False).count(),
        "login_otp": (
            # Required for every student: they all count, plus staff who opted in.
            User.objects.filter(Q(login_otp_enabled=True) | (~Q(role=Role.ADMIN) & Q(is_superuser=False))).count()
            if otp_service.students_must_use_codes()
            else User.objects.filter(login_otp_enabled=True).count()
        ),
        "week_sent": week["sent"] or 0,
        "week_failed": (week["failed"] or 0) + (week["blocked"] or 0),
    }

    webhook = bot.get("webhook") or {}
    webhook_expected = telegram_service.webhook_url()
    me_account = TelegramAccount.objects.filter(user=request.user).first()
    my_link = telegram_service.deep_link(me_account) if me_account and not me_account.is_linked else ""

    # Who has connected, newest first. Names are student records, so only a
    # role that reads student profiles gets the list (the counts stay for all).
    linked_page = None
    if staff_permissions.can(request.user, "students"):
        linked_page = Paginator(
            linked.select_related("user").order_by("-linked_at", "-pk"), LINKED_PER_PAGE,
        ).get_page(clean_int(request.GET.get("linked"), minimum=1) or 1)
    alerts = _alert_switches()

    return render(request, "console/telegram.html", _context(
        "telegram",
        configured=telegram_service.is_configured(),
        bot=bot,
        me=bot.get("me") or {},
        webhook=webhook,
        webhook_expected=webhook_expected,
        webhook_matches=bool(webhook.get("url")) and webhook.get("url") == webhook_expected,
        site_is_https=settings.SITE_URL.startswith("https://"),
        secret_set=bool(settings.TELEGRAM_WEBHOOK_SECRET),
        debug=settings.DEBUG,
        channel=channel,
        channel_id=telegram_service.channel_id(),
        channel_url=telegram_service.channel_url(),
        channel_title=telegram_service.channel_title(),
        codes_everywhere=otp_service.sends_everywhere(OTPPurpose.LOGIN),
        codes_required=otp_service.students_must_use_codes(),
        detected=telegram_service.detected_channel(),
        group=group,
        group_id=telegram_service.group_id(),
        group_url=telegram_service.group_url(),
        detected_group=_detected_group_hint(telegram_service.group_id()),
        alert_master=alerts[0],
        alert_categories=alerts[1:],
        alerts_on=sum(1 for row in alerts[1:] if row["on"]) if alerts[0]["on"] else 0,
        linked_page=linked_page,
        checked_at=live.get("checked_at"),
        checklist=_checklist(bot, channel, group),
        counts=counts,
        page=page,
        page_numbers=page_range(page),
        statuses=TelegramDeliveryStatus.choices,
        kinds=KIND_CHOICES,
        status_filter=status_filter,
        kind_filter=kind_filter,
        me_account=me_account,
        me_linked=bool(me_account and me_account.is_linked),
        me_can_receive=telegram_service.can_receive_private(me_account),
        my_link=my_link,
        my_qr=qr.svg(my_link, dark="currentColor") if my_link else "",
        bot_username=telegram_service.bot_username(),
        commands=telegram_service.BOT_COMMANDS,
        ttl=telegram_service.link_ttl_minutes(),
    ))


def _test_text(where: str) -> str:
    """A test message: plain bold text, no emoji — like everything the bot sends."""
    stamp = timezone.localtime().strftime("%d %b %Y, %H:%M")
    # Name the zone: Telegram shows the reader's own clock beside the message,
    # which differs from the site's whenever the reader is somewhere else.
    zone = settings.TIME_ZONE.rsplit("/", 1)[-1].replace("_", " ")
    return (f"<b>Test from the {telegram_service.escape_html(settings.BRAND['full_name'])} console</b>\n\n"
            f"If you can read this, {where} works. Sent {stamp} ({telegram_service.escape_html(zone)} time).")


@admin_required
@require_POST
def telegram_action(request):
    """Every button on the page. Each one says plainly what happened."""
    action = request.POST.get("action", "")
    back = redirect("console:telegram")

    if action == "login_otp":
        return _toggle_my_login_codes(request, back)

    if action == "alert_settings":
        # A setting, not a call to Telegram: it can be decided before the bot
        # exists, and is not rate-limited like the buttons that send.
        return _save_alert_settings(request, back)

    if not telegram_service.is_configured():
        messages.error(request, "Set TELEGRAM_BOT_TOKEN in .env first.")
        return back

    try:
        ratelimit.enforce("otp_send", f"tg-console:{request.user.pk}", limit=20, window=600)
    except RateLimited as exc:
        messages.error(request, exc.friendly_message)
        return back

    if action == "register_webhook":
        url = telegram_service.webhook_url()
        if not url.startswith("https://"):
            messages.error(request, f"Telegram only calls https:// addresses, and SITE_URL gives {url}. "
                                    "Use `python manage.py telegram_bot --poll` on this machine instead.")
            return back
        if not settings.TELEGRAM_WEBHOOK_SECRET and not settings.DEBUG:
            messages.error(request, "Set TELEGRAM_WEBHOOK_SECRET first — in production the webhook refuses "
                                    "every call without it.")
            return back
        try:
            telegram_service.set_webhook(url)
        except telegram_service.TelegramError as exc:
            messages.error(request, f"Telegram refused the webhook: {exc}")
            return back
        _audit(request, f"Registered the Telegram webhook at {url}")
        messages.success(request, f"Webhook registered: {url}")

    elif action == "set_commands":
        try:
            telegram_service.set_commands()
        except telegram_service.TelegramError as exc:
            messages.error(request, f"Telegram refused the command list: {exc}")
            return back
        _audit(request, "Registered the Telegram bot's command menu")
        messages.success(request, "Command menu registered — students see it next to the message box.")

    elif action == "test_channel":
        if not telegram_service.channel_configured():
            messages.error(request, "Set TELEGRAM_CHANNEL_ID first.")
            return back
        delivery = telegram_service.post_to_channel(_test_text("posting to this channel"), kind="test")
        if delivery.status == TelegramDeliveryStatus.SENT:
            _audit(request, "Sent a test post to the Telegram channel")
            messages.success(request, "Test posted to the channel. Delete it there when you have seen it.")
        else:
            messages.error(request, f"The channel post failed: {delivery.error or delivery.get_status_display()}")

    elif action == "test_group":
        if not telegram_service.group_configured():
            messages.error(request, "Set TELEGRAM_GROUP_ID first.")
            return back
        delivery = telegram_service.post_to_group(_test_text("posting to this group"), kind="test")
        if delivery.status == TelegramDeliveryStatus.SENT:
            _audit(request, "Sent a test post to the Telegram group")
            messages.success(request, "Test posted to the group. Delete it there when you have seen it.")
        else:
            error = delivery.error or delivery.get_status_display()
            moved = _detected_group_hint(telegram_service.group_id())
            if moved and moved["migrated"]:
                error += f" — it became a supergroup; its new id is {moved['id']} (see “Detected group”)"
            messages.error(request, f"The group post failed: {error}")

    elif action == "test_me":
        account = telegram_service.linked_account(request.user)
        if not telegram_service.can_receive_private(account):
            messages.error(request, "Connect your own Telegram below first"
                           + (" — you blocked the bot; open it and press Start." if account and account.blocked_at else "."))
            return back
        delivery = telegram_service.deliver(
            account.telegram_id, _test_text("delivery to your private chat"),
            user=request.user, account=account, kind="test",
        )
        if delivery.status == TelegramDeliveryStatus.SENT:
            messages.success(request, "Test sent to your Telegram.")
        else:
            messages.error(request, f"Telegram didn't take it: {delivery.error or delivery.get_status_display()}")

    elif action == "check":
        live = _live_status(refresh=True)
        if live["bot"]["error"]:
            messages.error(request, f"Telegram said: {live['bot']['error']}")
            return back
        problems = 0
        channel, group = live["channel"], live["group"]
        if channel["configured"] and channel["error"]:
            messages.error(request, f"The channel check failed: {channel['error']}")
            problems += 1
        elif channel["configured"] and not channel["can_post"]:
            messages.warning(request, "The bot is in the channel but cannot post. Make it an administrator "
                                      "with “Post messages”.")
            problems += 1
        if group["configured"] and group["error"]:
            messages.error(request, f"The group check failed: {group['error']}")
            problems += 1
        elif group["configured"] and not group["can_post"]:
            messages.warning(request, "The bot is in the group but cannot send messages. Let members send "
                                      "messages, or make the bot an administrator.")
            problems += 1
        if not problems:
            messages.success(request, "Checked just now — see the status below.")

    elif action == "connect_me":
        account = telegram_service.get_or_create_link(request.user)
        if account.is_linked:
            messages.info(request, "Your Telegram is already connected.")
        else:
            telegram_service.issue_link_token(request.user)
            messages.success(request, f"Your link is ready — it works once for {telegram_service.link_ttl_minutes()} minutes.")

    elif action == "unlink_me":
        account = telegram_service.linked_account(request.user)
        if account is not None:
            telegram_service.unlink(account)
            messages.success(request, "Your Telegram has been disconnected.")

    else:
        messages.error(request, "Unknown action.")
    return back


def _toggle_my_login_codes(request, back):
    """An administrator opting their own sign-in into the code step."""
    user = request.user
    wanted = request.POST.get("enabled") == "on"
    if wanted and not (telegram_service.private_target(user) or (user.email and user.is_verified)):
        messages.error(request, "Codes need somewhere to go: a verified e-mail or your own linked Telegram.")
        return back
    user.login_otp_enabled = wanted
    user.save(update_fields=["login_otp_enabled"])
    _audit(request, f"{'Enabled' if wanted else 'Disabled'} sign-in codes on their own account")
    messages.success(request, "Sign-in codes are on for your account." if wanted else "Sign-in codes are off.")
    return back


def _save_alert_settings(request, back):
    """The master switch and the five categories, saved together.

    An unticked box is simply absent from the post, so every key is written
    each time — what the form showed is what is stored.
    """
    changes = []
    for row in _alert_switches():
        wanted = request.POST.get(row["key"]) == "on"
        settings_service.set_value(row["key"], wanted, actor=request.user)
        if wanted != row["on"]:
            changes.append(f"{row['label']} {'on' if wanted else 'off'}")
    _audit(request, "Telegram alerts: " + (", ".join(changes) if changes else "saved, nothing changed"))
    if not changes:
        messages.info(request, "Nothing changed.")
    elif request.POST.get(ALERT_MASTER) != "on":
        messages.success(request, "Saved. Automatic alerts no longer go to Telegram; they stay in the app.")
    else:
        messages.success(request, "Saved — Telegram alerts follow the new switches from now on.")
    return back


# ---------------------------------------------------------------------------
# Send a message
# ---------------------------------------------------------------------------
#: Which staff area each destination needs: posting where everyone reads is
#: Content's job, writing to students is Student care's.
SEND_AREAS = {
    TelegramMessageTarget.CHANNEL: "content",
    TelegramMessageTarget.GROUP: "content",
    TelegramMessageTarget.STUDENT: "student_care",
    TelegramMessageTarget.STUDENTS: "student_care",
}
HISTORY_SIZE = 20


def _allowed_targets(user) -> list[str]:
    return [target for target, area in SEND_AREAS.items() if staff_permissions.can(user, area)]


@admin_required
def telegram_send(request):
    """Write one message and send it to the channel, the group or students."""
    allowed = _allowed_targets(request.user)
    if request.method == "POST":
        return _send(request, allowed)

    dest = telegram_message_service.destinations()
    can_message = TelegramMessageTarget.STUDENT in allowed
    students = [
        {"id": account.user_id, "name": account.user.display_name, "username": account.user.username,
         "telegram": f"@{account.username}" if account.username else (account.first_name or "")}
        for account in (dest["students"] if can_message else [])
    ]
    history = TelegramMessage.objects.filter(target__in=allowed).select_related("recipient", "created_by")[:HISTORY_SIZE]
    for row in history:
        row.deletable = telegram_message_service.can_delete(row)

    # A message that failed its checks comes back as it was typed; otherwise
    # ?to=student&student=<id> arrives from a student's page, preselected.
    draft = request.session.pop("tg_send_draft", None) or {}
    preset_target = draft.get("target") or request.GET.get("to", "")
    preset_student = clean_int(draft.get("student") or request.GET.get("student"))
    if preset_target not in allowed:
        preset_target = ""
    return render(request, "console/telegram_send.html", _context(
        "telegram_send",
        dest=dest,
        allowed=allowed,
        students=students,
        history=history,
        preset_target=preset_target,
        preset_student=preset_student,
        draft=draft,
        max_body=telegram_message_service.MAX_BODY,
        bot_name=settings.BRAND["full_name"],
        bot_username=telegram_service.bot_username(),
    ))


def _send(request, allowed):
    back = redirect("console:telegram_send")
    target = request.POST.get("target", "")
    if target not in allowed:
        messages.error(request, "Choose where to send it." if target not in SEND_AREAS
                       else staff_permissions.DENIED_AREA)
        return back
    try:
        ratelimit.enforce("otp_send", f"tg-send:{request.user.pk}", limit=30, window=600)
    except RateLimited as exc:
        messages.error(request, exc.friendly_message)
        return back

    recipient = None
    if target == TelegramMessageTarget.STUDENT:
        recipient = User.objects.filter(pk=clean_int(request.POST.get("student"))).first()
    try:
        message = telegram_message_service.create(
            created_by=request.user, target=target,
            title=request.POST.get("title", ""), body=request.POST.get("body", ""),
            button_label=request.POST.get("button_label", ""), button_url=request.POST.get("button_url", ""),
            recipient=recipient, in_app=request.POST.get("in_app") == "on",
        )
    except telegram_message_service.MessageError as exc:
        messages.error(request, str(exc))
        # Keep what was typed: the page reads it back from the session once.
        request.session["tg_send_draft"] = {
            key: request.POST.get(key, "")[:4000]
            for key in ("target", "student", "title", "body", "button_label", "button_url", "in_app")
        }
        return back

    where = {
        TelegramMessageTarget.CHANNEL: "the channel",
        TelegramMessageTarget.GROUP: "the community group",
        TelegramMessageTarget.STUDENT: recipient.display_name if recipient else "the student",
        TelegramMessageTarget.STUDENTS: f"{message.recipients:,} student{'s' if message.recipients != 1 else ''}",
    }[target]
    audit_service.record(
        AuditAction.SETTINGS_UPDATE, actor=request.user, request=request,
        target=recipient if recipient else None, target_label="Telegram",
        description=f"Sent a Telegram message to {where}"[:255],
    )
    if message.status == TelegramMessageStatus.SENDING:
        messages.success(request, f"Sending to {where} now. The list below fills in as it goes.")
    elif message.status == TelegramMessageStatus.SENT:
        messages.success(request, f"Sent to {where}.")
    else:
        note = " It is in their notifications on the site." if message.in_app else ""
        messages.error(request, f"Not delivered to {where}: {message.error or 'Telegram refused it'}.{note}")
    return back


@admin_required
@require_POST
def telegram_message_delete(request, pk: int):
    message = TelegramMessage.objects.filter(pk=pk).first()
    back = redirect("console:telegram_send")
    if message is None:
        messages.error(request, "That message no longer exists.")
        return back
    try:
        telegram_message_service.delete_post(message)
    except telegram_message_service.MessageError as exc:
        messages.error(request, str(exc))
        return back
    _audit(request, f"Deleted a Telegram {message.get_target_display().lower()} post")
    messages.success(request, "Deleted from Telegram.")
    return back


def _audit(request, description: str) -> None:
    audit_service.record(
        AuditAction.SETTINGS_UPDATE, actor=request.user, request=request,
        target_label="Telegram", description=description[:255],
    )
