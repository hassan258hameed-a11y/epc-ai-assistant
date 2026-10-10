"""Running an admin-created referral campaign, end to end.

The four moments in a campaign's life, in order:

1. someone opens the link          -> ``record_click``
2. someone signs up with the code  -> ``validate`` then ``attach``
3. they buy something              -> ``quote_discount`` then ``apply_to_payment``
4. the money clears                -> ``on_payment_confirmed``

Each step is separately idempotent, because each is reached from somewhere that
can be retried: a refreshed page, a resubmitted form, a payment webhook that
arrives twice. Paying a reward twice is the failure that matters here, so every
grant goes through the wallet's reference-keyed credit, which the database
refuses to apply a second time.
"""
from __future__ import annotations

import logging
import secrets
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from AI.models import (
    Referral,
    ReferralCampaign,
    ReferralClick,
    RewardTrigger,
    RewardType,
    RewardWho,
    User,
)
from AI.services import model_service, settings_service, wallet_service

logger = logging.getLogger("AI.referrals")

#: Unambiguous in print and over the phone. O, I and S are dropped rather
#: than 0, 1 and 5: dropping the letter of each confusable pair leaves the
#: digit meaning only itself, and keeps more of the alphabet than dropping
#: the digits would.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRTUVWXY3456789"


def generate_code(length: int = 8) -> str:
    """A code that does not already exist."""
    for _ in range(40):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))
        if not ReferralCampaign.objects.filter(code=code).exists():
            return code
    raise RuntimeError("Could not generate an unused referral code.")


def find(code: str) -> ReferralCampaign | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    return (
        ReferralCampaign.objects.filter(code=code)
        .select_related("ai_model")
        .prefetch_related("eligible_plans")
        .first()
    )


def validate(code: str, *, email: str = "") -> tuple[ReferralCampaign | None, str]:
    """Return (campaign, error). An empty code is not an error — it is no code.

    The error strings are written for the person typing the code, so they say
    what to do next rather than which rule was broken.
    """
    if not (code or "").strip():
        return None, ""
    if not settings_service.get("referral_enabled", True):
        return None, "Referral codes are not being accepted at the moment."

    campaign = find(code)
    if campaign is None:
        return None, ""          # not a campaign; may still be a personal code
    if not campaign.is_active:
        return None, "That code is no longer active."
    if not campaign.has_started:
        return None, "That code is not available yet."
    if campaign.is_expired:
        return None, "That code has expired."
    if campaign.is_exhausted:
        return None, "That code has reached its limit."
    return campaign, ""


def record_click(campaign: ReferralCampaign, *, ip: str | None = None, agent: str = "") -> None:
    """Note that the link was opened. Never allowed to fail the page."""
    try:
        ReferralClick.objects.create(
            campaign=campaign, ip_address=ip, user_agent=(agent or "")[:255]
        )
        ReferralCampaign.objects.filter(pk=campaign.pk).update(clicks=F("clicks") + 1)
    except Exception:  # pragma: no cover — a counter must not break a landing page
        logger.exception("could not record referral click for %s", campaign.code)


def _referrer_for(campaign: ReferralCampaign) -> User | None:
    """Who the referrer half is owed to — which, for a campaign, is nobody.

    A campaign code belongs to the organisation. Nobody shared it, so nobody
    earned the referrer's half of the deal. The administrator who created the
    code is the obvious candidate and the wrong one: they would appear in the
    console as having personally referred everyone who read a poster, and would
    collect a reward for it.

    A campaign with ``reward_who`` set to the referrer, or to both, therefore
    pays only the side that exists. The referrer half of the deal belongs to the
    personal-code programme, where there really is a person on the other end.
    """
    return None


@transaction.atomic
def attach(new_user: User, campaign: ReferralCampaign, *, ip: str | None = None) -> Referral | None:
    """Record that this account arrived through this campaign.

    Returns None when the account already has a referral: one account is
    referred once, and a second attempt is a resubmitted signup form rather
    than a second referral.
    """
    if Referral.objects.filter(referred_user=new_user).exists():
        return None

    referrer = _referrer_for(campaign)
    referral = Referral.objects.create(
        referrer=referrer,               # None for an organisation-owned code
        referred_user=new_user,
        code_used=campaign.code,
        campaign=campaign,
        ai_model=campaign.ai_model,
        signup_ip=ip,
        status=(
            Referral.Status.AWAITING_PURCHASE
            if campaign.reward_trigger == RewardTrigger.ON_PURCHASE
            else Referral.Status.PENDING
        ),
        referrer_reward_type=campaign.referrer_reward_type,
        referred_reward_type=campaign.referred_reward_type,
    )
    ReferralCampaign.objects.filter(pk=campaign.pk).update(
        registrations=F("registrations") + 1
    )
    return referral


def _tokens(value: Decimal) -> int:
    return int(value)


def _grant_tokens(user: User, campaign: ReferralCampaign, amount: int, *, referral, role: str) -> int:
    """Credit a token reward, to one model or to every model.

    A campaign with no model means "all models", and a token reward then has to
    be credited to each of them separately — tokens are a per-model currency, so
    a single combined figure would not be spendable anywhere.
    """
    if amount <= 0:
        return 0
    models = [campaign.ai_model] if campaign.ai_model else model_service.catalogue(active_only=True)
    granted = 0
    for ai_model in models:
        try:
            wallet_service.grant_referral_bonus(
                user, ai_model, amount,
                referral_id=referral.pk, role=f"{role}:{campaign.code}",
                note=f"Referral reward — {campaign.code}",
            )
            granted += amount
        except Exception:  # pragma: no cover — one model must not block the rest
            logger.exception("referral token grant failed user=%s model=%s", user.pk, ai_model.key)
    return granted


def quote_discount(user: User, plan) -> tuple[Decimal, str, Referral | None]:
    """What this student's referral takes off this plan, if anything.

    Returns (amount, reason, referral). Zero when there is no referral, when the
    reward is tokens rather than money, when the plan is not eligible, or when
    the discount has already been used — a referral discount is a welcome offer,
    not a standing price.
    """
    zero = (Decimal("0.00"), "", None)
    referral = (
        Referral.objects.filter(referred_user=user)
        .select_related("campaign", "campaign__ai_model")
        .first()
    )
    if referral is None or referral.campaign is None:
        return zero
    campaign = referral.campaign

    if referral.discount_applied and referral.discount_applied > 0:
        return zero                                   # already spent
    if campaign.referred_reward_type not in (RewardType.PERCENT, RewardType.FIXED):
        return zero
    if not campaign.referred_reward_value:
        return zero
    if campaign.is_expired:
        return zero

    # Eligibility: the model, then the specific plans, then the minimum spend.
    if campaign.ai_model_id and plan.ai_model_id != campaign.ai_model_id:
        return zero
    eligible = list(campaign.eligible_plans.all())
    if eligible and plan.pk not in {p.pk for p in eligible}:
        return zero
    if campaign.minimum_purchase and plan.price < campaign.minimum_purchase:
        return zero

    if campaign.referred_reward_type == RewardType.PERCENT:
        amount = (plan.price * campaign.referred_reward_value / Decimal("100"))
        reason = f"{campaign.referred_reward_value.normalize()}% referral discount ({campaign.code})"
    else:
        amount = campaign.referred_reward_value
        reason = f"${campaign.referred_reward_value} referral discount ({campaign.code})"

    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # Never below zero, and never more than the price — a discount larger than
    # the plan would otherwise produce a negative payment.
    amount = min(max(amount, Decimal("0.00")), plan.price)
    if amount <= 0:
        return zero
    return amount, reason, referral


def apply_to_payment(payment, referral: Referral, amount: Decimal, reason: str) -> None:
    """Record the discount on the payment and mark it spent on the referral."""
    payment.discount_amount = amount
    payment.discount_reason = reason[:120]
    payment.save(update_fields=["discount_amount", "discount_reason"])

    Referral.objects.filter(pk=referral.pk).update(discount_applied=amount)
    if referral.campaign_id:
        ReferralCampaign.objects.filter(pk=referral.campaign_id).update(
            discount_given=F("discount_given") + amount
        )


@transaction.atomic
def on_payment_confirmed(payment) -> Referral | None:
    """Settle a campaign referral once the student's money has cleared.

    This is the only place a purchase-triggered reward is paid. It is reached
    from payment confirmation, which can be retried, so it checks the referral's
    own state before doing anything and every grant is reference-keyed on top of
    that.
    """
    referral = (
        Referral.objects.select_for_update()
        .filter(referred_user=payment.user)
        .select_related("campaign", "campaign__ai_model")
        .first()
    )
    if referral is None or referral.campaign is None:
        return None
    if referral.status in (Referral.Status.REWARDED, Referral.Status.CANCELLED,
                           Referral.Status.REJECTED):
        return referral

    campaign = referral.campaign

    # Record the conversion whatever the reward rules say: an administrator
    # wants to know a code produced a sale even when it pays out nothing.
    first_conversion = referral.converted_at is None
    referral.payment = payment
    referral.plan = payment.plan
    referral.purchase_amount = payment.amount
    referral.converted_at = referral.converted_at or timezone.now()
    referral.status = Referral.Status.CONVERTED
    referral.save(update_fields=["payment", "plan", "purchase_amount",
                                 "converted_at", "status"])
    if first_conversion:
        ReferralCampaign.objects.filter(pk=campaign.pk).update(
            conversions=F("conversions") + 1,
            revenue=F("revenue") + payment.amount,
        )

    if campaign.reward_trigger != RewardTrigger.ON_PURCHASE:
        return referral                       # already paid at signup
    if campaign.minimum_purchase and payment.amount < campaign.minimum_purchase:
        return referral                       # bought, but below the threshold
    eligible = list(campaign.eligible_plans.all())
    if eligible and payment.plan_id not in {p.pk for p in eligible}:
        return referral

    return _pay(referral, campaign)


def settle_on_signup(referral: Referral) -> Referral:
    """Pay a signup-triggered campaign reward once the account is verified."""
    campaign = referral.campaign
    if campaign is None or campaign.reward_trigger != RewardTrigger.ON_SIGNUP:
        return referral
    if referral.status == Referral.Status.REWARDED:
        return referral
    return _pay(referral, campaign)


def _pay(referral: Referral, campaign: ReferralCampaign) -> Referral:
    """Hand out whatever the campaign owes, to whichever side it owes it."""
    who = campaign.reward_who
    pay_referrer = who in (RewardWho.REFERRER, RewardWho.BOTH)
    pay_referred = who in (RewardWho.REFERRED, RewardWho.BOTH)

    referrer_tokens = referred_tokens = 0

    if pay_referrer and campaign.referrer_reward_type == RewardType.TOKENS:
        referrer = _referrer_for(campaign)
        if referrer is not None:
            referrer_tokens = _grant_tokens(
                referrer, campaign, _tokens(campaign.referrer_reward_value),
                referral=referral, role="referrer",
            )
    if pay_referred and campaign.referred_reward_type == RewardType.TOKENS:
        referred_tokens = _grant_tokens(
            referral.referred_user, campaign, _tokens(campaign.referred_reward_value),
            referral=referral, role="referred",
        )

    referral.referrer_bonus = referrer_tokens
    referral.referred_bonus = referred_tokens
    referral.status = Referral.Status.REWARDED
    referral.rewarded_at = timezone.now()
    referral.save(update_fields=["referrer_bonus", "referred_bonus", "status", "rewarded_at"])

    total = referrer_tokens + referred_tokens
    if total:
        ReferralCampaign.objects.filter(pk=campaign.pk).update(
            rewards_given=F("rewards_given") + total
        )
    return referral


def expire_stale() -> int:
    """Close out referrals on campaigns that ended before they converted.

    Left alone these sit in "awaiting purchase" for ever, which makes the
    conversion rate on every dashboard slowly meaningless.
    """
    now = timezone.now()
    stale = Referral.objects.filter(
        status=Referral.Status.AWAITING_PURCHASE,
        campaign__expires_at__lt=now,
    )
    return stale.update(status=Referral.Status.EXPIRED)


def overview(scope=None) -> dict:
    """The referral dashboard's headline figures."""
    campaigns = ReferralCampaign.objects.all()
    referrals = Referral.objects.all()
    if scope is not None and not scope.is_all:
        campaigns = campaigns.filter(ai_model=scope.model)
        referrals = referrals.filter(ai_model=scope.model)

    live = [c for c in campaigns if c.is_usable]
    registered = referrals.count()
    converted = referrals.filter(
        status__in=[Referral.Status.CONVERTED, Referral.Status.REWARDED]
    ).count()

    sums = campaigns.aggregate(
        clicks=Sum("clicks"), revenue=Sum("revenue"),
        rewards=Sum("rewards_given"), discount=Sum("discount_given"),
    )
    return {
        "campaigns": campaigns.count(),
        "live": len(live),
        "clicks": sums["clicks"] or 0,
        "registrations": registered,
        "conversions": converted,
        "conversion_rate": int(round(converted / registered * 100)) if registered else 0,
        "revenue": sums["revenue"] or Decimal("0"),
        "tokens_given": sums["rewards"] or 0,
        "discount_given": sums["discount"] or Decimal("0"),
    }


def top_referrers(limit: int = 8) -> list[dict]:
    """People whose personal code brought the most accounts in."""
    rows = (
        Referral.objects.filter(campaign__isnull=True, referrer__isnull=False)
        .values("referrer__id", "referrer__username", "referrer__full_name")
        .annotate(
            total=Count("id"),
            rewarded=Count("id", filter=Q(status=Referral.Status.REWARDED)),
        )
        .order_by("-total")[:limit]
    )
    return [
        {
            "id": row["referrer__id"],
            "name": row["referrer__full_name"] or row["referrer__username"],
            "total": row["total"],
            "rewarded": row["rewarded"],
        }
        for row in rows
    ]
