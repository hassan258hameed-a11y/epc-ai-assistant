"""Console pages that answer "who needs attention, and what is it costing?".

Four questions an administrator currently has to answer by reading three other
pages and doing arithmetic:

* which students are about to run out, and of which model;
* whose access lapses this week, and how many tokens they are still holding;
* what the platform is being told, by e-mail, and whether it arrived;
* what each model costs against what it earns.

All four are model-aware, because all four have different answers per model.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from AI.models import (
    User,
    AIUsageLog,
    EmailLog,
    EmailStatus,
    EmailType,
    Payment,
    PaymentStatus,
    Role,
    Subscription,
    UserAIWallet,
)
from AI.services import model_scope, token_alert_service
from AI.views.console_ai import tone_for
from AI.utils.decorators import admin_required
from AI.utils.pagination import page_range, paginate


def _context(active: str, **extra) -> dict:
    context = {
        "nav_active": active,
        "pending_payment_count": Payment.objects.filter(status=PaymentStatus.PENDING).count(),
    }
    context.update(extra)
    return context


# ---------------------------------------------------------------------------
# Students running low
# ---------------------------------------------------------------------------
@admin_required
def low_tokens(request):
    """Students close to running out, most urgent first.

    Ordered by proportion rather than by count, so a student with 400 of 1,000
    tokens outranks one with 4,000 of 200,000 — the first is nearly out, the
    second has barely started.
    """
    scope = model_scope.resolve(request)
    try:
        threshold = int(request.GET.get("at") or 0) or None
    except ValueError:
        threshold = None

    rows = token_alert_service.at_risk(scope, threshold=threshold)

    # The last warning sent for each (student, model), so an administrator can
    # see whether the system has already spoken before deciding to intervene.
    pairs = {(row["user"].pk, row["ai_model"].pk) for row in rows}
    warnings = {}
    if pairs:
        for log in (
            EmailLog.objects.filter(
                email_type=EmailType.LOW_TOKENS,
                user_id__in={pk for pk, _ in pairs},
            )
            .select_related("ai_model")
            .order_by("-created_at")
        ):
            warnings.setdefault((log.user_id, log.ai_model_id), log)

    for row in rows:
        row["last_warning"] = warnings.get((row["user"].pk, row["ai_model"].pk))

    page = paginate(request, rows, per_page=30)
    return render(
        request,
        "console/low_tokens.html",
        _context(
            "low_tokens",
            page=page,
            page_numbers=page_range(page),
            rows=page.object_list,
            thresholds=token_alert_service.thresholds(),
            selected_threshold=threshold,
            ceiling=threshold or max(token_alert_service.thresholds()),
            # Built here rather than in the template: a filter chain starting
            # with a string literal cannot add a number to it — `add` coerces
            # to the type of its left operand, so "At or below "|add:50 gives
            # an empty string and the card silently loses its label.
            ceiling_label=f"At or below {threshold or max(token_alert_service.thresholds())}%",
            total=len(rows),
            emails_on=token_alert_service.emails_enabled(),
        ),
    )


# ---------------------------------------------------------------------------
# Access about to lapse
# ---------------------------------------------------------------------------
@admin_required
def expiring(request):
    """Subscriptions ending soon, and the tokens sitting behind them.

    Worth being precise about what expires here: in this product a *token* has
    no expiry date — a balance sits in the wallet until it is spent. What ends
    is the subscription that granted it. So this page reports lapsing access
    and shows the balance each student still holds, rather than inventing an
    expiry the schema does not have.
    """
    scope = model_scope.resolve(request)
    try:
        days = max(1, min(365, int(request.GET.get("days") or 30)))
    except ValueError:
        days = 30

    now = timezone.now()
    horizon = now + timedelta(days=days)
    rows = (
        scope.subscriptions(
            Subscription.objects.filter(
                status=Subscription.Status.ACTIVE, ends_at__gt=now, ends_at__lte=horizon,
            )
        )
        .select_related("user", "plan", "plan__ai_model")
        .order_by("ends_at")
    )

    # Each student's balance in the model their lapsing plan belongs to.
    wanted = {(sub.user_id, sub.plan.ai_model_id) for sub in rows if sub.plan.ai_model_id}
    balances = {}
    if wanted:
        for wallet in UserAIWallet.objects.filter(
            user_id__in={u for u, _ in wanted}, ai_model_id__in={m for _, m in wanted}
        ):
            balances[(wallet.user_id, wallet.ai_model_id)] = wallet

    prepared = [
        {
            "subscription": sub,
            "days_left": (sub.ends_at - now).days,
            "wallet": balances.get((sub.user_id, sub.plan.ai_model_id)),
        }
        for sub in rows
    ]

    page = paginate(request, prepared, per_page=30)
    return render(
        request,
        "console/expiring.html",
        _context(
            "expiring",
            page=page,
            page_numbers=page_range(page),
            rows=page.object_list,
            days=days,
            days_label=f"within {days} days",
            total=len(prepared),
            windows=[7, 14, 30, 90],
            tokens_at_stake=sum(
                (row["wallet"].balance if row["wallet"] else 0) for row in prepared
            ),
        ),
    )


# ---------------------------------------------------------------------------
# The e-mail log
# ---------------------------------------------------------------------------
@admin_required
def emails(request):
    """What the platform sent, to whom, and whether it arrived."""
    scope = model_scope.resolve(request)
    queryset = EmailLog.objects.select_related("user", "ai_model")
    if not scope.is_all:
        # Mail that is not about a model at all — a verification code — has no
        # model to filter on, and does not belong under a model heading.
        queryset = queryset.filter(ai_model=scope.model)

    kind = (request.GET.get("type") or "").strip()
    if kind:
        queryset = queryset.filter(email_type=kind)
    status = (request.GET.get("status") or "").strip()
    if status:
        queryset = queryset.filter(status=status)
    search = (request.GET.get("q") or "").strip()
    if search:
        queryset = queryset.filter(
            Q(recipient__icontains=search)
            | Q(user__username__icontains=search)
            | Q(user__full_name__icontains=search)
        )

    page = paginate(request, queryset.order_by("-created_at", "-id"), per_page=40)
    _mark_retryable(page.object_list)
    window = timezone.now() - timedelta(days=30)
    totals = queryset.filter(created_at__gte=window).aggregate(
        sent=Count("id", filter=Q(status=EmailStatus.SENT)),
        failed=Count("id", filter=Q(status=EmailStatus.FAILED)),
        warnings=Count("id", filter=Q(email_type=EmailType.LOW_TOKENS)),
    )

    return render(
        request,
        "console/emails.html",
        _context(
            "emails",
            page=page,
            page_numbers=page_range(page),
            totals=totals,
            types=EmailType.choices,
            statuses=EmailStatus.choices,
            selected_type=kind,
            selected_status=status,
            search=search,
            emails_on=token_alert_service.emails_enabled(),
            thresholds=token_alert_service.thresholds(),
            thresholds_label="at "
            + ", ".join(f"{point}%" for point in token_alert_service.thresholds()),
        ),
    )


#: The e-mail log keeps who, what kind, the subject and why — not the message
#: itself. Only a message the platform can compose again from the same facts can
#: be re-sent faithfully: a low-token warning is rebuilt from the wallet, and is
#: only still true while the wallet is still low. A support reply, a payment
#: notice or an admin message would have to be re-typed, and credentials carry
#: a password that is never stored — so those are not offered a retry at all.
RETRY_UNAVAILABLE = {
    EmailType.CREDENTIALS: "Holds a password that is not stored. Reset the password to send new credentials.",
    EmailType.OTP: "Verification codes expire. The student can ask for a new one.",
}
RETRY_NOT_STORED = "The message body is not stored, so it cannot be re-sent exactly."


def _mark_retryable(logs) -> None:
    """Give each failed row ``can_retry`` and, when it cannot, ``retry_note``."""
    logs = list(logs)
    wanted = {
        (log.user_id, log.ai_model_id) for log in logs
        if log.status == EmailStatus.FAILED and log.email_type == EmailType.LOW_TOKENS and log.ai_model_id
    }
    wallets = {}
    if wanted:
        for wallet in UserAIWallet.objects.filter(
            user_id__in={u for u, _ in wanted}, ai_model_id__in={m for _, m in wanted}
        ).select_related("ai_model", "user"):
            wallets[(wallet.user_id, wallet.ai_model_id)] = wallet
    for log in logs:
        log.can_retry = False
        log.retry_note = ""
        if log.status != EmailStatus.FAILED:
            continue
        if log.email_type == EmailType.LOW_TOKENS:
            wallet = wallets.get((log.user_id, log.ai_model_id))
            if wallet is None:
                log.retry_note = "That wallet no longer exists."
            elif token_alert_service.tier_for(wallet) is None:
                log.retry_note = "The balance has recovered since, so the warning is no longer true."
            else:
                log.can_retry = True
        else:
            log.retry_note = RETRY_UNAVAILABLE.get(log.email_type, RETRY_NOT_STORED)


@admin_required
def email_retry(request, pk: int):
    """Send a failed low-token warning again, built from the wallet as it is now."""
    from django.contrib import messages
    from django.shortcuts import get_object_or_404, redirect

    from AI.models import AuditAction
    from AI.services import audit_service

    if request.method != "POST":
        return redirect("console:emails")
    log = get_object_or_404(EmailLog.objects.select_related("user", "ai_model"), pk=pk)
    back = request.POST.get("next") or ""
    base = reverse("console:emails")
    back = back if back.startswith(base) and "//" not in back else base

    _mark_retryable([log])
    if not log.can_retry:
        messages.error(request, log.retry_note or "Only a failed message can be sent again.")
        return redirect(back)

    wallet = UserAIWallet.objects.select_related("user", "ai_model").get(user=log.user, ai_model=log.ai_model)
    # The warning is about how low the wallet is now, like a fresh one would be.
    tier = token_alert_service.tier_for(wallet)
    fresh = token_alert_service.send_warning(wallet, tier, force=True)
    audit_service.record(
        AuditAction.USER_UPDATE, actor=request.user, request=request, target=log.user,
        description=f"Re-sent the {log.ai_model.name} low-token warning (e-mail log #{log.pk})"
        + (f" — {fresh.status}" if fresh is not None else " — skipped"),
    )
    if fresh is None:
        messages.warning(
            request,
            f"Not sent: {log.user.display_name} has no e-mail address now, turned these alerts off, "
            "or the account is no longer an active student.",
        )
    elif fresh.status == EmailStatus.SENT:
        messages.success(request, f"Warning re-sent to {fresh.recipient}.")
    else:
        messages.error(request, f"It failed again: {fresh.error or fresh.status}.")
    return redirect(back)


# ---------------------------------------------------------------------------
# What each model costs, against what it earns
# ---------------------------------------------------------------------------
@admin_required
def model_costs(request):
    """Provider cost against revenue, per model.

    Cost is an estimate and says so: it is the provider's list price applied to
    the tokens actually logged, which is the best figure available without
    reconciling against a vendor invoice. Rows for a model with no configured
    price are shown as unpriced rather than as free — a zero here would read as
    "this model costs nothing", which is the one thing it certainly does not
    mean.

    Never rendered to a student: `admin_required` gates the view, and no student
    template reads any of it.
    """
    return render(request, "console/model_costs.html", _model_costs_context(request))


def _model_costs_context(request) -> dict:
    scope = model_scope.resolve(request)
    try:
        days = max(1, min(365, int(request.GET.get("days") or 30)))
    except ValueError:
        days = 30
    since = timezone.now() - timedelta(days=days)

    models = model_scope.catalogue()
    if not scope.is_all:
        models = [m for m in models if m.pk == scope.model.pk]

    rows = []
    totals = {"cost": Decimal("0"), "revenue": Decimal("0"), "tokens": 0, "requests": 0}
    for model in models:
        usage = AIUsageLog.objects.filter(model_key=model.key, created_at__gte=since).aggregate(
            input=Sum("input_tokens"), output=Sum("output_tokens"),
            tokens=Sum("total_tokens"), requests=Count("id"),
        )
        input_tokens = usage["input"] or 0
        output_tokens = usage["output"] or 0
        priced = bool(model.input_cost_per_1m or model.output_cost_per_1m)
        cost = (
            (Decimal(input_tokens) / Decimal(1_000_000) * model.input_cost_per_1m)
            + (Decimal(output_tokens) / Decimal(1_000_000) * model.output_cost_per_1m)
        ) if priced else Decimal("0")

        revenue = Payment.objects.filter(
            status=PaymentStatus.PAID, plan__ai_model=model, paid_at__gte=since
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0")

        students = (
            UserAIWallet.objects.filter(ai_model=model, user__role=Role.STUDENT)
            .values("user_id").distinct().count()
        )
        requests = usage["requests"] or 0
        margin = revenue - cost
        rows.append({
            "model": model,
            "tone": tone_for(model),
            "priced": priced,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "tokens": usage["tokens"] or 0,
            "requests": requests,
            "cost": cost,
            "revenue": revenue,
            "margin": margin,
            "margin_percent": _margin_percent(margin, revenue, cost),
            "students": students,
            "cost_per_student": (cost / students) if students else Decimal("0"),
            "cost_per_request": (cost / requests) if requests else Decimal("0"),
        })
        totals["cost"] += cost
        totals["revenue"] += revenue
        totals["tokens"] += usage["tokens"] or 0
        totals["requests"] += requests

    totals["margin"] = totals["revenue"] - totals["cost"]
    totals["margin_percent"] = _margin_percent(totals["margin"], totals["revenue"], totals["cost"])
    totals["unpriced"] = [row["model"].name for row in rows if not row["priced"]]
    totals["priced"] = sum(1 for row in rows if row["priced"])
    totals["models"] = len(rows)

    # The bars are drawn against the busiest model, so the widest bar is
    # always full and the rest read as a share of it.
    busiest = max((row["input_tokens"] + row["output_tokens"] for row in rows), default=0) or 1
    for index, row in enumerate(rows):
        total = row["input_tokens"] + row["output_tokens"]
        share = total * 100 / busiest
        row["in_pct"] = round(share * row["input_tokens"] / total, 1) if total else 0
        row["out_pct"] = round(share * row["output_tokens"] / total, 1) if total else 0
        row["delay"] = f"{0.34 + index * 0.05:.2f}s"
        # Coloured by the figure as printed: a third of a cent of loss shows
        # as $0.00, and $0.00 is not red.
        shown = row["margin"].quantize(Decimal("0.01"))
        row["margin_sign"] = 0 if not row["priced"] or shown == 0 else (1 if shown > 0 else -1)

    # Whole-platform figures the hero card puts next to the ring. Plans sold
    # follows the model scope like everything else on the page.
    plans_sold = Payment.objects.filter(status=PaymentStatus.PAID, paid_at__gte=since)
    if not scope.is_all:
        plans_sold = plans_sold.filter(plan__ai_model=scope.model)
    active_students = User.objects.filter(role=Role.STUDENT, is_active=True, status="active").count()
    totals["plans_sold"] = plans_sold.count()
    totals["per_request"] = (totals["cost"] / totals["requests"]) if totals["requests"] else Decimal("0")
    totals["per_student"] = (totals["cost"] / active_students) if active_students else Decimal("0")
    totals["active_students"] = active_students
    # cost as a share of revenue, for the second bar; never below a sliver
    totals["cost_pct"] = (
        max(1.5, float(totals["cost"] / totals["revenue"] * 100)) if totals["revenue"] > 0 else 2
    )

    return _context(
            "model_costs",
            rows=rows,
            totals=totals,
            days=days,
            windows=[7, 30, 90, 365],
            window_index=[7, 30, 90, 365].index(days) if days in (7, 30, 90, 365) else -1,
            ring_length=round(RING_CIRCUMFERENCE * (totals["margin_percent"] or 0) / 100, 1),
            ring_circumference=round(RING_CIRCUMFERENCE, 1),
    )


#: The ring's stroke is r=55, so this is what a full lap of it measures.
RING_CIRCUMFERENCE = 2 * 3.141592653589793 * 55


def _margin_percent(margin, revenue, cost) -> int | None:
    """Margin as a share of revenue, or ``None`` when nothing was earned.

    Floors rather than rounds when there is a cost, and never shows 100%
    while any cost exists at all: "100%" would say the tokens were free.
    """
    if not revenue or revenue <= 0:
        return None
    raw = margin / revenue * 100
    if cost > 0:
        return min(99, int(raw))
    return int(round(raw))


def _margin_row(row: dict) -> list:
    model = row["model"]
    return [
        model.name, model.provider_name, row["requests"], row["input_tokens"], row["output_tokens"],
        f"{row['cost']:.4f}" if row["priced"] else "unpriced",
        f"{row['revenue']:.2f}", f"{row['margin']:.4f}" if row["priced"] else "",
        row["margin_percent"] if row["margin_percent"] is not None else "",
        row["students"],
        f"{row['cost_per_student']:.4f}" if row["priced"] and row["students"] else "",
        f"{row['cost_per_request']:.4f}" if row["priced"] and row["requests"] else "",
    ]


@admin_required
def model_costs_export(request):
    """The per-model table as a CSV, for the same window the page shows.

    Computed by the page's own view so the file can never disagree with the
    screen, and recorded as an export because cost figures leave the platform
    with it.
    """
    import csv
    import io as _io

    from django.http import HttpResponse

    from AI.models import AuditAction
    from AI.services import audit_service

    context = _model_costs_context(request)
    rows, days = context["rows"], context["days"]

    buffer = _io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "model", "provider", "requests", "input_tokens", "output_tokens", "estimated_cost_usd",
        "revenue_usd", "margin_usd", "margin_percent", "students", "cost_per_student_usd",
        "cost_per_request_usd",
    ])
    for row in rows:
        writer.writerow(_margin_row(row))

    audit_service.record(
        AuditAction.DATA_EXPORT,
        actor=request.user,
        request=request,
        target_label="Cost & margin",
        description=f"Exported cost and margin for {len(rows)} model(s), last {days} days, as CSV.",
        new_value="model_costs",
    )
    out = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
    out["Content-Disposition"] = f'attachment; filename="cost-margin-{days}d.csv"'
    out["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    return out
