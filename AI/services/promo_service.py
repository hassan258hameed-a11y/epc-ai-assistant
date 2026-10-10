"""Promo codes: who may use which code on what, and what it takes off.

Every rule is checked twice — once when the student presses *Apply* so the
checkout can show the new total, and again under a row lock at the moment the
payment is created, because the first answer is only a preview and two
students racing for the last use of a code must not both get it.

A redemption's life follows its payment:

* created with the payment → **reserved** (holds a use for 24 hours),
* payment settles → **used**,
* payment is cancelled, fails or is refunded → **released**.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from AI.models import PaymentStatus, PromoCode, PromoRedemption

logger = logging.getLogger("AI.promo")


class PromoError(Exception):
    """A code that cannot be used here, with a reason safe to show."""

    def __init__(self, message: str, *, code: str = "invalid"):
        self.friendly_message = message
        self.code = code
        super().__init__(message)


@dataclass
class Quote:
    promo: PromoCode
    discount: Decimal
    total: Decimal
    reason: str

    def as_payload(self) -> dict:
        return {
            "code": self.promo.code,
            "discount": f"{self.discount:.2f}",
            "total": f"{self.total:.2f}",
            "label": self.promo.value_label,
            "reason": self.reason,
        }


def normalise(raw: str) -> str:
    return "".join((raw or "").split()).upper()[:32]


def quote(user, plan, raw_code: str) -> Quote:
    """What this code takes off this plan for this student — or why it cannot."""
    code = normalise(raw_code)
    if not code:
        raise PromoError("Enter a promo code.", code="empty")
    promo = PromoCode.objects.filter(code=code).select_related("ai_model").first()
    if promo is None or not promo.is_active:
        raise PromoError("That promo code is not valid.", code="unknown")
    return _check(user, plan, promo)


def _check(user, plan, promo: PromoCode) -> Quote:
    now = timezone.now()
    if promo.starts_at and promo.starts_at > now:
        raise PromoError("That promo code is not active yet.", code="scheduled")
    if promo.expires_at and promo.expires_at <= now:
        raise PromoError("That promo code has expired.", code="expired")
    if promo.max_uses and promo.uses >= promo.max_uses:
        raise PromoError("That promo code has been fully used.", code="used_up")
    if promo.per_user_limit:
        mine = promo.counted_redemptions().filter(user=user).count()
        if mine >= promo.per_user_limit:
            raise PromoError("You have already used that promo code.", code="already_used")
    if promo.ai_model_id and plan.ai_model_id != promo.ai_model_id:
        raise PromoError(
            f"That code only works on {promo.ai_model.name} packages.", code="wrong_model",
        )
    eligible = list(promo.eligible_plans.values_list("pk", flat=True))
    if eligible and plan.pk not in eligible:
        raise PromoError("That code does not apply to this package.", code="wrong_plan")
    if promo.min_purchase and plan.price < promo.min_purchase:
        raise PromoError(
            f"That code needs a package of at least ${promo.min_purchase:.2f}.", code="minimum",
        )

    discount = promo.discount_for(plan.price)
    if discount <= 0:
        raise PromoError("That code takes nothing off this package.", code="zero")
    reason = f"Promo {promo.code} — {promo.value_label}"
    return Quote(promo=promo, discount=discount, total=plan.price - discount, reason=reason)


def reserve(payment, user, plan, raw_code: str) -> Quote:
    """Re-check under a lock and attach the code to a new payment.

    Must run inside the transaction that created the payment, so a refusal
    here rolls the payment back too.
    """
    code = normalise(raw_code)
    promo = PromoCode.objects.select_for_update().filter(code=code, is_active=True).first()
    if promo is None:
        raise PromoError("That promo code is not valid.", code="unknown")
    result = _check(user, plan, promo)
    PromoRedemption.objects.create(
        promo=promo, user=user, payment=payment, amount=result.discount,
    )
    return result


def mark_used(payment) -> None:
    PromoRedemption.objects.filter(
        payment=payment, status=PromoRedemption.Status.RESERVED,
    ).update(status=PromoRedemption.Status.USED, updated_at=timezone.now())


def release(payment) -> None:
    PromoRedemption.objects.filter(payment=payment).exclude(
        status=PromoRedemption.Status.RELEASED,
    ).update(status=PromoRedemption.Status.RELEASED, updated_at=timezone.now())


# ---------------------------------------------------------------------------
# Console figures
# ---------------------------------------------------------------------------
def overview() -> dict:
    used = PromoRedemption.objects.filter(status=PromoRedemption.Status.USED)
    totals = used.aggregate(discount=Sum("amount"), revenue=Sum("payment__amount"))
    now = timezone.now()
    live = PromoCode.objects.filter(is_active=True).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    ).filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now))
    return {
        "live": live.count(),
        "redemptions": used.count(),
        "discount": totals["discount"] or Decimal("0"),
        "revenue": totals["revenue"] or Decimal("0"),
    }


def per_code_figures(codes) -> dict[int, dict]:
    rows = (
        PromoRedemption.objects.filter(promo__in=codes, status=PromoRedemption.Status.USED)
        .values("promo_id")
        .annotate(n=Count("id"), discount=Sum("amount"), revenue=Sum("payment__amount"))
    )
    return {row["promo_id"]: row for row in rows}


def is_final_failure(payment) -> bool:
    return payment.status in (PaymentStatus.CANCELLED, PaymentStatus.FAILED, PaymentStatus.REFUNDED)
