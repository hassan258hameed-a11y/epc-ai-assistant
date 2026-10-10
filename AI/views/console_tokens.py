"""Admin token management.

One page inside the console covers the whole loop an administrator works in:
students, their allocations, the models those tokens are spent on, the raw
activity and the trends over time.  Every mutation here writes to the same
ledger the chat runtime debits, so the admin view and the student view can never
drift apart.

There is no separate admin site: these views mount under ``/admin/`` on the main
application, behind ``@admin_required``.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings as django_settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Avg, Count, F, Q, Sum
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from AI.forms import AIModelForm, TokenGrantForm
from AI.views import console_ai
from AI.models import (
    AccountStatus,
    AIModel,
    AIProvider,
    AIUsageLog,
    AuditAction,
    Payment,
    PaymentStatus,
    Role,
    TokenAccount,
    TokenTransaction,
    TransactionType,
    User,
)
from AI.services import (
    analytics_service,
    audit_service,
    model_pool_service,
    model_service,
    notification_service,
    settings_service,
    token_service,
)
from AI.services.ai_service import provider_status
from AI.utils.decorators import admin_required
from AI.utils.pagination import page_range, paginate
from AI.utils.request import json_error, json_ok

logger = logging.getLogger("AI.console.tokens")

RANGE_CHOICES = {"7": 7, "14": 14, "30": 30, "90": 90}
DEFAULT_RANGE = 30


def _console_context(active: str, **extra) -> dict:
    context = {
        "nav_active": active,
        "pending_payment_count": Payment.objects.filter(status=PaymentStatus.PENDING).count(),
    }
    context.update(extra)
    return context


def _combined_pool() -> dict:
    """Every model's supply added up, in the shape the legacy template expects.

    Only ever rendered as background context. Distribution reads the per-model
    figures, because a combined total cannot tell an administrator whether the
    model a student needs actually has anything left in it.
    """
    totals = model_pool_service.grand_total()
    outstanding = max(0, totals["total_distributed"] - totals["total_reclaimed"])
    added = totals["total_added"]
    return {
        "balance": totals["balance"],
        "total_added": added,
        "total_distributed": totals["total_distributed"],
        "total_reclaimed": totals["total_reclaimed"],
        "outstanding": outstanding,
        "usage_percent": min(100, round(outstanding / added * 100)) if added else 0,
        "is_empty": totals["balance"] <= 0,
        "never_funded": added <= 0,
        # The split bar. Three shares of everything ever added, so the widths
        # sum to at most 100 and the track shows the remainder. Held is drawn
        # last and gets a floor of one percent when it is non-zero, because a
        # real balance rounding to a zero-width sliver reads as "none held",
        # which is the opposite of what it means.
        **_pool_shares(added, totals["total_reclaimed"], outstanding),
    }


def _pool_shares(added: int, returned: int, held: int) -> dict:
    if added <= 0:
        return {"share_added": 0, "share_returned": 0, "share_held": 0}

    def share(value: int) -> float:
        if value <= 0:
            return 0
        return max(1.0, round(value / added * 100, 2))

    returned_share = share(returned)
    held_share = share(held)
    # What is left of the bar after the other two, never negative.
    added_share = max(0.0, round(100 - returned_share - held_share, 2))
    return {
        "share_added": added_share,
        "share_returned": returned_share,
        "share_held": held_share,
    }


def _selected_days(request) -> int:
    return RANGE_CHOICES.get(request.GET.get("range", ""), DEFAULT_RANGE)


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------
def _overview(days: int) -> dict:
    now = timezone.now()
    window_start = now - timedelta(days=days)
    students = User.objects.filter(role=Role.STUDENT)

    totals = TokenAccount.objects.filter(user__role=Role.STUDENT).aggregate(
        remaining=Sum("balance"),
        allocated=Sum("cycle_allocated"),
        consumed=Sum("cycle_consumed"),
        lifetime_allocated=Sum("total_allocated"),
        lifetime_consumed=Sum("total_consumed"),
    )

    requests = AIUsageLog.objects.aggregate(
        total=Count("id"),
        failed=Count("id", filter=Q(success=False)),
        tokens=Sum("total_tokens", filter=Q(success=True)),
        inbound=Sum("input_tokens", filter=Q(success=True)),
        outbound=Sum("output_tokens", filter=Q(success=True)),
        latency=Avg("latency_ms", filter=Q(success=True)),
    )
    window = AIUsageLog.objects.filter(created_at__gte=window_start).aggregate(
        total=Count("id"),
        tokens=Sum("total_tokens", filter=Q(success=True)),
        failed=Count("id", filter=Q(success=False)),
    )
    today = AIUsageLog.objects.filter(created_at__date=timezone.localdate()).aggregate(
        total=Count("id"), tokens=Sum("total_tokens", filter=Q(success=True))
    )

    busiest = (
        AIUsageLog.objects.filter(success=True)
        .exclude(model_key="")
        .values("model_key")
        .annotate(tokens=Sum("total_tokens"), requests=Count("id"))
        .order_by("-tokens")
        .first()
    )
    top_model = None
    if busiest:
        top_model = AIModel.objects.filter(key=busiest["model_key"]).first()

    allocated = totals["allocated"] or 0
    consumed = totals["consumed"] or 0
    successful = (requests["total"] or 0) - (requests["failed"] or 0)

    return {
        "students_total": students.count(),
        "students_active": students.filter(status=AccountStatus.ACTIVE, is_active=True).count(),
        "students_chatting": AIUsageLog.objects.filter(created_at__gte=window_start)
        .values("user_id")
        .distinct()
        .count(),
        "students_empty": TokenAccount.objects.filter(user__role=Role.STUDENT, balance__lte=0).count(),
        "students_low": TokenAccount.objects.filter(
            user__role=Role.STUDENT,
            balance__gt=0,
            balance__lte=int(settings_service.get("low_token_threshold", 5000)),
        ).count(),
        "tokens_allocated": allocated,
        "tokens_consumed": consumed,
        "tokens_remaining": totals["remaining"] or 0,
        "lifetime_allocated": totals["lifetime_allocated"] or 0,
        "lifetime_consumed": totals["lifetime_consumed"] or 0,
        "consumed_percent": round(consumed / allocated * 100) if allocated else 0,
        "requests_total": requests["total"] or 0,
        "requests_failed": requests["failed"] or 0,
        "requests_window": window["total"] or 0,
        "requests_today": today["total"] or 0,
        "tokens_window": window["tokens"] or 0,
        "tokens_today": today["tokens"] or 0,
        "tokens_input": requests["inbound"] or 0,
        "tokens_output": requests["outbound"] or 0,
        "avg_tokens_per_request": round((requests["tokens"] or 0) / successful) if successful else 0,
        "avg_latency_ms": round(requests["latency"] or 0),
        "success_rate": round(successful / (requests["total"] or 1) * 100, 1),
        "top_model": top_model,
        "top_model_tokens": int(busiest["tokens"] or 0) if busiest else 0,
        "models_total": AIModel.objects.count(),
        "models_active": AIModel.objects.filter(is_active=True).count(),
    }


def _daily_series(days: int) -> dict:
    """Tokens, requests, input and output per day across the window."""
    start = timezone.localdate() - timedelta(days=days - 1)
    rows = (
        AIUsageLog.objects.filter(created_at__date__gte=start)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(
            tokens=Sum("total_tokens", filter=Q(success=True)),
            inbound=Sum("input_tokens", filter=Q(success=True)),
            outbound=Sum("output_tokens", filter=Q(success=True)),
            requests=Count("id"),
        )
        .order_by("day")
    )
    lookup = {row["day"]: row for row in rows}

    labels, tokens, requests, inbound, outbound = [], [], [], [], []
    for offset in range(days):
        day = start + timedelta(days=offset)
        entry = lookup.get(day, {})
        labels.append(day.strftime("%d %b"))
        tokens.append(int(entry.get("tokens") or 0))
        requests.append(int(entry.get("requests") or 0))
        inbound.append(int(entry.get("inbound") or 0))
        outbound.append(int(entry.get("outbound") or 0))

    return {
        "labels": labels,
        "tokens": tokens,
        "requests": requests,
        "input": inbound,
        "output": outbound,
    }


def _period_totals() -> dict:
    """Rolling day / week / month consumption, for the analytics tiles."""
    now = timezone.now()

    def spent(since):
        return int(
            AIUsageLog.objects.filter(created_at__gte=since, success=True).aggregate(
                total=Sum("total_tokens")
            )["total"]
            or 0
        )

    return {
        "today": spent(now - timedelta(days=1)),
        "week": spent(now - timedelta(days=7)),
        "month": spent(now - timedelta(days=30)),
        "quarter": spent(now - timedelta(days=90)),
    }


def _student_rows(request, days: int):
    """The filtered, paginated student table."""
    search = (request.GET.get("q") or "").strip()
    token_filter = request.GET.get("tokens") or ""
    status_filter = request.GET.get("status") or ""

    queryset = User.objects.filter(role=Role.STUDENT).select_related("token_account")

    if search:
        queryset = queryset.filter(
            Q(username__icontains=search)
            | Q(full_name__icontains=search)
            | Q(email__icontains=search)
        )
    if status_filter == "active":
        queryset = queryset.filter(status=AccountStatus.ACTIVE, is_active=True)
    elif status_filter == "disabled":
        queryset = queryset.filter(Q(status=AccountStatus.DISABLED) | Q(is_active=False))

    threshold = int(settings_service.get("low_token_threshold", 5000))
    if token_filter == "empty":
        queryset = queryset.filter(token_account__balance__lte=0)
    elif token_filter == "low":
        queryset = queryset.filter(token_account__balance__gt=0, token_account__balance__lte=threshold)
    elif token_filter == "healthy":
        queryset = queryset.filter(token_account__balance__gt=threshold)

    sort = request.GET.get("sort") or "-consumed"
    ordering = {
        "-consumed": "-token_account__cycle_consumed",
        "consumed": "token_account__cycle_consumed",
        "-balance": "-token_account__balance",
        "balance": "token_account__balance",
        "-allocated": "-token_account__cycle_allocated",
        "name": "username",
        "-joined": "-date_joined",
    }.get(sort, "-token_account__cycle_consumed")

    page = paginate(request, queryset.order_by(ordering, "-id"), per_page=12)

    # One grouped query for the window figures of just the visible page.
    visible_ids = [student.pk for student in page.object_list]
    window_start = timezone.now() - timedelta(days=days)
    window = {
        row["user_id"]: row
        for row in AIUsageLog.objects.filter(user_id__in=visible_ids, created_at__gte=window_start)
        .values("user_id")
        .annotate(
            tokens=Sum("total_tokens", filter=Q(success=True)),
            requests=Count("id", filter=Q(success=True)),
        )
    }

    rows = []
    for student in page.object_list:
        account = getattr(student, "token_account", None)
        stats = window.get(student.pk, {})
        allocated = account.allocation if account else 0
        consumed = account.used_in_cycle if account else 0
        balance = account.balance if account else 0
        rows.append(
            {
                "student": student,
                "allocated": allocated,
                "consumed": consumed,
                "balance": balance,
                "percent_used": round(consumed / allocated * 100) if allocated else 0,
                "percent_left": round(balance / allocated * 100) if allocated else 0,
                "period_tokens": int(stats.get("tokens") or 0),
                "period_requests": int(stats.get("requests") or 0),
                "is_empty": balance <= 0,
                "is_low": 0 < balance <= threshold,
            }
        )

    return {
        "page": page,
        "rows": rows,
        "page_numbers": page_range(page),
        "search": search,
        "token_filter": token_filter,
        "status_filter": status_filter,
        "sort": sort,
        "threshold": threshold,
    }


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
@admin_required
def token_management(request):
    days = _selected_days(request)
    model_service.ensure_seeded()

    stats = _overview(days)
    series = _daily_series(days)
    models = model_service.usage_by_model(days)
    flow = analytics_service.token_flow(days)

    # Surface a default that has drifted away from the configured model, since
    # that is where every student message actually goes.
    default_model = model_service.default_model()
    configured_key = (django_settings.OPENAI_MODEL or "").strip()
    env_model_mismatch = (
        configured_key
        if default_model and configured_key and default_model.key != configured_key
        else ""
    )

    activity = list(
        AIUsageLog.objects.select_related("user", "conversation").order_by("-created_at")[:30]
    )
    ledger = list(
        TokenTransaction.objects.select_related("user", "created_by")
        .exclude(transaction_type=TransactionType.USAGE)
        .order_by("-created_at")[:15]
    )

    # Retired rows carry no catalogue object — they are a bare key plus history.
    model_chart = [
        {
            "label": row["model"].name if row["model"] else f"{row['key']} (removed)",
            "value": row["period_tokens"],
        }
        for row in models
        if row["period_tokens"] > 0
    ]
    student_chart = [
        {"label": row["name"], "value": row["tokens"]}
        for row in analytics_service.top_consumers(8, days)
    ]

    return render(
        request,
        "console/tokens.html",
        _console_context(
            "tokens",
            stats=stats,
            consumed_hint=f"{stats['consumed_percent']}% of everything allocated",
            requests_hint=f"{stats['requests_today']:,} today · {stats['requests_window']:,} in {days} days",
            days=days,
            range_options=[("7", "7 days"), ("14", "14 days"), ("30", "30 days"), ("90", "90 days")],
            periods=_period_totals(),
            models=models,
            model_count=len(models),
            # The same card figures the AI tokens page builds, reused rather
            # than recomputed — two pages showing different numbers for the
            # same pool would be worse than either page not showing them.
            model_budgets=console_ai.model_cards(days),
            activity=activity,
            ledger=ledger,
            flow=flow,
            top_students=analytics_service.top_consumers(8, days),
            provider=provider_status(),
            env_model_mismatch=env_model_mismatch,
            # The legacy page reports the supply as one figure. It is now the
            # sum of the per-model pools rather than a dead standalone row, so
            # the number is at least true; the AI tokens page is where it is
            # broken out per model, which is the only actionable form.
            pool=_combined_pool(),
            pool_ledger=model_pool_service.recent(limit=10),
            per_model_pools=model_pool_service.summaries(),
            grant_form=TokenGrantForm(),
            model_form=AIModelForm(),
            # The model editor's two pickers. They post exactly what
            # ``AIModelForm`` reads — a vendor row and a reasoning mode — so the
            # dialog saves instead of failing on a field it never sent.
            ai_providers=list(AIProvider.objects.order_by("sort_order", "name")),
            thinking_choices=AIModel.Thinking.choices,
            tab=request.GET.get("tab") or "overview",
            usage_chart=json.dumps(
                {
                    "labels": series["labels"],
                    "series": [
                        {"name": "Tokens", "values": series["tokens"], "color": "#6E51F0"},
                    ],
                }
            ),
            split_chart=json.dumps(
                {
                    "labels": series["labels"],
                    "series": [
                        {"name": "Input", "values": series["input"], "color": "#12B3AA"},
                        {"name": "Output", "values": series["output"], "color": "#F5B547"},
                    ],
                }
            ),
            requests_chart=json.dumps(
                {
                    "labels": series["labels"],
                    "series": [{"name": "Requests", "values": series["requests"], "color": "#12B3AA"}],
                }
            ),
            model_chart=json.dumps(model_chart),
            student_chart=json.dumps(student_chart),
            **_student_rows(request, days),
        ),
    )


# ---------------------------------------------------------------------------
# Assigning tokens — moved to the per-model console
#
# Tokens stopped being one shared balance: each model has its own supply, its
# own student wallets and its own ledger. A form with no model field cannot
# express a valid allocation any more, and guessing a model here would silently
# fund the wrong currency — a billing error nobody would see until a student
# could not use the model they were promised.
#
# So these endpoints no longer move tokens. They redirect to the per-model
# pages, which ask the question this form cannot.
# ---------------------------------------------------------------------------
MOVED_MESSAGE = (
    "Token allocation now happens per AI model. Pick the student and the model "
    "on this page and you will see exactly what changes before you confirm."
)


def _redirect_to_per_model(request, destination: str = "console:ai_distribute"):
    """Send an admin (or an old open tab) to the page that can do the job."""
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return json_error(MOVED_MESSAGE, code="moved", redirect="/admin/ai-tokens/distribute/")
    messages.info(request, MOVED_MESSAGE)
    return redirect(destination)


@admin_required
@require_POST
def assign_tokens(request):
    """Superseded by ``console:ai_distribute``."""
    return _redirect_to_per_model(request)


@admin_required
@require_POST
def bulk_assign_tokens(request):
    """Superseded by the bulk allocation on ``console:ai_tokens``."""
    return _redirect_to_per_model(request, "console:ai_tokens")


@admin_required
@require_POST
def pool_topup(request):
    """Superseded by the per-model pools on ``console:ai_tokens``."""
    return _redirect_to_per_model(request, "console:ai_tokens")


@admin_required
def student_token_detail(request, pk: int):
    """Per-student token history, returned as JSON for the slide-over panel."""
    student = get_object_or_404(User, pk=pk)
    account = token_service.get_account(student)

    ledger = [
        {
            "amount": entry.amount,
            "type": entry.get_transaction_type_display(),
            "description": entry.description,
            "balance_after": entry.balance_after,
            "created_at": timezone.localtime(entry.created_at).strftime("%d %b %Y, %H:%M"),
            "actor": entry.created_by.display_name if entry.created_by else "",
            "credit": entry.amount >= 0,
        }
        for entry in TokenTransaction.objects.filter(user=student).select_related("created_by")[:25]
    ]

    by_model = [
        {
            "model": row["model_key"] or "—",
            "tokens": int(row["tokens"] or 0),
            "requests": row["requests"],
        }
        for row in AIUsageLog.objects.filter(user=student, success=True)
        .values("model_key")
        .annotate(tokens=Sum("total_tokens"), requests=Count("id"))
        .order_by("-tokens")[:8]
    ]

    return json_ok(
        student={
            "id": student.pk,
            "name": student.display_name,
            "username": student.username,
            "email": student.email,
            "status": student.get_status_display(),
            "joined": timezone.localtime(student.date_joined).strftime("%d %b %Y"),
            "last_seen": (
                timezone.localtime(student.last_seen_at).strftime("%d %b %Y, %H:%M")
                if student.last_seen_at
                else "Never"
            ),
        },
        tokens={
            "balance": account.balance,
            "allocated": account.allocation,
            "consumed": account.used_in_cycle,
            "percent_used": account.usage_percent,
            "lifetime_allocated": account.total_allocated,
            "lifetime_consumed": account.total_consumed,
        },
        ledger=ledger,
        by_model=by_model,
    )


# ---------------------------------------------------------------------------
# AI models
# ---------------------------------------------------------------------------
@admin_required
@require_POST
def model_save(request, pk: int | None = None):
    instance = get_object_or_404(AIModel, pk=pk) if pk else None
    data = request.POST
    # "Best for" is the one line the student model picker shows under the
    # name. A form posted by an older page doesn't send it, and must leave it
    # as it was rather than blank it.
    if instance is not None and "best_for" not in data:
        data = data.copy()
        data["best_for"] = instance.best_for
    form = AIModelForm(data, instance=instance)

    if not form.is_valid():
        first_error = next(iter(form.errors.values()))[0]
        messages.error(request, f"Could not save that model: {first_error}")
        return redirect("/admin/tokens/?tab=models")

    model = form.save()
    audit_service.record(
        AuditAction.SETTINGS_UPDATE,
        actor=request.user,
        request=request,
        description=f"{'Updated' if pk else 'Added'} AI model {model.key}",
        new_value=model.key,
    )
    messages.success(request, f"{model.name} saved.")
    return redirect("/admin/tokens/?tab=models")


@admin_required
@require_POST
def model_toggle(request, pk: int):
    model = get_object_or_404(AIModel, pk=pk)
    model.is_active = not model.is_active
    model.save(update_fields=["is_active", "updated_at"])

    if not model.is_active and model.is_default:
        # Never leave the platform without a usable default.
        model_service.ensure_seeded()

    audit_service.record(
        AuditAction.SETTINGS_UPDATE,
        actor=request.user,
        request=request,
        description=f"{'Enabled' if model.is_active else 'Disabled'} AI model {model.key}",
        new_value=str(model.is_active),
    )
    messages.success(
        request, f"{model.name} is now {'available to students' if model.is_active else 'disabled'}."
    )
    return redirect("/admin/tokens/?tab=models")


@admin_required
@require_POST
def model_default(request, pk: int):
    model = get_object_or_404(AIModel, pk=pk)
    model_service.make_default(model)
    audit_service.record(
        AuditAction.SETTINGS_UPDATE,
        actor=request.user,
        request=request,
        description=f"Set default AI model to {model.key}",
        new_value=model.key,
    )
    messages.success(request, f"{model.name} is now the default model.")
    return redirect("/admin/tokens/?tab=models")


@admin_required
@require_POST
def model_allocate(request, pk: int):
    model = get_object_or_404(AIModel, pk=pk)
    try:
        allocation = int(request.POST.get("token_allocation") or 0)
    except ValueError:
        allocation = 0

    model_service.set_allocation(
        model, allocation, reset_usage=request.POST.get("reset_usage") == "1"
    )
    audit_service.record(
        AuditAction.SETTINGS_UPDATE,
        actor=request.user,
        request=request,
        description=f"Set allocation for {model.key} to {allocation:,}",
        new_value=str(allocation),
    )
    messages.success(
        request,
        f"{model.name} allocation set to "
        + (f"{allocation:,} tokens." if allocation else "unlimited."),
    )
    return redirect("/admin/tokens/?tab=models")


@admin_required
@require_POST
def models_recalculate(request):
    updated = model_service.recalculate_totals()
    messages.success(request, f"Rebuilt usage totals for {updated} model(s) from the usage log.")
    return redirect("/admin/tokens/?tab=models")
