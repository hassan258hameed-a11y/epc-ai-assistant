"""Operational health of the running installation.

``check_setup`` answers "is this configured?" from a terminal. This module
answers the question an administrator actually has in production — *is the
platform working right now, and if not, what do I do about it* — and returns it
as data the console can render.

Three rules hold everywhere in here:

* **Nothing raises.** A health page that crashes is worse than useless, so
  every probe is wrapped and a failed probe becomes a visible check rather
  than a 500.
* **No secrets.** Checks report whether a credential is present, never its
  value.
* **No outbound calls.** Everything is read from settings and the database, so
  opening the page cannot cost money, hang on a slow provider, or trip a rate
  limit. Reachability is inferred from the request log the app already keeps.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from AI.models import (
    AccountStatus,
    AIUsageLog,
    AuditAction,
    AuditLog,
    Payment,
    PaymentStatus,
    Plan,
    ModelPoolAction,
    ModelPoolTransaction,
    RateLimitEvent,
    Role,
    Subscription,
    SupportTicket,
    User,
    OPEN_STATUSES,
)

logger = logging.getLogger("AI.health")

OK = "ok"
WARN = "warn"
FAIL = "fail"

# Worst-first, so a group's overall state is just `max(..., key=RANK.get)`.
RANK = {OK: 0, WARN: 1, FAIL: 2}

WINDOW_HOURS = 24


@dataclass
class Check:
    """One line on the health page."""

    key: str
    label: str
    status: str
    value: str
    detail: str = ""
    action_url: str = ""
    action_label: str = ""

    @property
    def is_ok(self) -> bool:
        return self.status == OK


@dataclass
class Group:
    key: str
    label: str
    description: str
    checks: list[Check] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.checks:
            return OK
        return max((c.status for c in self.checks), key=lambda s: RANK.get(s, 0))

    @property
    def failing(self) -> int:
        return sum(1 for c in self.checks if c.status != OK)


def _safe(fn, group: Group, label: str) -> None:
    """Run one probe; turn an unexpected failure into a visible check."""
    try:
        fn(group)
    except Exception as exc:  # pragma: no cover — the page must still render
        logger.exception("Health probe failed: %s", label)
        group.checks.append(
            Check(
                key=f"{group.key}_error",
                label=label,
                status=FAIL,
                value="Could not be read",
                detail=f"This check raised {type(exc).__name__}. The server log has the traceback.",
            )
        )


# ---------------------------------------------------------------------------
# Security and deployment posture
# ---------------------------------------------------------------------------
def _security(group: Group) -> None:
    group.checks.append(
        Check(
            key="debug",
            label="Debug mode",
            status=FAIL if settings.DEBUG else OK,
            value="On" if settings.DEBUG else "Off",
            detail=(
                "DEBUG=True serves tracebacks, settings and SQL to anyone who triggers an "
                "error. Set DEBUG=False in .env before this installation is public."
                if settings.DEBUG
                else "Errors are shown to users as a plain page, with detail kept in the log."
            ),
        )
    )

    weak = str(settings.SECRET_KEY or "").startswith("django-insecure")
    group.checks.append(
        Check(
            key="secret_key",
            label="Secret key",
            status=FAIL if weak else OK,
            value="Insecure default" if weak else "Set",
            detail=(
                "Sessions and password-reset links are signed with this key. The generated "
                "default is public knowledge — replace SECRET_KEY in .env and restart."
                if weak
                else "A unique key is configured. Rotating it signs everyone out."
            ),
        )
    )

    hosts = [h for h in settings.ALLOWED_HOSTS if h]
    open_hosts = "*" in hosts
    group.checks.append(
        Check(
            key="allowed_hosts",
            label="Allowed hosts",
            status=WARN if (open_hosts or not hosts) else OK,
            value=", ".join(hosts) if hosts else "Not set",
            detail=(
                "Accepting any Host header lets an attacker poison links in password-reset "
                "e-mails. Name your domains in ALLOWED_HOSTS."
                if (open_hosts or not hosts)
                else "Only these hostnames are served."
            ),
        )
    )

    secure = bool(getattr(settings, "SESSION_COOKIE_SECURE", False))
    group.checks.append(
        Check(
            key="cookies",
            label="Cookies over HTTPS only",
            status=OK if secure else (WARN if settings.DEBUG else FAIL),
            value="Enforced" if secure else "Not enforced",
            detail=(
                "Session cookies are sent over plain HTTP, so anyone on the same network can "
                "copy one and take over the session. Serve the site over TLS and set "
                "SESSION_COOKIE_SECURE=True."
                if not secure
                else "Session and CSRF cookies are only sent over TLS."
            ),
        )
    )

    engine = settings.DATABASES["default"]["ENGINE"].rsplit(".", 1)[-1]
    sqlite = engine == "sqlite3"
    group.checks.append(
        Check(
            key="database",
            label="Database",
            status=WARN if sqlite else OK,
            value=engine,
            detail=(
                "SQLite serialises writes and does not survive a busy multi-worker "
                "deployment. Move to PostgreSQL or MySQL before real load."
                if sqlite
                else "A server database is in use."
            ),
        )
    )

    admins = User.objects.filter(role=Role.ADMIN, status=AccountStatus.ACTIVE).count()
    group.checks.append(
        Check(
            key="admins",
            label="Administrator accounts",
            status=FAIL if admins == 0 else (WARN if admins == 1 else OK),
            value=f"{admins} active",
            detail=(
                "No active administrator can reach this console."
                if admins == 0
                else "Only one administrator exists — losing that password locks everyone out "
                "of the console. Add a second."
                if admins == 1
                else "More than one administrator can sign in."
            ),
            action_url="/admin/users/?role=admin",
            action_label="Review",
        )
    )


# ---------------------------------------------------------------------------
# The AI provider, judged on the traffic it actually served
# ---------------------------------------------------------------------------
def _provider(group: Group) -> None:
    """One credential check and one failure rate per vendor.

    Reported separately because the vendors fail separately: an Anthropic outage
    with OpenAI healthy is a completely different situation from both being
    down, and a combined figure cannot tell them apart.
    """
    from AI.models import AIProvider
    from AI.services import providers as provider_registry
    from AI.services.ai_service import provider_status

    rows = {row.slug: row for row in AIProvider.objects.all()}
    slugs = provider_registry.available_slugs()

    for slug in slugs:
        row = rows.get(slug)
        label = row.name if row else slug
        configured = provider_registry.is_configured(slug)
        enabled = row.is_active if row else True
        env_var = "OPENAI_API_KEY" if slug == "openai" else f"{slug.upper()}_API_KEY"

        if not configured:
            state = FAIL
            detail = (
                f"{label} models cannot answer until {env_var} is set in .env. "
                "Every other provider keeps working."
            )
            value = "No credentials"
        elif not enabled:
            state = WARN
            detail = (
                f"{label} is switched off in the console, so its models are hidden from "
                "students. Other providers are unaffected."
            )
            value = "Switched off"
        else:
            state = OK
            detail = f"{label} has credentials and its models are available."
            value = "Ready"

        group.checks.append(
            Check(
                key=f"provider_key:{slug}",
                label=f"{label} credentials",
                status=state,
                value=value,
                detail=detail,
                action_url="/admin/ai-tokens/",
                action_label="Manage",
            )
        )

    status = provider_status()
    group.checks.append(
        Check(
            key="provider_default",
            label="Default model",
            status=OK if status["configured"] else FAIL,
            value=status.get("model_name") or status["model"],
            detail=(
                "The model a message uses when a student has not picked one. "
                f"Image input is {'on' if status['vision'] else 'off'}."
            ),
        )
    )

    since = timezone.now() - timedelta(hours=WINDOW_HOURS)
    window = AIUsageLog.objects.filter(created_at__gte=since)

    # Per provider, so one vendor's outage is visible instead of being averaged
    # away by another vendor's healthy traffic.
    by_provider = {
        row["provider"]: row
        for row in window.values("provider").annotate(
            calls=Count("id"), failures=Count("id", filter=Q(success=False))
        )
    }
    for slug in slugs:
        row = rows.get(slug)
        label = row.name if row else slug
        stats = by_provider.get(slug, {})
        calls = int(stats.get("calls") or 0)
        failures = int(stats.get("failures") or 0)
        rate = (failures / calls * 100) if calls else 0.0

        if calls == 0:
            state, detail = (
                OK,
                f"No {label} requests in the last {WINDOW_HOURS} hours, so there is nothing to judge.",
            )
        elif rate >= 20:
            state, detail = (
                FAIL,
                f"One {label} request in five is failing. Check that provider's status page "
                "and your quota. Other providers are unaffected.",
            )
        elif rate >= 5:
            state, detail = (
                WARN,
                f"{label} failures are above the level normal retries explain.",
            )
        else:
            state, detail = OK, f"{label} failures are within ordinary network noise."

        group.checks.append(
            Check(
                key=f"provider_errors:{slug}",
                label=f"{label} failure rate ({WINDOW_HOURS}h)",
                status=state,
                value=f"{rate:.1f}%  ({failures} of {calls})",
                detail=detail,
            )
        )

    totals = window.aggregate(latency=Avg("latency_ms", filter=Q(success=True)))
    calls = window.count()

    latency = int(totals["latency"] or 0)
    if not calls:
        state = OK
    elif latency >= 15000:
        state = FAIL
    elif latency >= 8000:
        state = WARN
    else:
        state = OK
    group.checks.append(
        Check(
            key="provider_latency",
            label="Mean reply time",
            status=state,
            value=f"{latency / 1000:.1f}s" if latency else "—",
            detail=(
                "Students see this as the assistant hanging. A slower model, a smaller output "
                "budget, or provider congestion are the usual causes."
                if state != OK
                else "Successful replies are returning promptly."
            ),
        )
    )


# ---------------------------------------------------------------------------
# The token pool, and how long it lasts at the current rate
# ---------------------------------------------------------------------------
_runway_memo = threading.local()


@contextmanager
def runway_memo():
    """Within this block each model's runway is worked out once.

    The health page needs the same runways twice — inside the checks and in
    its forecast table — and each one is several queries per model. The memo
    lives only for the block, so nothing is ever stale across requests.
    """
    _runway_memo.values = {}
    try:
        yield
    finally:
        _runway_memo.values = None


def pool_runway(ai_model, days: int = 30) -> dict:
    memo = getattr(_runway_memo, "values", None)
    if memo is not None:
        key = (ai_model.pk, days)
        if key not in memo:
            memo[key] = _pool_runway(ai_model, days)
        return memo[key]
    return _pool_runway(ai_model, days)


def _pool_runway(ai_model, days: int = 30) -> dict:
    """How long one model's supply lasts if distribution carries on as it has.

    Measured on what was actually handed to students over the window, not on
    what they then spent — a pool empties when it is *given away*.

    Per model, because a combined runway is meaningless: the pool that runs dry
    first is the one that stops a student, and an average hides it.
    """
    from AI.services import model_pool_service, wallet_service

    summary = model_pool_service.summary(ai_model)
    since = timezone.now() - timedelta(days=days)
    handed_out = ModelPoolTransaction.objects.filter(
        ai_model=ai_model, action=ModelPoolAction.DISTRIBUTE, created_at__gte=since
    ).aggregate(amount=Sum("amount"), movements=Count("id"))

    # Distributions are stored negative — out of the pool.
    total_out = abs(handed_out["amount"] or 0)
    per_day = total_out / days if days else 0
    return {
        "model": ai_model,
        "balance": summary["balance"],
        # What students actually hold, read from the wallets. The pool's own
        # counter only knows what left the pool, and balances also arrive from
        # purchases, trials and the pre-multi-model migration.
        "outstanding": wallet_service.held_by_students(ai_model),
        "never_funded": summary["never_funded"],
        "is_empty": summary["is_empty"],
        "window_days": days,
        "distributed": total_out,
        "per_day": int(per_day),
        "runway_days": int(summary["balance"] / per_day) if per_day > 0 else None,
        "movements": handed_out["movements"] or 0,
    }


def pool_runways(days: int = 30) -> list[dict]:
    """A runway for every model in the catalogue, in catalogue order."""
    from AI.models import AIModel

    return [
        pool_runway(model, days)
        for model in AIModel.objects.select_related("provider_ref").order_by("sort_order", "name")
    ]


def _pool(group: Group) -> None:
    """One balance check and one runway check per model.

    Deliberately not summed. "2.9M tokens available" reads as healthy even when
    Sonnet ran out an hour ago and every Sonnet student is blocked.
    """
    from AI.services import model_pool_service, wallet_service

    summaries = model_pool_service.summaries()
    if not summaries:
        group.checks.append(
            Check(
                key="pool_models",
                label="AI models",
                status=FAIL,
                value="None",
                detail="No AI models exist, so no tokens can be distributed at all.",
                action_url="/admin/tokens/?tab=models",
                action_label="Add a model",
            )
        )
        return

    for entry in summaries:
        model = entry["model"]
        slug = model.token_type or model.key

        if entry["never_funded"]:
            state, detail = (
                FAIL,
                f"The {model.name} pool has never been funded, so no student can be given "
                f"{model.name} tokens.",
            )
        elif entry["is_empty"]:
            state, detail = (
                FAIL,
                f"{model.name} allocations are being refused. Top this pool up to resume.",
            )
        else:
            state, detail = OK, f"{model.name} tokens are available to distribute."

        group.checks.append(
            Check(
                key=f"pool_balance:{slug}",
                label=f"{model.name} — available",
                status=state,
                value=f"{entry['balance']:,} tokens",
                detail=detail,
                action_url="/admin/ai-tokens/",
                action_label="Manage",
            )
        )

        runway = pool_runway(model)
        days = runway["runway_days"]
        if days is None:
            state, value, detail = (
                OK,
                "No recent distribution",
                f"No {model.name} tokens were handed out in the last "
                f"{runway['window_days']} days, so there is no rate to project.",
            )
        elif days <= 7:
            state, value, detail = (
                FAIL,
                f"about {days} days",
                f"At {runway['per_day']:,} tokens a day the {model.name} pool runs dry "
                f"within the week.",
            )
        elif days <= 30:
            state, value, detail = (
                WARN,
                f"about {days} days",
                f"At {runway['per_day']:,} tokens a day, plan a {model.name} top-up this month.",
            )
        else:
            state, value, detail = (
                OK,
                f"about {days} days",
                f"Current {model.name} rate is {runway['per_day']:,} tokens a day.",
            )

        group.checks.append(
            Check(
                key=f"pool_runway:{slug}",
                label=f"{model.name} — projected runway",
                status=state,
                value=value,
                detail=detail,
            )
        )

        group.checks.append(
            Check(
                key=f"pool_outstanding:{slug}",
                label=f"{model.name} — held by students",
                status=OK,
                value=f"{wallet_service.held_by_students(model):,} tokens",
                detail="Already allocated and not yet spent. A liability, not free supply.",
            )
        )


# ---------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------
def _integrations(group: Group) -> None:
    smtp = bool(settings.EMAIL_HOST_USER)
    group.checks.append(
        Check(
            key="email",
            label="E-mail delivery",
            status=OK if smtp else WARN,
            value=f"{settings.EMAIL_HOST}:{settings.EMAIL_PORT}" if smtp else "Console only",
            detail=(
                "Sign-in codes and receipts are printed to the server log instead of being "
                "sent. Configure SMTP in .env before students rely on e-mail."
                if not smtp
                else "One-time codes and receipts are sent over SMTP."
            ),
        )
    )

    token = bool(settings.TELEGRAM_BOT_TOKEN)
    if not token:
        state, value, detail = WARN, "Not configured", "E-mail is the only channel for one-time codes."
    elif not settings.TELEGRAM_BOT_USERNAME or not settings.TELEGRAM_WEBHOOK_SECRET:
        state, value, detail = (
            WARN,
            "Partly configured",
            "The bot token is set but TELEGRAM_BOT_USERNAME or TELEGRAM_WEBHOOK_SECRET is "
            "missing, so deep links or callback authentication will not work.",
        )
    else:
        state, value, detail = OK, f"@{settings.TELEGRAM_BOT_USERNAME}", "Codes and alerts can be delivered by bot."
    group.checks.append(Check(key="telegram", label="Telegram bot", status=state, value=value, detail=detail))

    try:
        from AI.services.payments import available_providers

        gateways = available_providers()
        live = [g for g in gateways if g.is_configured]
        group.checks.append(
            Check(
                key="payments",
                label="Payment gateways",
                status=OK if live else WARN,
                value=f"{len(live)} of {len(gateways)} verifying automatically",
                detail=(
                    "Every payment has to be confirmed by hand in the console. That works, but "
                    "students wait for a human."
                    if not live
                    else "Confirmed automatically: " + ", ".join(g.label for g in live) + "."
                ),
                action_url="/admin/payments/?status=pending",
                action_label="Pending",
            )
        )
    except Exception:  # pragma: no cover
        logger.exception("Could not read the payment providers")

    plans = Plan.objects.filter(is_active=True).count()
    group.checks.append(
        Check(
            key="plans",
            label="Purchasable plans",
            status=OK if plans else WARN,
            value=f"{plans} active",
            detail="With no active plan a student has no way to buy tokens." if not plans else "",
            action_url="/admin/plans/",
            action_label="Plans",
        )
    )

    # Search by meaning — counted from the database, never by calling the
    # embeddings endpoint.
    try:
        from AI.services import embedding_service

        info = embedding_service.health()
        if not info["switched_on"]:
            state, value, detail = OK, "Switched off", "Students search by keyword only. Turn it on in Settings."
        elif not info["configured"]:
            state, value, detail = (
                WARN,
                "Not configured",
                "Set EMBEDDING_API_KEY (or OPENAI_API_KEY) in .env to let students find past answers "
                "by meaning. Keyword search works without it.",
            )
        else:
            state = WARN if info["pending"] > 1000 else OK
            value = f"{info['indexed']:,} indexed, {info['pending']:,} pending"
            detail = (
                f"Enabled with {info['model']} at {info['dimensions']} dimensions. New answers are indexed "
                "on search and by the hourly send_reminders run."
                + (" A large backlog is waiting — run `python manage.py index_embeddings`."
                   if state == WARN else "")
            )
        group.checks.append(
            Check(key="semantic_search", label="Search by meaning", status=state, value=value, detail=detail)
        )
    except Exception:  # pragma: no cover - the page must still render
        logger.exception("Could not read the search-by-meaning index")


# ---------------------------------------------------------------------------
# What needs a human right now
# ---------------------------------------------------------------------------
def _queues(group: Group) -> None:
    since = timezone.now() - timedelta(hours=WINDOW_HOURS)

    pending = Payment.objects.filter(status=PaymentStatus.PENDING).count()
    stale = Payment.objects.filter(
        status=PaymentStatus.PENDING, created_at__lt=timezone.now() - timedelta(hours=48)
    ).count()
    group.checks.append(
        Check(
            key="payments_pending",
            label="Payments awaiting confirmation",
            status=FAIL if stale else (WARN if pending else OK),
            value=f"{pending} waiting" + (f", {stale} over 48h" if stale else ""),
            detail=(
                "A student has paid and is still waiting for their tokens."
                if stale
                else "Confirm these to release the tokens." if pending else "Nothing is waiting."
            ),
            action_url="/admin/payments/?status=pending",
            action_label="Open",
        )
    )

    open_tickets = SupportTicket.objects.filter(status__in=OPEN_STATUSES).count()
    group.checks.append(
        Check(
            key="tickets_open",
            label="Support tickets open",
            status=WARN if open_tickets else OK,
            value=f"{open_tickets} open",
            detail="Students are waiting for a reply." if open_tickets else "Nothing outstanding.",
            action_url="/admin/tickets/",
            action_label="Open",
        )
    )

    failed_logins = AuditLog.objects.filter(action=AuditAction.LOGIN_FAILED, created_at__gte=since).count()
    group.checks.append(
        Check(
            key="failed_logins",
            label=f"Failed sign-ins ({WINDOW_HOURS}h)",
            status=WARN if failed_logins >= 25 else OK,
            value=str(failed_logins),
            detail=(
                "An unusual number of failures — worth checking the audit log for one address "
                "trying many accounts."
                if failed_logins >= 25
                else "Nothing out of the ordinary."
            ),
            action_url="/admin/audit-logs/?action=login_failed",
            action_label="Review",
        )
    )

    trips = RateLimitEvent.objects.filter(created_at__gte=since).count()
    group.checks.append(
        Check(
            key="rate_limits",
            label=f"Rate-limit events ({WINDOW_HOURS}h)",
            status=OK,
            value=str(trips),
            detail="Counts every throttled attempt across sign-in, OTP and chat.",
        )
    )

    lapsing = Subscription.objects.filter(
        status=Subscription.Status.ACTIVE, ends_at__lte=timezone.now() + timedelta(days=7), ends_at__gte=timezone.now()
    ).count()
    group.checks.append(
        Check(
            key="subscriptions",
            label="Subscriptions lapsing in 7 days",
            status=WARN if lapsing else OK,
            value=str(lapsing),
            detail="Worth a reminder before access stops." if lapsing else "",
            action_url="/admin/subscriptions/",
            action_label="Open",
        )
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def _error_breakdown(limit: int = 6) -> list[dict]:
    """The distinct provider failures behind the failure rate."""
    since = timezone.now() - timedelta(hours=WINDOW_HOURS)
    rows = (
        AIUsageLog.objects.filter(created_at__gte=since, success=False)
        .values("error_code")
        .annotate(count=Count("id"))
        .order_by("-count")[:limit]
    )
    out = []
    for row in rows:
        sample = (
            AIUsageLog.objects.filter(
                created_at__gte=since, success=False, error_code=row["error_code"]
            )
            .values_list("error_message", flat=True)
            .first()
        )
        out.append(
            {
                "code": row["error_code"] or "unknown",
                "count": row["count"],
                "message": sample or "",
            }
        )
    return out


def report() -> dict:
    """Everything the health page renders, in one call."""
    groups = [
        Group("security", "Security & deployment", "How this installation is exposed to the internet."),
        Group("provider", "AI provider", "Judged on the requests actually served, not on configuration alone."),
        Group("pool", "Token supply", "What is left to give out, and how long it lasts."),
        Group("integrations", "Integrations", "Everything the platform depends on outside itself."),
        Group("queues", "Waiting on a human", "Work that has arrived and not yet been dealt with."),
    ]
    probes = {
        "security": (_security, "Security checks"),
        "provider": (_provider, "Provider checks"),
        "pool": (_pool, "Token pool checks"),
        "integrations": (_integrations, "Integration checks"),
        "queues": (_queues, "Queue checks"),
    }
    for group in groups:
        probe, label = probes[group.key]
        _safe(probe, group, label)

    overall = max((g.status for g in groups), key=lambda s: RANK.get(s, 0)) if groups else OK
    failing = [c for g in groups for c in g.checks if c.status != OK]

    return {
        "groups": groups,
        "overall": overall,
        "failing": failing,
        "fail_count": sum(1 for c in failing if c.status == FAIL),
        "warn_count": sum(1 for c in failing if c.status == WARN),
        "check_count": sum(len(g.checks) for g in groups),
        "errors": _error_breakdown(),
        "window_hours": WINDOW_HOURS,
        "generated_at": timezone.now(),
    }


ALERT_CACHE_KEY = "health:alert_count"
ALERT_CACHE_TTL = 120


def alert_count(force: bool = False) -> int:
    """How many checks are failing, for the navigation badge.

    Every console page renders the badge, so the answer is cached briefly. A
    two-minute-old count is fine for a nudge; the page itself always runs the
    checks fresh.
    """
    if not force:
        cached = cache.get(ALERT_CACHE_KEY)
        if cached is not None:
            return cached
    try:
        count = sum(1 for c in (c for g in report()["groups"] for c in g.checks) if c.status == FAIL)
    except Exception:  # pragma: no cover — a badge must never break a page
        logger.exception("Could not compute the health alert count")
        return 0
    cache.set(ALERT_CACHE_KEY, count, ALERT_CACHE_TTL)
    return count


def summary() -> dict:
    """The compact form, for polling without re-rendering the page."""
    full = report()
    return {
        "overall": full["overall"],
        "fail_count": full["fail_count"],
        "warn_count": full["warn_count"],
        "check_count": full["check_count"],
        "generated_at": full["generated_at"].isoformat(),
        "checks": [
            {"key": c.key, "label": c.label, "status": c.status, "value": c.value}
            for g in full["groups"]
            for c in g.checks
        ],
    }
