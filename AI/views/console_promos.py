"""Console pages for promo codes: publish them, pause them, see what they cost.

Every figure here counts *used* redemptions only — a code attached to a payment
that was never completed has not taken anything off anyone's revenue.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from AI.forms import PromoCodeForm
from AI.models import AuditAction, PromoCode, PromoRedemption
from AI.models.extras import RESERVATION_HOURS
from AI.services import audit_service, promo_service
from AI.utils.decorators import admin_required
from AI.utils.pagination import page_range, paginate
from AI.views.console_insights import _context


def _counted_uses():
    """``PromoCode.uses`` as an annotation: used, plus reservations still held.

    The same rule as ``PromoCode.counted_redemptions``, asked once for the
    whole list rather than twice per code (once for the count, once inside
    ``status_label``).
    """
    fresh = timezone.now() - timedelta(hours=RESERVATION_HOURS)
    return Count(
        "redemptions",
        filter=Q(redemptions__status=PromoRedemption.Status.USED)
        | Q(redemptions__status=PromoRedemption.Status.RESERVED, redemptions__created_at__gte=fresh),
        distinct=True,
    )


def _status_for(promo, uses: int) -> tuple[str, str]:
    """``PromoCode.status_label``, reading an already-counted ``uses``."""
    if not promo.is_active:
        return "Paused", "neutral"
    if promo.is_expired:
        return "Expired", "danger"
    if promo.is_scheduled:
        return "Scheduled", "warning"
    if promo.max_uses and uses >= promo.max_uses:
        return "Used up", "danger"
    return "Live", "success"


@admin_required
def promos(request):
    codes = list(
        PromoCode.objects.select_related("ai_model")
        .prefetch_related("eligible_plans")
        .annotate(counted_uses=_counted_uses())
        .order_by("-created_at", "-id")
    )
    figures = promo_service.per_code_figures(codes)
    rows = []
    for promo in codes:
        figure = figures.get(promo.pk, {})
        label, tone = _status_for(promo, promo.counted_uses)
        rows.append({
            "promo": promo,
            "status": label,
            "tone": tone,
            "uses": promo.counted_uses,
            "paid_uses": figure.get("n", 0),
            "discount": figure.get("discount") or 0,
            "revenue": figure.get("revenue") or 0,
        })
    return render(
        request,
        "console/promos.html",
        _context("promos", rows=rows, overview=promo_service.overview()),
    )


@admin_required
def promo_form(request, pk: int | None = None):
    promo = get_object_or_404(PromoCode, pk=pk) if pk else None
    form = PromoCodeForm(request.POST or None, instance=promo)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        if promo is None:
            saved.created_by = request.user
        saved.save()
        form.save_m2m()
        audit_service.record(
            AuditAction.SETTINGS_UPDATE, actor=request.user, request=request, target=saved,
            description=f"{'Updated' if promo else 'Created'} promo code {saved.code} ({saved.value_label})",
        )
        messages.success(request, f"Promo code {saved.code} saved.")
        return redirect("console:promo_detail", pk=saved.pk)
    return render(
        request,
        "console/promo_form.html",
        _context("promos", form=form, promo=promo),
    )


@admin_required
def promo_detail(request, pk: int):
    promo = get_object_or_404(PromoCode.objects.select_related("ai_model"), pk=pk)
    redemptions = (
        PromoRedemption.objects.filter(promo=promo)
        .select_related("user", "payment", "payment__plan")
        .order_by("-created_at", "-id")
    )
    page = paginate(request, redemptions, per_page=25)
    figure = promo_service.per_code_figures([promo]).get(promo.pk, {})
    label, tone = promo.status_label
    return render(
        request,
        "console/promo_detail.html",
        _context(
            "promos", promo=promo, page=page, page_numbers=page_range(page),
            redemptions=page.object_list, status=label, tone=tone, uses=promo.uses,
            # Built here: a template cannot prefix a literal onto a number.
            uses_foot=f"of {promo.max_uses:,} allowed" if promo.max_uses else "no overall limit",
            paid_uses=figure.get("n", 0), discount=figure.get("discount") or 0,
            revenue=figure.get("revenue") or 0,
        ),
    )


@admin_required
@require_POST
def promo_toggle(request, pk: int):
    promo = get_object_or_404(PromoCode, pk=pk)
    promo.is_active = not promo.is_active
    promo.save(update_fields=["is_active", "updated_at"])
    audit_service.record(
        AuditAction.SETTINGS_UPDATE, actor=request.user, request=request, target=promo,
        description=f"{'Resumed' if promo.is_active else 'Paused'} promo code {promo.code}",
    )
    messages.success(request, f"{promo.code} is now {'live' if promo.is_active else 'paused'}.")
    # Back to where the button was — but only ever to a promo page, never to a
    # URL supplied by the request.
    target = request.POST.get("next") or ""
    if target == "detail":
        return redirect("console:promo_detail", pk=promo.pk)
    return redirect("console:promos")
