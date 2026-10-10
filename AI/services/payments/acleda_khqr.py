"""ACLEDA Bank — KHQR paid into the merchant's Bakong account at ACLEDA.

KHQR is Cambodia's national QR standard (EMVCo-based, run by the National
Bank of Cambodia on Bakong). Each order gets its own *dynamic* KHQR: the
merchant's Bakong account ID at ACLEDA (``name@aclb``), the amount in KHR, our
reference as the bill number, and a creation/expiry timestamp. Any Cambodian
banking app — ACLEDA mobile, ABA, Wing… — can scan and pay it.

Confirmation comes from the National Bank's Bakong Open API: the MD5 of the
exact KHQR string identifies the transaction, and
``/v1/check_transaction_by_md5`` returns it once it has settled — with the
receiving account, currency and amount, all of which are checked against the
order before it counts as paid. Bakong sends no callbacks, so the payment page
polls and a scheduled job sweeps anything left pending.

Without an API token the QR still works, and the order waits for an
administrator to confirm it against the ACLEDA statement.
"""
from __future__ import annotations

import hashlib
import logging
import time

import requests
from django.conf import settings

from AI.services.payments.base import CheckoutSession, PaymentError, PaymentProvider, VerificationResult

logger = logging.getLogger("AI.payments.acleda")

CURRENCY_CODES = {"KHR": "116", "USD": "840"}


def _tlv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def crc16(payload: str) -> str:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF), as EMVCo and KHQR require."""
    crc = 0xFFFF
    for byte in payload.encode():
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def build_khqr(
    *, bakong_id: str, merchant_name: str, city: str, amount, currency: str, bill_number: str,
    merchant_id: str = "", account_information: str = "", acquiring_bank: str = "ACLEDA Bank",
    created_ms: int | None = None, expires_ms: int | None = None, store_label: str = "",
) -> str:
    """One KHQR payload string, per the NBC KHQR content guideline.

    Individual accounts use tag 29; a merchant account (with a merchant ID
    from the bank) uses tag 30. An amount makes the QR dynamic (``01=12``).
    """
    currency = currency.upper()
    if currency not in CURRENCY_CODES:
        raise ValueError(f"KHQR does not support {currency}")
    if merchant_id:
        account = _tlv("30", _tlv("00", bakong_id) + _tlv("01", merchant_id[:32]) + _tlv("02", acquiring_bank[:32]))
    else:
        account = _tlv("29", _tlv("00", bakong_id)
                       + (_tlv("01", account_information[:32]) if account_information else "")
                       + (_tlv("02", acquiring_bank[:32]) if acquiring_bank else ""))

    payload = _tlv("00", "01")
    payload += _tlv("01", "12" if amount is not None else "11")
    payload += account
    payload += _tlv("52", "5999")
    payload += _tlv("53", CURRENCY_CODES[currency])
    if amount is not None:
        payload += _tlv("54", f"{int(round(amount))}" if currency == "KHR" else f"{amount:.2f}")
    payload += _tlv("58", "KH")
    payload += _tlv("59", merchant_name[:25])
    payload += _tlv("60", city[:15])
    additional = _tlv("01", bill_number[:25])
    if store_label:
        additional += _tlv("03", store_label[:25])
    payload += _tlv("62", additional)
    created_ms = created_ms or int(time.time() * 1000)
    timestamps = _tlv("00", str(created_ms)) + (_tlv("01", str(expires_ms)) if expires_ms else "")
    payload += _tlv("99", timestamps)
    payload += "6304"
    return payload + crc16(payload)


def khqr_md5(khqr: str) -> str:
    return hashlib.md5(khqr.encode()).hexdigest()


class ACLEDAKHQRProvider(PaymentProvider):
    key = "acleda_khqr"
    label = "ACLEDA Bank (KHQR)"
    description = "Scan the KHQR with ACLEDA mobile or any Cambodian banking app. Paid in riel."
    icon = "khqr"
    currencies = ("KHR", "USD")
    steps = (
        "Open ACLEDA mobile (or any Cambodian banking app) and choose Scan KHQR.",
        "Scan the code — the amount in riel is filled in for you — and confirm.",
        "This page updates by itself and the plan activates once the bank confirms.",
    )
    required_env = ("ACLEDA_KHQR_BAKONG_ID", "BAKONG_API_TOKEN")
    optional_env = ("ACLEDA_KHQR_ACCOUNT_NAME", "ACLEDA_KHQR_CITY", "ACLEDA_KHQR_ACCOUNT_NUMBER",
                    "ACLEDA_KHQR_MERCHANT_ID", "ACLEDA_KHQR_CURRENCY", "BAKONG_API_BASE_URL")

    @property
    def bakong_id(self) -> str:
        return settings.ACLEDA_KHQR_BAKONG_ID.strip()

    @property
    def can_generate(self) -> bool:
        """A real KHQR needs only the Bakong account ID."""
        return bool(self.bakong_id)

    @property
    def is_configured(self) -> bool:
        """Automatic confirmation needs the Bakong API token as well."""
        return bool(self.bakong_id and settings.BAKONG_API_TOKEN)

    @property
    def is_sandbox(self) -> bool:
        return "api-bakong.nbc.gov.kh" not in settings.BAKONG_API_BASE_URL or "sit-" in settings.BAKONG_API_BASE_URL

    @property
    def charge_currency(self) -> str:
        currency = (settings.ACLEDA_KHQR_CURRENCY or "KHR").upper()
        return currency if currency in CURRENCY_CODES else "KHR"

    def create_checkout(self, payment) -> CheckoutSession:
        if not self.can_generate:
            return self.manual_fallback(
                payment,
                instructions=[
                    "Transfer from ACLEDA mobile to the account an administrator gives you.",
                    f"Send exactly {payment.pay_display}.",
                    f"Put the reference {payment.reference} in the remark.",
                    "Upload a screenshot below — an administrator confirms it against the bank statement.",
                ],
                fields={"Reference": payment.reference, "Amount": payment.pay_display},
            )

        expires_ms = int(payment.expires_at.timestamp() * 1000) if payment.expires_at else None
        khqr = build_khqr(
            bakong_id=self.bakong_id,
            merchant_name=settings.ACLEDA_KHQR_ACCOUNT_NAME or settings.BRAND["full_name"],
            city=settings.ACLEDA_KHQR_CITY or "Phnom Penh",
            amount=payment.pay_amount,
            currency=payment.pay_currency,
            bill_number=payment.reference,
            merchant_id=settings.ACLEDA_KHQR_MERCHANT_ID,
            account_information=settings.ACLEDA_KHQR_ACCOUNT_NUMBER,
            expires_ms=expires_ms,
            store_label=settings.BRAND["full_name"][:25],
        )
        fields = {"Pay to": settings.ACLEDA_KHQR_ACCOUNT_NAME, "Bakong ID": self.bakong_id,
                  "Amount": payment.pay_display, "Reference": payment.reference}
        if settings.ACLEDA_KHQR_ACCOUNT_NUMBER:
            fields["ACLEDA account"] = settings.ACLEDA_KHQR_ACCOUNT_NUMBER
        steps = list(self.steps)
        if not self.is_configured:
            steps[-1] = "Then upload a screenshot below — an administrator confirms it against the bank statement."
        return CheckoutSession(kind="qr", qr_payload=khqr, instructions=steps, fields=fields, order_id=khqr_md5(khqr))

    def _check_md5(self, md5: str) -> dict:
        try:
            response = requests.post(
                f"{settings.BAKONG_API_BASE_URL.rstrip('/')}/v1/check_transaction_by_md5",
                json={"md5": md5},
                headers={"Authorization": f"Bearer {settings.BAKONG_API_TOKEN}", "Content-Type": "application/json"},
                timeout=20,
            )
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Bakong check failed for %s: %s", md5, exc)
            raise PaymentError("We could not reach the bank. Please try again shortly.", code="unreachable")

    def health_check(self) -> tuple[bool, str]:
        """Look up a transaction that cannot exist: "not found" means the token works."""
        if not self.can_generate:
            return False, "Bakong account ID missing — no KHQR can be made."
        if not settings.BAKONG_API_TOKEN:
            return False, "KHQR codes work, but BAKONG_API_TOKEN is missing — payments need manual confirmation."
        try:
            data = self._check_md5(hashlib.md5(f"health-{time.time()}".encode()).hexdigest())
        except PaymentError as exc:
            return False, f"{exc.friendly_message} (the Bakong API may only answer Cambodian IP addresses)"
        if data.get("errorCode") == 6:
            return False, "Bakong rejected the API token — it expires every 90 days; renew it at api-bakong.nbc.gov.kh."
        if data.get("errorCode") == 1 or data.get("responseCode") == 0:
            return True, "Bakong accepted the API token."
        return False, f"Unexpected Bakong answer: {data.get('responseMessage') or data}"

    def verify(self, payment) -> VerificationResult:
        if not self.is_configured or not payment.provider_order_id:
            return VerificationResult(status="pending", message="Awaiting confirmation of your transfer.")
        data = self._check_md5(payment.provider_order_id)

        code, error = data.get("responseCode"), data.get("errorCode")
        detail = data.get("data") or {}
        if code == 0 and detail:
            to_account = str(detail.get("toAccountId") or "")
            if to_account and to_account.lower() != self.bakong_id.lower():
                logger.error("Bakong transaction for %s went to %s, not %s", payment.reference, to_account, self.bakong_id)
                return VerificationResult(status="pending", message="Paid to a different account — needs review.", raw=detail)
            return VerificationResult(
                status="paid", provider_txn_id=str(detail.get("hash") or ""),
                amount=str(detail.get("amount", "")), currency=str(detail.get("currency") or ""),
                message="The bank confirmed the KHQR payment.", raw=detail,
            )
        if error == 3:
            return VerificationResult(status="failed", message="The bank reported the transfer as failed.", raw=data)
        if error == 6:
            logger.error("Bakong API token rejected — renew BAKONG_API_TOKEN")
            raise PaymentError("Automatic confirmation is temporarily unavailable.", code="bakong_token")
        return VerificationResult(status="pending", message="Waiting for the transfer to arrive.", raw=data)
