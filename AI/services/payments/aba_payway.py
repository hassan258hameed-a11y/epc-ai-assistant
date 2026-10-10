"""ABA PayWay — hosted checkout ("Purchase" API).

The browser POSTs a signed form to PayWay's purchase endpoint and pays on
ABA's own page (ABA Mobile, KHQR or card). Every request is signed with
``base64(HMAC-SHA512(concatenated fields, merchant API key))``, the fields
concatenated in the order PayWay documents.

Each press of "Pay" is its own attempt with its own ``tran_id``: PayWay
refuses one it has already seen ("Duplicated transaction ID", code 4), so a
student who comes back and pays again would otherwise be stuck. The signed
form is built at that moment, never stored and re-posted.

After payment PayWay calls ``return_url`` — our pushback URL — server to
server. The pushback is treated as a *hint only*: the server then asks PayWay
itself (Check Transaction, signed with our key) and settles only on that
answer. The browser's own return (``continue_success_url``) does the same
check and never settles anything by itself.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import date
from datetime import timezone as dt_timezone

import requests
from django.conf import settings
from django.utils import timezone

from AI.services.payments import attempts
from AI.services.payments.base import (
    CheckoutSession,
    Handoff,
    PaymentError,
    PaymentProvider,
    VerificationResult,
)

logger = logging.getLogger("AI.payments.aba")

#: The purchase request's hashed fields, in PayWay's documented order.
#: ``payment_gate`` and ``view_type`` are posted but never signed.
PURCHASE_HASH_FIELDS = (
    "req_time", "merchant_id", "tran_id", "amount", "items", "shipping", "firstname", "lastname",
    "email", "phone", "type", "payment_option", "return_url", "cancel_url", "continue_success_url",
    "return_deeplink", "currency", "custom_fields", "return_params", "payout", "lifetime",
    "additional_params", "google_pay_token", "skip_success_page",
)

#: PayWay caps tran_id at 20 characters.
MAX_TRAN_ID = 20

#: payment_status from Check Transaction, as this application reads it.
PAYMENT_STATUS = {
    "APPROVED": "paid", "COMPLETED": "paid", "SUCCESS": "paid",
    # Held, not taken: a purchase never pre-authorises, and money that is
    # only held is not money received.
    "PRE-AUTH": "pending", "PENDING": "pending", "CREATED": "pending",
    "DECLINED": "failed", "FAILED": "failed", "REFUNDED": "failed",
    "CANCELLED": "cancelled", "CANCELED": "cancelled", "EXPIRED": "expired",
}
#: payment_status_code, which PayWay sends alongside (and which wins).
PAYMENT_STATUS_CODE = {0: "paid", 2: "pending", 3: "failed", 4: "failed", 7: "cancelled"}
#: The older Check Transaction answers with a numeric status.
LEGACY_STATUS = {0: "paid", 1: "pending", 2: "pending", 3: "failed", 4: "failed", 7: "cancelled"}

#: API envelope codes (``status.code``) that mean our credentials were refused.
CREDENTIAL_CODES = {"1", "5", "8", "401"}
#: "Transaction not found": the student never finished that attempt.
NOT_FOUND = "6"
#: Days ahead of a key's expiry that the gateways page starts warning.
EXPIRY_WARNING_DAYS = 21


def format_amount(amount, currency: str) -> str:
    """KHR has no minor unit, so PayWay wants whole riel; USD takes cents."""
    return f"{int(round(amount))}" if currency.upper() == "KHR" else f"{amount:.2f}"


def tran_id_for(payment, number: int) -> str:
    """"EPC-260928-1A2B3C4D", attempt 2 → "EPC2609281A2B3C4DR2" (≤ 20 characters)."""
    return f"{attempts.compact(payment.reference)}R{number}"[:MAX_TRAN_ID]


class ABAPayWayProvider(PaymentProvider):
    key = "aba_payway"
    label = "ABA PayWay"
    description = "Pay with ABA Mobile, any KHQR banking app, or a card."
    icon = "aba"
    currencies = ("KHR", "USD")
    steps = (
        "Continue to ABA PayWay's secure page.",
        "Pay with ABA Mobile, scan the KHQR with any bank app, or use a card.",
        "You come back here and the plan activates once ABA confirms.",
    )
    required_env = ("ABA_PAYWAY_MERCHANT_ID", "ABA_PAYWAY_API_KEY", "ABA_PAYWAY_BASE_URL")
    optional_env = ("ABA_PAYWAY_CURRENCY", "ABA_PAYWAY_PAYMENT_OPTION", "ABA_PAYWAY_PAYMENT_GATE",
                    "ABA_PAYWAY_KEY_EXPIRES")

    @property
    def merchant_id(self) -> str:
        return settings.ABA_PAYWAY_MERCHANT_ID

    @property
    def api_key(self) -> str:
        return settings.ABA_PAYWAY_API_KEY

    @property
    def base_url(self) -> str:
        return settings.ABA_PAYWAY_BASE_URL.rstrip("/")

    @property
    def is_configured(self) -> bool:
        return bool(self.merchant_id and self.api_key)

    @property
    def is_sandbox(self) -> bool:
        return "sandbox" in self.base_url or "payway.com.kh" not in self.base_url

    @property
    def supports_webhook(self) -> bool:
        return True

    @property
    def charge_currency(self) -> str:
        currency = (settings.ABA_PAYWAY_CURRENCY or "KHR").upper()
        return currency if currency in self.currencies else "KHR"

    def notices(self) -> list[str]:
        raw = (getattr(settings, "ABA_PAYWAY_KEY_EXPIRES", "") or "").strip()
        if not raw or not self.is_configured:
            return []
        try:
            expires = date.fromisoformat(raw)
        except ValueError:
            return [f"ABA_PAYWAY_KEY_EXPIRES should be a date like 2026-12-31, not {raw!r}."]
        days = (expires - timezone.localdate()).days
        if days < 0:
            return [f"ABA's credential sheet says this key expired on {expires:%d %b %Y}. "
                    "If Test connection still passes, ABA is still accepting it — ask ABA to confirm "
                    "or renew it before it stops."]
        if days <= EXPIRY_WARNING_DAYS:
            return [f"The ABA key expires on {expires:%d %b %Y} ({days} days). Ask ABA for a renewed key."]
        return []

    # -- signing ------------------------------------------------------------------------
    def hash_fields(self, fields: dict, order) -> str:
        payload = "".join(str(fields.get(name) or "") for name in order).encode()
        return base64.b64encode(hmac.new(self.api_key.encode(), payload, hashlib.sha512).digest()).decode()

    # -- checkout ---------------------------------------------------------------------------
    def create_checkout(self, payment) -> CheckoutSession:
        if not self.is_configured:
            return self.manual_fallback(
                payment,
                instructions=[
                    "Open ABA Mobile and transfer to the account an administrator gives you.",
                    f"Send exactly {payment.pay_display}.",
                    f"Put the reference {payment.reference} in the remark.",
                    "Upload a screenshot below — an administrator confirms it against the bank statement.",
                ],
                fields={"Reference": payment.reference, "Amount": payment.pay_display},
            )
        # Nothing is signed yet: the form is built fresh, with a fresh
        # tran_id, each time the student presses Pay (start_attempt).
        return CheckoutSession(kind="handoff", instructions=list(self.steps), order_id=payment.reference)

    def purchase_fields(self, payment, tran_id: str) -> dict:
        """The signed purchase form for one attempt."""
        currency = payment.pay_currency.upper()
        amount = format_amount(payment.pay_amount, currency)
        site = settings.SITE_URL.rstrip("/")
        user = payment.user
        names = (user.full_name or user.display_name or "").split(" ", 1)
        lifetime = 15
        if payment.expires_at:
            lifetime = max(3, int((payment.expires_at - timezone.now()).total_seconds() // 60) or 3)

        fields = {
            "req_time": timezone.now().astimezone(dt_timezone.utc).strftime("%Y%m%d%H%M%S"),
            "merchant_id": self.merchant_id,
            "tran_id": tran_id,
            "amount": amount,
            "items": base64.b64encode(json.dumps(
                [{"name": payment.plan.name[:60], "quantity": 1, "price": amount}], separators=(",", ":"),
            ).encode()).decode(),
            # An explicit number: the current gateway refuses a blank shipping
            # price ("Wrong shipping price", code 10).
            "shipping": format_amount(0, currency),
            "firstname": names[0][:20],
            "lastname": (names[1] if len(names) > 1 else "")[:20],
            "email": (user.email or "")[:50],
            "phone": "",
            "type": "purchase",
            "payment_option": settings.ABA_PAYWAY_PAYMENT_OPTION or "",
            # The server-to-server pushback. PayWay wants this one base64-encoded.
            "return_url": base64.b64encode(f"{site}/webhooks/payments/aba_payway/".encode()).decode(),
            "cancel_url": f"{site}/payments/{payment.reference}/",
            "continue_success_url": f"{site}/payments/{payment.reference}/return/",
            "currency": currency,
            "lifetime": str(lifetime),
        }
        fields["hash"] = self.hash_fields(fields, PURCHASE_HASH_FIELDS)
        gate = (getattr(settings, "ABA_PAYWAY_PAYMENT_GATE", "") or "").strip()
        if gate:
            # After signing: PayWay leaves payment_gate out of the hash. "0"
            # asks for the full hosted page even on a QR-API merchant profile.
            fields["payment_gate"] = gate
        return fields

    def start_attempt(self, payment, *, request=None, hosted: bool = False) -> Handoff:
        if not self.is_configured:
            raise PaymentError("ABA PayWay is not set up yet.", code="not_configured")
        number = attempts.count(payment) + 1
        if number > attempts.MAX_ATTEMPTS:
            raise PaymentError("This order has been tried too many times — please start a new one.",
                               code="too_many_attempts")
        tran_id = tran_id_for(payment, number)
        attempts.record(payment, provider_ref=tran_id,
                        data={"amount": format_amount(payment.pay_amount, payment.pay_currency),
                              "currency": payment.pay_currency.upper()})
        return Handoff(
            action=f"{self.base_url}/api/payment-gateway/v1/payments/purchase",
            fields=self.purchase_fields(payment, tran_id),
            reference=tran_id,
            # PayWay's own samples post multipart, and its live page takes it.
            enctype="multipart/form-data",
        )

    # -- status ---------------------------------------------------------------------------------
    def check_transaction(self, reference: str) -> VerificationResult:
        """Ask PayWay for the authoritative state of one tran_id."""
        req_time = timezone.now().astimezone(dt_timezone.utc).strftime("%Y%m%d%H%M%S")
        body = {"req_time": req_time, "merchant_id": self.merchant_id, "tran_id": reference}
        body["hash"] = self.hash_fields(body, ("req_time", "merchant_id", "tran_id"))
        try:
            response = requests.post(
                f"{self.base_url}/api/payment-gateway/v1/payments/check-transaction-2", json=body, timeout=20,
            )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("PayWay check failed for %s: %s", reference, exc)
            raise PaymentError("We could not reach ABA PayWay. Please try again shortly.", code="unreachable")
        if not isinstance(data, dict):
            data = {}

        if isinstance(data.get("status"), dict):
            envelope = data["status"]
            code = str(envelope.get("code", "")).strip()
            if code in CREDENTIAL_CODES:
                logger.error("PayWay refused our credentials (code %s: %s)", code, envelope.get("message"))
                raise PaymentError("ABA PayWay refused this site's credentials.", code="gateway_auth")
            if code not in ("00", "0"):
                # Not found means the student never finished this attempt.
                return VerificationResult(
                    status="pending", raw=data,
                    message=f"ABA: {envelope.get('message') or 'no answer yet'}",
                )
            detail = data.get("data") or {}
            payment_status = str(detail.get("payment_status") or "").upper()
            try:
                status_code = int(detail.get("payment_status_code"))
            except (TypeError, ValueError):
                status_code = None
            status = PAYMENT_STATUS_CODE.get(status_code) or PAYMENT_STATUS.get(payment_status, "pending")
            amount = next((detail[k] for k in ("total_amount", "payment_amount", "original_amount")
                           if detail.get(k) not in (None, "")), "")
            return VerificationResult(
                status=status,
                provider_txn_id=str(detail.get("apv") or ""),
                amount=str(amount),
                # Only the currency actually charged: original_currency can be
                # the profile's default rather than this purchase's.
                currency=str(detail.get("payment_currency") or "").upper(),
                original_amount=str(detail.get("original_amount") or ""),
                original_currency=str(detail.get("original_currency") or "").upper(),
                message=f"ABA: {payment_status or envelope.get('message') or 'no answer yet'}",
                raw=data,
            )
        # The older endpoint's shape: {"status": 0, "payment_status": "APPROVED", ...}.
        try:
            legacy = int(data.get("status"))
        except (TypeError, ValueError):
            legacy = None
        payment_status = str(data.get("payment_status") or "").upper()
        status = PAYMENT_STATUS.get(payment_status) or LEGACY_STATUS.get(legacy, "pending")
        return VerificationResult(
            status=status, provider_txn_id=str(data.get("apv") or ""),
            amount=str(data.get("totalAmount", data.get("amount", ""))), currency=str(data.get("currency") or ""),
            message=f"ABA: {data.get('description') or payment_status or 'no answer yet'}", raw=data,
        )

    def verify(self, payment) -> VerificationResult:
        if not self.is_configured:
            return VerificationResult(status="pending", message="Awaiting manual confirmation.")
        refs = [attempt.provider_ref for attempt in attempts.to_check(payment)]
        legacy = (payment.checkout_payload or {}).get("kind") == "redirect"
        if legacy or not refs:
            # An order opened before attempts existed was sent as its reference.
            refs.append(payment.reference)
        newest = None
        for ref in refs:
            result = self.check_transaction(ref)
            if result.is_paid:
                return result
            newest = newest or result
        if newest.status in {"failed", "cancelled", "expired"} and len(refs) < attempts.OLDER_LIMIT + 1:
            # Before calling the order failed, make sure no older attempt —
            # one the student finished in another tab — was paid.
            checked = set(refs)
            for attempt in payment.attempts.order_by("-created_at", "-id")[:attempts.OLDER_LIMIT + 1]:
                if attempt.provider_ref in checked:
                    continue
                result = self.check_transaction(attempt.provider_ref)
                if result.is_paid:
                    return result
        return newest

    def health_check(self) -> tuple[bool, str]:
        """Check Transaction for a reference that cannot exist: "not found"
        proves the merchant ID and API key were accepted."""
        if not self.is_configured:
            return False, "Merchant ID or API key missing."
        try:
            result = self.check_transaction(f"HEALTH{int(timezone.now().timestamp())}")
        except PaymentError as exc:
            if exc.code == "gateway_auth":
                return False, "PayWay rejected the credentials (wrong hash or merchant) — check the key, or ask ABA whether it has expired."
            return False, exc.friendly_message
        status = result.raw.get("status") if isinstance(result.raw, dict) else None
        answer = (status.get("message") if isinstance(status, dict) else result.raw.get("description")) or ""
        warning = " ".join(self.notices())
        return True, f"PayWay answered{': ' + answer if answer else ''} — credentials accepted." + (
            f" {warning}" if warning else "")

    # -- pushback ------------------------------------------------------------------------------------
    def parse_webhook(self, request) -> tuple[str, VerificationResult]:
        if not self.is_configured:
            raise PaymentError("ABA PayWay is not configured.", code="not_configured")
        try:
            body = json.loads(request.body.decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            body = request.POST.dict()
        if not isinstance(body, dict):
            body = {}
        reference = str(body.get("tran_id") or "")
        if not reference:
            raise PaymentError("Callback is missing a transaction reference.", code="bad_callback")
        # The pushback is only a nudge: what it claims is ignored, and the
        # answer comes from PayWay's signed Check Transaction instead.
        return reference, self.check_transaction(reference)
