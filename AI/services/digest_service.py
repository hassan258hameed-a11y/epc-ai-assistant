"""The daily administrator digest: the last 24 hours on one screen.

Sent once per calendar day (Asia/Phnom_Penh) to every active administrator —
to their own private Telegram chat when they have linked one, and by e-mail.
``send_if_due`` is called from the hourly ``send_reminders`` command; each
(administrator, day) pair is claimed in ``AlertLog`` before anything is sent,
so running the command twice, or on two machines, never sends it twice.

Nothing in here raises into its caller. A digest is a courtesy: a missing
table or a mail server that is down must not take the reminder run with it.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone

from AI.models import (
    OPEN_STATUSES,
    AccountStatus,
    AIModel,
    AIUsageLog,
    EmailType,
    Payment,
    PaymentStatus,
    Role,
    SupportTicket,
    User,
)
from AI.services import mail_service, model_status_service, settings_service, telegram_service

logger = logging.getLogger("AI.digest")

#: The digest's calendar. A "day" is a day in Phnom Penh whatever the server's
#: own clock says, so the dedupe key never straddles two local mornings.
DIGEST_TZ = ZoneInfo("Asia/Phnom_Penh")
#: Not before 08:00 local: the first hourly run after that sends it, so it
#: lands with the morning coffee rather than just after midnight.
SEND_AFTER_HOUR = 8
ALERT_TYPE = "admin_digest"
WINDOW = timedelta(hours=24)


# ---------------------------------------------------------------------------
# Who gets it
# ---------------------------------------------------------------------------
def recipients():
    """Every active administrator account."""
    return (
        User.objects.filter(is_active=True)
        .filter(Q(role=Role.ADMIN) | Q(is_superuser=True))
        .exclude(status=AccountStatus.DISABLED)
        .order_by("pk")
    )


def is_enabled() -> bool:
    return bool(settings_service.get("admin_digest_enabled", False))


def local_day(now=None):
    return timezone.localtime(now or timezone.now(), DIGEST_TZ).date()


# ---------------------------------------------------------------------------
# What it says
# ---------------------------------------------------------------------------
def _runways(active_ids: set[int]) -> list[dict]:
    from AI.services import health_service

    rows = []
    for runway in health_service.pool_runways():
        model = runway["model"]
        if model.pk not in active_ids:
            continue
        rows.append({
            "name": model.name,
            "balance": int(runway["balance"] or 0),
            "runway_days": runway["runway_days"],
            "per_day": runway["per_day"],
            "is_empty": runway["is_empty"],
            "never_funded": runway["never_funded"],
        })
    return rows


def build(now=None) -> dict:
    """Every figure the digest reports, as data. Read-only."""
    now = now or timezone.now()
    since = now - WINDOW
    models = list(AIModel.objects.order_by("sort_order", "name"))
    names = {model.key: model.name for model in models}
    active = [model for model in models if model.is_active]

    paid = Payment.objects.filter(status=PaymentStatus.PAID, paid_at__gte=since).aggregate(
        count=Count("id"), revenue=Sum("amount"),
    )

    usage = []
    for row in (
        AIUsageLog.objects.filter(created_at__gte=since)
        .values("model_key")
        .annotate(
            requests=Count("id"),
            failures=Count("id", filter=Q(success=False)),
            tokens=Sum("tokens_charged"),
        )
    ):
        requests = row["requests"] or 0
        failures = row["failures"] or 0
        usage.append({
            "key": row["model_key"],
            "name": names.get(row["model_key"], row["model_key"] or "Unknown model"),
            "requests": requests,
            "failures": failures,
            "tokens": int(row["tokens"] or 0),
            "error_rate": round(failures * 100 / requests, 1) if requests else 0.0,
        })
    usage.sort(key=lambda row: (-row["tokens"], -row["requests"], row["name"]))

    chat_paused, chat_note = model_status_service.chat_pause()
    incidents = [
        {
            "name": model.name,
            "status": model.service_status,
            "label": model.get_service_status_display(),
            "note": model.status_note,
        }
        for model in active
        if model.service_status != AIModel.ServiceStatus.OPERATIONAL
    ]

    try:
        runways = _runways({model.pk for model in active})
    except Exception:  # pragma: no cover — a pool problem must not lose the digest
        logger.exception("Digest: could not read the token pools")
        runways = []

    return {
        "generated_at": now,
        "since": since,
        "day": local_day(now),
        "signups": User.objects.filter(role=Role.STUDENT, is_superuser=False, date_joined__gte=since).count(),
        "paid_count": paid["count"] or 0,
        "revenue_usd": paid["revenue"] or Decimal("0"),
        "pending_payments": Payment.objects.filter(status=PaymentStatus.PENDING).count(),
        "open_tickets": SupportTicket.objects.filter(status__in=OPEN_STATUSES).count(),
        "usage": usage,
        "requests": sum(row["requests"] for row in usage),
        "failures": sum(row["failures"] for row in usage),
        "chat_paused": chat_paused,
        "chat_note": chat_note,
        "incidents": incidents,
        "runways": runways,
    }


def _n(value) -> str:
    return f"{int(value or 0):,}"


def _tokens(value) -> str:
    value = int(value or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1_000:.0f}k"
    return f"{value:,}"


def _plural(count: int, one: str, many: str | None = None) -> str:
    return one if count == 1 else (many or one + "s")


def _runway_text(row: dict) -> str:
    if row["never_funded"]:
        return "never funded"
    if row["is_empty"]:
        return "EMPTY"
    left = f"{_tokens(row['balance'])} left"
    if row["runway_days"] is None:
        return f"{left} · nothing handed out lately"
    return f"{left} · about {row['runway_days']} {_plural(row['runway_days'], 'day')} at the current rate"


def lines(digest: dict) -> list[tuple[str, list[str]]]:
    """``[(section, [line, …]), …]`` — the one wording both channels share."""
    overall = (
        round(digest["failures"] * 100 / digest["requests"], 1) if digest["requests"] else 0.0
    )
    summary = [
        f"New students: {_n(digest['signups'])}",
        f"Paid orders: {_n(digest['paid_count'])} · US${digest['revenue_usd']:,.2f}",
        f"Waiting for payment: {_n(digest['pending_payments'])}",
        f"Open support tickets: {_n(digest['open_tickets'])}",
        f"AI requests: {_n(digest['requests'])} · {overall}% failed",
    ]
    sections = [("Last 24 hours", summary)]

    attention = []
    if digest["chat_paused"]:
        attention.append("All chat is PAUSED" + (f" — “{digest['chat_note']}”" if digest["chat_note"] else ""))
    for row in digest["incidents"]:
        attention.append(f"{row['name']}: {row['label']}" + (f" — “{row['note']}”" if row["note"] else ""))
    for row in digest["usage"]:
        if row["requests"] >= model_status_service.MIN_SAMPLE and row["error_rate"] >= 25:
            attention.append(f"{row['name']} failed {row['error_rate']}% of requests")
    for row in digest["runways"]:
        if row["is_empty"] or (row["runway_days"] is not None and row["runway_days"] < 7):
            attention.append(f"{row['name']} pool: {_runway_text(row)}")
    sections.append(("Needs attention", attention or ["Nothing — every model is working normally."]))

    if digest["usage"]:
        sections.append((
            "Tokens used, by model",
            [
                f"{row['name']}: {_tokens(row['tokens'])} tokens · {_n(row['requests'])} "
                f"{_plural(row['requests'], 'request')} · {row['error_rate']}% errors"
                for row in digest["usage"]
            ],
        ))
    else:
        sections.append(("Tokens used, by model", ["No AI requests in the last 24 hours."]))

    if digest["runways"]:
        sections.append(("Token pools", [f"{row['name']}: {_runway_text(row)}" for row in digest["runways"]]))
    return sections


def subject(digest: dict, *, test: bool = False) -> str:
    stamp = digest["day"].strftime("%a %d %b")
    return ("[Test] " if test else "") + f"Daily digest · {stamp}"


def as_text(digest: dict) -> str:
    """Plain text, for the e-mail body."""
    blocks = []
    for title, rows in lines(digest):
        blocks.append(title.upper() + "\n" + "\n".join(f"• {row}" for row in rows))
    return "\n\n".join(blocks)


def as_telegram(digest: dict, *, test: bool = False) -> str:
    """Telegram HTML. Every value is escaped; only the tags here are markup."""
    esc = telegram_service.escape_html
    parts = [f"<b>{esc(subject(digest, test=test))}</b>"]
    for title, rows in lines(digest):
        parts.append(f"<b>{esc(title)}</b>\n" + "\n".join(f"• {esc(row)}" for row in rows))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def deliver(user, digest: dict | None = None, *, test: bool = False) -> dict:
    """Send one digest to one administrator on both channels. Never raises.

    ``{"telegram": bool, "email": "sent"|"failed"|"skipped", "email_error": str}``.
    """
    result = {"telegram": False, "email": "skipped", "email_error": ""}
    try:
        digest = digest or build()
        path = reverse("console:model_status")
        result["telegram"] = telegram_service.notify_user(
            user, as_telegram(digest, test=test),
            buttons=[("Open the console", telegram_service.absolute_url(path))],
            kind="digest",
        )
        log = mail_service.send_notice(
            user,
            email_type=EmailType.OTHER,
            subject=subject(digest, test=test),
            heading="Your daily digest" if not test else "Your daily digest (test)",
            intro="here is the platform over the last 24 hours.",
            body=as_text(digest),
            button_label="Open model status",
            button_path=path,
            reason="because you are an administrator.",
            trigger="admin_digest_test" if test else "admin_digest",
        )
        result["email"] = log.status
        result["email_error"] = log.error
    except Exception as exc:  # noqa: BLE001 — a digest must never break its caller
        logger.exception("Digest delivery failed for user=%s", getattr(user, "pk", None))
        result["email_error"] = str(exc)[:255]
    return result


def send_test(user) -> dict:
    """Send a digest to ``user`` only, now. Not claimed: tomorrow's still goes."""
    return deliver(user, test=True)


def send_if_due(now=None) -> dict:
    """Send today's digest to every administrator who has not had it. Never raises.

    Returns ``{"sent": n, "skipped": reason-or-"", "day": date}``.
    """
    from AI.services.notification_service import _claim_alert

    outcome = {"sent": 0, "skipped": "", "day": None}
    try:
        if not is_enabled():
            outcome["skipped"] = "disabled"
            return outcome
        now = now or timezone.now()
        local = timezone.localtime(now, DIGEST_TZ)
        outcome["day"] = local.date()
        if local.hour < SEND_AFTER_HOUR:
            outcome["skipped"] = "too early"
            return outcome
        key = local.date().isoformat()
        digest = None
        for admin in recipients():
            # Claimed before sending: a crash after the claim loses one digest,
            # which is better than a retry loop that sends five.
            if not _claim_alert(admin, ALERT_TYPE, key):
                continue
            digest = digest or build(now)
            deliver(admin, digest)
            outcome["sent"] += 1
    except Exception:  # noqa: BLE001
        logger.exception("Daily digest run failed")
        outcome["skipped"] = "error"
    return outcome


def last_sent(user=None):
    """When the scheduled digest last went out (to ``user``, or to anyone)."""
    from AI.models import AlertLog

    logs = AlertLog.objects.filter(alert_type=ALERT_TYPE)
    if user is not None:
        logs = logs.filter(user=user)
    row = logs.order_by("-created_at").first()
    return row.created_at if row else None
