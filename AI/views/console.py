"""Admin console.

Every view is wrapped in ``@admin_required`` — students never reach these,
regardless of what the template renders.
"""
from __future__ import annotations

import json
import logging
import math

from django.conf import settings as django_settings
from django.contrib import messages
from django.db.models import Count, F, Prefetch, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from AI.forms import (
    AdminUserForm,
    AnnouncementForm,
    BulkImportForm,
    PlanForm,
    TokenGrantForm,
    UserFilterForm,
)
from AI.models import (
    AccountStatus,
    AITokenTransaction,
    AIUsageLog,
    Announcement,
    AuditAction,
    AuditLog,
    Conversation,
    Notification,
    Payment,
    PaymentStatus,
    Plan,
    Referral,
    Role,
    Subscription,
    TokenTransaction,
    TransactionType,
    User,
    UserAIWallet,
)
from AI.services import (
    admin_tools_service,
    account_service,
    analytics_service,
    audit_service,
    image_service,
    model_scope,
    notification_service,
    payment_service,
    settings_service,
    token_alert_service,
    token_service,
)
from AI.services import model_service
from AI.models import AIModel, NotificationLevel
from AI.models import AccountSource
from AI.services.ai_service import provider_status
from AI.services.payments import PaymentError, available_providers, get_provider
from AI.utils.decorators import admin_required
from AI.utils.pagination import clean_date, page_range, paginate
from AI.utils.redirects import next_url, safe_next
from AI.utils.uploads import InvalidUpload

logger = logging.getLogger("AI.console")

IMPORT_SESSION_KEY = "bulk_import_rows"
#: The accounts a bulk import just created, with their plaintext passwords.
#: Lives only until the result page has been shown once.
IMPORT_RESULT_SESSION_KEY = "bulk_import_result"


def _base_context(active: str, **extra) -> dict:
    context = {
        "nav_active": active,
        "pending_payment_count": Payment.objects.filter(status=PaymentStatus.PENDING).count(),
    }
    context.update(extra)
    return context


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@admin_required
def dashboard(request):
    # Every figure below is denominated in the selected model, so the numbers,
    # the charts and the tables on this page always describe the same thing.
    # Signups are the one exception: an account is not created per model.
    scope = model_scope.resolve(request)
    stats = analytics_service.overview(scope)

    # The window the charts cover. Read from the query string so the choice
    # survives a reload and can be linked to, and clamped to the three the
    # control actually offers — an arbitrary ?days=9000 would otherwise become
    # a very expensive query on somebody else's behalf.
    RANGES = (7, 14, 30)
    try:
        days = int(request.GET.get("range", 14))
    except (TypeError, ValueError):
        days = 14
    if days not in RANGES:
        days = 14

    usage = analytics_service.usage_series(days, scope=scope)
    signups = analytics_service.signup_series(days)
    revenue = analytics_service.revenue_series(days, scope=scope)

    # The live-requests panel draws the same fourteen days the chart below it
    # draws, scaled to its own height in the template. Passing the values
    # rather than re-deriving them is what keeps the two in agreement.
    request_values = list(usage["requests"])
    spark_peak = max(request_values) or 1
    requests_spark = [
        {"value": value, "percent": max(6, round(value / spark_peak * 100))}
        for value in request_values
    ]
    # The dial reads today against the busiest of the last fourteen days, which
    # is the only comparison available without inventing a target nobody set.
    # A full sweep therefore means "the busiest day in a fortnight", not "done".
    requests_dial = round(min(1.0, stats["requests_today"] / spark_peak) * 100)

    def _spark(values):
        """A series as bar heights in percent.

        Empty and all-zero series both collapse to a flat floor rather than
        dividing by zero — a new install has plenty of both, and a card that
        raises is worse than a card that shows a quiet line.
        """
        values = list(values)
        peak = max(values) if values else 0
        if not peak:
            return [{"percent": 6} for _ in values]
        return [{"percent": max(6, round(v / peak * 100))} for v in values]

    # The one share a tile quotes. It is a ratio the console already holds,
    # not a trend against a period there is no data for — which is why it does
    # not read as "up 12% on last month".
    #
    # Tokens remaining deliberately carries no pill: balance over all-time
    # allocation computes to about 1% on a healthy install, which is true and
    # completely misleading, and there is no honest comparison to put there
    # instead.
    active_percent = (
        round(stats["active_users"] / stats["total_users"] * 100)
        if stats["total_users"] else 0
    )

    # ---- new students, and something to compare it against --------------
    # The window against the window before it, which is the only comparison
    # this data supports honestly. Asking for twice the range and splitting it
    # costs one extra query and means the two halves are always the same
    # length and always end where they should.
    signups_window = analytics_service.signup_series(days * 2)["values"]
    signups_now = sum(signups_window[-days:])
    signups_before = sum(signups_window[:-days])
    if signups_before:
        signups_change = round((signups_now - signups_before) / signups_before * 100)
    else:
        # No signups at all last period. A percentage against zero is either
        # infinity or a lie, so the tile says how many arrived instead.
        signups_change = None
    signups_peak = max(signups["values"]) if signups["values"] else 0
    signups_peak_day = (
        signups["labels"][signups["values"].index(signups_peak)]
        if signups_peak else ""
    )

    # ---- platform vitals ------------------------------------------------
    # Three shares on one dial, drawn as concentric arcs. Each is a ratio the
    # console already holds; none is a trend against a period there is no data
    # for. A denominator of zero yields 0%, not a division error and not a
    # flattering 100%.
    def _share(part, whole):
        return round(part / whole * 100) if whole else 0

    _payments_seen = stats["payments_paid"] + stats["payments_pending"]
    VITAL_RADII = (67, 54, 41)
    vitals = []
    for radius, (label, percent, colour) in zip(
        VITAL_RADII,
        (
            ("Students active", _share(stats["active_users"], stats["total_users"]), "var(--chart-1)"),
            ("Token utilisation", _share(stats["tokens_consumed"], stats["tokens_allocated"]), "var(--chart-5)"),
            ("Payments confirmed", _share(stats["payments_paid"], _payments_seen), "var(--chart-2)"),
        ),
    ):
        circumference = 2 * math.pi * radius
        drawn = circumference * percent / 100
        vitals.append(
            {
                "label": label,
                "percent": percent,
                "colour": colour,
                "radius": radius,
                # The arc is one dash the length of the share and one gap the
                # length of the remainder, so the stroke *is* the measurement.
                "dash": f"{drawn:.1f}",
                "gap": f"{circumference - drawn:.1f}",
            }
        )

    sparks = {
        "students": _spark(signups["values"]),
        "tokens": _spark(usage["tokens"]),
        "revenue": _spark(revenue["values"]),
        "requests": _spark(usage["requests"]),
    }

    # ---- the briefing's balance sentence ---------------------------------
    # "Balances look healthy" is only true when nobody is near the bottom, so
    # the sentence is chosen from a count rather than written as a constant.
    # The warning line is the one the Students running low page and the nav
    # badge use, narrowed to the selected model like every figure here.
    ceiling = max(token_alert_service.thresholds())
    running_low = (
        scope.wallets(
            UserAIWallet.objects.filter(
                user__role=Role.STUDENT,
                user__is_active=True,
                allocated_tokens__gt=0,
                balance__gt=0,
                balance__lte=F("allocated_tokens") * ceiling / 100.0,
            )
        )
        .values("user_id")
        .distinct()
        .count()
    )

    provider = provider_status()

    return render(
        request,
        "console/dashboard.html",
        _base_context(
            "dashboard",
            stats=stats,
            requests_spark=requests_spark,
            requests_dial=requests_dial,
            sparks=sparks,
            vitals=vitals,
            signups_now=signups_now,
            signups_before=signups_before,
            signups_change=signups_change,
            signups_peak=signups_peak,
            signups_peak_day=signups_peak_day,
            chart_days=days,
            chart_ranges=RANGES,
            active_percent=active_percent,
            today=timezone.localdate(),
            usage_chart=json.dumps(
                {"labels": usage["labels"], "series": [{"name": "Tokens", "values": usage["tokens"]}]}
            ),
            requests_chart=json.dumps(
                {"labels": usage["labels"], "series": [{"name": "Requests", "values": usage["requests"], "color": "#2563EB"}]}
            ),
            signup_chart=json.dumps(
                {"labels": signups["labels"], "series": [{"name": "New students", "values": signups["values"], "color": "#60A5FA"}]}
            ),
            revenue_chart=json.dumps(
                {"labels": revenue["labels"], "series": [{"name": "Revenue", "values": revenue["values"], "color": "#E11D48"}]}
            ),
            model_rows=analytics_service.model_breakdown() if scope.is_all else None,
            method_split=analytics_service.payment_method_split(scope),
            top_users=analytics_service.top_consumers(scope=scope),
            recent_users=User.objects.filter(role=Role.STUDENT).select_related("token_account")[:6],
            recent_payments=Payment.objects.select_related("user", "plan")[:6],
            recent_activity=AuditLog.objects.select_related("actor")[:8],
            recent_usage=AIUsageLog.objects.select_related("user")[:8],
            running_low=running_low,
            provider=provider,
            unconfigured_providers=_unconfigured_providers(provider),
        ),
    )


def _unconfigured_providers(status: dict) -> list[dict]:
    """Vendors an active model still routes to that have no credential.

    Read from ``provider_status`` so the banner and the status pages agree on
    which vendors are configured. A model with a credential of its own is left
    out — it answers whether or not its vendor's default key is set — and so is
    a vendor no active model uses, because nothing is broken by it.
    """
    missing: dict[str, dict] = {}
    for model in (
        AIModel.objects.filter(is_active=True)
        .select_related("provider_ref")
        .order_by("sort_order", "name")
    ):
        if status["providers"].get(model.provider) or model.has_own_credential:
            continue
        row = missing.setdefault(
            model.provider,
            {
                "name": model.provider_name,
                # The vendor's default credential, named the way every shipped
                # provider reads it: OPENAI_API_KEY, ANTHROPIC_API_KEY.
                "setting": f"{model.provider.upper()}_API_KEY",
                "models": [],
                "has_default": False,
            },
        )
        row["models"].append(model.name)
        row["has_default"] = row["has_default"] or model.is_default
    return list(missing.values())


# ---------------------------------------------------------------------------
# Students
# ---------------------------------------------------------------------------
@admin_required
def users(request):
    scope = model_scope.resolve(request)
    form = UserFilterForm(request.GET or None)
    # The list renders each user's live subscription. Fetched for the whole page
    # in one query rather than one per row — see ``User.active_subscription``,
    # which reads this attribute when a caller has provided it.
    queryset = User.objects.select_related("token_account").prefetch_related(
        Prefetch(
            "subscriptions",
            queryset=Subscription.objects.filter(
                status=Subscription.Status.ACTIVE, ends_at__gt=timezone.now()
            ).select_related("plan").order_by("-ends_at"),
            to_attr="live_subscriptions",
        )
    )

    # Each filter stands on its own. ``is_valid`` checks every field, and the
    # ones that pass land in ``cleaned_data`` even when another one fails — so
    # a stale ``?plan=`` from an old bookmark, or a hand-typed bad value, is
    # ignored by itself instead of silently switching every other filter off.
    form.is_valid()
    data = getattr(form, "cleaned_data", {}) if form.is_bound else {}
    if data.get("q"):
        term = data["q"]
        queryset = queryset.filter(
            Q(username__icontains=term) | Q(full_name__icontains=term)
            | Q(email__icontains=term) | Q(phone__icontains=term)
        )
    if data.get("status"):
        queryset = queryset.filter(status=data["status"])
    if data.get("source"):
        queryset = queryset.filter(source=data["source"])
    if data.get("role"):
        queryset = queryset.filter(role=data["role"])
    else:
        queryset = queryset.filter(role=Role.STUDENT)
    # "Empty", "low" and "healthy" are questions about a balance, so they
    # have to be asked of the balance the page is displaying: the selected
    # model's wallet, or the global account when no model is selected.
    threshold = int(settings_service.get("low_token_threshold", 5000))
    balance_field = "ai_wallets__balance" if not scope.is_all else "token_account__balance"
    if scope.is_all:
        wallet_scope = {}
    else:
        wallet_scope = {"ai_wallets__ai_model": scope.model}
    if data.get("tokens") == "empty":
        queryset = queryset.filter(**wallet_scope, **{f"{balance_field}__lte": 0})
    elif data.get("tokens") == "low":
        queryset = queryset.filter(
            **wallet_scope,
            **{f"{balance_field}__gt": 0, f"{balance_field}__lte": threshold},
        )
    elif data.get("tokens") == "healthy":
        queryset = queryset.filter(**wallet_scope, **{f"{balance_field}__gt": threshold})
    if data.get("plan"):
        queryset = queryset.filter(
            subscriptions__plan_id=data["plan"], subscriptions__status=Subscription.Status.ACTIVE
        ).distinct()

    # Only students who hold this model's currency. A student with no GPT-4o
    # wallet is not a GPT-4o student with zero tokens; they are not on the list.
    if not scope.is_all:
        queryset = queryset.filter(ai_wallets__ai_model=scope.model).distinct()

    page = paginate(request, queryset.order_by("-date_joined", "-id"), per_page=25)
    active_subs = {
        sub.user_id: sub
        for sub in Subscription.objects.filter(
            user__in=[u.pk for u in page.object_list],
            status=Subscription.Status.ACTIVE,
            ends_at__gt=timezone.now(),
        ).select_related("plan")
    }

    # The selected model's wallet for everyone on this page, in one query
    # rather than one per row.
    scoped_wallets = {}
    if not scope.is_all:
        scoped_wallets = {
            wallet.user_id: wallet
            for wallet in UserAIWallet.objects.filter(
                user__in=[u.pk for u in page.object_list], ai_model=scope.model
            )
        }

    return render(
        request,
        "console/users.html",
        _base_context(
            "users",
            form=form,
            page=page,
            page_numbers=page_range(page),
            active_subs=active_subs,
            total_matching=page.paginator.count,
            scoped_wallets=scoped_wallets,
        ),
    )


@admin_required
def user_detail(request, pk: int):
    student = get_object_or_404(User.objects.select_related("token_account"), pk=pk)
    account = token_service.get_account(student)

    transactions = TokenTransaction.objects.filter(user=student).select_related("created_by", "payment")[:25]
    payments = Payment.objects.filter(user=student).select_related("plan")[:10]
    subscriptions = Subscription.objects.filter(user=student).select_related("plan")[:10]
    audit = AuditLog.objects.filter(
        Q(target_type="User", target_id=str(student.pk)) | Q(actor=student)
    ).select_related("actor")[:20]
    usage = analytics_service.student_usage(student, days=14)
    conversations = Conversation.objects.filter(user=student)[:8]

    # The Manage tokens dialog moves one model's balance, so it offers the
    # models a student can be given, each with what this student holds of it.
    held = dict(UserAIWallet.objects.filter(user=student).values_list("ai_model_id", "balance"))
    grant_models = list(AIModel.objects.filter(is_active=True).order_by("sort_order", "name"))
    for model in grant_models:
        model.student_balance = held.get(model.pk, 0)

    subscription = student.active_subscription
    access = model_access(student, subscription=subscription)

    return render(
        request,
        "console/user_detail.html",
        _base_context(
            "users",
            student=student,
            account=account,
            transactions=transactions,
            payments=payments,
            subscriptions=subscriptions,
            audit=audit,
            usage=usage,
            usage_chart=json.dumps(
                {"labels": usage["labels"], "series": [{"name": "Tokens", "values": usage["tokens"]}]}
            ),
            conversations=conversations,
            subscription=subscription,
            access=access,
            is_self=student.pk == request.user.pk,
            grant_form=TokenGrantForm(initial={"user_id": student.pk}),
            grant_models=grant_models,
            notes=student.admin_notes.select_related("author"),
            plans=Plan.objects.filter(is_active=True).select_related("ai_model")
            .prefetch_related("model_grants__ai_model").order_by("price"),
            known_tags=admin_tools_service.all_tags(),
            message_levels=NotificationLevel.choices,
        ),
    )


#: Check states, in the order the page ranks them.
BLOCK, WARN, OK, INFO = "block", "warn", "ok", "info"


def model_access(student, *, subscription=None) -> dict:
    """"Can this student chat?" — answered per model, the way a send is decided.

    The checks run in the order the chat path makes them (``chat_service``
    guards the account, ``model_router`` resolves and authorises the model and
    builds the vendor client, then the budget and usage-window guards run), so
    the first check that fails here is the refusal the student actually sees.
    It writes nothing of its own; only reads.

    Returns ``{"account": [checks], "rows": [...]}``. Each row carries the
    wallet figures, the three limit windows, the checks, and a verdict:
    ``{"state": "ready"|"warn"|"blocked", "label": "Ready" | "Blocked: …"}``.
    A check may carry an ``action``: ``grant`` (the Manage tokens dialog, with
    this model picked), ``toggle`` (flip access), or ``link`` (a console page).
    """
    from AI.services import chat_service, providers, usage_limit_service, wallet_service

    try:  # the declared incident status; built alongside this page
        from AI.services import model_status_service
    except ImportError:  # pragma: no cover
        model_status_service = None

    floor = chat_service.min_request_tokens()
    models = list(AIModel.objects.select_related("provider_ref").order_by("sort_order", "name"))
    overview = wallet_service.overview(student, models)

    # -- account-wide: these stop every model at once -----------------------
    account = []
    if not student.is_active_account:
        account.append({
            "key": "account", "state": BLOCK, "label": "Account",
            "short": "account disabled",
            "detail": "This account is disabled, so it cannot sign in or chat. Enable it above.",
        })
    chat_paused, pause_note = (False, "")
    if model_status_service is not None and hasattr(model_status_service, "chat_pause"):
        try:
            chat_paused, pause_note = model_status_service.chat_pause()
        except Exception:  # pragma: no cover - a status read must not break the page
            chat_paused = False
    if chat_paused:
        account.append({
            "key": "maintenance", "state": BLOCK, "label": "Site-wide chat",
            "short": "chat paused for maintenance",
            "detail": pause_note or "Chat is paused for everybody for maintenance.",
        })

    plan_models = set()
    if subscription is not None:
        plan_models = {row["model"].pk for row in subscription.plan.grant_rows}

    last_failure = {}
    for log in (
        AIUsageLog.objects.filter(user=student, success=False)
        .order_by("-created_at", "-id")[:200]
    ):
        last_failure.setdefault(log.model_key, log)

    rows = []
    for entry in overview:
        model, wallet = entry["model"], entry["wallet"]
        held_anything = bool(wallet and (wallet.balance or wallet.allocated_tokens or wallet.lifetime_allocated))
        if not model.is_active and not held_anything:
            continue      # a switched-off model this student never had is noise here

        checks = []

        def add(key, label, state, detail, short="", action=None):
            checks.append({"key": key, "label": label, "state": state, "detail": detail,
                           "short": short, "action": action})

        # 1. The vendor and the catalogue row (model_router.resolve_model).
        if not model.provider_is_active:
            add("provider", "Provider", BLOCK,
                f"{model.provider_name} is switched off, which stops every {model.provider_name} model.",
                short=f"{model.provider_name} switched off",
                action={"kind": "link", "label": "Provider switches", "url": reverse("console:ai_tokens")})
        else:
            add("provider", "Provider", OK, f"{model.provider_name} is switched on.")

        if not model.is_active:
            add("model", "Model", BLOCK, f"{model.name} is switched off for every student.",
                short="model switched off",
                action={"kind": "link", "label": "AI models", "url": reverse("console:tokens") + "?tab=models"})
        elif model.is_exhausted:
            add("model", "Model", BLOCK,
                f"{model.name} has used its whole platform allocation of {model.token_allocation:,} tokens.",
                short="model allocation spent",
                action={"kind": "link", "label": "Raise the allocation", "url": reverse("console:tokens") + "?tab=models"})
        else:
            add("model", "Model", OK, f"{model.name} is switched on.")

        # 2. The declared incident status (paused stops sending; degraded warns).
        notice = None
        if model_status_service is not None and hasattr(model_status_service, "notice_for"):
            try:
                notice = model_status_service.notice_for(model)
            except Exception:  # pragma: no cover
                notice = None
        if notice and notice.get("level") == "paused":
            add("status", "Service status", BLOCK, notice.get("text") or f"{model.name} is paused.",
                short="model paused")
        elif notice and notice.get("level") == "degraded":
            add("status", "Service status", WARN, notice.get("text") or f"{model.name} is slow right now.",
                short="model is slow")
        else:
            add("status", "Service status", OK, "Working normally.")

        # 3. The vendor client (model_router.route): a key must be configured.
        try:
            configured = providers.for_model(model).is_configured
        except Exception:
            configured = False
        if configured:
            add("credentials", "Server key", OK, "An API key is configured for this model.")
        else:
            add("credentials", "Server key", BLOCK,
                "No API key is configured on the server for this model, so it cannot answer anyone.",
                short="no API key on the server",
                action={"kind": "link", "label": "Settings", "url": reverse("console:settings")})

        # 4. The student's own wallet (wallet_service.authorize, then the budget guard).
        if not entry["is_enabled"]:
            add("access", "Access", BLOCK, f"Access to {model.name} is turned off for this student.",
                short="access turned off",
                action={"kind": "toggle", "label": "Turn access on", "model_pk": model.pk})
        else:
            add("access", "Access", OK, f"Allowed to use {model.name}.",
                action={"kind": "toggle", "label": "Turn access off", "model_pk": model.pk, "quiet": True})

        balance = entry["balance"]
        if balance <= 0:
            add("balance", "Tokens", BLOCK, f"No {model.name} tokens left.", short="out of tokens",
                action={"kind": "grant", "label": "Give tokens", "model_pk": model.pk})
        elif balance < floor:
            add("balance", "Tokens", BLOCK,
                f"{balance:,} left — a reply needs at least {floor:,}.", short="too few tokens for a reply",
                action={"kind": "grant", "label": "Give tokens", "model_pk": model.pk})
        else:
            add("balance", "Tokens", OK, f"{balance:,} tokens available.")

        # 5. Daily, weekly and monthly ceilings (usage_limit_service.check).
        snap = usage_limit_service.snapshot(student, model)
        windows = []
        for name in usage_limit_service.WINDOWS:
            info = snap.windows.get(name, {})
            windows.append({
                "name": name,
                "label": name.capitalize(),
                "enforced": bool(info.get("enforced")),
                "limit": info.get("limit") or 0,
                "used": info.get("used") or 0,
                "percent": info.get("percent") or 0,
                "reset": info.get("reset_display") or "",
                "full": bool(info.get("enforced")) and (info.get("remaining") or 0) <= 0,
            })
        limited = []
        if snap.metered and balance >= floor and snap.spendable < floor:
            limited = snap.blocked or [w["name"] for w in windows if w["full"]]
        if limited:
            first = next(w for w in windows if w["name"] == limited[0])
            add("limits", "Usage limits", BLOCK,
                f"{' and '.join(n.capitalize() for n in limited)} limit reached"
                + (f" — resets {first['reset']}." if first["reset"] else "."),
                short=f"{limited[0]} limit reached")
        elif snap.metered:
            add("limits", "Usage limits", OK, f"{snap.spendable:,} tokens spendable before a limit is hit.")
        else:
            add("limits", "Usage limits", INFO, "No daily, weekly or monthly ceiling applies.")

        # 6. Context — never a reason on its own.
        if subscription is not None:
            covers = model.pk in plan_models
            add("plan", "Plan", INFO,
                f"On {subscription.plan.name} until {timezone.localtime(subscription.ends_at):%d %b %Y}"
                + ("" if covers else f" — that plan does not include {model.name}") + ".")
        else:
            add("plan", "Plan", INFO, "No live plan. Tokens here came from grants, trials or referrals.")

        failure = last_failure.get(model.key)
        if failure is not None:
            add("last_error", "Last error", INFO,
                f"{timezone.localtime(failure.created_at):%d %b %Y, %H:%M} — "
                + (failure.error_message or failure.error_code or "request failed"))

        blocking = [c for c in account + checks if c["state"] == BLOCK]
        warning = [c for c in checks if c["state"] == WARN]
        if blocking:
            verdict = {"state": "blocked", "label": f"Blocked: {blocking[0]['short']}",
                       "detail": blocking[0]["detail"], "action": blocking[0].get("action")}
        elif warning:
            verdict = {"state": "warn", "label": f"Ready — {warning[0]['short']}",
                       "detail": warning[0]["detail"], "action": None}
        else:
            verdict = {"state": "ready", "label": "Ready", "detail": "Can send a message now.", "action": None}

        rows.append({
            "model": model,
            "tone": _tone_for(model),
            "balance": balance,
            "allocated": entry["allocated"],
            "used": entry["used"],
            "percent_used": entry["percent_used"],
            "is_enabled": entry["is_enabled"],
            "windows": windows,
            "metered": snap.metered,
            "checks": checks,
            "verdict": verdict,
            "in_plan": model.pk in plan_models,
        })

    return {
        "account": account,
        "rows": rows,
        "ready": sum(1 for row in rows if row["verdict"]["state"] != "blocked"),
        "floor": floor,
    }


def _tone_for(model) -> dict:
    from AI.views.console_ai import tone_for

    return tone_for(model)


@admin_required
def user_edit(request, pk: int):
    student = get_object_or_404(User, pk=pk)
    # Captured before the form is validated: a ModelForm writes the posted
    # values onto its instance while it validates, so reading them afterwards
    # gives the new values twice and the audit "before" is a lie.
    previous = {"status": student.status, "role": student.role, "email": student.email}
    form = AdminUserForm(request.POST or None, instance=student)

    if (
        request.method == "POST"
        and form.is_valid()
        and not _refuse_self_lockout(request, form, student, previous["role"])
    ):
        before = f"status={previous['status']}, role={previous['role']}, email={previous['email']}"
        status = form.cleaned_data["status"]
        updated = form.save(commit=False)
        password = form.cleaned_data.get("password")
        if password:
            updated.set_password(password)
            updated.must_change_password = False
        # Enabling and disabling are not a field write: disabling signs the
        # student out everywhere, stamps when and why, and is audited as its
        # own act. Everything else on the form is saved first, then the status
        # goes through the same service the Enable / Disable button uses.
        updated.status = previous["status"]
        updated.is_active = previous["status"] == AccountStatus.ACTIVE
        updated.save()

        if status != previous["status"]:
            if status == AccountStatus.ACTIVE:
                account_service.set_enabled(updated, True, actor=request.user, request=request)
            elif status == AccountStatus.DISABLED:
                account_service.set_enabled(
                    updated, False, actor=request.user, request=request,
                    reason=form.cleaned_data.get("disable_reason", ""),
                )
            else:
                # Pending verification is neither: the account simply cannot
                # sign in until it is verified.
                updated.status = status
                updated.is_active = False
                updated.save(update_fields=["status", "is_active"])

        audit_service.record(
            AuditAction.USER_UPDATE, actor=request.user, request=request, target=updated,
            description="Updated account details", old_value=before,
            new_value=f"status={updated.status}, role={updated.role}, email={updated.email}",
        )
        messages.success(request, f"{updated.display_name} has been updated.")
        return redirect("console:user_detail", pk=updated.pk)

    return render(
        request,
        "console/user_form.html",
        _base_context("users", form=form, student=student, is_create=False),
    )


def _refuse_self_lockout(request, form, student, previous_role: str) -> bool:
    """Stop an administrator disabling or demoting the account they are using.

    Either one ends the session they are working in, and on a console with a
    single administrator nobody is left to undo it. Returns True, with the
    reason on the form, when the change is refused.
    """
    if student.pk != request.user.pk:
        return False
    refused = False
    if form.cleaned_data.get("status") != AccountStatus.ACTIVE:
        form.add_error("status", "You cannot disable your own account. Ask another administrator.")
        refused = True
    if previous_role == Role.ADMIN and form.cleaned_data.get("role") != Role.ADMIN:
        form.add_error("role", "You cannot remove your own administrator role. Ask another administrator.")
        refused = True
    if refused:
        messages.error(request, "That change would lock you out of the console, so nothing was saved.")
    return refused


@admin_required
@require_POST
def user_toggle(request, pk: int):
    student = get_object_or_404(User, pk=pk)
    if student.pk == request.user.pk:
        messages.error(request, "You cannot disable your own account.")
        return redirect("console:user_detail", pk=pk)

    enable = student.status != AccountStatus.ACTIVE
    account_service.set_enabled(
        student, enable, actor=request.user,
        reason=(request.POST.get("reason") or "").strip(), request=request,
    )
    messages.success(request, f"{student.display_name} has been {'enabled' if enable else 'disabled'}.")
    return redirect(safe_next(request, "console:user_detail", pk=pk))


@admin_required
@require_POST
def user_reset_password(request, pk: int):
    student = get_object_or_404(User, pk=pk)
    if student.pk == request.user.pk:
        # A reset signs the account out everywhere — including this session,
        # before the page that shows the new password could be drawn. Your own
        # password is changed from your account settings, where you type it.
        messages.error(
            request,
            "You cannot reset your own password here — it would sign you out before showing it. "
            "Change it from your account settings.",
        )
        return redirect("console:user_detail", pk=pk)
    password = account_service.reset_password(student, actor=request.user, request=request)
    messages.success(
        request,
        f"Password reset for {student.display_name}. Temporary password: {password} — share it securely.",
    )
    return redirect("console:user_detail", pk=pk)


# ---------------------------------------------------------------------------
# Account creation & bulk import
# ---------------------------------------------------------------------------
@admin_required
def accounts(request):
    """Create accounts: one at a time with a live handover card, or in bulk."""
    form = AdminUserForm(request.POST or None, is_create=True)
    import_form = BulkImportForm()
    if not form.is_bound:
        # The page's own default; an administrator who lands here is usually
        # typing in somebody who just messaged on Telegram.
        form.fields["source"].initial = AccountSource.TELEGRAM
        landing = model_service.default_model()
        if landing is not None:
            form.fields["initial_model"].initial = landing.pk

    if request.method == "POST" and request.POST.get("action") == "create":
        if form.is_valid():
            data = form.cleaned_data
            landing = data.get("initial_model") or model_service.default_model()
            user, password = account_service.create_account(
                username=data["username"],
                password=data.get("password") or "",
                full_name=data.get("full_name", ""),
                email=data.get("email", ""),
                source=data.get("source"),
                phone=data.get("phone", ""),
                notes=data.get("notes", ""),
                tokens=data.get("initial_tokens") or 0,
                role=data.get("role", Role.STUDENT),
                status=data.get("status", AccountStatus.ACTIVE),
                created_by=request.user,
                request=request,
                ai_model=data.get("initial_model"),
                plan=data.get("plan"),
                force_password_change=bool(data.get("force_password_change")),
                send_credentials=bool(data.get("email_credentials")),
            )
            tokens = int(data.get("initial_tokens") or 0)
            # The page keeps a short memory of what it created this session so
            # the new rows are marked, and shows the password exactly once -
            # a generated one the administrator has never seen would otherwise
            # be lost the moment the page changed.
            created = [user.pk] + [pk for pk in request.session.get(CREATED_SESSION_KEY, []) if pk != user.pk]
            request.session[CREATED_SESSION_KEY] = created[:20]
            request.session[HANDOVER_SESSION_KEY] = {
                "name": user.display_name,
                "username": user.username,
                "password": password,
                "source": user.get_source_display(),
                "starting": f"{tokens:,} \u00b7 {landing.name}" if tokens and landing else "No tokens",
                "emailed": bool(data.get("email_credentials")) and bool(user.email),
                "email": user.email,
            }
            request.session[TOAST_SESSION_KEY] = (
                f"{user.display_name} can sign in now"
                + (f" with {tokens:,} {landing.name} tokens." if tokens and landing else ".")
            )
            return redirect("console:accounts")
        messages.error(request, "Please fix the highlighted fields.")

    now = timezone.now()
    overview = analytics_service.overview()
    created_ids = request.session.get(CREATED_SESSION_KEY, [])
    recent = list(
        User.objects.exclude(created_by=None)
        .select_related("created_by", "token_account")
        .order_by("-date_joined")[:5]
    )
    for student in recent:
        student.is_new_here = student.pk in created_ids

    models = [
        {
            "pk": model.pk,
            "key": model.key,
            "name": model.name,
            "available": int(getattr(getattr(model, "pool", None), "balance", 0) or 0),
        }
        for model in AIModel.objects.filter(is_active=True).select_related("pool").order_by("sort_order", "name")
    ]
    plans = [
        {"pk": plan.pk, "label": _plan_label(plan)}
        for plan in Plan.objects.filter(is_active=True).order_by("price", "name")
    ]

    return render(
        request,
        "console/accounts.html",
        _base_context(
            "accounts",
            form=form,
            import_form=import_form,
            recent=recent,
            sample_csv_url="/admin/accounts/sample.csv",
            preview=request.session.get(IMPORT_SESSION_KEY),
            hero={
                "created_7d": User.objects.filter(
                    created_by__isnull=False, date_joined__gte=now - timezone.timedelta(days=7)
                ).count(),
                "total": overview["total_users"],
                "active": overview["active_users"],
                "no_tokens": overview["no_token_users"],
            },
            models_json=json.dumps(models),
            plans=plans,
            sources=ACCOUNT_SOURCES,
            source_tones=json.dumps({code: tone for code, _label, tone in ACCOUNT_SOURCES}),
            created_here=len(created_ids),
            toast=request.session.pop(TOAST_SESSION_KEY, None),
            handover=request.session.pop(HANDOVER_SESSION_KEY, None),
            check_username_url=reverse("console:accounts_check_username"),
        ),
    )


def _plan_label(plan) -> str:
    """``Starter \u00b7 $3 / mo`` - the plan the way the page names it."""
    price = plan.price or 0
    amount = f"{price:.0f}" if price == int(price) else f"{price:.2f}"
    period = "mo" if (plan.duration_days or 30) == 30 else f"{plan.duration_days}d"
    return f"{plan.name} \u00b7 ${amount} / {period}"


CREATED_SESSION_KEY = "accounts_created_here"
HANDOVER_SESSION_KEY = "accounts_handover"
TOAST_SESSION_KEY = "accounts_toast"

#: The six origins the page offers, in its order, each with the colour its chip
#: and its pill wear. Code -> label -> (ink, fill). Referral is a real
#: AccountSource; "Created by admin" and "Bulk import" are recorded but are
#: not something you choose for somebody, so they are not offered here.
ACCOUNT_SOURCES = [
    (AccountSource.TELEGRAM, "Telegram", ("#1D7FB5", "#229ED9")),
    (AccountSource.MESSENGER, "Messenger", ("#1D4ED8", "#2563EB")),
    (AccountSource.INSTAGRAM, "Instagram", ("#BE185D", "#DB2777")),
    (AccountSource.TIKTOK, "TikTok", ("#0B1222", "#0B1222")),
    (AccountSource.WEB, "Website signup", ("#0369A1", "#0EA5E9")),
    (AccountSource.REFERRAL, "Referral", ("#4338CA", "#4F46E5")),
]


@admin_required
def accounts_check_username(request):
    """Is this username free? Answered while the administrator is still typing.

    The same rules the form enforces, so the page can never say "available"
    about a name the form will then refuse. Codes rather than sentences, so
    the page keeps its own wording.
    """
    username = (request.GET.get("u") or "").strip()
    if not username:
        code = "empty"
    elif len(username) > 150:
        code = "long"
    elif not all(char.isalnum() or char in "._-@+" for char in username):
        code = "chars"
    elif User.objects.filter(username__iexact=username).exists():
        code = "taken"
    else:
        code = "ok"
    return JsonResponse({"code": code, "available": code == "ok"})


@admin_required
def accounts_sample_csv(request):
    response = HttpResponse(account_service.sample_csv(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="account-import-template.csv"'
    return response


@admin_required
@require_POST
def accounts_import_preview(request):
    form = BulkImportForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Choose a CSV or XLSX file to import.")
        return redirect("console:accounts")

    try:
        rows = account_service.parse_import(request.FILES["file"])
    except InvalidUpload as exc:
        messages.error(request, str(exc))
        return redirect("console:accounts")

    payload = [row.as_dict() for row in rows]
    request.session[IMPORT_SESSION_KEY] = payload
    request.session.modified = True

    valid_count = sum(1 for row in payload if row["valid"])
    messages.info(
        request,
        f"Reviewed {len(payload)} row(s): {valid_count} ready to import, {len(payload) - valid_count} need attention.",
    )
    return redirect("console:accounts")


@admin_required
@require_POST
def accounts_import_confirm(request):
    payload = request.session.get(IMPORT_SESSION_KEY)
    if not payload:
        messages.error(request, "Nothing to import — upload a file first.")
        return redirect("console:accounts")

    rows = []
    for item in payload:
        row = account_service.ImportRow(item["index"], item["data"])
        row.errors = item["errors"]
        row.warnings = item["warnings"]
        rows.append(row)

    result = account_service.run_import(rows, actor=request.user, request=request)
    request.session.pop(IMPORT_SESSION_KEY, None)
    request.session[IMPORT_RESULT_SESSION_KEY] = result["created"]

    messages.success(
        request,
        f"Imported {result['created_count']} account(s). {result['failed']} row(s) were skipped.",
    )
    return redirect("console:accounts_import_result")


@never_cache
@admin_required
def accounts_import_result(request):
    """The new accounts and their passwords — shown exactly once.

    The list holds plaintext passwords, so it leaves the session the moment it
    is rendered rather than sitting there for as long as the administrator
    stays signed in. Copy and Download CSV work from the page itself, so they
    keep working for as long as the page stays open; a reload finds nothing.
    """
    if IMPORT_RESULT_SESSION_KEY not in request.session:
        messages.info(
            request,
            "Imported passwords are shown only once and have been cleared. "
            "Reset a password from the student's page if one was missed.",
        )
        return redirect("console:accounts")
    created = request.session.pop(IMPORT_RESULT_SESSION_KEY) or []
    return render(request, "console/import_result.html", _base_context("accounts", created=created))


@admin_required
@require_POST
def accounts_import_clear(request):
    if request.session.pop(IMPORT_SESSION_KEY, None) is not None:
        messages.info(request, "Import discarded. Nothing was created.")
    # Back to the upload panel, which is where a discarded file is replaced.
    return redirect(reverse("console:accounts") + "?mode=bulk")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
@admin_required
def token_history(request):
    scope = model_scope.resolve(request)

    # Two ledgers exist for a reason: the global one predates per-model
    # currencies and its rows carry no model, so it can only answer "all
    # models". The per-model ledger records the same movements against the
    # currency each one actually moved, and is the only one that can answer
    # "GPT-4o". Reading whichever one can answer the question asked is what
    # keeps the page honest — the alternative is showing global rows under a
    # model heading.
    if scope.is_all:
        queryset = TokenTransaction.objects.select_related("user", "created_by", "payment")
        type_choices = TransactionType.choices
    else:
        queryset = AITokenTransaction.objects.select_related(
            "user", "performed_by", "payment", "ai_model"
        ).filter(ai_model=scope.model)
        # The two ledgers name their event types differently, so the filter has
        # to offer the vocabulary of the ledger actually being shown. Offering
        # the other one gives a dropdown where every option returns nothing.
        type_choices = AITokenTransaction._meta.get_field("transaction_type").choices

    kind = request.GET.get("type")
    if kind:
        queryset = queryset.filter(transaction_type=kind)
    search = (request.GET.get("q") or "").strip()
    if search:
        queryset = queryset.filter(
            Q(user__username__icontains=search) | Q(user__full_name__icontains=search)
        )
    page = paginate(request, queryset, per_page=40)
    return render(
        request,
        "console/token_history.html",
        _base_context(
            "tokens",
            page=page,
            page_numbers=page_range(page),
            types=type_choices,
            selected_type=kind,
            search=search,
        ),
    )


# ---------------------------------------------------------------------------
# Usage analytics
# ---------------------------------------------------------------------------
@admin_required
def usage(request):
    scope = model_scope.resolve(request)
    days = 30 if request.GET.get("range") == "30" else 14
    series = analytics_service.usage_series(days, scope=scope)
    flow = analytics_service.token_flow(days, scope=scope)

    logs = scope.usage(AIUsageLog.objects.select_related("user", "conversation"))
    if request.GET.get("status") == "failed":
        logs = logs.filter(success=False)
    elif request.GET.get("status") == "ok":
        logs = logs.filter(success=True)
    search = (request.GET.get("q") or "").strip()
    if search:
        logs = logs.filter(Q(user__username__icontains=search) | Q(user__full_name__icontains=search))

    page = paginate(request, logs.order_by("-created_at", "-id"), per_page=30)
    # Totals for the whole scope, not for the filtered page: the cards answer
    # "how much has this model done", the table answers "which requests".
    totals = scope.usage(AIUsageLog.objects.all()).aggregate(
        tokens=Sum("total_tokens"), requests=Count("id"),
        input_tokens=Sum("input_tokens"), output_tokens=Sum("output_tokens"),
        failures=Count("id", filter=Q(success=False)),
    )

    return render(
        request,
        "console/usage.html",
        _base_context(
            "usage",
            page=page,
            page_numbers=page_range(page),
            totals=totals,
            days=days,
            search=search,
            status=request.GET.get("status", ""),
            usage_chart=json.dumps(
                {
                    "labels": series["labels"],
                    "series": [
                        {"name": "Tokens", "values": series["tokens"]},
                    ],
                }
            ),
            requests_chart=json.dumps(
                {
                    "labels": series["labels"],
                    "series": [{"name": "Requests", "values": series["requests"], "color": "#12B3AA"}],
                }
            ),
            flow=flow,
            model_rows=analytics_service.model_breakdown() if scope.is_all else None,
            # Selecting each model in turn adds up to less than this page shows
            # under "all models". Saying why, here, is the difference between a
            # filter that looks broken and one that is understood.
            unattributed=analytics_service.unattributed_usage() if scope.is_all else None,
            top_users=analytics_service.top_consumers(10, days, scope=scope),
            provider=provider_status(),
        ),
    )


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------
@admin_required
def plans(request):
    scope = model_scope.resolve(request)
    # `grant_rows` walks each plan's extra models, so they are fetched for the
    # whole page at once rather than once per card.
    queryset = scope.plans(
        Plan.objects.select_related("ai_model")
        .prefetch_related("model_grants__ai_model")
        .annotate(
            sold=Count("payments", filter=Q(payments__status=PaymentStatus.PAID)),
            revenue=Sum("payments__amount", filter=Q(payments__status=PaymentStatus.PAID)),
        )
    ).order_by("display_order", "price")
    return render(
        request,
        "console/plans.html",
        _base_context("plans", plans=queryset, scope=scope),
    )


@admin_required
def plan_create(request):
    form = PlanForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        plan = form.save()
        audit_service.record(
            AuditAction.PLAN_CREATE, actor=request.user, request=request, target=plan,
            description=f"Created plan {plan.name}",
            new_value=f"tokens={plan.tokens}, price={plan.price} {plan.currency}, days={plan.duration_days}",
        )
        messages.success(request, f"Plan “{plan.name}” has been created.")
        return redirect("console:plans")
    return render(request, "console/plan_form.html", _base_context("plans", form=form, plan=None))


@admin_required
def plan_edit(request, pk: int):
    plan = get_object_or_404(Plan, pk=pk)
    before = f"tokens={plan.tokens}, price={plan.price}, days={plan.duration_days}, active={plan.is_active}"
    form = PlanForm(request.POST or None, instance=plan)

    if request.method == "POST" and form.is_valid():
        updated = form.save()
        audit_service.record(
            AuditAction.PLAN_UPDATE, actor=request.user, request=request, target=updated,
            description=f"Updated plan {updated.name}", old_value=before,
            new_value=f"tokens={updated.tokens}, price={updated.price}, days={updated.duration_days}, active={updated.is_active}",
        )
        messages.success(request, f"Plan “{updated.name}” has been updated.")
        return redirect("console:plans")

    return render(request, "console/plan_form.html", _base_context("plans", form=form, plan=plan))


@admin_required
@require_POST
def plan_toggle(request, pk: int):
    plan = get_object_or_404(Plan, pk=pk)
    plan.is_active = not plan.is_active
    plan.save(update_fields=["is_active", "updated_at"])
    audit_service.record(
        AuditAction.PLAN_TOGGLE, actor=request.user, request=request, target=plan,
        description=f"{'Activated' if plan.is_active else 'Disabled'} plan {plan.name}",
        new_value="active" if plan.is_active else "inactive",
    )
    messages.success(request, f"Plan “{plan.name}” is now {'active' if plan.is_active else 'inactive'}.")
    return redirect("console:plans")


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------
@admin_required
def payments(request):
    scope = model_scope.resolve(request)
    # A payment reaches a model through the plan that was bought — the only
    # link between money and a model the schema records.
    queryset = scope.payments(
        Payment.objects.select_related("user", "plan", "plan__ai_model", "verified_by")
    )

    # Every status the ledger records, plus "declined" for all four that will
    # never release tokens. An unknown value is ignored rather than filtering
    # the list down to nothing.
    status = (request.GET.get("status") or "").strip()
    if status == "declined":
        queryset = queryset.filter(status__in=DECLINED_STATUSES)
    elif status in PaymentStatus.values:
        queryset = queryset.filter(status=status)
    else:
        status = ""
    method = (request.GET.get("method") or "").strip()
    if method in dict(Payment._meta.get_field("method").choices):
        queryset = queryset.filter(method=method)
    else:
        method = ""
    search = (request.GET.get("q") or "").strip()
    if search:
        queryset = queryset.filter(
            Q(reference__icontains=search) | Q(user__username__icontains=search)
            | Q(user__full_name__icontains=search) | Q(provider_txn_id__icontains=search)
        )
    # The day the order was placed, in the site's own calendar.
    date_from = clean_date(request.GET.get("from"))
    date_to = clean_date(request.GET.get("to"))
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    if date_from:
        queryset = queryset.filter(created_at__date__gte=date_from)
    if date_to:
        queryset = queryset.filter(created_at__date__lte=date_to)

    page = paginate(request, queryset.order_by("-created_at", "-id"), per_page=25)
    everything = scope.payments(Payment.objects.all())
    totals = everything.filter(status=PaymentStatus.PAID).aggregate(revenue=Sum("amount"), count=Count("id"))
    totals["revenue"] = totals["revenue"] or 0
    totals["count"] = totals["count"] or 0
    totals["pending"] = everything.filter(status=PaymentStatus.PENDING).count()
    totals["declined"] = everything.filter(status__in=DECLINED_STATUSES).count()
    totals["all"] = everything.count()

    for index, payment in enumerate(page.object_list):
        payment.tone = METHOD_TONES.get(payment.method, METHOD_TONES["aba_payway"])
        payment.state = payment_state(payment)
        payment.delay = f"{0.3 + index * 0.05:.2f}s"

    split = analytics_service.payment_method_split(scope)
    split_total = sum(row["total"] for row in split) or 1
    for row in split:
        row["tone"] = METHOD_TONES.get(row["key"], METHOD_TONES["aba_payway"])
        row["share"] = round(row["total"] * 100 / split_total, 1)

    providers = []
    for index, provider in enumerate(available_providers()):
        providers.append({
            "key": provider.key,
            "label": provider.label,
            "is_configured": provider.is_configured,
            "is_sandbox": provider.is_sandbox,
            "tone": METHOD_TONES.get(provider.key, METHOD_TONES["aba_payway"]),
            "delay": f"{0.1 + index * 0.06:.2f}s",
        })

    return render(
        request,
        "console/payments.html",
        _base_context(
            "payments",
            page=page,
            page_numbers=page_range(page),
            totals=totals,
            status=status,
            method=method,
            search=search,
            date_from=date_from.isoformat() if date_from else "",
            date_to=date_to.isoformat() if date_to else "",
            dirty=bool(status or method or search or date_from or date_to),
            statuses=PaymentStatus.choices,
            # The status menu beyond the three segment buttons.
            more_statuses=[("declined", "Declined (any)")] + [
                (value, label) for value, label in PaymentStatus.choices
                if value in DECLINED_STATUSES
            ],
            methods=Payment._meta.get_field("method").choices,
            method_split=split,
            providers=providers,
            toast=request.session.pop(PAYMENTS_TOAST_KEY, None),
        ),
    )


#: A payment that will never release tokens, whichever way it got there.
DECLINED_STATUSES = (PaymentStatus.FAILED, PaymentStatus.CANCELLED, PaymentStatus.REFUNDED, PaymentStatus.EXPIRED)

#: One-shot toast for the list page after an inline confirm, decline or refund.
PAYMENTS_TOAST_KEY = "payments_toast"

#: The colour each payment method wears on the page: its mark, its bar, its
#: provider row. Keyed by the model's method codes.
METHOD_TONES = {
    "binance": {"color": "#B07908", "c2": "#FBBF24", "soft": "#FEF3E2", "line": "#F7E0BC", "icon": "i-coin"},
    "aba_payway": {"color": "#1D4ED8", "c2": "#60A5FA", "soft": "#EAF1FE", "line": "#D3E2FD", "icon": "i-card"},
    "acleda_khqr": {"color": "#0A7F5C", "c2": "#34D399", "soft": "#E8F8F1", "line": "#C9EDDE", "icon": "i-qr"},
    "acleda_xpay": {"color": "#0B3C8C", "c2": "#4F7DD8", "soft": "#E8EEFA", "line": "#CFDBF3", "icon": "i-shield"},
}


def payment_state(payment) -> str:
    """pending, paid or declined — the three things the page distinguishes."""
    if payment.status == PaymentStatus.PAID:
        return "paid"
    if payment.status == PaymentStatus.PENDING:
        return "pending"
    return "declined"


@admin_required
def payment_detail(request, reference: str):
    payment = get_object_or_404(
        Payment.objects.select_related("user", "plan", "verified_by"), reference=reference
    )
    events = list(payment.events.all()[:40])
    provider = get_provider(payment.method)
    return render(
        request,
        "console/payment_detail.html",
        _base_context(
            "payments",
            payment=payment,
            events=events,
            subscription=getattr(payment, "subscription", None),
            provider=provider,
            gateway_mode=("sandbox" if provider.is_sandbox else "live") if provider.is_configured else "manual",
            # Gateway confirmations held back because they did not match the order.
            flags=[event for event in events if event.event_type in ("amount_mismatch", "duplicate_transaction")],
        ),
    )


@admin_required
@require_POST
def payment_action(request, reference: str):
    payment = get_object_or_404(Payment.objects.select_related("user", "plan"), reference=reference)
    action = request.POST.get("action")
    note = (request.POST.get("note") or "")[:255]

    # The list page acts on a row in place and wants to come back to itself,
    # with the page's own toast rather than the shell's. Only a local URL is
    # honoured; anything else falls back to the detail page as before.
    back = next_url(request)

    def done(text: str, kind: str, fallback: str) -> None:
        if back:
            request.session[PAYMENTS_TOAST_KEY] = {"text": text, "kind": kind}
        else:
            messages.success(request, fallback)

    def refuse(text: str) -> None:
        """Nothing changed: say so, in the list's toast or the detail page's message."""
        if back:
            request.session[PAYMENTS_TOAST_KEY] = {"text": text, "kind": "warn"}
        else:
            messages.warning(request, text)

    # The page the button was on can be minutes old — another administrator,
    # or the gateway, may have settled or closed the payment since. What
    # happens, and what the message says, is decided from the row as it is now.
    current = Payment.objects.filter(pk=payment.pk).values_list("status", flat=True).first()

    try:
        if action == "confirm":
            if current == PaymentStatus.PAID:
                refuse(f"{payment.reference} was already confirmed — nothing more was credited.")
            elif current == PaymentStatus.REFUNDED:
                refuse(f"{payment.reference} has been refunded, so it cannot be confirmed again.")
            else:
                settled = payment_service.settle(
                    payment, source="admin", actor=request.user, note=note or "Confirmed by administrator"
                )
                credited = sum(amount for _model, amount in payment_service.credited_by_payment(settled))
                audit_service.record(
                    AuditAction.PAYMENT_CONFIRM, actor=request.user, request=request, target=payment,
                    description=f"Confirmed {payment.reference} and credited {credited:,} tokens",
                    old_value=current or "", new_value="paid",
                )
                done(
                    f"${payment.amount:,.2f} confirmed — {credited:,} tokens released to "
                    f"{payment.user.display_name}.",
                    "ok",
                    f"Payment {payment.reference} confirmed — {credited:,} tokens credited.",
                )

        elif action == "reject":
            if current == PaymentStatus.PAID:
                refuse(
                    f"{payment.reference} is already paid and its tokens were released, so it was not "
                    "declined. Refund it instead if the money has to go back."
                )
            elif current != PaymentStatus.PENDING:
                label = dict(PaymentStatus.choices).get(current, current or "closed").lower()
                refuse(f"{payment.reference} is already {label} — nothing changed.")
            else:
                result = payment_service.mark_failed(
                    payment, note or "Rejected by administrator", actor=request.user
                )
                if result.status == PaymentStatus.FAILED:
                    audit_service.record(
                        AuditAction.PAYMENT_REJECT, actor=request.user, request=request, target=payment,
                        description=note or "Rejected payment", old_value=current or "", new_value="failed",
                    )
                    done(
                        f"{payment.reference} declined — nothing was released.", "bad",
                        f"Payment {payment.reference} was marked as failed.",
                    )
                else:
                    refuse(
                        f"{payment.reference} was settled while you were deciding, so it was not declined."
                    )

        elif action == "refund":
            refunded = payment_service.refund(payment, actor=request.user, note=note)
            removed = int(getattr(refunded, "tokens_removed", 0) or 0)
            audit_service.record(
                AuditAction.PAYMENT_REFUND, actor=request.user, request=request, target=payment,
                description=(note or "Refunded payment") + f" — {removed:,} tokens taken back",
                old_value="paid", new_value="refunded",
            )
            taken = (
                f"{removed:,} tokens taken back" if removed
                else "no tokens were left to take back"
            )
            done(
                f"{payment.reference} refunded — {taken}.", "warn",
                f"Payment {payment.reference} has been refunded — {taken}.",
            )

        elif action == "verify":
            checked = payment_service.verify_payment(payment)
            if checked.status == PaymentStatus.PAID:
                messages.success(request, f"The gateway confirmed {payment.reference} — tokens credited.")
            else:
                last = checked.events.filter(event_type="verified").first()
                messages.info(request, f"Gateway says: {last.message if last else checked.get_status_display()}")

        else:
            messages.error(request, "Unknown action.")

    except PaymentError as exc:
        if back:
            request.session[PAYMENTS_TOAST_KEY] = {"text": exc.friendly_message, "kind": "bad"}
        else:
            messages.error(request, exc.friendly_message)

    if back:
        return redirect(back)
    return redirect("console:payment_detail", reference=reference)


# ---------------------------------------------------------------------------
# Referrals
# ---------------------------------------------------------------------------
@admin_required
def referrals(request):
    queryset = Referral.objects.select_related("referrer", "referred_user").order_by("-created_at", "-id")
    status = request.GET.get("status", "")
    if status:
        queryset = queryset.filter(status=status)
    page = paginate(request, queryset, per_page=30)

    leaders = (
        User.objects.filter(referrals_made__isnull=False)
        .annotate(
            invited=Count("referrals_made"),
            rewarded=Count("referrals_made", filter=Q(referrals_made__status=Referral.Status.REWARDED)),
            earned=Sum("referrals_made__referrer_bonus"),
        )
        .order_by("-invited")[:8]
    )

    return render(
        request,
        "console/referrals.html",
        _base_context(
            "referrals",
            page=page,
            page_numbers=page_range(page),
            leaders=leaders,
            status=status,
            statuses=Referral.Status.choices,
            totals={
                "total": Referral.objects.count(),
                "rewarded": Referral.objects.filter(status=Referral.Status.REWARDED).count(),
                "tokens": Referral.objects.aggregate(total=Sum("referrer_bonus"))["total"] or 0,
            },
            bonus_referrer=int(settings_service.get("referral_bonus_referrer", 0)),
            bonus_referred=int(settings_service.get("referral_bonus_referred", 0)),
        ),
    )


# ---------------------------------------------------------------------------
# Announcements
# ---------------------------------------------------------------------------
#: Announcements published from this browser session, newest first. Only the
#: "this session" figure and the toast depend on it; the list is persistent.
ANNOUNCE_SESSION_KEY = "announcements_published_here"
ANNOUNCE_TOAST_KEY = "announcements_toast"

#: The four levels as the page draws them. The model calls the last one
#: "Alert"; on this page it is "Critical", which is what it means to a student.
ANNOUNCEMENT_LEVELS = [
    {"value": "info", "label": "Info", "color": "#2563EB", "ink": "#1D4ED8", "c2": "#60A5FA",
     "soft": "#EAF1FE", "line": "#D3E2FD", "glyph": "M8 7.2v4.2M8 4.9v.2"},
    {"value": "success", "label": "Success", "color": "#059669", "ink": "#0A7F5C", "c2": "#34D399",
     "soft": "#E8F8F1", "line": "#C9EDDE", "glyph": "M3.6 8.4 6.6 11.4l5.8-6.8"},
    {"value": "warning", "label": "Warning", "color": "#B07908", "ink": "#7A5A08", "c2": "#FBBF24",
     "soft": "#FEF3E2", "line": "#F7E0BC", "glyph": "M8 2.6 14 13H2zM8 6.4v3M8 10.8v.4"},
    {"value": "danger", "label": "Critical", "color": "#E11D48", "ink": "#9F1239", "c2": "#FB7185",
     "soft": "rgba(225,29,72,.1)", "line": "rgba(225,29,72,.26)",
     "glyph": "M8 4v4.6M8 11.2v.4M8 2.2a5.8 5.8 0 1 0 0 11.6 5.8 5.8 0 0 0 0-11.6"},
]
ANNOUNCEMENT_LEVEL_BY_VALUE = {level["value"]: level for level in ANNOUNCEMENT_LEVELS}

#: What each audience means, in one line, for the option's second row.
ANNOUNCEMENT_AUDIENCE_NOTES = {
    "all": "Everyone who can sign in today",
    "low": "Below the warning line in any model",
    "empty": "Cannot chat until you allocate",
    "plan": "A live subscription to the plan you pick",
    "model": "Holds tokens for the model you pick",
}


@admin_required
def notifications(request):
    """Write a broadcast, choose who gets it, watch it land.

    The form is the model's own. The page around it is live: the preview and
    the call to action follow the fields as they are typed, the audience
    counts are real, and a published row shows how many of its recipients
    have opened it so far.
    """
    from datetime import timedelta

    from AI.models import NotificationCategory

    form = AnnouncementForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        announcement = form.save(commit=False)
        announcement.created_by = request.user
        announcement.save()

        if announcement.scheduled_for:
            # Sent by the scheduler (the hourly command, or the once-a-minute
            # check on page views) when its time comes.
            audit_service.record(
                AuditAction.ANNOUNCEMENT, actor=request.user, request=request, target=announcement,
                description=f"Scheduled announcement for {timezone.localtime(announcement.scheduled_for):%d %b %Y %H:%M}",
            )
            request.session[ANNOUNCE_TOAST_KEY] = (
                f"Scheduled for {timezone.localtime(announcement.scheduled_for):%a %d %b, %H:%M}."
            )
            return redirect("console:notifications")

        count = notification_service.publish_announcement(announcement)
        audit_service.record(
            AuditAction.ANNOUNCEMENT, actor=request.user, request=request, target=announcement,
            description=f"Published announcement to {count} student(s)",
        )
        here = [announcement.pk] + [
            pk for pk in request.session.get(ANNOUNCE_SESSION_KEY, []) if pk != announcement.pk
        ]
        request.session[ANNOUNCE_SESSION_KEY] = here[:20]
        request.session[ANNOUNCE_TOAST_KEY] = (
            f"Published to {count} student{'' if count == 1 else 's'} — "
            f"{announcement.get_audience_display().lower()}."
            + _telegram_toast(announcement)
        )
        return redirect("console:notifications")

    # "On a plan" and "with a model" are counted per choice: the row shows the
    # reach of whichever plan or model is picked below it, updated as it changes.
    counts = notification_service.audience_counts()
    breakdown = notification_service.audience_breakdown()
    plans = list(Plan.objects.filter(is_active=True).order_by("price"))
    targeted_models = list(AIModel.objects.filter(is_active=True).order_by("sort_order", "name"))
    for plan in plans:
        plan.reach = breakdown["plan"].get(plan.pk, 0)
    for model in targeted_models:
        model.reach = breakdown["model"].get(model.pk, 0)
    counts["plan"] = plans[0].reach if plans else 0
    counts["model"] = targeted_models[0].reach if targeted_models else 0
    audiences = [
        {"value": value, "label": label, "sub": ANNOUNCEMENT_AUDIENCE_NOTES[value], "count": counts.get(value, 0)}
        for value, label in form.fields["audience"].choices
        if value in ANNOUNCEMENT_AUDIENCE_NOTES
    ]
    scheduled = list(
        Announcement.objects.filter(is_published=False, retracted_at__isnull=True, scheduled_for__isnull=False)
        .select_related("audience_plan", "audience_model").order_by("scheduled_for")
    )

    here = request.session.get(ANNOUNCE_SESSION_KEY, [])
    published = list(
        Announcement.objects.filter(is_published=True)
        .select_related("created_by")
        .annotate(opened=Count("deliveries", filter=Q(deliveries__is_read=True)))
        .order_by("-published_at", "-pk")[:20]
    )
    for index, row in enumerate(published):
        row.tone = ANNOUNCEMENT_LEVEL_BY_VALUE.get(row.level, ANNOUNCEMENT_LEVELS[0])
        row.delay = f"{index * 0.08:.2f}s"
        row.read_pct = round(row.opened * 100 / row.recipients) if row.recipients else 0
        row.is_new_here = row.pk in here

    week_ago = timezone.now() - timedelta(days=7)
    alerts = Notification.objects.exclude(category=NotificationCategory.ANNOUNCEMENT)
    feed = list(alerts.select_related("user")[:12])
    for index, note in enumerate(feed):
        note.tone = ANNOUNCEMENT_LEVEL_BY_VALUE.get(note.level, ANNOUNCEMENT_LEVELS[0])
        note.delay = f"{0.3 + index * 0.05:.2f}s"

    # Every level the form allows is one the page can draw; a new choice on
    # the model has to be given colours here before it can be offered.
    allowed = {value for value, _label in form.fields["level"].choices}
    levels = [level for level in ANNOUNCEMENT_LEVELS if level["value"] in allowed]

    from AI.services import telegram_service

    return render(
        request,
        "console/notifications.html",
        _base_context(
            "notifications",
            form=form,
            # The two Telegram switches: offered only when there is somewhere
            # to send, with the reason shown when there is not.
            telegram_ready=telegram_service.is_configured(),
            channel_ready=telegram_service.channel_configured(),
            group_ready=telegram_service.group_configured(),
            levels=levels,
            audiences=audiences,
            hero={
                "published_here": len(here),
                "reachable": counts["all"],
                "alerts_7d": alerts.filter(created_at__gte=week_ago).count(),
            },
            published=published,
            feed=feed,
            plans=plans,
            targeted_models=targeted_models,
            scheduled=scheduled,
            toast=request.session.pop(ANNOUNCE_TOAST_KEY, None),
        ),
    )


def _telegram_toast(announcement) -> str:
    """What happened on Telegram, appended to the publish toast."""
    parts = []
    if announcement.channel_status == "sent":
        parts.append("Posted to the Telegram channel.")
    elif announcement.channel_status == "failed":
        parts.append(f"Telegram channel post failed: {announcement.channel_error or 'unknown error'}.")
    if announcement.group_status == "sent":
        parts.append("Posted to the Telegram group.")
    elif announcement.group_status == "failed":
        parts.append(f"Telegram group post failed: {announcement.group_error or 'unknown error'}.")
    queued = getattr(announcement, "telegram_queued", 0)
    if queued:
        parts.append(f"Sending to {queued} Telegram chat{'' if queued == 1 else 's'}.")
    return (" " + " ".join(parts)) if parts else ""


@admin_required
@require_POST
def announcement_action(request, pk: int):
    """Retract a sent announcement, or send or cancel a scheduled one."""
    announcement = get_object_or_404(Announcement, pk=pk)
    action = request.POST.get("action")
    if action == "retract" and announcement.is_published and not announcement.retracted_at:
        removed = notification_service.retract_announcement(announcement)
        message = f"Retracted — removed from {removed} inbox{'es' if removed != 1 else ''}."
        if announcement.channel_status == "deleted":
            message += " The Telegram channel post was deleted."
        elif announcement.channel_status == "sent":
            message += " The Telegram channel post could not be deleted — remove it in Telegram."
        if announcement.group_status == "deleted":
            message += " The Telegram group post was deleted."
        elif announcement.group_status == "sent":
            message += " The Telegram group post could not be deleted — remove it in Telegram."
    elif action == "send_now" and not announcement.is_published and not announcement.retracted_at:
        # Claimed atomically, as the scheduler claims it: a double-click, a
        # second administrator or the minute-check reaching it at the same time
        # can never send it twice.
        count = notification_service.publish_now(announcement)
        if count is None:
            request.session[ANNOUNCE_TOAST_KEY] = "That announcement has already been sent."
            return redirect("console:notifications")
        message = f"Published to {count} student{'' if count == 1 else 's'}." + _telegram_toast(announcement)
    elif action == "cancel" and not announcement.is_published:
        announcement.delete()
        message = "Scheduled announcement cancelled."
    else:
        messages.error(request, "That announcement can no longer be changed that way.")
        return redirect("console:notifications")
    audit_service.record(
        AuditAction.ANNOUNCEMENT, actor=request.user, request=request, target_label=announcement.title[:140],
        description=f"{action.replace('_', ' ').capitalize()}: “{announcement.title}”",
    )
    request.session[ANNOUNCE_TOAST_KEY] = message
    return redirect("console:notifications")


# ---------------------------------------------------------------------------
# Audit logs
# ---------------------------------------------------------------------------
@admin_required
def audit_logs(request):
    queryset = AuditLog.objects.select_related("actor")

    action = request.GET.get("action", "")
    if action:
        queryset = queryset.filter(action=action)
    search = (request.GET.get("q") or "").strip()
    if search:
        queryset = queryset.filter(
            Q(target_label__icontains=search) | Q(description__icontains=search)
            | Q(actor__username__icontains=search) | Q(ip_address__icontains=search)
        )
    # Dates that do not parse are ignored rather than passed to the database,
    # where "abc" in a date lookup is a server error.
    date_from = clean_date(request.GET.get("from"))
    if date_from:
        queryset = queryset.filter(created_at__date__gte=date_from)
    date_to = clean_date(request.GET.get("to"))
    if date_to:
        queryset = queryset.filter(created_at__date__lte=date_to)

    page = paginate(request, queryset.order_by("-created_at", "-id"), per_page=40)

    return render(
        request,
        "console/audit_logs.html",
        _base_context(
            "audit",
            page=page,
            page_numbers=page_range(page),
            actions=AuditAction.choices,
            action=action,
            search=search,
            date_from=date_from.isoformat() if date_from else "",
            date_to=date_to.isoformat() if date_to else "",
        ),
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@admin_required
def console_settings(request):
    if request.method == "POST":
        changes = []
        for key, spec in settings_service.DEFAULTS.items():
            value_type = spec[1]
            if value_type == "bool":
                new_value = f"setting_{key}" in request.POST
            elif value_type == "secret":
                # A blank box means "leave it as it is", not "erase it" — the
                # field is never populated on render, so an empty POST is what
                # you get from simply not touching it. Clearing one is an
                # explicit act, via its own checkbox.
                raw = (request.POST.get(f"setting_{key}") or "").strip()
                if request.POST.get(f"clear_{key}"):
                    raw = ""
                elif not raw:
                    continue
                if settings_service.get(key) != raw:
                    settings_service.set_value(key, raw, actor=request.user)
                    # The value itself never reaches the audit trail.
                    changes.append(f"{key}: updated" if raw else f"{key}: cleared")
                continue
            else:
                raw = request.POST.get(f"setting_{key}")
                if raw is None:
                    continue
                if value_type == "int":
                    try:
                        new_value = int(str(raw).strip() or 0)
                    except ValueError:
                        messages.error(request, f"“{spec[3]}” must be a number.")
                        continue
                elif value_type == "json":
                    try:
                        new_value = [int(part.strip()) for part in str(raw).split(",") if part.strip()]
                    except ValueError:
                        messages.error(request, f"“{spec[3]}” must be a comma-separated list of numbers.")
                        continue
                else:
                    new_value = str(raw).strip()

            old_value = settings_service.get(key)
            if old_value != new_value:
                settings_service.set_value(key, new_value, actor=request.user)
                changes.append(f"{key}: {old_value} → {new_value}")

        if changes:
            audit_service.record(
                AuditAction.SETTINGS_UPDATE, actor=request.user, request=request,
                description=f"Updated {len(changes)} setting(s)", new_value="; ".join(changes)[:2000],
            )
            messages.success(request, f"Saved {len(changes)} change(s).")
        else:
            messages.info(request, "No changes to save.")
        return redirect("console:settings")

    return render(
        request,
        "console/settings.html",
        _base_context(
            "settings",
            groups=settings_service.grouped(),
            provider=provider_status(),
            providers=available_providers(),
            telegram_configured=bool(django_settings.TELEGRAM_BOT_TOKEN),
            # Whether drawing actually works right now, said plainly, so an
            # operator does not have to send a test message to find out.
            image_status=image_service.status(),
            image_sizes=sorted(image_service.VALID_SIZES),
            image_qualities=image_service.VALID_QUALITIES,
        ),
    )
