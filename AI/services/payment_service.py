"""Payment lifecycle: create → verify → settle.

Settlement is the only path that credits tokens or activates a subscription, it
runs inside a locked transaction and it is idempotent — a webhook may arrive
twice, an admin may click confirm twice, and the student is credited once.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from AI.models import (
    Payment,
    PaymentEvent,
    PaymentMethod,
    PaymentStatus,
    Plan,
    Subscription,
)
from AI.services import (
    notification_service,
    promo_service,
    referral_campaign_service,
    settings_service,
    token_service,
    wallet_service,
)
from AI.services.payments import PaymentError, get_provider

logger = logging.getLogger("AI.payments")


def log_event(payment: Payment, event_type: str, message: str = "", data: dict | None = None) -> PaymentEvent:
    return PaymentEvent.objects.create(
        payment=payment, event_type=event_type, message=message[:255], data=data or {}
    )


#: A KHR charge is rounded up to whole hundreds of riel — the smallest note in
#: everyday use — so the amount on the QR is one people can actually pay.
KHR_ROUNDING = 100
#: Seconds after an order's expiry before it is closed: a payment made in the
#: last moment still gets the chance to be confirmed.
EXPIRY_GRACE_SECONDS = 120


def khr_rate() -> Decimal:
    return Decimal(str(max(1, int(settings_service.get("payment_khr_rate", 4100)))))


def quote_charge(method: str, amount: Decimal, currency: str = "USD") -> tuple[Decimal, str, Decimal | None]:
    """What ``amount`` (in the plan's currency) costs through ``method``.

    Returns ``(charge_amount, charge_currency, exchange_rate)``. USDT is taken
    one-to-one with the dollar; riel uses the administrator's rate, rounded up
    to a whole hundred.
    """
    charge_currency = get_provider(method).charge_currency.upper()
    if charge_currency == "KHR" and currency.upper() == "USD":
        rate = khr_rate()
        riel = math.ceil(Decimal(amount) * rate / KHR_ROUNDING) * KHR_ROUNDING
        return Decimal(riel), "KHR", rate
    return Decimal(amount).quantize(Decimal("0.01")), charge_currency, None


def expiry_minutes() -> int:
    return max(3, int(settings_service.get("payment_expiry_minutes", 15)))


def open_payment_for(user, plan: Plan, method: str) -> Payment | None:
    """An unpaid, unexpired order for the same plan and method, if one exists.

    Pressing "Continue" twice, or going back and forward, finds the order
    already made instead of opening a second one to pay by mistake.
    """
    return (
        Payment.objects.filter(
            user=user, plan=plan, method=method, status=PaymentStatus.PENDING,
            expires_at__gt=timezone.now(), discount_amount=0,
        )
        .order_by("-created_at")
        .first()
    )


def create_payment(user, plan: Plan, method: str, *, note: str = "", promo_code: str = "") -> Payment:
    if method not in dict(PaymentMethod.choices):
        raise PaymentError("That payment method is not available.", code="unknown_method")
    if not plan.is_active:
        raise PaymentError("That plan is no longer available.", code="plan_inactive")

    # A referral campaign may take something off the price. Quoted here, at the
    # moment the payment is created, so the student is asked for the amount they
    # will actually be charged rather than being surprised at the gateway.
    # The tokens granted are unaffected: a discount changes the price, not the
    # size of the package.
    discount, reason, referral = referral_campaign_service.quote_discount(user, plan)

    # A promo code is the other discount. One per order: whichever takes more
    # off wins, and a referral discount that loses stays unspent for later.
    code = promo_service.normalise(promo_code)
    use_promo = False
    if code:
        try:
            preview = promo_service.quote(user, plan, code)
        except promo_service.PromoError as exc:
            raise PaymentError(exc.friendly_message, code=f"promo_{exc.code}")
        use_promo = preview.discount >= discount

    with transaction.atomic():
        payment = Payment.objects.create(
            user=user,
            plan=plan,
            amount=plan.price if use_promo else plan.price - discount,
            currency=plan.currency,
            method=method,
            tokens_to_grant=plan.tokens,
            payer_note=note[:255],
        )
        if use_promo:
            # Re-checked under a lock: the preview above could be seconds old,
            # and the last use of a code must go to exactly one student.
            try:
                applied = promo_service.reserve(payment, user, plan, code)
            except promo_service.PromoError as exc:
                raise PaymentError(exc.friendly_message, code=f"promo_{exc.code}")
            payment.amount = applied.total
            payment.discount_amount = applied.discount
            payment.discount_reason = applied.reason[:120]
            payment.save(update_fields=["amount", "discount_amount", "discount_reason"])
            log_event(payment, "discounted", applied.reason, {"discount": str(applied.discount)})
        elif discount > 0 and referral is not None:
            referral_campaign_service.apply_to_payment(payment, referral, discount, reason)
            log_event(payment, "discounted", reason, {"discount": str(discount)})

        # What the gateway will actually ask for, fixed now: a rate changed
        # later must never alter an order that is already open.
        charge, charge_currency, rate = quote_charge(method, payment.amount, payment.currency)
        payment.charge_amount, payment.charge_currency, payment.exchange_rate = charge, charge_currency, rate
        payment.save(update_fields=["charge_amount", "charge_currency", "exchange_rate"])
        log_event(payment, "created", f"{plan.name} via {method} — {payment.pay_display}")
    return payment


def start_checkout(payment: Payment):
    provider = get_provider(payment.method)
    if payment.expires_at is None:
        payment.expires_at = timezone.now() + timedelta(minutes=expiry_minutes())
        payment.save(update_fields=["expires_at", "updated_at"])
    session = provider.create_checkout(payment)
    payment.checkout_payload = session.to_dict()
    fields = ["checkout_payload", "updated_at"]
    if session.kind == "manual":
        # A bank transfer confirmed by hand takes longer than a QR scan.
        payment.expires_at = timezone.now() + timedelta(hours=24)
        fields.append("expires_at")
    if session.order_id:
        payment.provider_order_id = session.order_id[:120]
        fields.append("provider_order_id")
    payment.save(update_fields=fields)
    log_event(payment, "checkout_started", f"kind={session.kind}", {"order_id": session.order_id})
    return session


def start_attempt(payment: Payment, *, request=None, hosted: bool = False):
    """One press of "Pay" on a hosted gateway: a fresh, signed hand-off.

    Refused for an order that can no longer be paid — the gateway would take
    the money for an order we would then have to refund by hand.
    """
    if payment.status != PaymentStatus.PENDING:
        raise PaymentError("This order can no longer be paid. Start a new one.", code="not_payable")
    if payment.expires_at and payment.expires_at <= timezone.now():
        raise PaymentError("This order has expired. Start a new one.", code="expired")
    handoff = get_provider(payment.method).start_attempt(payment, request=request, hosted=hosted)
    log_event(payment, "attempt_started", f"Sent to the gateway as {handoff.reference}",
              {"reference": handoff.reference})
    return handoff


def verify_payment(payment: Payment) -> Payment:
    """Ask the provider for the truth and act on it.

    An expired order is still asked: money that arrived in the last moment is
    real, and is credited. A cancelled, paid or refunded one is left alone.
    """
    if payment.status in {PaymentStatus.PAID, PaymentStatus.REFUNDED, PaymentStatus.CANCELLED}:
        return payment

    provider = get_provider(payment.method)
    result = provider.verify(payment)
    log_event(payment, "verified", result.message or result.status, {"status": result.status})
    return apply_result(payment, result, source="provider")


def apply_result(payment: Payment, result, *, source: str) -> Payment:
    """Settle, fail or expire ``payment`` on what the gateway said."""
    if result.is_paid:
        return confirm(payment, result, source=source)
    if payment.status != PaymentStatus.PENDING:
        return payment
    if result.status == "expired":
        return mark_expired(payment, result.message or "The payment window closed.")
    if result.status in {"failed", "cancelled"}:
        return mark_failed(payment, result.message or "The payment did not complete.")
    return expire_if_due(payment)


def expire_if_due(payment: Payment) -> Payment:
    """Expire an open order whose window (plus grace) has passed.

    One carrying transfer evidence is left for an administrator instead.
    """
    if (
        payment.status == PaymentStatus.PENDING and payment.expires_at and not payment.proof
        and timezone.now() > payment.expires_at + timedelta(seconds=EXPIRY_GRACE_SECONDS)
    ):
        return mark_expired(payment, "No payment arrived before the order expired.")
    return payment


def confirm(payment: Payment, result, *, source: str) -> Payment:
    """Settle on a paid result — once it has been checked against the order.

    Two things stop a confirmation: a figure that does not match what was
    asked for, and a gateway transaction that already paid for another
    order (the same payment presented twice). Either leaves the order pending
    and flagged for an administrator; neither ever credits tokens.
    """
    provider = get_provider(payment.method)
    if not provider.result_matches(payment, result):
        note = f"Amount mismatch: {result.amount} {result.currency} reported, {payment.pay_display} expected."
        log_event(payment, "amount_mismatch", note, {"amount": result.amount, "currency": result.currency})
        Payment.objects.filter(pk=payment.pk).update(admin_note=note[:255])
        logger.error("%s for %s", note, payment.reference)
        return payment
    if result.provider_txn_id and Payment.objects.filter(
        method=payment.method, provider_txn_id=result.provider_txn_id, status=PaymentStatus.PAID,
    ).exclude(pk=payment.pk).exists():
        note = f"Transaction {result.provider_txn_id} already paid for another order."
        log_event(payment, "duplicate_transaction", note)
        Payment.objects.filter(pk=payment.pk).update(admin_note=note[:255])
        logger.error("%s (%s)", note, payment.reference)
        return payment
    return settle(payment, provider_txn_id=result.provider_txn_id, payload=result.raw, source=source)


@transaction.atomic
def settle(
    payment: Payment,
    *,
    provider_txn_id: str = "",
    payload: dict | None = None,
    source: str = "provider",
    actor=None,
    note: str = "",
) -> Payment:
    """Mark paid, credit tokens and activate the subscription — exactly once."""
    locked = Payment.objects.select_for_update().select_related("plan", "user").get(pk=payment.pk)

    if locked.status == PaymentStatus.PAID and locked.tokens_credited:
        log_event(locked, "settle_skipped", "Already settled — ignoring duplicate confirmation.")
        return locked

    locked.status = PaymentStatus.PAID
    locked.paid_at = locked.paid_at or timezone.now()
    if provider_txn_id:
        locked.provider_txn_id = provider_txn_id[:120]
    if payload:
        locked.provider_payload = payload
    if actor is not None:
        locked.verified_by = actor
    if note:
        locked.admin_note = note[:255]

    # Tokens are per model, so a purchase credits the wallet for the model the
    # plan was sold for — never a shared balance that could be spent anywhere.
    token_model = locked.plan.token_model
    if token_model is None:
        raise PaymentError(
            "No AI model is configured to receive these tokens.", code="no_token_model"
        )

    # A plan grants one model's tokens by default and several when it is a
    # bundle. Each model is credited separately because tokens are a separate
    # currency per model — there is no combined balance to add them to. The
    # reference key includes the model, so a payment settled twice still
    # credits each wallet exactly once.
    ledger = None
    for row in locked.plan.grant_rows:
        if row["tokens"] <= 0:
            continue
        result = wallet_service.credit_payment(
            locked, row["model"], tokens=row["tokens"]
        )
        if row["is_primary"]:
            ledger = result
    if ledger is None:                       # a plan whose primary grants zero
        ledger = wallet_service.credit_payment(locked, token_model)

    locked.tokens_credited = True
    locked.save()
    promo_service.mark_used(locked)

    subscription = _activate_subscription(locked)

    log_event(
        locked,
        "settled",
        f"Credited {locked.tokens_to_grant:,} tokens via {source}.",
        {"balance_after": ledger.balance_after, "subscription_id": subscription.pk if subscription else None},
    )

    # A campaign that pays out on purchase settles here, inside the same
    # transaction that credited the tokens: if the credit rolls back, so does
    # the reward. It is idempotent, so a webhook delivered twice pays once.
    try:
        referral_campaign_service.on_payment_confirmed(locked)
    except Exception:  # pragma: no cover — a reward must not void a payment
        logger.exception("referral settlement failed for payment=%s", locked.pk)

    transaction.on_commit(lambda: _post_settlement(locked))
    return locked


def _post_settlement(payment: Payment) -> None:
    try:
        notification_service.reset_low_balance_alerts(payment.user)
        from AI.services import payment_notice_service

        payment_notice_service.confirmed(payment)
    except Exception:  # pragma: no cover — never fail a payment over a notification
        logger.exception("Post-settlement notification failed for %s", payment.reference)


def _activate_subscription(payment: Payment) -> Subscription | None:
    existing = Subscription.objects.filter(payment=payment).first()
    if existing:
        return existing

    now = timezone.now()
    # Stack renewals: extend from the current expiry when one is still running.
    current = (
        Subscription.objects.filter(
            user=payment.user, status=Subscription.Status.ACTIVE, ends_at__gt=now
        )
        .order_by("-ends_at")
        .first()
    )
    starts_at = current.ends_at if current else now
    return Subscription.objects.create(
        user=payment.user,
        plan=payment.plan,
        payment=payment,
        tokens_granted=payment.tokens_to_grant,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(days=payment.plan.duration_days or 30),
    )


@transaction.atomic
def mark_failed(payment: Payment, reason: str = "", *, actor=None) -> Payment:
    """Mark a payment failed — never one that has already been settled.

    The row is re-read under a lock so a stale in-memory copy can never
    downgrade a paid payment.
    """
    locked = Payment.objects.select_for_update().get(pk=payment.pk)
    if locked.status == PaymentStatus.PAID:
        return locked

    was_pending = locked.status == PaymentStatus.PENDING
    locked.status = PaymentStatus.FAILED
    locked.admin_note = (reason or locked.admin_note)[:255]
    if actor is not None:
        locked.verified_by = actor
    locked.save(update_fields=["status", "admin_note", "verified_by", "updated_at"])
    promo_service.release(locked)
    log_event(locked, "failed", reason)
    if was_pending:
        transaction.on_commit(lambda: _notice(locked, "failed", reason))
    return locked


@transaction.atomic
def mark_expired(payment: Payment, reason: str = "") -> Payment:
    """Close an order nobody paid in time. Never a paid one."""
    locked = Payment.objects.select_for_update().get(pk=payment.pk)
    if locked.status != PaymentStatus.PENDING:
        return locked
    locked.status = PaymentStatus.EXPIRED
    locked.save(update_fields=["status", "updated_at"])
    promo_service.release(locked)
    log_event(locked, "expired", reason or "The payment window closed.")
    transaction.on_commit(lambda: _close_and_notice(locked, reason))
    return locked


def _close_and_notice(payment: Payment, reason: str) -> None:
    try:
        get_provider(payment.method).close_order(payment)
    except Exception:  # pragma: no cover — closing is a courtesy to the gateway
        logger.exception("Closing order at the gateway failed for %s", payment.reference)
    _notice(payment, "expired", reason)


def _notice(payment: Payment, outcome: str, reason: str) -> None:
    try:
        from AI.services import payment_notice_service

        payment_notice_service.not_completed(payment, outcome, reason)
    except Exception:  # pragma: no cover
        logger.exception("Payment notice failed for %s", payment.reference)


def expire_stale(limit: int = 200) -> int:
    """Close pending orders whose window has passed — after one last check.

    The last check matters: a student who paid at minute fourteen and closed
    the tab is credited here, not expired.
    """
    cutoff = timezone.now() - timedelta(seconds=EXPIRY_GRACE_SECONDS)
    closed = 0
    for payment in Payment.objects.filter(status=PaymentStatus.PENDING, expires_at__lt=cutoff).select_related(
        "plan", "user",
    )[:limit]:
        try:
            if get_provider(payment.method).is_configured:
                payment = verify_payment(payment)
        except PaymentError as exc:
            log_event(payment, "verify_failed", exc.friendly_message)
        if payment.status == PaymentStatus.PENDING:
            if (payment.proof and settings_service.get("payment_manual_review", True)):
                continue          # evidence is waiting for an administrator
            payment = mark_expired(payment, "No payment arrived before the order expired.")
        if payment.status == PaymentStatus.EXPIRED:
            closed += 1
    return closed


def sweep_pending(limit: int = 25) -> int:
    """Check open orders whose confirmation may never be pushed to us.

    Bakong never calls back; PayWay's pushback, ACLEDA's callback and Binance's
    webhook can only reach a public server (and a webhook can be lost). The payment page polls while it is open; this
    catches the student who paid and closed it. Returns how many were settled.
    """
    settled = 0
    recent = timezone.now() - timedelta(hours=6)
    polled = (
        Q(method__in=[PaymentMethod.ACLEDA_KHQR, PaymentMethod.BINANCE]) & ~Q(provider_order_id="")
    ) | Q(method__in=[PaymentMethod.ABA_PAYWAY, PaymentMethod.ACLEDA_XPAY], attempts__isnull=False)
    for payment in Payment.objects.filter(
        polled, status=PaymentStatus.PENDING, created_at__gte=recent,
    ).distinct().select_related("plan", "user")[:limit]:
        try:
            if verify_payment(payment).status == PaymentStatus.PAID:
                settled += 1
        except PaymentError:
            continue
    return settled


def sweep_throttled() -> None:
    """``sweep_pending`` and ``expire_stale`` at most once a minute, for callers on
    the request path — so nothing depends on a scheduler being set up.

    They talk to gateways, so they run on a background thread: a slow bank
    must never make somebody's page slow.
    """
    if not settings.PAYMENT_SWEEP_ON_REQUEST or not cache.add("payments:sweep", 1, timeout=60):
        return
    import threading

    def run():
        from django.db import connections

        try:
            sweep_pending()
            expire_stale()
        except Exception:  # pragma: no cover — never break anything over this
            logger.exception("Payment sweep failed")
        finally:
            connections.close_all()

    threading.Thread(target=run, name="payment-sweep", daemon=True).start()


@transaction.atomic
def cancel(payment: Payment, *, actor=None) -> Payment:
    """Cancel an unsettled payment. A paid one must be refunded instead."""
    locked = Payment.objects.select_for_update().get(pk=payment.pk)
    if locked.status == PaymentStatus.PAID:
        raise PaymentError("A completed payment cannot be cancelled — issue a refund instead.", code="already_paid")
    if locked.status == PaymentStatus.REFUNDED:
        raise PaymentError("A refunded payment cannot be cancelled.", code="already_refunded")

    locked.status = PaymentStatus.CANCELLED
    if actor is not None:
        locked.verified_by = actor
    locked.save(update_fields=["status", "verified_by", "updated_at"])
    promo_service.release(locked)
    log_event(locked, "cancelled", "Cancelled by user or admin.")
    transaction.on_commit(lambda: get_provider(locked.method).close_order(locked))
    return locked


@transaction.atomic
def refund(payment: Payment, *, actor=None, note: str = "") -> Payment:
    """Reverse a settled payment: pull back tokens and end the subscription."""
    locked = Payment.objects.select_for_update().select_related("user", "plan").get(pk=payment.pk)
    if locked.status != PaymentStatus.PAID:
        raise PaymentError("Only a paid transaction can be refunded.", code="not_paid")

    # Reversing a purchase pulls the tokens back out of every wallet they were
    # credited to — each model of a bundle, not only the plan's main one —
    # clamped at zero if the student has already spent some. The ledger is the
    # record of what settlement actually credited, so it is what is reversed:
    # re-reading the plan would reverse today's bundle, which may not be the
    # one that was bought.
    removed = 0
    for model, amount in credited_by_payment(locked):
        result = wallet_service.refund_payment(
            _with_amount(locked, amount), model, performed_by=actor
        )
        removed += -int(result.amount or 0)

    Subscription.objects.filter(payment=locked).update(
        status=Subscription.Status.CANCELLED, updated_at=timezone.now()
    )

    locked.status = PaymentStatus.REFUNDED
    locked.admin_note = (note or "Refunded")[:255]
    locked.verified_by = actor
    locked.save(update_fields=["status", "admin_note", "verified_by", "updated_at"])
    promo_service.release(locked)
    log_event(locked, "refunded", note or "Refunded by admin.")

    locked.tokens_removed = removed
    transaction.on_commit(
        lambda: notification_service.notify(
            locked.user,
            "Payment refunded",
            f"{locked.reference} has been refunded. "
            + (
                f"{removed:,} tokens were removed from your balance."
                if removed else "No tokens were left to remove from your balance."
            ),
            level="warning",
            category="payment",
            telegram=True,
        )
    )
    return locked


def credited_by_payment(payment: Payment) -> list[tuple]:
    """``[(model, tokens)]`` — what settling ``payment`` put into each wallet.

    Read from the per-model ledger, where each credit is keyed by payment and
    model. A payment settled before that ledger existed has no rows there, so
    it falls back to the plan's main model and the payment's own figure, which
    is exactly what was credited in those days.
    """
    from AI.models import AITokenTransaction, WalletTransactionType

    rows: dict[int, list] = {}
    for entry in (
        AITokenTransaction.objects.filter(
            payment=payment, transaction_type=WalletTransactionType.PLAN_PURCHASE, amount__gt=0,
        )
        .select_related("ai_model")
        .order_by("id")
    ):
        slot = rows.setdefault(entry.ai_model_id, [entry.ai_model, 0])
        slot[1] += int(entry.amount)
    if rows:
        return [(model, amount) for model, amount in rows.values() if amount > 0]
    token_model = payment.plan.token_model
    if token_model is None or not payment.tokens_to_grant:
        return []
    return [(token_model, int(payment.tokens_to_grant))]


def _with_amount(payment: Payment, amount: int) -> Payment:
    """A copy of ``payment`` whose ``tokens_to_grant`` is one model's share.

    ``wallet_service.refund_payment`` reverses ``tokens_to_grant`` — the whole
    order for a single-model plan. A bundle credited each model its own figure,
    so each reversal is handed that figure instead. The copy is never saved.
    """
    import copy

    clone = copy.copy(payment)
    clone.tokens_to_grant = int(amount)
    return clone


def find_by_reference(reference: str) -> Payment | None:
    """Resolve a provider reference, tolerating stripped separators."""
    reference = (reference or "").strip()
    if not reference:
        return None
    payment = Payment.objects.filter(reference=reference).select_related("plan", "user").first()
    if payment:
        return payment
    # One attempt at a hosted gateway: PayWay's tran_id, XPAY's txid.
    from AI.services.payments import attempts

    attempt = attempts.find(reference)
    if attempt is not None:
        return attempt.payment
    # Binance trade numbers are the reference without its separators:
    # "EPC260927AB12CD34" is "EPC-260927-AB12CD34".
    compact = "".join(ch for ch in reference if ch.isalnum()).upper()
    if len(compact) == 17 and compact.startswith("EPC"):
        dashed = f"{compact[:3]}-{compact[3:9]}-{compact[9:]}"
        payment = Payment.objects.filter(reference=dashed).select_related("plan", "user").first()
        if payment:
            return payment
    for candidate in Payment.objects.filter(
        status__in=[PaymentStatus.PENDING, PaymentStatus.EXPIRED, PaymentStatus.FAILED],
    ).select_related("plan", "user")[:500]:
        if "".join(ch for ch in candidate.reference if ch.isalnum()).upper() == compact:
            return candidate
    return None


def handle_webhook(method: str, request) -> tuple[dict, int]:
    """Authenticate a gateway callback and act on it.

    Returns the ``(body, status)`` the gateway expects back. An authenticated
    callback for an order we do not know is still acknowledged — refusing it
    would only make the gateway retry for days — and logged loudly.
    """
    provider = get_provider(method)
    reference, result = provider.parse_webhook(request)   # raises PaymentError if not authentic

    payment = find_by_reference(reference)
    if payment is None or payment.method != method:
        logger.warning("Webhook for unknown payment reference=%s method=%s", reference, method)
        return provider.webhook_ack(True)

    log_event(payment, "webhook", result.message or result.status, {"status": result.status})
    apply_result(payment, result, source="webhook")
    return provider.webhook_ack(True)
