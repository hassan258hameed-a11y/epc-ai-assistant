"""Attempts at a hosted gateway: one per press of "Pay".

A hosted gateway remembers every reference it has been sent. ABA PayWay
refuses a ``tran_id`` it has seen ("Duplicated transaction ID", code 4) and an
ACLEDA XPAY session cancels itself if its form is posted twice. So an order is
paid through *attempts*, each with a reference of its own, and checking an
order means checking its attempts — the newest every time, the older ones now
and then, because a student can finish an older one in another tab.
"""
from __future__ import annotations

from django.core.cache import cache

from AI.models import PaymentAttempt

#: More than this and the order is stale anyway: the student starts a new one.
MAX_ATTEMPTS = 20
#: Seconds between checks of an order's older attempts. The newest is checked
#: on every poll; older ones only matter for a student who went back to one.
OLDER_CHECK_SECONDS = 60
#: How many older attempts are worth asking about at all.
OLDER_LIMIT = 5


def compact(reference: str) -> str:
    """"EPC-260928-1A2B3C4D" → "EPC2609281A2B3C4D"."""
    return "".join(ch for ch in reference if ch.isalnum()).upper()


def count(payment) -> int:
    return PaymentAttempt.objects.filter(payment=payment).count()


def record(payment, *, provider_ref: str, token: str = "", session_id: str = "",
           data: dict | None = None) -> PaymentAttempt:
    return PaymentAttempt.objects.create(
        payment=payment, method=payment.method, provider_ref=provider_ref[:64],
        token=(token or "")[:200], session_id=(session_id or "")[:200], data=data or {},
    )


def to_check(payment) -> list[PaymentAttempt]:
    """The attempts worth asking the gateway about right now, newest first."""
    attempts = list(PaymentAttempt.objects.filter(payment=payment).order_by("-created_at", "-id")[:OLDER_LIMIT + 1])
    if len(attempts) <= 1:
        return attempts
    if cache.add(f"payments:older:{payment.pk}", 1, timeout=OLDER_CHECK_SECONDS):
        return attempts
    return attempts[:1]


def find(provider_ref: str = "", *, token: str = "") -> PaymentAttempt | None:
    query = PaymentAttempt.objects.select_related("payment", "payment__plan", "payment__user")
    if provider_ref:
        return query.filter(provider_ref=provider_ref).first()
    if token:
        return query.filter(token=token).first()
    return None
