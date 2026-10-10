"""Binance Pay — the merchant API.

Requests we send are signed with our API secret: HMAC-SHA512 over
``timestamp\\nnonce\\nbody\\n``, upper-case hex, with the API key as
``BinancePay-Certificate-SN``.

Webhooks Binance sends are *not* signed with our secret. They carry an RSA
signature (SHA256withRSA, base64) made with Binance's own key, verified against
the public key Binance publishes at ``/binancepay/openapi/certificates`` —
looked up by the ``BinancePay-Certificate-SN`` header and cached. A callback
that fails that check is rejected before anything else looks at it, and one
that passes is still cross-checked with an order query when the API answers.

Binance Pay has no public sandbox: test with a small real order, or with the
local emulator (``PAYMENT_EMULATOR``) which speaks the same API.

Two details from the Create Order v3 reference (checked 29 Sep 2026):

* ``goodsName`` may not contain special characters ("\\", quotes, emoji…), so
  names and the description are sent as plain ASCII — a plan called
  "GPT-5 Mini — Standard" goes as "GPT-5 Mini - Standard".
* ``orderExpireTime`` is described as a *duration* in v3 but was an epoch
  timestamp in v2. It is not sent: Binance's default (one hour) outlives the
  order's own window, and the order is closed at Binance (``order/close``)
  when that window ends — after a last status check, so a payment made at
  the last moment is still credited.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import unicodedata

import requests
from django.conf import settings
from django.core.cache import cache

from AI.services.payments.base import CheckoutSession, PaymentError, PaymentProvider, VerificationResult

logger = logging.getLogger("AI.payments.binance")

PRODUCTION_HOST = "bpay.binanceapi.com"
CERT_CACHE_KEY = "binancepay:cert:{}"
CERT_CACHE_SECONDS = 12 * 3600

#: Order states from /v2/order/query, as this application reads them.
QUERY_STATUS = {
    "INITIAL": "pending", "PENDING": "pending", "PAID": "paid",
    "CANCELED": "cancelled", "CANCELLED": "cancelled", "ERROR": "failed",
    "EXPIRED": "expired", "REFUNDING": "failed", "REFUNDED": "failed", "FULL_REFUNDED": "failed",
}


def trade_no(payment) -> str:
    """Our reference as Binance accepts it: letters and digits, at most 32."""
    return "".join(ch for ch in payment.reference if ch.isalnum()).upper()[:32]


def plain_text(text: str, limit: int = 256) -> str:
    """What Binance accepts in a goods name or description: plain ASCII.

    Dashes and quotes become their plain forms, accents are dropped, and
    anything else outside letters, digits and ordinary punctuation (emoji,
    backslashes, double quotes) is removed.
    """
    text = str(text or "").replace("\u2014", "-").replace("\u2013", "-").replace("\u2019", "'")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9 .,:;()/&+'#%@!?_-]", "", text)
    return re.sub(r"\s+", " ", text).strip()[:limit] or "AI tokens"


class BinancePayProvider(PaymentProvider):
    key = "binance"
    label = "Binance Pay"
    description = "Scan with the Binance app and pay in USDT. No network fees."
    icon = "binance"
    currencies = ("USDT",)
    steps = (
        "Scan the QR code with the Binance app, or open it on this phone.",
        "Confirm the USDT payment in Binance.",
        "Your plan activates as soon as Binance confirms it.",
    )
    required_env = ("BINANCE_PAY_API_KEY", "BINANCE_PAY_API_SECRET")
    optional_env = ("BINANCE_PAY_BASE_URL", "BINANCE_PAY_MERCHANT_ID", "BINANCE_PAY_CURRENCY")

    @property
    def base_url(self) -> str:
        return settings.BINANCE_PAY_BASE_URL.rstrip("/")

    @property
    def is_configured(self) -> bool:
        return bool(settings.BINANCE_PAY_API_KEY and settings.BINANCE_PAY_API_SECRET)

    @property
    def is_sandbox(self) -> bool:
        return PRODUCTION_HOST not in self.base_url

    @property
    def supports_webhook(self) -> bool:
        return True

    @property
    def charge_currency(self) -> str:
        return (settings.BINANCE_PAY_CURRENCY or "USDT").upper()

    # -- requests we sign ----------------------------------------------------------
    def _sign(self, timestamp: str, nonce: str, body: str) -> str:
        payload = f"{timestamp}\n{nonce}\n{body}\n".encode()
        return hmac.new(settings.BINANCE_PAY_API_SECRET.encode(), payload, hashlib.sha512).hexdigest().upper()

    def _request(self, path: str, body: dict) -> dict:
        timestamp = str(int(time.time() * 1000))
        # 32 characters from [A-Za-z0-9], as the API requires.
        nonce = secrets.token_hex(16)
        raw_body = json.dumps(body, separators=(",", ":"))
        headers = {
            "Content-Type": "application/json",
            "BinancePay-Timestamp": timestamp,
            "BinancePay-Nonce": nonce,
            "BinancePay-Certificate-SN": settings.BINANCE_PAY_API_KEY,
            "BinancePay-Signature": self._sign(timestamp, nonce, raw_body),
        }
        try:
            response = requests.post(f"{self.base_url}{path}", headers=headers, data=raw_body, timeout=25)
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Binance Pay request failed %s: %s", path, exc)
            raise PaymentError("We could not reach Binance Pay. Please try again shortly.", code="unreachable")

    # -- checkout ------------------------------------------------------------------------
    def create_checkout(self, payment) -> CheckoutSession:
        if not self.is_configured:
            return self.manual_fallback(
                payment,
                instructions=[
                    "Open Binance → Pay → Send, and transfer USDT to the Pay ID below.",
                    f"Send exactly {payment.pay_amount} USDT.",
                    f"Add the reference {payment.reference} to the note.",
                    "Upload a screenshot below — an administrator confirms it against Binance.",
                ],
                fields={
                    "Binance Pay ID": settings.BINANCE_PAY_MERCHANT_ID or "Contact support",
                    "Reference": payment.reference,
                    "Amount": payment.pay_display,
                },
            )

        site = settings.SITE_URL.rstrip("/")
        body = {
            "env": {"terminalType": "WEB"},
            "merchantTradeNo": trade_no(payment),
            "orderAmount": float(f"{payment.pay_amount:.2f}"),
            "currency": self.charge_currency,
            "description": plain_text(f"{settings.BRAND['full_name']} - {payment.plan.name}"),
            "goodsDetails": [{
                "goodsType": "02",             # virtual goods
                "goodsCategory": "Z000",       # others
                "referenceGoodsId": f"plan{payment.plan_id}",
                "goodsName": plain_text(payment.plan.name),
                "goodsDetail": plain_text(f"{payment.tokens_to_grant:,} AI tokens"),
            }],
            "returnUrl": f"{site}/payments/{payment.reference}/return/",
            "cancelUrl": f"{site}/payments/{payment.reference}/",
            "webhookUrl": f"{site}/webhooks/payments/binance/",
        }
        data = self._request("/binancepay/openapi/v3/order", body)

        if str(data.get("status")) != "SUCCESS":
            message = data.get("errorMessage") or "Binance Pay rejected the order."
            logger.warning("Binance create order failed for %s: %s %s", payment.reference, data.get("code"), message)
            raise PaymentError(f"Could not start the Binance payment: {message}", code="create_failed")

        result = data.get("data") or {}
        return CheckoutSession(
            kind="qr",
            qr_payload=result.get("qrContent", ""),
            qr_image_url=result.get("qrcodeLink", ""),
            checkout_url=result.get("checkoutUrl", ""),
            deeplink=result.get("universalUrl") or result.get("deeplink", ""),
            order_id=str(result.get("prepayId", "")),
            instructions=list(self.steps),
            raw=result,
        )

    # -- status ------------------------------------------------------------------------------
    def verify(self, payment) -> VerificationResult:
        if not self.is_configured:
            return VerificationResult(status="pending", message="Awaiting manual confirmation.")

        data = self._request("/binancepay/openapi/v2/order/query", {"merchantTradeNo": trade_no(payment)})
        if str(data.get("status")) != "SUCCESS":
            code = str(data.get("code") or "")
            if code in {"400002", "400003", "400004", "400005", "400006"}:
                # Signature, key or timestamp refused: our setup, not the order.
                logger.error("Binance refused the order query for %s: %s %s",
                             payment.reference, code, data.get("errorMessage"))
            return VerificationResult(status="pending", message="Binance has not confirmed the order yet.", raw=data)

        result = data.get("data") or {}
        status = QUERY_STATUS.get(str(result.get("status") or "").upper(), "pending")
        return VerificationResult(
            status=status,
            provider_txn_id=str(result.get("transactionId") or result.get("prepayId") or ""),
            amount=str(result.get("orderAmount") or ""),
            currency=str(result.get("currency") or ""),
            message={
                "paid": "Binance confirmed the payment.",
                "expired": "The Binance order expired before it was paid.",
                "cancelled": "The Binance order was cancelled.",
                "failed": "The Binance payment did not complete.",
            }.get(status, "Waiting for Binance to confirm."),
            raw=result,
        )

    def close_order(self, payment) -> None:
        if not self.is_configured:
            return
        try:
            self._request("/binancepay/openapi/order/close", {"merchantTradeNo": trade_no(payment)})
        except PaymentError:
            logger.info("Could not close Binance order for %s (it may already be closed)", payment.reference)

    def health_check(self) -> tuple[bool, str]:
        """Fetch the webhook certificates: a signed call that needs a valid key pair."""
        if not self.is_configured:
            return False, "API key or secret missing."
        try:
            data = self._request("/binancepay/openapi/certificates", {})
        except PaymentError as exc:
            return False, exc.friendly_message
        if str(data.get("status")) == "SUCCESS":
            count = len(data.get("data") or [])
            return True, f"Binance accepted the API key; {count} webhook certificate{'s' if count != 1 else ''} fetched."
        return False, f"Binance rejected the request: {data.get('code')} {data.get('errorMessage') or ''}".strip()

    # -- webhooks ------------------------------------------------------------------------------
    def _public_key(self, serial: str):
        """Binance's webhook-signing key for ``serial``, from cache or the API."""
        from cryptography.hazmat.primitives import serialization

        pem = cache.get(CERT_CACHE_KEY.format(serial))
        if not pem:
            data = self._request("/binancepay/openapi/certificates", {})
            for cert in data.get("data") or []:
                if cert.get("certPublic"):
                    cache.set(CERT_CACHE_KEY.format(cert.get("certSerial", "")), cert["certPublic"], CERT_CACHE_SECONDS)
                    if cert.get("certSerial") == serial or not serial:
                        pem = cert["certPublic"]
        if not pem:
            raise PaymentError("Unknown Binance signing certificate.", code="bad_signature")
        return serialization.load_pem_public_key(pem.encode())

    def verify_signature(self, request) -> None:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        timestamp = request.headers.get("BinancePay-Timestamp", "")
        nonce = request.headers.get("BinancePay-Nonce", "")
        signature = request.headers.get("BinancePay-Signature", "")
        serial = request.headers.get("BinancePay-Certificate-SN", "")
        if not (timestamp and nonce and signature):
            raise PaymentError("Callback is missing its signature headers.", code="bad_signature")
        payload = f"{timestamp}\n{nonce}\n{request.body.decode()}\n".encode()
        try:
            self._public_key(serial).verify(
                base64.b64decode(signature), payload, padding.PKCS1v15(), hashes.SHA256(),
            )
        except (InvalidSignature, ValueError, TypeError):
            logger.warning("Rejected Binance webhook with a bad signature (cert %s)", serial)
            raise PaymentError("Callback signature is invalid.", code="bad_signature")

    def parse_webhook(self, request) -> tuple[str, VerificationResult]:
        if not self.is_configured:
            raise PaymentError("Binance Pay is not configured.", code="not_configured")
        self.verify_signature(request)

        try:
            envelope = json.loads(request.body.decode() or "{}")
            payload = json.loads(envelope.get("data") or "{}")
        except ValueError:
            raise PaymentError("Callback body could not be parsed.", code="bad_callback")

        biz_type = str(envelope.get("bizType") or "").upper()
        biz_status = str(envelope.get("bizStatus") or "").upper()
        reference = str(payload.get("merchantTradeNo") or "")
        if biz_type != "PAY":
            # Refund and payout notifications are acknowledged and ignored.
            return reference, VerificationResult(status="pending", message=f"Ignored {biz_type or 'unknown'} event.")

        # PAY_CLOSED per the notification reference; PAY_CLOSE as some of
        # Binance's guides spell it.
        status = {"PAY_SUCCESS": "paid", "PAY_CLOSED": "expired", "PAY_CLOSE": "expired"}.get(biz_status, "pending")
        result = VerificationResult(
            status=status,
            provider_txn_id=str(payload.get("transactionId") or ""),
            amount=str(payload.get("totalFee") or ""),
            currency=str(payload.get("currency") or ""),
            message={"paid": "Binance reported the payment as successful.",
                     "expired": "Binance closed the order unpaid."}.get(status, biz_status),
            raw=payload,
        )
        return reference, result

    def webhook_ack(self, ok: bool = True) -> tuple[dict, int]:
        # Binance retries until it gets exactly this back.
        if ok:
            return {"returnCode": "SUCCESS", "returnMessage": None}, 200
        return {"returnCode": "FAIL", "returnMessage": "rejected"}, 400
