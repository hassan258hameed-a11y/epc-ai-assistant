"""In-app notifications, Telegram delivery and reminder scheduling.

Every alert is de-duplicated through :class:`AlertLog` so students are never
spammed with the same reminder twice.
"""
from __future__ import annotations

import logging
import time

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Case, Count, F, IntegerField, Q, QuerySet, When
from django.utils import timezone

from AI.models import (
    AlertLog,
    Announcement,
    AnnouncementAudience,
    Notification,
    NotificationCategory,
    NotificationLevel,
    NotificationPreference,
    Role,
    Subscription,
    User,
    UserAIWallet,
)
from AI.services import settings_service, telegram_service

logger = logging.getLogger("AI.notifications")


def preferences(user) -> NotificationPreference:
    prefs, _ = NotificationPreference.objects.get_or_create(user=user)
    return prefs


def notify(
    user,
    title: str,
    body: str = "",
    *,
    level: str = NotificationLevel.INFO,
    category: str = NotificationCategory.SYSTEM,
    action_url: str = "",
    action_label: str = "",
    telegram: bool = False,
    telegram_text: str = "",
    telegram_buttons: list[tuple[str, str]] | None = None,
) -> Notification:
    """Create an in-app notification and optionally mirror it to Telegram."""
    notification = Notification.objects.create(
        user=user,
        title=title[:140],
        body=body,
        level=level,
        category=category,
        action_url=action_url[:255],
        action_label=action_label[:48],
    )

    if telegram and telegram_category_on(category):
        # A push is a courtesy on top of the in-app row: whatever happens on
        # the Telegram side, the caller's operation has already succeeded.
        try:
            prefs = preferences(user)
            allowed = prefs.telegram_enabled and _category_enabled(prefs, category)
            if allowed:
                text = telegram_text or (
                    f"<b>{telegram_service.escape_html(title)}</b>"
                    + (f"\n\n{telegram_service.escape_html(body)}" if body else "")
                )
                buttons = telegram_buttons
                if buttons is None and action_url:
                    buttons = [(action_label or "Open", _absolute(action_url))]
                telegram_service.notify_user(user, text, buttons=buttons, kind=_TELEGRAM_KIND.get(category, "alert"))
        except Exception:  # pragma: no cover
            logger.exception("Telegram mirror failed for user=%s", getattr(user, "pk", None))

    # Browser notifications on the devices the student turned on. Same rule
    # as Telegram — a courtesy that can never undo the in-app row — and sent
    # after commit on a background thread, so no request waits on a push
    # service. push_service applies push_enabled and the category switch.
    try:
        from AI.services import push_service

        push_service.queue(
            user, title, body, url=action_url or "/app/notifications/", tag=f"n-{notification.pk}", category=category,
        )
    except Exception:  # pragma: no cover
        logger.exception("Browser notification failed for user=%s", getattr(user, "pk", None))

    return notification


#: What a delivery row calls a mirrored notification, so the console's
#: delivery log can be filtered by it.
_TELEGRAM_KIND = {
    NotificationCategory.PAYMENT: "payment",
    NotificationCategory.ANNOUNCEMENT: "announcement",
}


#: The administrator's switch for each category (Console → Telegram).
_TELEGRAM_SWITCH = {
    NotificationCategory.PAYMENT: "telegram_send_payment",
    NotificationCategory.TOKENS: "telegram_send_tokens",
    NotificationCategory.EXPIRY: "telegram_send_expiry",
    NotificationCategory.SYSTEM: "telegram_send_system",
    NotificationCategory.REFERRAL: "telegram_send_referral",
}


def telegram_category_on(category: str) -> bool:
    """Has the administrator left Telegram on for this kind of alert?

    The student's own switches still apply on top. Sign-in codes are not
    alerts and never pass through here: they go to Telegram whenever the
    student chose Telegram for them.
    """
    if not settings_service.get("telegram_alerts_enabled", True):
        return False
    key = _TELEGRAM_SWITCH.get(category)
    return bool(settings_service.get(key, True)) if key else True


def _absolute(path: str) -> str:
    if path.startswith("http"):
        return path
    return f"{settings.SITE_URL.rstrip('/')}{path}"


def _category_enabled(prefs: NotificationPreference, category: str) -> bool:
    return {
        NotificationCategory.TOKENS: prefs.low_token_alerts,
        NotificationCategory.EXPIRY: prefs.expiry_reminders,
        NotificationCategory.PAYMENT: prefs.payment_alerts,
        NotificationCategory.ANNOUNCEMENT: prefs.announcements,
    }.get(category, True)


def _claim_alert(user, alert_type: str, key: str) -> bool:
    """Reserve an alert slot. Returns False if it was already sent.

    The insert runs inside its own savepoint: hitting the unique constraint is
    the expected "already sent" path and must not poison an outer transaction.
    """
    try:
        with transaction.atomic():
            AlertLog.objects.create(user=user, alert_type=alert_type, key=key)
        return True
    except IntegrityError:
        return False


def reset_low_balance_alerts(user) -> None:
    """Called after a top-up so future low-balance alerts can fire again."""
    AlertLog.objects.filter(user=user, alert_type="low_tokens").delete()


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
def check_low_balance(user, balance: int | None = None) -> Notification | None:
    if user.is_admin_user:
        return None
    balance = user.token_balance if balance is None else balance

    critical = int(settings_service.get("low_token_threshold_critical", 1000))
    warning = int(settings_service.get("low_token_threshold", 5000))

    if balance <= 0:
        tier, level, headline = "empty", NotificationLevel.DANGER, "You are out of tokens"
        body = "Your token balance has reached zero. Choose a plan to continue chatting with the assistant."
    elif balance <= critical:
        tier, level, headline = f"critical:{critical}", NotificationLevel.DANGER, "Critically low tokens"
        body = f"Only {balance:,} tokens left. Top up now to avoid an interruption."
    elif balance <= warning:
        tier, level, headline = f"low:{warning}", NotificationLevel.WARNING, "Running low on tokens"
        body = f"You have {balance:,} tokens remaining. Consider renewing your plan soon."
    else:
        return None

    if not _claim_alert(user, "low_tokens", tier):
        return None

    return notify(
        user,
        headline,
        body,
        level=level,
        category=NotificationCategory.TOKENS,
        action_url="/app/pricing/",
        action_label="View plans",
        telegram=True,
        telegram_text=(
            f"<b>{headline}</b>\n\n{telegram_service.escape_html(body)}"
        ),
        telegram_buttons=[("Renew now", _absolute("/app/pricing/"))],
    )


def send_expiry_reminders() -> dict:
    """Notify students whose subscription expires in a configured window."""
    days_config = settings_service.get("expiry_reminder_days", [7, 3, 1]) or []
    try:
        days_list = sorted({int(d) for d in days_config if int(d) >= 0}, reverse=True)
    except (TypeError, ValueError):
        days_list = [7, 3, 1]

    now = timezone.now()
    sent = 0
    checked = 0

    subscriptions = (
        Subscription.objects.filter(status=Subscription.Status.ACTIVE, ends_at__gt=now)
        .select_related("user", "plan")
    )

    for subscription in subscriptions:
        checked += 1
        remaining_days = (subscription.ends_at - now).days
        for threshold in days_list:
            if remaining_days == threshold:
                key = f"{subscription.pk}:{threshold}"
                if not _claim_alert(subscription.user, "expiry", key):
                    break
                label = "tomorrow" if threshold <= 1 else f"in {threshold} days"
                notify(
                    subscription.user,
                    f"Your {subscription.plan.name} plan expires {label}",
                    f"Renew before {timezone.localtime(subscription.ends_at).strftime('%d %b %Y')} "
                    "to keep uninterrupted access to the assistant.",
                    level=NotificationLevel.WARNING,
                    category=NotificationCategory.EXPIRY,
                    action_url="/app/pricing/",
                    action_label="Renew plan",
                    telegram=True,
                    telegram_text=(
                        f"<b>Plan expiring {label}</b>\n\n"
                        f"Your <b>{telegram_service.escape_html(subscription.plan.name)}</b> plan ends on "
                        f"{timezone.localtime(subscription.ends_at).strftime('%d %b %Y')}."
                    ),
                    telegram_buttons=[("Renew now", _absolute("/app/pricing/"))],
                )
                sent += 1
                break

    return {"checked": checked, "sent": sent}


def expire_subscriptions() -> int:
    """Flip finished subscriptions to expired and tell the student once."""
    now = timezone.now()
    finished = Subscription.objects.filter(status=Subscription.Status.ACTIVE, ends_at__lte=now).select_related(
        "user", "plan"
    )
    count = 0
    for subscription in finished:
        subscription.status = Subscription.Status.EXPIRED
        subscription.save(update_fields=["status", "updated_at"])
        if _claim_alert(subscription.user, "expired", str(subscription.pk)):
            notify(
                subscription.user,
                f"Your {subscription.plan.name} plan has expired",
                "Renew to restore full access to the assistant.",
                level=NotificationLevel.DANGER,
                category=NotificationCategory.EXPIRY,
                action_url="/app/pricing/",
                action_label="Renew plan",
                telegram=True,
            )
        count += 1
    return count


def payment_confirmed(payment) -> Notification:
    return notify(
        payment.user,
        "Payment confirmed",
        f"{payment.plan.name} activated — {payment.tokens_to_grant:,} tokens added to your balance.",
        level=NotificationLevel.SUCCESS,
        category=NotificationCategory.PAYMENT,
        action_url=f"/payments/{payment.reference}/receipt/",
        action_label="View receipt",
        telegram=True,
        telegram_text=(
            f"<b>Payment confirmed</b>\n\n"
            f"Plan: <b>{telegram_service.escape_html(payment.plan.name)}</b>\n"
            f"Amount: {telegram_service.escape_html(payment.pay_display)}\n"
            f"Tokens added: <b>{payment.tokens_to_grant:,}</b>\n"
            f"Reference: <code>{telegram_service.escape_html(payment.reference)}</code>"
        ),
        telegram_buttons=[("View receipt", _absolute(f"/payments/{payment.reference}/receipt/"))],
    )


def referral_rewarded(user, amount: int, *, referred_name: str = "") -> Notification:
    detail = f" for inviting {referred_name}" if referred_name else ""
    return notify(
        user,
        "Referral bonus earned",
        f"{amount:,} tokens have been added to your balance{detail}.",
        level=NotificationLevel.SUCCESS,
        category=NotificationCategory.REFERRAL,
        action_url="/referrals/",
        action_label="View referrals",
        telegram=True,
    )


def audience_students(audience: str = AnnouncementAudience.ALL, *, plan=None, model=None) -> QuerySet:
    """The students a broadcast to ``audience`` would reach, right now.

    "Running low" is the same question the Running-low page and the sidebar
    badge ask — a wallet below its model's warning line, as a share of what
    the student was given — so the three places never disagree about who is
    low. "No tokens" is a student with nothing spendable in any model, which
    includes one who was never given a wallet at all. "On a plan" is a live
    subscription to ``plan``; "with a model" is a wallet for ``model`` that
    holds or has held tokens.

    A student who switched announcements off in Settings is never reached —
    the switch used to be saved and then ignored.
    """
    from AI.models import Subscription
    from AI.services import token_alert_service  # circular at import time only

    students = User.objects.filter(role=Role.STUDENT, is_active=True, status="active").exclude(
        notification_preference__announcements=False
    )
    if audience == AnnouncementAudience.PLAN:
        if plan is None:
            return students.none()
        live = Subscription.objects.filter(
            plan=plan, status=Subscription.Status.ACTIVE, ends_at__gt=timezone.now(),
        ).values("user_id")
        return students.filter(pk__in=live)
    if audience == AnnouncementAudience.MODEL:
        if model is None:
            return students.none()
        holders = UserAIWallet.objects.filter(ai_model=model, is_enabled=True).filter(
            Q(balance__gt=0) | Q(allocated_tokens__gt=0)
        ).values("user_id")
        return students.filter(pk__in=holders)
    if audience == AnnouncementAudience.LOW:
        ceiling = max(token_alert_service.thresholds())
        low_wallets = UserAIWallet.objects.filter(
            allocated_tokens__gt=0,
            balance__lte=F("allocated_tokens") * ceiling / 100.0,
        ).values("user_id")
        return students.filter(pk__in=low_wallets)
    if audience == AnnouncementAudience.EMPTY:
        funded = UserAIWallet.objects.filter(balance__gt=0).values("user_id")
        return students.exclude(pk__in=funded)
    return students


def audience_counts() -> dict[str, int]:
    """How many students each audience reaches — the figures the page shows.

    Plan and model audiences depend on which one is picked, so they are
    counted per choice by ``audience_breakdown`` instead.
    """
    targeted = {AnnouncementAudience.PLAN, AnnouncementAudience.MODEL}
    wanted = [value for value, _label in AnnouncementAudience.choices if value not in targeted]
    # One aggregate over the reachable students, one conditional count per
    # audience, each asking exactly what ``audience_students`` asks — rather
    # than one COUNT query per audience.
    everyone = audience_students(AnnouncementAudience.ALL)
    conditions = {}
    for value in wanted:
        if value == AnnouncementAudience.ALL:
            conditions[value] = Q()
        else:
            conditions[value] = Q(pk__in=audience_students(value).values("pk"))
    figures = everyone.aggregate(
        **{f"n_{value}": Count("pk", filter=condition) for value, condition in conditions.items()}
    )
    return {value: int(figures.get(f"n_{value}") or 0) for value in wanted}


def audience_breakdown() -> dict[str, dict[int, int]]:
    """Reach per plan and per model, for the targeting pickers."""
    from AI.models import AIModel, Plan

    # Two grouped queries, whatever the size of the catalogue: the same
    # conditions ``audience_students`` applies for one plan or one model,
    # counted for all of them at once.
    reachable = audience_students(AnnouncementAudience.ALL).values("pk")
    by_plan = {
        row["plan_id"]: row["n"]
        for row in Subscription.objects.filter(
            status=Subscription.Status.ACTIVE, ends_at__gt=timezone.now(), user_id__in=reachable,
        )
        .values("plan_id")
        .annotate(n=Count("user_id", distinct=True))
    }
    by_model = {
        row["ai_model_id"]: row["n"]
        for row in UserAIWallet.objects.filter(is_enabled=True, user_id__in=reachable)
        .filter(Q(balance__gt=0) | Q(allocated_tokens__gt=0))
        .values("ai_model_id")
        .annotate(n=Count("user_id", distinct=True))
    }
    return {
        "plan": {pk: by_plan.get(pk, 0) for pk in Plan.objects.values_list("pk", flat=True)},
        "model": {
            pk: by_model.get(pk, 0)
            for pk in AIModel.objects.filter(is_active=True).values_list("pk", flat=True)
        },
    }


def inbox(user) -> QuerySet:
    """A student's notifications in reading order.

    A pinned broadcast stays first until it is opened; after that it takes
    its place in time like everything else.
    """
    return (
        Notification.objects.filter(user=user)
        .annotate(
            pin_rank=Case(
                When(is_pinned=True, is_read=False, then=0), default=1, output_field=IntegerField()
            )
        )
        .order_by("pin_rank", "-created_at", "-pk")
    )


def publish_announcement(announcement: Announcement) -> int:
    """Fan an announcement out to its audience."""
    recipients = audience_students(
        announcement.audience, plan=announcement.audience_plan, model=announcement.audience_model,
    )
    created = [
        Notification(
            user=user,
            title=announcement.title[:140],
            body=announcement.body,
            level=announcement.level,
            category=NotificationCategory.ANNOUNCEMENT,
            announcement=announcement,
            is_pinned=announcement.is_pinned,
            action_url=announcement.action_url,
            action_label=announcement.action_label,
        )
        for user in recipients
    ]
    Notification.objects.bulk_create(created, batch_size=500)

    announcement.is_published = True
    announcement.published_at = timezone.now()
    announcement.recipients = len(created)
    announcement.save(update_fields=["is_published", "published_at", "recipients"])

    # Telegram comes after the in-app fan-out and can never undo it: a failed
    # channel or group post is recorded on the announcement, not raised.
    _post_announcement_to_channel(announcement)
    _post_announcement_to_group(announcement)
    announcement.telegram_queued = _queue_private_announcement(announcement)
    # Browser notifications to the same audience, in the background once
    # this commits; students with push switched off are left out.
    try:
        from AI.services import push_service

        announcement.push_queued = push_service.queue_announcement(announcement)
    except Exception:  # pragma: no cover
        logger.exception("Queueing announcement %s for browsers failed", announcement.pk)
    return len(created)


def retract_announcement(announcement: Announcement) -> int:
    """Take a sent announcement back out of every inbox. Returns how many.

    The channel post is deleted too when Telegram allows it (a bot may delete
    its own channel posts for 48 hours); if it cannot be, the reason is kept
    on the announcement so the console can say so.
    """
    removed, _ = Notification.objects.filter(announcement=announcement).delete()
    announcement.retracted_at = timezone.now()
    fields = ["retracted_at"]
    if announcement.channel_message_id and announcement.channel_status == "sent":
        deleted, error = telegram_service.delete_channel_message(announcement.channel_message_id)
        if deleted:
            announcement.channel_status, announcement.channel_error = "deleted", ""
        else:
            announcement.channel_error = f"Could not delete the channel post: {error}"[:255]
        fields += ["channel_status", "channel_error"]
    if announcement.group_message_id and announcement.group_status == "sent":
        deleted, error = telegram_service.delete_group_message(announcement.group_message_id)
        if deleted:
            announcement.group_status, announcement.group_error = "deleted", ""
        else:
            announcement.group_error = f"Could not delete the group post: {error}"[:255]
        fields += ["group_status", "group_error"]
    announcement.save(update_fields=fields)
    return removed


# ---------------------------------------------------------------------------
# Announcements on Telegram
# ---------------------------------------------------------------------------
#: The line under an announcement's title on Telegram. Words, not emoji.
_LEVEL_LINE = {
    NotificationLevel.INFO: "Announcement",
    NotificationLevel.SUCCESS: "Good news",
    NotificationLevel.WARNING: "Important",
    NotificationLevel.DANGER: "Urgent",
}

#: Seconds between private messages in a broadcast — about twenty a second,
#: comfortably under Telegram's ~30/s bot limit so a big audience never trips it.
TELEGRAM_PACE_SECONDS = 0.05


def announcement_telegram_text(announcement: Announcement, *, private: bool = False) -> str:
    esc = telegram_service.escape_html
    line = _LEVEL_LINE.get(announcement.level, "Announcement")
    text = (f"<b>{esc(announcement.title)}</b>\n<i>{line} · {esc(settings.BRAND['full_name'])}</i>"
            f"\n\n{esc(announcement.body.strip())}")
    if private:
        text += "\n\n<i>You get announcements here because they are on in Settings → Telegram.</i>"
    return text


def _announcement_buttons(announcement: Announcement) -> list[tuple[str, str]] | None:
    if announcement.action_label and announcement.action_url:
        return [(announcement.action_label, _absolute(announcement.action_url))]
    return None


def _post_announcement_to_channel(announcement: Announcement) -> None:
    """Post to the public channel when asked to and a channel is configured."""
    if not announcement.telegram_channel:
        return
    if not telegram_service.channel_configured():
        announcement.channel_status, announcement.channel_error = "skipped", "No Telegram channel is configured."
    else:
        delivery = telegram_service.post_to_channel(
            announcement_telegram_text(announcement), buttons=_announcement_buttons(announcement),
        )
        announcement.channel_status = "sent" if delivery.status == "sent" else "failed"
        announcement.channel_message_id = delivery.telegram_message_id
        announcement.channel_error = delivery.error
    announcement.save(update_fields=["channel_status", "channel_message_id", "channel_error"])


def _post_announcement_to_group(announcement: Announcement) -> None:
    """Post to the community group when asked to and a group is configured."""
    if not announcement.telegram_group:
        return
    if not telegram_service.group_configured():
        announcement.group_status, announcement.group_error = "skipped", "No Telegram group is configured."
    else:
        delivery = telegram_service.post_to_group(
            announcement_telegram_text(announcement), buttons=_announcement_buttons(announcement),
        )
        announcement.group_status = "sent" if delivery.status == "sent" else "failed"
        announcement.group_message_id = delivery.telegram_message_id
        announcement.group_error = delivery.error
    announcement.save(update_fields=["group_status", "group_message_id", "group_error"])


def telegram_announcement_recipients(announcement: Announcement) -> list[tuple[User, object]]:
    """Students in the audience who can and want to get this in their bot chat.

    Linked, private, not blocked, Telegram alerts on, announcements on — the
    last is already part of the audience itself.
    """
    from AI.models import TelegramAccount

    audience = audience_students(
        announcement.audience, plan=announcement.audience_plan, model=announcement.audience_model,
    ).exclude(notification_preference__telegram_enabled=False)
    accounts = (
        TelegramAccount.objects.filter(user__in=audience, is_linked=True, blocked_at__isnull=True)
        .exclude(telegram_id__isnull=True).select_related("user")
    )
    return [(account.user, account) for account in accounts if telegram_service.can_receive_private(account)]


def _queue_private_announcement(announcement: Announcement) -> int:
    """Send to students' bot chats in the background. Returns how many are queued.

    A big audience at twenty messages a second is minutes of work, which must
    not hold the administrator's request open. The work starts only once the
    publishing transaction has committed, so the thread sees the announcement.
    """
    if not announcement.telegram_private or not telegram_service.is_configured():
        return 0
    recipients = telegram_announcement_recipients(announcement)
    if not recipients:
        return 0
    user_ids = [user.pk for user, _account in recipients]
    transaction.on_commit(lambda: _spawn(send_private_announcement, announcement.pk, user_ids))
    return len(user_ids)


def _spawn(target, *args) -> None:
    """Run ``target`` on a daemon thread that closes its own DB connections."""
    import threading

    def run():
        from django.db import connections

        try:
            target(*args)
        except Exception:  # pragma: no cover
            logger.exception("Background Telegram job %s failed", getattr(target, "__name__", target))
        finally:
            connections.close_all()

    threading.Thread(target=run, name="telegram-broadcast", daemon=True).start()


def send_private_announcement(announcement_id: int, user_ids: list[int] | None = None) -> dict:
    """Deliver one announcement to each recipient's private bot chat, paced.

    Re-checks every recipient at send time — somebody may have unlinked,
    blocked the bot or switched announcements off since it was queued — and
    stops if the announcement is retracted part-way through.
    """
    from AI.models import TelegramAccount

    counts = {"sent": 0, "failed": 0, "blocked": 0, "skipped": 0}
    announcement = Announcement.objects.filter(pk=announcement_id).first()
    if announcement is None or not telegram_service.is_configured():
        return counts
    if user_ids is None:
        user_ids = [user.pk for user, _account in telegram_announcement_recipients(announcement)]

    text = announcement_telegram_text(announcement, private=True)
    buttons = _announcement_buttons(announcement)
    for index, user_id in enumerate(user_ids):
        if index and index % 50 == 0 and Announcement.objects.filter(
            pk=announcement.pk, retracted_at__isnull=False
        ).exists():
            break
        account = TelegramAccount.objects.filter(user_id=user_id, is_linked=True).select_related("user").first()
        prefs = NotificationPreference.objects.filter(user_id=user_id).first()
        wanted = prefs is None or (prefs.telegram_enabled and prefs.announcements)
        if account is None or not wanted or not telegram_service.can_receive_private(account):
            counts["skipped"] += 1
            continue
        delivery = telegram_service.deliver(
            account.telegram_id, text, user=account.user, account=account, kind="announcement",
            buttons=buttons, max_retry_wait=30,
        )
        counts[delivery.status] = counts.get(delivery.status, 0) + 1
        time.sleep(TELEGRAM_PACE_SECONDS)
    logger.info("Announcement %s sent to Telegram chats: %s", announcement.pk, counts)
    return counts


def publish_due(now=None) -> int:
    """Send every scheduled announcement whose time has come.

    Each is claimed with a conditional update before it is sent, so two
    workers — the hourly command and a page view — can never both send it.
    """
    now = now or timezone.now()
    sent = 0
    due = Announcement.objects.filter(
        is_published=False, retracted_at__isnull=True, scheduled_for__isnull=False, scheduled_for__lte=now,
    )
    for announcement in due:
        claimed = Announcement.objects.filter(pk=announcement.pk, is_published=False).update(is_published=True)
        if not claimed:
            continue
        try:
            publish_announcement(announcement)
            sent += 1
        except Exception:  # pragma: no cover - release the claim so the next run retries
            Announcement.objects.filter(pk=announcement.pk).update(is_published=False)
            logger.exception("Scheduled announcement %s failed to send", announcement.pk)
    return sent


def publish_now(announcement: Announcement) -> int | None:
    """Send a scheduled announcement straight away — at most once.

    The same conditional claim ``publish_due`` makes: the row is marked
    published by an UPDATE that only matches while it is still unsent, so a
    double-click, a second administrator, or the scheduler reaching it in the
    same second cannot send it twice. Returns the number reached, or ``None``
    when somebody else already sent (or retracted) it.
    """
    was_scheduled_for = announcement.scheduled_for
    claimed = Announcement.objects.filter(
        pk=announcement.pk, is_published=False, retracted_at__isnull=True,
    ).update(is_published=True, scheduled_for=None)
    if not claimed:
        return None
    announcement.scheduled_for = None
    try:
        return publish_announcement(announcement)
    except Exception:
        # Release the claim — back in the schedule, unsent — so it can be sent
        # again, and let the caller report it.
        Announcement.objects.filter(pk=announcement.pk).update(
            is_published=False, scheduled_for=was_scheduled_for,
        )
        announcement.scheduled_for = was_scheduled_for
        raise


def publish_due_throttled() -> None:
    """``publish_due`` at most once a minute, for callers on the request path —
    so scheduling works on a machine where nobody set up the hourly command."""
    from django.core.cache import cache

    if cache.add("announcements:due-check", 1, timeout=60):
        try:
            publish_due()
        except Exception:  # pragma: no cover - never break a page over this
            logger.exception("Scheduled announcement check failed")


def unread_count(user) -> int:
    return Notification.objects.filter(user=user, is_read=False).count()
