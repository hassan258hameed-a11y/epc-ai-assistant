"""AI token management — the admin console for per-model token supply.

Two pages, deliberately separated by the job they do:

* **AI tokens** (``/admin/ai-tokens/``) — one card per model showing what is
  left to give out, what has been given, what has been spent, how many students
  hold it and whether it is switched on. Never one combined figure: a single
  "2,340,000 tokens" number would hide that Sonnet ran out an hour ago.

* **Distribute** (``/admin/ai-tokens/distribute/``) — pick a student, pick a
  model, enter an amount, see exactly what will change on both sides, confirm.

Everything that writes goes through :mod:`AI.services.distribution_service`, so
the pool movement, the wallet movement, the ledger entry and the audit record
are one atomic act rather than four things a view has to remember to do.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from AI.forms import ModelAdjustmentForm, ModelPoolTopUpForm, ModelTokenGrantForm
from AI.models import (
    AccountStatus,
    AIModel,
    AIProvider,
    AITokenTransaction,
    AIUsageLog,
    AuditAction,
    ModelPoolTransaction,
    Payment,
    PaymentStatus,
    Role,
    User,
    UserAIWallet,
    WalletTransactionType,
)
from AI.services import (
    audit_service,
    distribution_service,
    model_pool_service,
    model_scope,
    model_service,
    providers,
    settings_service,
    wallet_service,
)
from AI.services.model_pool_service import InsufficientPool
from AI.utils.decorators import admin_required
from AI.utils.pagination import clean_date, clean_id, clean_int, page_range, paginate
from AI.utils.redirects import safe_next
from AI.utils.request import json_error, json_ok

logger = logging.getLogger("AI.console.ai")

RANGE_CHOICES = {"7": 7, "14": 14, "30": 30, "90": 90}
DEFAULT_RANGE = 30


def _console_context(active: str, **extra) -> dict:
    context = {
        "nav_active": active,
        "pending_payment_count": Payment.objects.filter(status=PaymentStatus.PENDING).count(),
    }
    context.update(extra)
    return context


def _selected_days(request) -> int:
    return RANGE_CHOICES.get(request.GET.get("range", ""), DEFAULT_RANGE)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
#: A colour per model, so a card, its top-up button and its distribute link
#: all read as the same thing at a glance. Keyed by model id rather than by
#: position, so adding a model never re-colours the ones already there — an
#: administrator who has learned that Opus is the red card keeps that.
#:
#: `ink` is the text weight of the pair: dark enough to carry small type on
#: white, which the lighter gradient stop is not.
#: `icon` differs per model as well as the colour. Six identical marks in six
#: different colours makes the colour do all the work; a distinct glyph means
#: the cards are still tellable apart in greyscale, or by someone who does not
#: separate red from green.
MODEL_TONES = {
    "gpt-5-mini":        {"from": "#60A5FA", "to": "#2563EB", "ink": "#2563EB", "icon": "i-layers"},
    "gpt-4o":            {"from": "#67D3F8", "to": "#0EA5E9", "ink": "#0369A1", "icon": "i-cpu"},
    "claude-haiku-4-5":  {"from": "#8B84F5", "to": "#4F46E5", "ink": "#4F46E5", "icon": "i-sparkles"},
    "claude-sonnet-5":   {"from": "#A78BFA", "to": "#7C3AED", "ink": "#7C3AED", "icon": "i-server"},
    "claude-fable-5-1":  {"from": "#F472B6", "to": "#DB2777", "ink": "#DB2777", "icon": "i-tag"},
    "claude-opus-5":     {"from": "#F87171", "to": "#DC2626", "ink": "#DC2626", "icon": "i-database"},
    "gpt-5.6-sol":       {"from": "#FBBF24", "to": "#D97706", "ink": "#B45309", "icon": "i-sun"},
    "gpt-6-astra":       {"from": "#34D399", "to": "#059669", "ink": "#047857", "icon": "i-star"},
}

#: A model the palette has never seen still gets a colour rather than a hole.
FALLBACK_TONE = {"from": "#9AA5BC", "to": "#5B6884", "ink": "#47536C", "icon": "i-cpu"}


def tone_for(model) -> dict:
    return MODEL_TONES.get(model.key, FALLBACK_TONE)


def model_cards(days: int) -> list[dict]:
    """One self-contained figure set per model. The page's whole backbone.

    Deliberately per model and never summed: "available to distribute" only
    means something for a named model, because the tokens are not fungible.
    """
    model_service.ensure_seeded()
    models = list(
        AIModel.objects.select_related("provider_ref", "pool").order_by("sort_order", "name")
    )
    if not models:
        return []

    window_start = timezone.now() - timedelta(days=days)

    # Wallet figures per model, one query.
    held = {
        row["ai_model_id"]: row
        for row in UserAIWallet.objects.filter(ai_model__in=models)
        .values("ai_model_id")
        .annotate(
            outstanding=Sum("balance"),
            allocated=Sum("allocated_tokens"),
            consumed=Sum("lifetime_used"),
            holders=Count("id", filter=Q(balance__gt=0)),
            with_access=Count("id", filter=Q(is_enabled=True)),
        )
    }

    # Live usage per model, one query.
    usage = {
        row["model_key"]: row
        for row in AIUsageLog.objects.filter(created_at__gte=window_start)
        .values("model_key")
        .annotate(
            tokens=Sum("total_tokens", filter=Q(success=True)),
            requests=Count("id", filter=Q(success=True)),
            failures=Count("id", filter=Q(success=False)),
            students=Count("user_id", distinct=True, filter=Q(success=True)),
        )
    }

    configured = {slug: providers.is_configured(slug) for slug in providers.available_slugs()}

    cards = []
    for model in models:
        pool = getattr(model, "pool", None)
        wallets = held.get(model.pk, {})
        window = usage.get(model.key, {})
        label, tone = model.availability_badge

        cards.append(
            {
                "model": model,
                "provider": model.provider_ref,
                "tone": tone_for(model),
                # What the admin still has to hand out.
                "available": pool.balance if pool else 0,
                "pool_added": pool.total_added if pool else 0,
                "distributed": pool.total_distributed if pool else 0,
                "reclaimed": pool.total_reclaimed if pool else 0,
                "pool_percent": pool.usage_percent if pool else 0,
                "pool_empty": (pool.balance if pool else 0) <= 0,
                # How the supply was bought, for a model purchased as a fixed
                # block of input and output capacity. Empty for the rest, and
                # the card simply does not render the row.
                "direction_rows": pool.direction_rows() if pool else [],
                # What students hold and have burned.
                "outstanding": int(wallets.get("outstanding") or 0),
                "allocated": int(wallets.get("allocated") or 0),
                "consumed": int(wallets.get("consumed") or 0),
                "holders": int(wallets.get("holders") or 0),
                "with_access": int(wallets.get("with_access") or 0),
                # How hard it is actually being used.
                "period_tokens": int(window.get("tokens") or 0),
                "period_requests": int(window.get("requests") or 0),
                "period_failures": int(window.get("failures") or 0),
                "period_students": int(window.get("students") or 0),
                # Whether it can answer at all.
                "status_label": label,
                "status_tone": tone,
                "credentials_ok": configured.get(model.provider, False),
                "lifetime_tokens": model.tokens_consumed,
                "estimated_cost": model.estimated_cost,
            }
        )
    return cards


#: This table's own model filter. Not ``?model=``: that key is the console-wide
#: model selector, and filtering the table with it switched (and remembered)
#: the whole console's scope as a side effect.
STUDENT_MODEL_PARAM = "smodel"


def _student_rows(request, models: list[AIModel], scope=None):
    """The student table: one row per student, one wallet cell per model."""
    search = (request.GET.get("q") or "").strip()
    model_filter = (request.GET.get(STUDENT_MODEL_PARAM) or "").strip()
    status_filter = request.GET.get("status") or ""

    queryset = User.objects.filter(role=Role.STUDENT)
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

    selected = next((m for m in models if m.key == model_filter), None)
    if selected is None:
        model_filter = ""
        # With one model selected console-wide, "has tokens" can only mean
        # that model's tokens — so the balance filter works without a second
        # pick of the same model in this form.
        if scope is not None and not scope.is_all:
            selected = next((m for m in models if m.pk == scope.model.pk), None)
    tokens_filter = request.GET.get("tokens") or ""
    if tokens_filter not in ("empty", "funded"):
        tokens_filter = ""
    if selected is not None and tokens_filter:
        # Filtering by balance only makes sense against a named model.
        if tokens_filter == "empty":
            with_tokens = UserAIWallet.objects.filter(
                ai_model=selected, balance__gt=0
            ).values("user_id")
            queryset = queryset.exclude(pk__in=with_tokens)
        elif tokens_filter == "funded":
            queryset = queryset.filter(
                pk__in=UserAIWallet.objects.filter(ai_model=selected, balance__gt=0).values("user_id")
            )

    page = paginate(request, queryset.order_by("-date_joined", "-id"), per_page=12)

    visible = list(page.object_list)
    wallets: dict[tuple[int, int], UserAIWallet] = {
        (wallet.user_id, wallet.ai_model_id): wallet
        for wallet in UserAIWallet.objects.filter(user__in=visible, ai_model__in=models)
    }

    rows = []
    for student in visible:
        cells = []
        for model in models:
            wallet = wallets.get((student.pk, model.pk))
            cells.append(
                {
                    "model": model,
                    "tone": tone_for(model),
                    "balance": wallet.balance if wallet else 0,
                    "allocated": wallet.allocated_tokens if wallet else 0,
                    "used": wallet.used_tokens if wallet else 0,
                    "percent_used": wallet.usage_percent if wallet else 0,
                    "enabled": wallet.is_enabled if wallet else True,
                    "empty": (wallet.balance if wallet else 0) <= 0,
                }
            )
        rows.append(
            {
                "student": student,
                "cells": cells,
                "total": sum(cell["balance"] for cell in cells),
                "has_any": any(cell["balance"] > 0 for cell in cells),
            }
        )

    return {
        "page": page,
        "rows": rows,
        "page_numbers": page_range(page),
        "search": search,
        "status_filter": status_filter,
        "model_filter": model_filter,
        "tokens_filter": tokens_filter,
        # Said on the page, so "Has tokens" is never ambiguous about which model.
        "tokens_model": selected,
    }


def _ledger(request, models: list[AIModel]):
    """The audit history, filtered the way an admin actually asks for it."""
    # ``ai_model__provider_ref`` is joined because every row renders the vendor
    # name, and that walks the model to its provider. Without it the history is
    # one extra query per row — sixty on a full page, and growing.
    entries = AITokenTransaction.objects.select_related(
        "user", "ai_model", "ai_model__provider_ref", "performed_by"
    ).order_by("-created_at", "-id")

    model_key = (request.GET.get("hmodel") or "").strip()
    if model_key:
        entries = entries.filter(ai_model__key=model_key)

    kind = (request.GET.get("type") or "").strip()
    if kind in dict(WalletTransactionType.choices):
        entries = entries.filter(transaction_type=kind)

    student = (request.GET.get("student") or "").strip()
    if student:
        entries = entries.filter(
            Q(user__username__icontains=student) | Q(user__full_name__icontains=student)
        )

    # A date that does not parse is ignored, not handed to the database.
    since = clean_date(request.GET.get("since"))
    if since:
        entries = entries.filter(created_at__date__gte=since)
    until = clean_date(request.GET.get("until"))
    if until:
        entries = entries.filter(created_at__date__lte=until)

    return {
        "ledger": list(entries[:60]),
        "ledger_types": WalletTransactionType.choices,
        "ledger_model": model_key,
        "ledger_type": kind,
        "ledger_student": student,
        "ledger_since": since.isoformat() if since else "",
        "ledger_until": until.isoformat() if until else "",
    }


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@admin_required
def ai_tokens(request):
    """The AI Token Management dashboard: one section per model."""
    days = _selected_days(request)
    selected = model_scope.resolve(request)

    # This page was already one card per model — it just always showed four.
    # Narrowing the list is all "select a model" has to mean here, and it keeps
    # the student table and the ledger below in step with the card above them,
    # because both are built from `models`.
    cards = model_cards(days)
    if not selected.is_all:
        cards = [card for card in cards if card["model"].pk == selected.model.pk]
    models = [card["model"] for card in cards]

    # The four figures across the top. Each is a sum over the cards below, so
    # the header and the grid can never disagree — and each is labelled with
    # what it counts, because "tokens" alone means four different things on
    # this page.
    #
    # These are the one place a total across models is legitimate: they are
    # explicitly "across every pool", not a balance anyone can spend.
    summary = {
        # Built here rather than in the template: Django's `add` filter returns
        # an empty string when a string literal meets a number, so
        # `"Requests · "|add:days` silently renders nothing at all.
        "requests_label": f"Requests · {days}d",
        "pools_label": f"Across all {len(cards)} model pool{'' if len(cards) == 1 else 's'}",
        "available": sum(card["available"] for card in cards),
        "distributed": sum(card["distributed"] for card in cards),
        "held": sum(card["outstanding"] for card in cards),
        "requests": sum(card["period_requests"] for card in cards),
        "pools": len(cards),
    }

    return render(
        request,
        "console/ai_tokens.html",
        _console_context(
            "ai_tokens",
            cards=cards,
            summary=summary,
            models=models,
            providers_list=list(AIProvider.objects.order_by("sort_order", "name")),
            days=days,
            range_options=[("7", "7 days"), ("14", "14 days"), ("30", "30 days"), ("90", "90 days")],
            grand_total=model_pool_service.grand_total(),
            students_total=User.objects.filter(role=Role.STUDENT).count(),
            pool_form=ModelPoolTopUpForm(),
            pool_ledger=model_pool_service.recent(limit=12),
            tab=request.GET.get("tab") or "overview",
            # The two filter forms each carry the other's values as hidden
            # fields, so filtering one table never resets the other.
            student_params=_carried(request, ("q", "status", STUDENT_MODEL_PARAM, "tokens")),
            ledger_params=_carried(request, ("hmodel", "type", "student", "since", "until")),
            range_param=_carried(request, ("range",)),
            bulk_targets=BULK_TARGETS,
            **_student_rows(request, models, selected),
            **_ledger(request, models),
        ),
    )


#: Who a bulk allocation can be aimed at, as the bulk form offers it.
BULK_TARGETS = [
    ("active", "Every active student"),
    ("empty", "Students with none of this model's tokens"),
    ("low", "Students running low on this model"),
]


def _carried(request, keys) -> list[tuple[str, str]]:
    """``[(name, value)]`` for the given query parameters that are set."""
    return [(key, request.GET.get(key)) for key in keys if request.GET.get(key)]


@admin_required
def distribute(request):
    """The distribution workflow: student, model, amount, preview, confirm."""
    model_service.ensure_seeded()
    models = list(
        AIModel.objects.select_related("provider_ref", "pool").order_by("sort_order", "name")
    )

    search = (request.GET.get("q") or "").strip()
    students = User.objects.filter(role=Role.STUDENT)
    if search:
        students = students.filter(
            Q(username__icontains=search)
            | Q(full_name__icontains=search)
            | Q(email__icontains=search)
        )
    students = list(students.order_by("username")[:200])

    selected = None
    selected_id = (request.GET.get("student") or "").strip()
    if selected_id.isdigit():
        selected = User.objects.filter(pk=selected_id, role=Role.STUDENT).first()

    # A link from one model's card (or a low-balance row) arrives with that
    # model already chosen. It is the model *key*, the same ``?model=`` every
    # console page reads, so following the link keeps the console's model
    # selector in step instead of resetting it. No key, or an unknown one,
    # leaves the first model picked as before.
    model_key = (request.GET.get("model") or "").strip()
    selected_model = next((model for model in models if model.key == model_key), None)

    balances = []
    if selected is not None:
        balances = wallet_service.overview(selected, models)

    return render(
        request,
        "console/ai_distribute.html",
        _console_context(
            "ai_tokens",
            models=models,
            pools=model_pool_service.summaries(models),
            students=students,
            search=search,
            selected_student=selected,
            selected_model=selected_model,
            balances=balances,
            grant_form=ModelTokenGrantForm(
                initial={"user_id": selected.pk if selected else None}
            ),
            recent=list(
                AITokenTransaction.objects.filter(
                    transaction_type__in=[
                        WalletTransactionType.ADMIN_ALLOCATION,
                        WalletTransactionType.ADMIN_REVERSAL,
                        WalletTransactionType.MANUAL_ADJUSTMENT,
                    ]
                )
                .select_related("user", "ai_model", "performed_by")
                .order_by("-created_at", "-id")[:15]
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
@admin_required
def distribute_preview(request):
    """Show what a distribution *would* do, before anything is written.

    The confirmation modal renders straight from this, so the numbers an admin
    approves are the numbers the server computed — not a guess made in the
    browser from stale page data.
    """
    form = ModelTokenGrantForm(request.GET)
    if not form.is_valid():
        first_error = next(iter(form.errors.values()))[0]
        return json_error(first_error, code="invalid")

    student = form.student
    ai_model = form.ai_model
    plan = distribution_service.preview(
        student, ai_model, form.cleaned_data["amount"], form.cleaned_data["action"]
    )

    return json_ok(
        student={"id": student.pk, "name": student.display_name, "username": student.username},
        model={"id": ai_model.pk, "name": ai_model.name, "provider": ai_model.provider_name},
        action=plan["action"],
        amount=plan["amount"],
        delta=plan["delta"],
        current_balance=plan["current_balance"],
        new_balance=plan["new_balance"],
        pool_before=plan["pool_before"],
        pool_after=plan["pool_after"],
        affordable=plan["affordable"],
        shortfall=plan["shortfall"],
    )


@admin_required
@require_POST
def distribute_submit(request):
    """Apply one distribution. Pool out, wallet in, atomically."""
    form = ModelTokenGrantForm(request.POST)
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    # Back to the page the form was on — the distribute page, or a student's
    # own page — but only ever to a page on this site.
    fallback = safe_next(request, "console:ai_tokens")

    if not form.is_valid():
        first_error = next(iter(form.errors.values()))[0]
        if wants_json:
            return json_error(first_error, code="invalid")
        messages.error(request, first_error)
        return redirect(fallback)

    student = form.student
    ai_model = form.ai_model

    try:
        result = distribution_service.distribute(
            student,
            ai_model,
            form.cleaned_data["amount"],
            action=form.cleaned_data["action"],
            reason=(form.cleaned_data.get("reason") or "").strip(),
            performed_by=request.user,
            request=request,
        )
    except InsufficientPool as exc:
        if wants_json:
            return json_error(exc.friendly_message, code="pool_empty", pool_balance=exc.balance)
        messages.error(request, exc.friendly_message)
        return redirect(fallback)
    except ValueError as exc:
        if wants_json:
            return json_error(str(exc), code="invalid")
        messages.error(request, str(exc))
        return redirect(fallback)
    except Exception:
        logger.exception("Distribution failed student=%s model=%s", student.pk, ai_model.key)
        if wants_json:
            return json_error("Could not update that balance. Please try again.", status=500)
        messages.error(request, "Could not update that balance. Please try again.")
        return redirect(fallback)

    if wants_json:
        return json_ok(
            message=result.summary,
            user_id=student.pk,
            model=ai_model.key,
            balance=result.balance_after,
            balance_before=result.balance_before,
            pool=result.pool_after,
        )

    messages.success(
        request,
        f"{result.summary} New {ai_model.name} balance: {result.balance_after:,}. "
        f"Pool remaining: {result.pool_after:,}.",
    )
    return redirect(fallback)


@admin_required
@require_POST
def pool_topup(request):
    """Add to — or correct — one model's admin supply."""
    form = ModelPoolTopUpForm(request.POST)
    # The Token management page tops up from its own pool card too, and wants
    # to land back on itself rather than here.
    back = safe_next(request, "console:ai_tokens")
    if not form.is_valid():
        messages.error(request, next(iter(form.errors.values()))[0])
        return redirect(back)

    ai_model = form.ai_model
    amount = form.cleaned_data["amount"]
    note = (form.cleaned_data.get("note") or "").strip()

    try:
        if amount > 0:
            model_pool_service.top_up(
                ai_model, amount, description=note or f"{ai_model.name} pool top-up",
                created_by=request.user,
            )
            verb = f"Added {amount:,} tokens to"
        else:
            model_pool_service.adjust(
                ai_model, amount, description=note or f"{ai_model.name} pool correction",
                created_by=request.user,
            )
            verb = f"Removed {abs(amount):,} tokens from"
    except Exception:
        logger.exception("Pool top-up failed model=%s", ai_model.key)
        messages.error(request, "Could not update that pool. Please try again.")
        return redirect(back)

    audit_service.record(
        AuditAction.TOKENS_ADD if amount > 0 else AuditAction.TOKENS_REMOVE,
        actor=request.user,
        request=request,
        description=f"{verb} the {ai_model.name} pool" + (f" — {note}" if note else ""),
        new_value=f"{ai_model.key}:{model_pool_service.balance(ai_model)}",
    )
    messages.success(
        request,
        f"{verb} the {ai_model.name} pool. "
        f"Available now: {model_pool_service.balance(ai_model):,} tokens.",
    )
    return redirect(back)


@admin_required
@require_POST
def adjust_wallet(request):
    """A signed manual correction to one wallet, with a mandatory reason."""
    form = ModelAdjustmentForm(request.POST)
    if not form.is_valid():
        messages.error(request, next(iter(form.errors.values()))[0])
        return redirect("console:ai_distribute")

    student = get_object_or_404(User, pk=form.cleaned_data["user_id"])
    ai_model = get_object_or_404(AIModel, pk=form.cleaned_data["model_id"])

    result = distribution_service.adjust(
        student,
        ai_model,
        form.cleaned_data["delta"],
        reason=form.cleaned_data["reason"],
        performed_by=request.user,
        request=request,
    )
    messages.success(
        request,
        f"Adjusted {student.display_name}'s {ai_model.name} balance to "
        f"{result.balance_after:,} tokens.",
    )
    return redirect(f"/admin/ai-tokens/distribute/?student={student.pk}")


@admin_required
@require_POST
def bulk_distribute(request):
    """Give the same per-model allocation to every student matching a selector."""
    amount = clean_int(str(request.POST.get("amount") or "").replace(",", ""), maximum=100_000_000) or 0
    model_id = clean_id(request.POST.get("model_id"))
    action = request.POST.get("action") or "add"
    if action not in ("add", "set"):
        action = "add"
    target = request.POST.get("target") or "active"
    if target not in dict(BULK_TARGETS):
        target = "active"
    reason = (request.POST.get("reason") or "").strip()

    ai_model = AIModel.objects.filter(pk=model_id).first() if model_id else None
    if ai_model is None:
        messages.error(request, "Pick the AI model to distribute.")
        return redirect("console:ai_tokens")
    if amount <= 0:
        messages.error(request, "Enter a token amount greater than zero.")
        return redirect("console:ai_tokens")

    students = User.objects.filter(role=Role.STUDENT)
    if target == "active":
        students = students.filter(status=AccountStatus.ACTIVE, is_active=True)
    elif target == "empty":
        funded = UserAIWallet.objects.filter(ai_model=ai_model, balance__gt=0).values("user_id")
        students = students.exclude(pk__in=funded)
    elif target == "low":
        threshold = int(settings_service.get("low_token_threshold", 5000))
        students = students.filter(
            pk__in=UserAIWallet.objects.filter(
                ai_model=ai_model, balance__gt=0, balance__lte=threshold
            ).values("user_id")
        )

    students = list(students[:2000])
    if not students:
        messages.warning(request, "No students matched that selection.")
        return redirect("console:ai_tokens")

    outcome = distribution_service.bulk_distribute(
        students, ai_model, amount,
        action=action, reason=reason, performed_by=request.user, request=request,
    )

    if outcome["stopped_short"]:
        messages.warning(
            request,
            f"Updated {outcome['changed']} of {outcome['attempted']} students, then the "
            f"{ai_model.name} pool ran out. Top it up and run the rest.",
        )
    else:
        student_word = "student" if outcome["changed"] == 1 else "students"
        messages.success(
            request,
            f"Updated {outcome['changed']} {student_word} for {ai_model.name}. "
            f"Pool remaining: {outcome['pool_balance']:,}.",
        )
    return redirect("console:ai_tokens")


@admin_required
@require_POST
def toggle_access(request, pk: int, model_pk: int):
    """Grant or revoke one model for one student, without moving any tokens."""
    student = get_object_or_404(User, pk=pk)
    ai_model = get_object_or_404(AIModel, pk=model_pk)

    wallet = wallet_service.get_wallet(student, ai_model)
    distribution_service.set_model_access(
        student, ai_model, not wallet.is_enabled, performed_by=request.user, request=request
    )
    state = "can now use" if not wallet.is_enabled else "can no longer use"
    messages.success(request, f"{student.display_name} {state} {ai_model.name}.")
    return redirect(safe_next(request, "console:ai_tokens"))


@admin_required
@require_POST
def provider_toggle(request, pk: int):
    """Take a whole vendor offline, or bring it back.

    This is the switch for "Anthropic is having an outage": every Claude model
    stops, and every OpenAI model keeps answering, because availability is
    resolved per model at routing time.
    """
    provider = get_object_or_404(AIProvider, pk=pk)
    provider.is_active = not provider.is_active
    provider.save(update_fields=["is_active", "updated_at"])

    audit_service.record(
        AuditAction.SETTINGS_UPDATE,
        actor=request.user,
        request=request,
        description=f"{'Enabled' if provider.is_active else 'Disabled'} provider {provider.name}",
        new_value=str(provider.is_active),
    )
    if provider.is_active:
        messages.success(request, f"{provider.name} models are available again.")
    else:
        messages.warning(
            request,
            f"{provider.name} models are switched off. Every other provider keeps working.",
        )
    return redirect("console:ai_tokens")


@admin_required
def student_wallets(request, pk: int):
    """Per-student, per-model detail for the slide-over panel. JSON."""
    student = get_object_or_404(User, pk=pk)
    models = list(AIModel.objects.select_related("provider_ref").order_by("sort_order", "name"))
    rows = wallet_service.overview(student, models)

    ledger = [
        {
            "amount": entry.amount,
            "model": entry.ai_model.name,
            "type": entry.get_transaction_type_display(),
            "reason": entry.reason,
            "balance_before": entry.balance_before,
            "balance_after": entry.balance_after,
            "created_at": timezone.localtime(entry.created_at).strftime("%d %b %Y, %H:%M"),
            "actor": entry.performed_by.display_name if entry.performed_by else "",
            "credit": entry.amount >= 0,
        }
        for entry in AITokenTransaction.objects.filter(user=student)
        .select_related("ai_model", "performed_by")
        .order_by("-created_at", "-id")[:30]
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
        wallets=[
            {
                "model_id": row["model"].pk,
                "model": row["model"].name,
                "key": row["model"].key,
                "provider": row["model"].provider_name,
                "balance": row["balance"],
                "allocated": row["allocated"],
                "used": row["used"],
                "percent_used": row["percent_used"],
                "enabled": row["is_enabled"],
                "usable": row["usable"],
                "reason": row["reason"],
            }
            for row in rows
        ],
        ledger=ledger,
    )


@admin_required
@require_POST
def recalculate_wallet(request, pk: int, model_pk: int):
    """Rebuild one wallet from its ledger. The repair button."""
    student = get_object_or_404(User, pk=pk)
    ai_model = get_object_or_404(AIModel, pk=model_pk)
    wallet = wallet_service.recalculate(student, ai_model)
    messages.success(
        request,
        f"Rebuilt {student.display_name}'s {ai_model.name} balance from the ledger: "
        f"{wallet.balance:,} tokens.",
    )
    return redirect(safe_next(request, "console:ai_tokens"))
