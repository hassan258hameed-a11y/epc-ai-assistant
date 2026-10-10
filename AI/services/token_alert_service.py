"""Low-token warnings, per model, by e-mail.

The console already had a low-balance alert: one absolute threshold against one
global balance, delivered in-app and to Telegram. Three things were missing for
a product whose tokens are a per-model currency.

*It could not name the model.* A student with 90,000 GPT-5 Mini tokens and 200
GPT-4o tokens was "fine" on the global figure, right up to the message where
GPT-4o stopped answering. A warning has to be about the currency that is
running out.

*A fixed number is not a warning.* 5,000 tokens left is nearly empty on a
100,000-token package and comfortable on a 10,000-token one. Thresholds here
are percentages of what the student was actually given, so the same rule means
the same thing on every package.

*It never reached the inbox.* The alert lived in the app, which a student who
has stopped visiting will not see.

Delivery reuses the Gmail account the verification codes already go through:
same SMTP settings, same `DEFAULT_FROM_EMAIL`, same
``EmailMultiAlternatives``. There is no second mail system to configure, and
if OTP mail works then this works.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.db.models import F
from django.template.loader import render_to_string

from AI.models import (
    AccountStatus,
    AlertLog,
    EmailLog,
    EmailStatus,
    EmailType,
    NotificationPreference,
    Role,
    UserAIWallet,
)
from AI.services import settings_service

logger = logging.getLogger("AI.alerts")

#: Where the "Renew tokens" button goes. A plain in-site path, so the student's
#: own session decides whose account opens: signed in, they land on pricing;
#: signed out, the login page collects them and `?next=` returns them here.
#: Nothing in this URL identifies or authenticates anyone, which is what stops
#: a forwarded e-mail from opening someone else's account.
PRICING_PATH = "/app/pricing/"

#: Percentages of the student's allocation at which to warn. Configurable, and
#: read every run so a change takes effect without a deploy.
DEFAULT_THRESHOLDS = [50, 25, 10, 5]
THRESHOLD_SETTING = "low_token_percent_thresholds"


def thresholds() -> list[int]:
    """The configured warning points, highest first.

    Descending order matters: a wallet at 4% has crossed every threshold, and
    the message should be about the most urgent one it has reached, not the
    first one in the list.
    """
    raw = settings_service.get(THRESHOLD_SETTING, DEFAULT_THRESHOLDS)
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.replace(";", ",").split(",")]
    values = []
    for item in raw or []:
        try:
            percent = int(str(item).strip().rstrip("%"))
        except (TypeError, ValueError):
            continue
        if 0 < percent <= 100:
            values.append(percent)
    return sorted(set(values or DEFAULT_THRESHOLDS), reverse=True)


def thresholds_for(ai_model) -> list[int]:
    """The warning points that apply to one model.

    A model may override the platform default — an expensive model worth
    warning about earlier than a cheap one. Anything else uses the global
    setting, which is what every model does until told otherwise.
    """
    own = getattr(ai_model, "warning_points", None)
    return own or thresholds()


def tier_for(wallet: UserAIWallet) -> int | None:
    """The lowest configured threshold this wallet has fallen below.

    Returns None when the wallet is comfortable, or when there is nothing to
    take a percentage of — a wallet that was never funded is not "0% remaining",
    it is simply not in use, and warning about it would be noise.
    """
    allocated = wallet.allocated_tokens or 0
    if allocated <= 0:
        return None
    percent = (wallet.balance / allocated) * 100

    # A wallet at 4% has crossed 50, 25, 10 and 5. The message should be about
    # the most urgent of those, so the student is told how bad it is now rather
    # than how bad it was when it first became worth mentioning.
    points = thresholds_for(wallet.ai_model)
    crossed = [threshold for threshold in points if percent <= threshold]
    return min(crossed) if crossed else None


def _claim(user, key: str) -> bool:
    """Reserve one warning. False means it has already been sent.

    The insert is the lock: a unique constraint decides, so two workers running
    the reminder command at once still send exactly one message. It runs in its
    own savepoint because hitting that constraint is the expected path, not an
    error, and must not poison a surrounding transaction.
    """
    try:
        with transaction.atomic():
            AlertLog.objects.create(user=user, alert_type="low_tokens_email", key=key)
        return True
    except IntegrityError:
        return False


def reset_for(user, ai_model=None) -> int:
    """Forget past warnings after a top-up, so the next fall warns again.

    Scoped to one model when given one: topping up GPT-4o should not re-arm the
    Claude warning that is still legitimately overdue.
    """
    logs = AlertLog.objects.filter(user=user, alert_type="low_tokens_email")
    if ai_model is not None:
        logs = logs.filter(key__startswith=f"{ai_model.key}:")
    deleted, _ = logs.delete()
    return deleted


def emails_enabled() -> bool:
    """The administrator's master switch for this channel."""
    return bool(settings_service.get("low_token_email_enabled", True))


def _eligible(user) -> bool:
    """Only a real, active student who has not opted out."""
    if not emails_enabled():
        return False
    if not user.is_active or user.status != AccountStatus.ACTIVE:
        return False
    if user.role != Role.STUDENT or user.is_admin_user:
        return False
    if not (user.email or "").strip():
        return False
    # Read the preference from the database rather than off the user instance.
    # A signal creates this row when the account is created, which populates
    # the reverse one-to-one cache on that instance — so any caller holding a
    # user object from earlier in the request would consult a copy of the
    # preferences as they were then, and mail a student who has since opted
    # out. One indexed lookup, and only for a wallet already low enough to be
    # worth mailing about.
    prefs = (
        NotificationPreference.objects.filter(user=user)
        .values("email_enabled", "low_token_alerts")
        .first()
    )
    if prefs and not (prefs["email_enabled"] and prefs["low_token_alerts"]):
        return False
    return True


def send_warning(wallet: UserAIWallet, tier: int, *, force: bool = False) -> EmailLog | None:
    """Warn one student that one model's tokens are nearly gone.

    Returns the log row, or None when the message was not appropriate to send.
    Every outcome is recorded — sent, failed or skipped — because "no e-mail
    arrived" and "we never tried" are different problems with different fixes.
    """
    user = wallet.user
    ai_model = wallet.ai_model

    if not _eligible(user):
        return None

    key = f"{ai_model.key}:{tier}"
    if not force and not _claim(user, key):
        return None

    allocated = wallet.allocated_tokens or 0
    percent = int(round((wallet.balance / allocated) * 100)) if allocated else 0
    site = settings.SITE_URL.rstrip("/")
    context = {
        "user": user,
        "brand": settings.BRAND,
        "model_name": ai_model.name,
        "remaining": wallet.balance,
        "allocated": allocated,
        "used": wallet.used_tokens,
        "percent": percent,
        "threshold": tier,
        "pricing_url": f"{site}{PRICING_PATH}",
        "site_url": site,
    }
    subject = f"Your {ai_model.name} tokens are running low — renew now"

    log = EmailLog(
        user=user,
        recipient=user.email,
        email_type=EmailType.LOW_TOKENS,
        subject=subject[:190],
        ai_model=ai_model,
        trigger=f"balance <= {tier}% of allocation",
        balance_at_send=wallet.balance,
    )

    try:
        message = EmailMultiAlternatives(
            subject,
            render_to_string("emails/low_tokens.txt", context),
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
        )
        message.attach_alternative(render_to_string("emails/low_tokens.html", context), "text/html")
        message.send(fail_silently=False)
        log.status = EmailStatus.SENT
    except Exception as exc:                      # noqa: BLE001 - recorded, not raised
        # A mail server that is down must not take a page or a cron run with
        # it. The failure is written to the log where an administrator can see
        # it, and the alert claim is released so the next run tries again.
        logger.warning("low-token e-mail to %s failed: %s", user.email, exc)
        log.status = EmailStatus.FAILED
        log.error = str(exc)[:255]
        AlertLog.objects.filter(user=user, alert_type="low_tokens_email", key=key).delete()

    log.save()
    if log.status == EmailStatus.SENT:
        _telegram_copy(wallet, percent, context["pricing_url"])
        _push_copy(wallet, percent)
    return log


def _push_copy(wallet: UserAIWallet, percent: int) -> None:
    """The same warning as a browser notification, on devices that allow it.

    Under the same rule as the Telegram copy: only with a delivered e-mail.
    Off the request thread, and only for a student with a subscribed device.
    """
    try:
        from AI.models import NotificationCategory
        from AI.services import push_service

        push_service.queue(
            wallet.user,
            f"{wallet.ai_model.name} tokens running low",
            f"{wallet.balance:,} left — {percent}% of your allocation. Top up to keep chatting.",
            url=PRICING_PATH, tag=f"low-tokens-{wallet.ai_model.key}", category=NotificationCategory.TOKENS,
        )
    except Exception:  # pragma: no cover — a push must never break the warning
        logger.exception("Low-token push copy failed for user %s", wallet.user_id)


def _telegram_copy(wallet: UserAIWallet, percent: int, pricing_url: str) -> None:
    """The same warning in the student's private Telegram chat, when they allow it.

    Sent only alongside a delivered e-mail, so a mail server that keeps failing
    (and releases the claim for a retry) does not repeat it on every run.
    """
    try:
        from AI.services import notification_service, telegram_service

        prefs = notification_service.preferences(wallet.user)
        if not (prefs.telegram_enabled and prefs.low_token_alerts):
            return
        if not notification_service.telegram_category_on("tokens"):
            return
        name = telegram_service.escape_html(wallet.ai_model.name)
        telegram_service.notify_user(
            wallet.user,
            f"<b>{name} tokens running low</b>\n\n"
            f"{wallet.balance:,} left — {percent}% of your allocation. Top up to keep chatting.",
            buttons=[("Top up", pricing_url)],
            kind="alert",
        )
    except Exception:  # pragma: no cover — a push must never break the warning
        logger.exception("Low-token Telegram copy failed for user %s", wallet.user_id)


def check_wallet(wallet: UserAIWallet) -> EmailLog | None:
    """Warn about this wallet if it has just crossed a threshold."""
    tier = tier_for(wallet)
    if tier is None:
        return None
    return send_warning(wallet, tier)


def check_user(user) -> list[EmailLog]:
    """Every model this student holds, checked independently."""
    wallets = UserAIWallet.objects.filter(user=user).select_related("ai_model", "user")
    results = []
    for wallet in wallets:
        sent = check_wallet(wallet)
        if sent is not None:
            results.append(sent)
    return results


def sweep(*, limit: int | None = None) -> dict:
    """Check every funded student wallet. Safe to run as often as you like.

    Idempotent by construction: a student who has already been warned at a
    given threshold for a given model is claimed in ``AlertLog`` and skipped,
    so running hourly does not mean warning hourly.
    """
    # The highest warning point in use anywhere, not the lowest: a wallet at
    # 24% has crossed the 25% threshold and is owed a message, and a model that
    # overrides the default to warn at 80% has to be looked at that early.
    # Skipping everything above the lowest point meant only wallets below 5%
    # were ever considered, so the 50, 25 and 10 per cent warnings never fired.
    from AI.models import AIModel

    ceiling = max(thresholds())
    for model in AIModel.objects.exclude(warning_thresholds=""):
        points = model.warning_points
        if points:
            ceiling = max(ceiling, max(points))
    wallets = (
        UserAIWallet.objects.filter(
            user__role=Role.STUDENT,
            user__is_active=True,
            user__status=AccountStatus.ACTIVE,
            allocated_tokens__gt=0,
        )
        .select_related("user", "ai_model", "user__notification_preference")
        .order_by("balance")
    )

    checked = sent = failed = 0
    for wallet in wallets:
        allocated = wallet.allocated_tokens or 0
        if allocated <= 0:
            continue
        if (wallet.balance / allocated) * 100 > ceiling:
            continue                                   # comfortable
        checked += 1
        log = check_wallet(wallet)
        if log is None:
            continue
        if log.status == EmailStatus.SENT:
            sent += 1
        elif log.status == EmailStatus.FAILED:
            failed += 1
        if limit and sent >= limit:
            break
    return {"checked": checked, "sent": sent, "failed": failed}


def at_risk_count(threshold: int | None = None) -> int:
    """How many wallets are below the warning line — as one query.

    ``at_risk`` computes each percentage in Python because it also builds rows
    for a page. This is the same question asked of the database, because it is
    answered on every console page to draw a badge, and a page load must not
    walk every wallet in the product to do it.
    """
    ceiling = threshold if threshold is not None else max(thresholds())
    return (
        UserAIWallet.objects.filter(
            user__role=Role.STUDENT,
            user__is_active=True,
            allocated_tokens__gt=0,
            # balance/allocated * 100 <= ceiling, rearranged so the database
            # can answer it without dividing.
            balance__lte=F("allocated_tokens") * ceiling / 100.0,
        ).count()
    )


def at_risk(scope=None, *, threshold: int | None = None):
    """Wallets below the given percentage — the Low-token students page.

    Ordered by how little is left proportionally rather than absolutely, so the
    student closest to being cut off is first regardless of package size.
    """
    ceiling = threshold if threshold is not None else max(thresholds())
    wallets = (
        UserAIWallet.objects.filter(
            user__role=Role.STUDENT, allocated_tokens__gt=0
        )
        .select_related("user", "ai_model")
    )
    if scope is not None and not scope.is_all:
        wallets = wallets.filter(ai_model=scope.model)

    rows = []
    for wallet in wallets:
        percent = (wallet.balance / wallet.allocated_tokens) * 100
        # An explicit filter applies to every model; otherwise each model's own
        # warning points decide whether this wallet is worth listing.
        limit = ceiling if threshold is not None else max(thresholds_for(wallet.ai_model))
        if percent > limit:
            continue
        rows.append({
            "wallet": wallet,
            "user": wallet.user,
            "ai_model": wallet.ai_model,
            "percent": int(round(percent)),
            "remaining": wallet.balance,
            "allocated": wallet.allocated_tokens,
            "used": wallet.used_tokens,
        })
    rows.sort(key=lambda row: (row["percent"], row["remaining"]))
    return rows
