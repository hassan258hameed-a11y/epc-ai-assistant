"""Aggregations behind the admin dashboard and the student usage page.

All series are computed with grouped queries rather than per-row loops so the
dashboard stays fast as the ledger grows.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from AI.models import (
    AITokenTransaction,
    AIUsageLog,
    AccountStatus,
    Conversation,
    Message,
    Payment,
    PaymentStatus,
    Referral,
    Role,
    Subscription,
    TokenTransaction,
    TransactionType,
    User,
    UserAIWallet,
)


def _scoped(scope):
    """``None`` and "all models" are the same thing; say so once.

    Every function below takes an optional scope. Returning None for both the
    unset case and the all-models case means each one has a single branch to
    write rather than two, and the aggregate query is literally the scoped
    query with the filter left off - which is what stops the totals disagreeing
    with the parts.
    """
    return None if (scope is None or scope.is_all) else scope


def _date_series(days: int) -> list:
    today = timezone.localdate()
    return [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]


def _series_start(days: int):
    """Local midnight at the start of the first day a ``days``-long series shows.

    The buckets are local calendar days (``TruncDate`` uses the site's time
    zone), so the window has to open at the first bucket's midnight. Opening it
    at "now minus N days" left the first bar holding only the part of its day
    after the current time of day, so it always read low.
    """
    first = timezone.localdate() - timedelta(days=days - 1)
    return timezone.make_aware(datetime.combine(first, time.min))


def overview(scope=None) -> dict:
    """Headline numbers, for one model or for all of them.

    Token figures come from the per-model wallets in both cases, so selecting
    each model in turn and adding the results gives exactly the all-models
    figure. Revenue, subscriptions and payments reach a model through the plan
    that was bought, which is the only link between money and a model that the
    schema actually records.
    """
    scope = _scoped(scope)
    now = timezone.now()
    active_window = now - timedelta(days=7)

    students = User.objects.filter(role=Role.STUDENT)
    wallets = UserAIWallet.objects.filter(user__role=Role.STUDENT)
    payments = Payment.objects.all()
    subscriptions = Subscription.objects.all()
    usage = AIUsageLog.objects.all()
    conversations = Conversation.objects.all()
    referrals = Referral.objects.all()

    if scope:
        wallets = wallets.filter(ai_model=scope.model)
        # "Students" for a single model means the students who hold that
        # model's currency - not everyone with an account, most of whom may
        # have never been given any of it.
        students = students.filter(ai_wallets__ai_model=scope.model).distinct()
        payments = payments.filter(plan__ai_model=scope.model)
        subscriptions = subscriptions.filter(plan__ai_model=scope.model)
        usage = usage.filter(model_key=scope.model.key)
        conversations = conversations.filter(messages__model_key=scope.model.key).distinct()

    totals = wallets.aggregate(
        balance=Sum("balance"),
        allocated=Sum("lifetime_allocated"),
        consumed=Sum("lifetime_used"),
    )
    paid = payments.filter(status=PaymentStatus.PAID)
    revenue = paid.aggregate(total=Sum("amount"), count=Count("id"))
    revenue_30 = paid.filter(paid_at__gte=now - timedelta(days=30)).aggregate(
        total=Sum("amount")
    )["total"] or Decimal("0")

    usage_today = usage.filter(created_at__date=timezone.localdate()).aggregate(
        tokens=Sum("total_tokens"), requests=Count("id")
    )

    return {
        "scope_label": scope.label if scope else "All models",
        "scope_key": scope.key if scope else "all",
        "total_users": students.count(),
        "active_users": students.filter(status=AccountStatus.ACTIVE, is_active=True).count(),
        "disabled_users": students.filter(Q(status=AccountStatus.DISABLED) | Q(is_active=False)).count(),
        "new_users_30d": students.filter(date_joined__gte=now - timedelta(days=30)).count(),
        "recently_active": students.filter(last_seen_at__gte=active_window).count(),
        "tokens_remaining": totals["balance"] or 0,
        "tokens_allocated": totals["allocated"] or 0,
        "tokens_consumed": totals["consumed"] or 0,
        "active_subscriptions": subscriptions.filter(
            status=Subscription.Status.ACTIVE, ends_at__gt=now
        ).count(),
        "expiring_soon": subscriptions.filter(
            status=Subscription.Status.ACTIVE, ends_at__gt=now, ends_at__lte=now + timedelta(days=7)
        ).count(),
        "revenue_total": revenue["total"] or Decimal("0"),
        "revenue_30d": revenue_30,
        "payments_paid": revenue["count"] or 0,
        "payments_pending": payments.filter(status=PaymentStatus.PENDING).count(),
        # What is actually sitting in the queue, not just how many rows. Four
        # payments is a number; $36.49 is the decision.
        "payments_pending_total": payments.filter(status=PaymentStatus.PENDING).aggregate(
            total=Sum("amount")
        )["total"]
        or Decimal("0.00"),
        "referrals_total": referrals.count(),
        "referrals_rewarded": referrals.filter(status=Referral.Status.REWARDED).count(),
        "free_trials": TokenTransaction.objects.filter(
            transaction_type=TransactionType.FREE_TRIAL
        ).count(),
        "requests_today": usage_today["requests"] or 0,
        "tokens_today": usage_today["tokens"] or 0,
        "conversations": conversations.count(),
        # A student with no tokens *of this model*. Globally it is a student
        # with nothing left anywhere.
        "no_token_users": wallets.filter(balance__lte=0).values("user_id").distinct().count()
        if scope
        else UserAIWallet.objects.filter(user__role=Role.STUDENT)
        .values("user_id")
        .annotate(total=Sum("balance"))
        .filter(total__lte=0)
        .count(),
        "avg_tokens_per_student": int(
            (totals["consumed"] or 0) / students.count()
        ) if students.count() else 0,
    }


def unattributed_usage(days: int | None = None) -> dict:
    """Requests whose model is not one of the four in the catalogue.

    Selecting each model in turn and adding up the requests gives fewer than
    the all-models total, and an administrator is entitled to know why rather
    than to assume the filter is broken. Three things land here:

    * a retired model — usage logged under a key that has since been removed
      from the catalogue, which is history and cannot be re-attributed;
    * transcription and other non-chat calls, which are real usage against a
      model that was never in the chat picker;
    * rows with no key at all, from before the model was recorded.

    Reporting the gap is the honest option. Folding these into one of the four
    would credit a model with work it did not do.
    """
    from AI.services import model_scope as scope_service

    known = {m.key for m in scope_service.catalogue()}
    rows = AIUsageLog.objects.all()
    if days:
        rows = rows.filter(created_at__gte=timezone.now() - timedelta(days=days))
    orphans = rows.exclude(model_key__in=known)
    totals = orphans.aggregate(requests=Count("id"), tokens=Sum("total_tokens"))
    # `order_by()` with no arguments first: the model has a default ordering,
    # and Django adds those columns to the SELECT, so a `.values(...).distinct()`
    # would be distinct over (model_key, created_at) and deduplicate nothing.
    keys = sorted(
        (row["model_key"] or "(not recorded)")
        for row in orphans.order_by().values("model_key").distinct()
    )
    return {
        "requests": totals["requests"] or 0,
        "tokens": totals["tokens"] or 0,
        "keys": keys,
    }


def model_breakdown() -> list[dict]:
    """Every model side by side - the drill-down path out of "All models".

    Built from the same wallet and payment queries `overview` uses, so a row
    here and that model's own overview cannot report different numbers.
    """
    from AI.services import model_scope as scope_service

    rows = []
    for model in scope_service.catalogue():
        wallets = UserAIWallet.objects.filter(user__role=Role.STUDENT, ai_model=model)
        totals = wallets.aggregate(
            balance=Sum("balance"), allocated=Sum("lifetime_allocated"), used=Sum("lifetime_used")
        )
        revenue = Payment.objects.filter(
            status=PaymentStatus.PAID, plan__ai_model=model
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
        pool = getattr(model, "pool", None)
        rows.append({
            "model": model,
            "key": model.key,
            "name": model.name,
            "provider": model.provider_name,
            "is_active": model.is_active,
            "students": wallets.values("user_id").distinct().count(),
            "balance": totals["balance"] or 0,
            "allocated": totals["allocated"] or 0,
            "used": totals["used"] or 0,
            "pool_balance": pool.balance if pool else 0,
            "revenue": revenue,
            "requests": model.request_count,
        })
    return rows



def usage_series(days: int = 14, scope=None) -> dict:
    scope = _scoped(scope)
    start = _series_start(days)
    base = AIUsageLog.objects.filter(created_at__gte=start)
    if scope:
        base = base.filter(model_key=scope.model.key)
    rows = (
        base
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(tokens=Sum("total_tokens"), requests=Count("id"))
        .order_by("day")
    )
    lookup = {row["day"]: row for row in rows}
    labels, tokens, requests = [], [], []
    for day in _date_series(days):
        entry = lookup.get(day, {})
        labels.append(day.strftime("%d %b"))
        tokens.append(int(entry.get("tokens") or 0))
        requests.append(int(entry.get("requests") or 0))
    return {"labels": labels, "tokens": tokens, "requests": requests}


def signup_series(days: int = 14) -> dict:
    start = _series_start(days)
    rows = (
        User.objects.filter(role=Role.STUDENT, date_joined__gte=start)
        .annotate(day=TruncDate("date_joined"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    )
    lookup = {row["day"]: row["count"] for row in rows}
    labels, values = [], []
    for day in _date_series(days):
        labels.append(day.strftime("%d %b"))
        values.append(int(lookup.get(day, 0)))
    return {"labels": labels, "values": values}


def revenue_series(days: int = 14, scope=None) -> dict:
    scope = _scoped(scope)
    start = _series_start(days)
    base = Payment.objects.filter(status=PaymentStatus.PAID, paid_at__gte=start)
    if scope:
        base = base.filter(plan__ai_model=scope.model)
    rows = (
        base
        .annotate(day=TruncDate("paid_at"))
        .values("day")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("day")
    )
    lookup = {row["day"]: row for row in rows}
    labels, values, counts = [], [], []
    for day in _date_series(days):
        entry = lookup.get(day, {})
        labels.append(day.strftime("%d %b"))
        values.append(float(entry.get("total") or 0))
        counts.append(int(entry.get("count") or 0))
    return {"labels": labels, "values": values, "counts": counts}


def payment_method_split(scope=None) -> list[dict]:
    scope = _scoped(scope)
    base = Payment.objects.filter(status=PaymentStatus.PAID)
    if scope:
        base = base.filter(plan__ai_model=scope.model)
    rows = (
        base
        .values("method")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("-total")
    )
    labels = dict(Payment._meta.get_field("method").choices)
    return [
        {
            "label": labels.get(row["method"], row["method"]),
            "key": row["method"],
            "total": float(row["total"] or 0),
            "count": row["count"],
        }
        for row in rows
    ]


def top_consumers(limit: int = 8, days: int = 30, scope=None) -> list[dict]:
    scope = _scoped(scope)
    start = timezone.now() - timedelta(days=days)
    base = AIUsageLog.objects.filter(created_at__gte=start, success=True)
    if scope:
        base = base.filter(model_key=scope.model.key)
    rows = (
        base
        .values("user__id", "user__username", "user__full_name")
        .annotate(tokens=Sum("total_tokens"), requests=Count("id"))
        .order_by("-tokens")[:limit]
    )
    return [
        {
            "id": row["user__id"],
            "name": row["user__full_name"] or row["user__username"],
            "username": row["user__username"],
            "tokens": int(row["tokens"] or 0),
            "requests": row["requests"],
        }
        for row in rows
    ]


def token_flow(days: int = 30, scope=None) -> dict:
    """Where tokens came from and went, by transaction type.

    The global ledger has no model on it, so a scoped call reads the per-model
    ledger instead - the same events, recorded against the currency they moved.
    """
    scope = _scoped(scope)
    start = timezone.now() - timedelta(days=days)
    if scope:
        ledger = AITokenTransaction.objects.filter(
            created_at__gte=start, ai_model=scope.model
        )
        field = "transaction_type"
        labels = dict(AITokenTransaction._meta.get_field(field).choices)
    else:
        ledger = TokenTransaction.objects.filter(created_at__gte=start)
        field = "transaction_type"
        labels = dict(TokenTransaction._meta.get_field(field).choices)
    rows = ledger.values(field).annotate(total=Sum("amount"), count=Count("id"))
    granted, consumed = 0, 0
    breakdown = []
    for row in rows:
        total = int(row["total"] or 0)
        if total >= 0:
            granted += total
        else:
            consumed += -total
        breakdown.append(
            {
                "label": labels.get(row["transaction_type"], row["transaction_type"]),
                "key": row["transaction_type"],
                "total": abs(total),
                "count": row["count"],
                "credit": total >= 0,
            }
        )
    breakdown.sort(key=lambda item: item["total"], reverse=True)
    return {"granted": granted, "consumed": consumed, "breakdown": breakdown}


def student_usage(user, days: int = 14, *, model_key: str = "") -> dict:
    start = _series_start(days)
    logs = AIUsageLog.objects.filter(user=user)
    if model_key:
        logs = logs.filter(model_key=model_key)
    rows = (
        logs.filter(created_at__gte=start)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(tokens=Sum("total_tokens"), requests=Count("id"))
        .order_by("day")
    )
    lookup = {row["day"]: row for row in rows}
    labels, tokens, requests = [], [], []
    for day in _date_series(days):
        entry = lookup.get(day, {})
        labels.append(day.strftime("%d %b"))
        tokens.append(int(entry.get("tokens") or 0))
        requests.append(int(entry.get("requests") or 0))

    peak = max(tokens) if tokens else 0
    totals = logs.aggregate(
        tokens=Sum("total_tokens"), requests=Count("id"),
        input_tokens=Sum("input_tokens"), output_tokens=Sum("output_tokens"),
    )
    return {
        "labels": labels,
        "tokens": tokens,
        "requests": requests,
        "total_tokens": int(totals["tokens"] or 0),
        "total_requests": int(totals["requests"] or 0),
        "input_tokens": int(totals["input_tokens"] or 0),
        "output_tokens": int(totals["output_tokens"] or 0),
        "period_tokens": sum(tokens),
        "period_requests": sum(requests),
        # Pre-shaped for the chart. Bar heights are worked out here rather than
        # in the template: a percentage of the busiest day is arithmetic, and
        # arithmetic in a template is where rounding bugs go to hide.
        "peak": peak,
        "series": [
            {
                "label": label,
                "tokens": token_count,
                "requests": request_count,
                "height": round(token_count / peak * 100) if peak else 0,
            }
            for label, token_count, request_count in zip(labels, tokens, requests)
        ],
        "average_tokens": round(sum(tokens) / sum(requests)) if sum(requests) else 0,
    }


def student_usage_by_model(user, days: int = 30) -> list[dict]:
    """Where the period's tokens went, one row per model, largest first."""
    from AI.models import AIModel

    start = _series_start(days)
    rows = list(
        AIUsageLog.objects.filter(user=user, created_at__gte=start, success=True)
        .values("model_key")
        .annotate(tokens=Sum("total_tokens"), requests=Count("id"),
                  input_tokens=Sum("input_tokens"), output_tokens=Sum("output_tokens"))
        .order_by("-tokens")
    )
    names = dict(AIModel.objects.values_list("key", "name"))
    total = sum(int(r["tokens"] or 0) for r in rows) or 0
    return [
        {
            "key": r["model_key"],
            "name": names.get(r["model_key"], r["model_key"] or "Other"),
            "tokens": int(r["tokens"] or 0),
            "requests": r["requests"],
            "input_tokens": int(r["input_tokens"] or 0),
            "output_tokens": int(r["output_tokens"] or 0),
            "percent": round(int(r["tokens"] or 0) * 100 / total) if total else 0,
        }
        for r in rows if r["model_key"]
    ]
