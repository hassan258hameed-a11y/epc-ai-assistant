"""Referral programme and the new-account welcome grant."""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from AI.models import Referral, User
from AI.services import (
    model_service,
    notification_service,
    settings_service,
    token_service,
    wallet_service,
)

logger = logging.getLogger("AI.referrals")


def find_referrer(code: str) -> User | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    return User.objects.filter(referral_code=code, is_active=True).first()


def resolve_code(code: str, *, email: str = "") -> tuple[User | None, object | None, str]:
    """Work out what a typed code is: a person's, a campaign's, or nothing.

    Returns ``(referrer, campaign, error)``. Two kinds of code share one field
    on the signup form because they are one thing to the person typing it —
    which of the two it turns out to be is the platform's problem, not theirs.

    Campaigns are checked first: an administrator who deliberately issues a code
    should not be able to be shadowed by a personal code that happens to match.
    """
    code = (code or "").strip().upper()
    if not code:
        return None, None, ""
    if not settings_service.get("referral_enabled", True):
        return None, None, "Referral codes are not being accepted at the moment."

    from AI.services import referral_campaign_service

    campaign, error = referral_campaign_service.validate(code, email=email)
    if error:
        return None, None, error
    if campaign is not None:
        return None, campaign, ""

    referrer = find_referrer(code)
    if referrer is None:
        return None, None, "That referral code is not valid."
    if email and referrer.email and referrer.email.lower() == email.lower():
        return None, None, "You cannot refer yourself."
    return referrer, None, ""


def validate_code(code: str, *, email: str = "") -> tuple[User | None, str]:
    """Personal codes only. Kept for callers that predate campaigns."""
    referrer, _campaign, error = resolve_code(code, email=email)
    return referrer, error


@transaction.atomic
def register_referral(new_user: User, referrer: User, *, ip: str | None = None) -> Referral | None:
    """Create the pending referral link at signup time.

    Bonuses are only paid once the new account is verified — see
    :func:`reward_referral`.
    """
    if referrer is None or referrer.pk == new_user.pk:
        return None
    if Referral.objects.filter(referred_user=new_user).exists():
        return None

    new_user.referred_by = referrer
    new_user.save(update_fields=["referred_by"])

    try:
        return Referral.objects.create(
            referrer=referrer,
            referred_user=new_user,
            code_used=referrer.referral_code,
            signup_ip=ip,
        )
    except IntegrityError:
        return None


def reward_referral(new_user: User) -> Referral | None:
    """Pay both sides once — guarded by idempotency keys in the ledger."""
    referral = (
        Referral.objects.filter(referred_user=new_user)
        .select_related("referrer", "campaign")
        .first()
    )
    if referral is None or referral.status == Referral.Status.REWARDED:
        return referral
    if not settings_service.get("referral_enabled", True):
        return referral

    # A campaign referral is settled by its own rules — which reward, to which
    # side, at which moment. Falling through to the global bonus here would pay
    # a campaign signup twice: once by its campaign and once by the platform
    # default it was created to override.
    if referral.campaign_id:
        from AI.services import referral_campaign_service

        return referral_campaign_service.settle_on_signup(referral)

    referrer_bonus = int(settings_service.get("referral_bonus_referrer", 0))
    referred_bonus = int(settings_service.get("referral_bonus_referred", 0))

    # Bonuses are paid in the default model's tokens: a bonus has to be spendable
    # somewhere specific, and the default is the model everyone can reach.
    landing = model_service.default_model()
    if landing is None:
        return referral

    if referrer_bonus > 0:
        wallet_service.grant_referral_bonus(
            referral.referrer,
            landing,
            referrer_bonus,
            referral_id=referral.pk,
            role="referrer",
            note=f"Referral bonus — {new_user.display_name} joined",
        )
    if referred_bonus > 0:
        wallet_service.grant_referral_bonus(
            new_user,
            landing,
            referred_bonus,
            referral_id=referral.pk,
            role="referred",
            note=f"Welcome bonus — invited by {referral.referrer.display_name}",
        )

    referral.status = Referral.Status.REWARDED
    referral.referrer_bonus = referrer_bonus
    referral.referred_bonus = referred_bonus
    referral.rewarded_at = timezone.now()
    referral.save(update_fields=["status", "referrer_bonus", "referred_bonus", "rewarded_at"])

    if referrer_bonus > 0:
        try:
            notification_service.referral_rewarded(
                referral.referrer, referrer_bonus, referred_name=new_user.display_name
            )
        except Exception:  # pragma: no cover
            logger.exception("Referral notification failed")

    return referral


def welcome_model_name() -> str:
    """The model the free trial is paid in, for "3,000 free GPT-5 Mini tokens"."""
    ai_model = model_service.default_model()
    return ai_model.name if ai_model is not None and ai_model.auto_grant else ""


def grant_welcome_tokens(user: User) -> int:
    """Credit the free trial once per account, to the default model only.

    Tokens are a separate currency per model, and the trial is a taste of the
    platform's everyday model (GPT-5 Mini) rather than a slice of every model:
    every other model starts at zero until the student buys a plan or an
    administrator grants tokens. The new chat opens on the same model, so the
    trial tokens are the ones the meter shows. The grant carries its own
    reference, so re-running this is a no-op.

    Returns the amount credited, which is what the welcome message quotes.
    """
    if not settings_service.get("free_trial_enabled", True):
        return 0
    amount = int(settings_service.get("free_trial_tokens", 0))
    if amount <= 0:
        return 0

    ai_model = model_service.default_model()
    # A model whose supply is distributed by hand is never handed out by a
    # signup. Its tokens are a block the organisation bought, and giving
    # every new account a slice of one is the administrator's decision.
    if ai_model is None or not ai_model.auto_grant:
        return 0
    try:
        result = wallet_service.grant_free_trial(user, ai_model, amount)
    except Exception:  # pragma: no cover — a failed grant must not block the sign-up
        logger.exception("Welcome grant failed for user=%s model=%s", user.pk, ai_model.key)
        return 0
    return amount if result.created else 0


def onboard(user: User) -> dict:
    """Everything that happens the moment an account becomes verified."""
    trial = grant_welcome_tokens(user)
    referral = reward_referral(user)
    return {
        "free_trial_tokens": trial,
        "referral_bonus": referral.referred_bonus if referral else 0,
        "balance": token_service.balance_of(user),
    }


def stats_for(user: User) -> dict:
    referrals = Referral.objects.filter(referrer=user).select_related("referred_user")
    rewarded = [r for r in referrals if r.status == Referral.Status.REWARDED]
    return {
        "total": len(referrals),
        "rewarded": len(rewarded),
        "pending": len(referrals) - len(rewarded),
        "tokens_earned": sum(r.referrer_bonus for r in rewarded),
        "referrals": referrals,
        "bonus_referrer": int(settings_service.get("referral_bonus_referrer", 0)),
        "bonus_referred": int(settings_service.get("referral_bonus_referred", 0)),
    }
