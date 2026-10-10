"""Student checkout, payment status and receipts."""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from AI.forms import CheckoutForm, PaymentProofForm
from AI.models import Payment, PaymentStatus, Plan
from AI.services import payment_service, promo_service, referral_campaign_service, settings_service
from AI.services.payments import PaymentError, get_provider
from AI.utils import ratelimit
from AI.utils.decorators import login_required_student
from AI.utils.ratelimit import RateLimited
from AI.utils import qr
from AI.utils.request import client_ip, json_error, json_ok
from AI.utils.uploads import InvalidUpload, validate_image

logger = logging.getLogger("AI.views.payments")


#: The order the methods are offered in.
CHECKOUT_METHODS = ("aba_payway", "acleda_xpay", "acleda_khqr", "binance")


def _method_options(plan) -> list[dict]:
    """Each method with what it would charge for ``plan`` and how it confirms.

    A method with no credentials is offered only while manual confirmation is
    allowed; otherwise it would lead to a payment nobody can confirm.

    ACLEDA is one bank, so it is one choice: its own gateway (XPAY) when that
    is set up, otherwise the Bakong KHQR — which also covers paying ACLEDA by
    hand, so XPAY is never offered for manual confirmation itself.
    """
    manual_ok = settings_service.get("payment_manual_review", True)
    xpay_live = get_provider("acleda_xpay").is_configured
    options = []
    for key in CHECKOUT_METHODS:
        provider = get_provider(key)
        automatic = provider.is_configured
        if (key == "acleda_xpay" and not automatic) or (key == "acleda_khqr" and xpay_live):
            continue
        if not automatic and not manual_ok:
            continue
        charge, currency, rate = payment_service.quote_charge(key, plan.price, plan.currency)
        preview = Payment(amount=plan.price, currency=plan.currency, charge_amount=charge, charge_currency=currency)
        options.append({
            "provider": provider,
            "key": key,
            "charge": preview.pay_display,
            "currency": currency,
            "rate": rate,
            "automatic": automatic,
        })
    return options


@login_required_student
def checkout(request, slug: str):
    plan = get_object_or_404(Plan, slug=slug, is_active=True)
    form = CheckoutForm(request.POST or None)
    options = _method_options(plan)
    providers = [option["provider"] for option in options]

    if request.method == "POST":
        try:
            ratelimit.enforce("payment_create", str(request.user.pk))
        except RateLimited as exc:
            messages.error(request, exc.friendly_message)
            return redirect("student:pricing")

        if form.is_valid() and form.cleaned_data["method"] not in {option["key"] for option in options}:
            messages.error(request, "That payment method is not available right now.")
        elif form.is_valid():
            # Pressing Continue twice (or coming back) reopens the order already
            # made instead of creating a second one to pay by mistake.
            existing = None
            if not (form.cleaned_data.get("promo_code") or "").strip():
                existing = payment_service.open_payment_for(request.user, plan, form.cleaned_data["method"])
            if existing is not None:
                return redirect("payments:detail", reference=existing.reference)
            try:
                payment = payment_service.create_payment(
                    request.user, plan, form.cleaned_data["method"],
                    note=form.cleaned_data.get("note", ""),
                    promo_code=form.cleaned_data.get("promo_code", ""),
                )
                if payment.amount <= 0:
                    # Paid in full by a discount: there is nothing for a
                    # gateway to collect, so it settles here and now.
                    payment_service.settle(
                        payment, source="promo", note="Paid in full by a discount",
                    )
                    messages.success(request, "Your discount covered the whole price — tokens added.")
                    return redirect("payments:receipt", reference=payment.reference)
                payment_service.start_checkout(payment)
            except PaymentError as exc:
                messages.error(request, exc.friendly_message)
                if exc.code.startswith("promo_"):
                    return redirect("payments:checkout", slug=plan.slug)
                return redirect("student:pricing")
            return redirect("payments:detail", reference=payment.reference)
        if not form.is_valid():
            messages.error(request, "Choose a payment method to continue.")

    return render(
        request,
        "student/checkout.html",
        {
            "nav_active": "plans",
            "plan": plan,
            "form": form,
            "providers": providers,
            "options": options,
            # The exchange-rate note only matters when a method charges riel.
            "charges_riel": any(option["currency"] == "KHR" for option in options),
            "khr_rate": payment_service.khr_rate(),
        },
    )


@login_required_student
def promo_check(request, slug: str):
    """Preview a promo code on a package: the new total, or why it cannot apply.

    Only a preview — the code is checked again, under a lock, when the payment
    is actually created.
    """
    plan = get_object_or_404(Plan, slug=slug, is_active=True)
    try:
        ratelimit.enforce("promo_check", str(request.user.pk), limit=20, window=600)
    except RateLimited as exc:
        return json_error(exc.friendly_message, status=429, code="rate_limited")
    try:
        result = promo_service.quote(request.user, plan, request.GET.get("code", ""))
    except promo_service.PromoError as exc:
        return json_error(exc.friendly_message, status=400, code=exc.code)

    referral, _, _ = referral_campaign_service.quote_discount(request.user, plan)
    if referral > result.discount:
        return json_error(
            f"Your referral discount (${referral:.2f}) is bigger, so it will be used instead.",
            status=400, code="referral_better",
        )
    return json_ok(price=f"{plan.price:.2f}", **result.as_payload())


OUTCOMES = {
    PaymentStatus.EXPIRED: ("This order expired", "No payment arrived in time, so nothing was charged. "
                            "Start again and you will get a fresh code."),
    PaymentStatus.FAILED: ("The payment did not go through", "Nothing was charged for this order. "
                           "You can try again, or pick another way to pay."),
    PaymentStatus.CANCELLED: ("This order was cancelled", "Nothing was charged. You can start a new one at any time."),
}


def _seconds_left(payment) -> int:
    if not payment.expires_at or payment.status != PaymentStatus.PENDING:
        return 0
    return max(0, int((payment.expires_at - timezone.now()).total_seconds()))


def _refresh(payment):
    """Bring an open order up to date: one gateway check, and expiry if due."""
    if payment.status == PaymentStatus.PENDING and payment.expires_at and payment.expires_at <= timezone.now():
        try:
            payment = payment_service.verify_payment(payment)
        except PaymentError:
            pass
        payment = payment_service.expire_if_due(payment)
    return payment


@login_required_student
def detail(request, reference: str):
    payment = get_object_or_404(
        Payment.objects.select_related("plan", "user"), reference=reference, user=request.user
    )
    payment = _refresh(payment)
    if payment.status == PaymentStatus.PAID:
        return redirect("payments:receipt", reference=payment.reference)

    provider = get_provider(payment.method)
    session = payment.checkout_payload or {}
    # An ABA order opened before each Pay became its own attempt carries a
    # stored, signed form whose tran_id PayWay may already have seen.
    stored_form = session.get("kind") == "redirect" and payment.method == "aba_payway"
    if (not session or stored_form) and payment.status == PaymentStatus.PENDING:
        try:
            session = payment_service.start_checkout(payment).to_dict()
        except PaymentError as exc:
            messages.error(request, exc.friendly_message)

    qr_svg = qr.svg(session.get("qr_payload", ""), dark="#0B1020") if session.get("qr_payload") else ""
    outcome = OUTCOMES.get(payment.status)

    return render(
        request,
        "student/payment_detail.html",
        {
            "nav_active": "plans",
            "payment": payment,
            "provider": provider,
            "session": session,
            # As pairs: PayWay's form has a field called "items", which a
            # template would read instead of the dict's .items().
            "session_fields": list((session.get("fields") or {}).items()),
            "qr_svg": qr_svg,
            "proof_form": PaymentProofForm(),
            "manual_review": session.get("kind") == "manual" or (
                not provider.is_configured and settings_service.get("payment_manual_review", True)
            ),
            "automatic": provider.is_configured,
            "seconds_left": _seconds_left(payment),
            "outcome": outcome,
            "emulator": settings.PAYMENT_EMULATOR,
        },
    )


@login_required_student
@require_POST
@never_cache
def pay(request, reference: str):
    """Press "Pay": open a fresh attempt at the gateway and hand the browser to it.

    The signed form is made here, for this press only. Never cached, so the
    Back button cannot post a single-use session a second time.
    """
    payment = get_object_or_404(
        Payment.objects.select_related("plan", "user"), reference=reference, user=request.user
    )
    if payment.status == PaymentStatus.PAID:
        return redirect("payments:receipt", reference=payment.reference)
    provider = get_provider(payment.method)
    try:
        ratelimit.enforce("payment_create", f"attempt:{request.user.pk}", limit=15, window=600)
        # The bank's own page, as for ABA — unless the student asked to see
        # ACLEDA's KHQR here instead ("show=qr").
        handoff = payment_service.start_attempt(payment, request=request, hosted=request.POST.get("show") != "qr")
    except RateLimited as exc:
        messages.error(request, exc.friendly_message)
        return redirect("payments:detail", reference=payment.reference)
    except PaymentError as exc:
        messages.error(request, exc.friendly_message)
        return redirect("payments:detail", reference=payment.reference)

    if handoff.qr_payload:
        # The gateway's own KHQR, shown on the order page — where the
        # countdown and the status polling already are.
        payment.checkout_payload = {
            "kind": "qr", "qr_payload": handoff.qr_payload,
            "instructions": list(getattr(provider, "qr_steps", provider.steps)),
            "fields": {"Pay to": provider.label, "Amount": payment.pay_display, "Reference": payment.reference},
            "attempt": handoff.reference,
        }
        payment.save(update_fields=["checkout_payload", "updated_at"])
        return redirect("payments:detail", reference=payment.reference)
    return render(
        request,
        "student/payment_handoff.html",
        {
            "nav_active": "plans",
            "payment": payment,
            "provider": provider,
            "handoff": handoff,
            # As pairs: ABA's form has a field called "items".
            "fields": list(handoff.fields.items()),
        },
    )


@login_required_student
@require_POST
def submit_proof(request, reference: str):
    """Attach transfer evidence for an admin to verify.

    Uploading proof never marks the payment paid — an administrator or the
    provider must confirm it.
    """
    payment = get_object_or_404(Payment, reference=reference, user=request.user)
    if payment.status == PaymentStatus.PAID:
        return redirect("payments:receipt", reference=payment.reference)

    form = PaymentProofForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Please attach a clear screenshot of your payment.")
        return redirect("payments:detail", reference=reference)

    try:
        validated = validate_image(request.FILES["proof"])
    except InvalidUpload as exc:
        messages.error(request, str(exc))
        return redirect("payments:detail", reference=reference)

    payment.proof = validated.file
    payment.payer_note = (form.cleaned_data.get("note") or payment.payer_note)[:255]
    payment.save(update_fields=["proof", "payer_note", "updated_at"])
    payment_service.log_event(payment, "proof_submitted", "Student uploaded payment evidence.")

    messages.success(
        request,
        "Thanks — we received your payment proof. Your plan activates as soon as our team confirms it.",
    )
    return redirect("payments:detail", reference=reference)


@login_required_student
def check_status(request, reference: str):
    """The order's authoritative state, for the payment page's polling (JSON).

    The browser never decides the outcome: this asks our backend, which asks
    the gateway.
    """
    payment = Payment.objects.filter(reference=reference, user=request.user).select_related("plan").first()
    if payment is None:
        return json_error("Payment not found.", status=404, code="not_found")

    message = ""
    if payment.status == PaymentStatus.PENDING:
        try:
            ratelimit.enforce("payment_status", f"{request.user.pk}:{reference}", limit=40, window=60)
            payment = payment_service.verify_payment(payment)
        except RateLimited:
            pass
        except PaymentError as exc:
            message = exc.friendly_message
        payment = _refresh(payment)

    paid = payment.status == PaymentStatus.PAID
    return json_ok(
        status=payment.status,
        paid=paid,
        final=payment.status != PaymentStatus.PENDING,
        seconds_left=_seconds_left(payment),
        message=message,
        receipt_url=f"/payments/{payment.reference}/receipt/" if paid else "",
    )


@login_required_student
@require_POST
def cancel(request, reference: str):
    payment = get_object_or_404(Payment, reference=reference, user=request.user)
    try:
        payment_service.cancel(payment)
        messages.success(request, "That payment has been cancelled.")
    except PaymentError as exc:
        messages.error(request, exc.friendly_message)
    return redirect("student:pricing")


@login_required_student
def receipt(request, reference: str):
    payment = get_object_or_404(
        Payment.objects.select_related("plan", "user"), reference=reference, user=request.user
    )
    subscription = getattr(payment, "subscription", None)
    return render(
        request,
        "student/receipt.html",
        {"nav_active": "plans", "payment": payment, "subscription": subscription},
    )


@csrf_exempt
def provider_return(request, reference: str):
    """Landing page after a hosted gateway sends the browser back.

    What the browser brings is only a hint; the backend asks the gateway.
    A bank may send the browser back with a cross-site POST, which carries
    neither a CSRF token nor (SameSite) the session cookie — so this does not
    require either: it only asks the gateway, which is safe for anyone to
    trigger, and then redirects to pages that do require the student.
    """
    payment = get_object_or_404(Payment, reference=reference)
    owner = request.user.is_authenticated and request.user.pk == payment.user_id
    try:
        ratelimit.enforce("payment_status", f"return:{client_ip(request)}", limit=30, window=60)
        payment = payment_service.verify_payment(payment)
    except RateLimited:
        pass
    except PaymentError as exc:
        logger.info("Return verification failed for %s: %s", reference, exc)

    if payment.status == PaymentStatus.PAID:
        return redirect("payments:receipt", reference=payment.reference)
    if payment.status == PaymentStatus.PENDING and owner:
        if request.GET.get("outcome") == "error":
            messages.warning(request, "The bank's page reported a problem. If money left your account it will "
                                      "still be confirmed here — otherwise press Pay to try again.")
        else:
            # Arriving back is not proof of payment: say so, and keep checking.
            messages.info(request, "Welcome back — we are confirming your payment with the bank. This takes a moment.")
    return redirect("payments:detail", reference=payment.reference)
