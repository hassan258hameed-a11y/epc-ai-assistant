"""Referral campaigns in the console: create them, watch them, close them."""
from __future__ import annotations

from django.contrib import messages as flash
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from AI.forms.referral_forms import ReferralCampaignForm
from AI.models import (
    AuditAction,
    Payment,
    PaymentStatus,
    Referral,
    ReferralCampaign,
)
from AI.services import audit_service, model_scope, referral_campaign_service
from AI.utils.decorators import admin_required
from AI.utils.pagination import page_range, paginate


def _context(active: str, **extra) -> dict:
    context = {
        "nav_active": active,
        "pending_payment_count": Payment.objects.filter(status=PaymentStatus.PENDING).count(),
    }
    context.update(extra)
    return context


@admin_required
def campaigns(request):
    """Every code an administrator has issued, and how each is doing."""
    scope = model_scope.resolve(request)
    queryset = ReferralCampaign.objects.select_related("ai_model", "created_by")
    if not scope.is_all:
        # A campaign restricted to this model, plus the ones that apply to
        # every model — an all-models code is genuinely part of this model's
        # picture, so hiding it would understate what is running.
        queryset = queryset.filter(Q(ai_model=scope.model) | Q(ai_model__isnull=True))

    state = (request.GET.get("state") or "").strip()
    search = (request.GET.get("q") or "").strip()
    if search:
        queryset = queryset.filter(Q(code__icontains=search) | Q(name__icontains=search))

    rows = list(queryset)
    if state:
        rows = [c for c in rows if c.state == state]

    page = paginate(request, rows, per_page=25)
    return render(
        request,
        "console/referral_campaigns.html",
        _context(
            "referral_campaigns",
            page=page,
            page_numbers=page_range(page),
            rows=page.object_list,
            stats=referral_campaign_service.overview(scope),
            state=state,
            search=search,
            states=[
                ("live", "Live"), ("scheduled", "Scheduled"), ("paused", "Paused"),
                ("expired", "Expired"), ("exhausted", "Fully used"),
            ],
        ),
    )


@admin_required
def campaign_create(request):
    initial = {"code": referral_campaign_service.generate_code()}
    form = ReferralCampaignForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        campaign = form.save(commit=False)
        campaign.created_by = request.user
        campaign.save()
        form.save_m2m()
        audit_service.record(
            AuditAction.SETTINGS_UPDATE, actor=request.user, request=request, target=campaign,
            description=f"Created referral campaign {campaign.code}",
            new_value=(
                f"reward_who={campaign.reward_who}, trigger={campaign.reward_trigger}, "
                f"referrer={campaign.referrer_reward_type}:{campaign.referrer_reward_value}, "
                f"referred={campaign.referred_reward_type}:{campaign.referred_reward_value}, "
                f"model={campaign.ai_model.key if campaign.ai_model else 'all'}"
            ),
        )
        flash.success(request, f"Referral code {campaign.code} is ready to share.")
        return redirect("console:referral_campaigns")

    return render(
        request,
        "console/referral_campaign_form.html",
        _context("referral_campaigns", form=form, campaign=None),
    )


@admin_required
def campaign_edit(request, pk: int):
    campaign = get_object_or_404(ReferralCampaign, pk=pk)
    before = (
        f"active={campaign.is_active}, limit={campaign.usage_limit}, "
        f"expires={campaign.expires_at}, "
        f"referred={campaign.referred_reward_type}:{campaign.referred_reward_value}"
    )
    form = ReferralCampaignForm(request.POST or None, instance=campaign)
    if request.method == "POST" and form.is_valid():
        updated = form.save()
        audit_service.record(
            AuditAction.SETTINGS_UPDATE, actor=request.user, request=request, target=updated,
            description=f"Edited referral campaign {updated.code}",
            old_value=before,
            new_value=(
                f"active={updated.is_active}, limit={updated.usage_limit}, "
                f"expires={updated.expires_at}, "
                f"referred={updated.referred_reward_type}:{updated.referred_reward_value}"
            ),
        )
        flash.success(request, f"{updated.code} has been updated.")
        return redirect("console:referral_campaigns")

    return render(
        request,
        "console/referral_campaign_form.html",
        _context("referral_campaigns", form=form, campaign=campaign),
    )


@admin_required
@require_POST
def campaign_toggle(request, pk: int):
    """Pause or resume a code without deleting anything.

    Deliberately never a delete: the referrals a code produced are history, and
    the students who used it keep what they were given. Pausing stops new
    signups from using it and leaves everything else intact.
    """
    campaign = get_object_or_404(ReferralCampaign, pk=pk)
    campaign.is_active = not campaign.is_active
    campaign.save(update_fields=["is_active", "updated_at"])
    audit_service.record(
        AuditAction.SETTINGS_UPDATE, actor=request.user, request=request, target=campaign,
        description=f"{'Resumed' if campaign.is_active else 'Paused'} referral campaign {campaign.code}",
        old_value=str(not campaign.is_active),
        new_value=str(campaign.is_active),
    )
    flash.success(
        request,
        f"{campaign.code} is now {'live' if campaign.is_active else 'paused'}.",
    )
    return redirect("console:referral_campaigns")


@admin_required
def campaign_detail(request, pk: int):
    """One code: its rules, its numbers, and everyone who used it."""
    campaign = get_object_or_404(
        ReferralCampaign.objects.select_related("ai_model", "created_by")
        .prefetch_related("eligible_plans"),
        pk=pk,
    )
    referrals = (
        Referral.objects.filter(campaign=campaign)
        .select_related("referred_user", "referrer", "plan", "payment", "ai_model")
    )
    page = paginate(request, referrals.order_by("-created_at", "-id"), per_page=30)

    return render(
        request,
        "console/referral_campaign_detail.html",
        _context(
            "referral_campaigns",
            campaign=campaign,
            page=page,
            page_numbers=page_range(page),
            recent_clicks=campaign.click_log.all()[:10],
            funnel={
                "clicks": campaign.clicks,
                "registrations": campaign.registrations,
                "conversions": campaign.conversions,
                "rewarded": referrals.filter(status=Referral.Status.REWARDED).count(),
            },
        ),
    )
