"""ACLEDA Bank — the XPAY e-commerce gateway (ACLEDA's hosted payment page).

The flow in ACLEDA's integration appendix:

1. ``openSessionV2`` opens a session for one attempt → ``sessionid`` and
   ``paymentTokenid``.
2. The browser POSTs the documented form to ``paymentPage.jsp`` — ACLEDA's
   own page, where the payer scans its KHQR with any Cambodian banking app.
   Or, when the student asks to stay on our page, the server posts it itself
   and shows that KHQR there, under the order's countdown and status polling.
3. From ACLEDA's page, the browser comes back to ``successUrlToReturn`` /
   ``errorUrl``.
4. ``getTxnStatus`` answers, authoritatively, whether money arrived.

The form must be a plain urlencoded POST: ACLEDA's page answers multipart
with a generic "The system has been updating, please try again later."

There is deliberately no "open ACLEDA mobile" button. The session's
``deeplinkUrl`` lands on an ACLEDA page that dead-ends on "An error has
occurred, Please contact bank!" unless ACLEDA's separate QR app is installed
(found in production on this merchant's other site). The KHQR works in every
Cambodian banking app and settles the same session.

Only step 4 settles anything. The browser's return and any callback are hints
that trigger it. A transaction counts as paid only when getTxnStatus reports
success *and* a non-zero ``confirmDate``: an opened-but-unpaid session also
answers with a core reference number, so that alone proves nothing.

Signing: every call carries ``hash`` = uppercase hex HMAC-SHA512 with the
merchant's Secret Code, over merchantID + loginId + password + txid (open) or
+ paymentTokenid (status).

A session is single-use — posting the same token to paymentPage.jsp twice
cancels it ("You have refreshed page, transaction has been cancelled", -85) —
so each press of "Pay" opens a fresh one (see ``attempts``).

The request shapes, limits and status rules follow the integration already
live on ACLEDA's production gateway for this merchant.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import requests
from django.conf import settings
from django.utils import timezone

from AI.services.payments import attempts
from AI.services.payments.acleda_khqr import crc16
from AI.services.payments.base import (
    CheckoutSession,
    Handoff,
    PaymentError,
    PaymentProvider,
    VerificationResult,
)

logger = logging.getLogger("AI.payments.xpay")

OPEN_SESSION_PATH = "XPAYConnectorServiceInterfaceImplV2/XPAYConnectorServiceInterfaceImplV2RS/openSessionV2"
TXN_STATUS_PATH = "XPAYConnectorServiceInterfaceImplV2/XPAYConnectorServiceInterfaceImplV2RS/getTxnStatus"
PAYMENT_PAGE_PATH = "paymentPage.jsp"

#: Limits from the appendix's request tables.
MAX_TXID = 16
MAX_DESCRIPTION = 16
MAX_INVOICE_ID = 64

#: getTxnStatus codes seen on ACLEDA's gateway.
CODE_AWAITING_PAYMENT = -23      # session open, nothing paid yet
CODE_NOT_FOUND = -1              # "The transaction you are looking for does not exist!"
CODE_VERIFICATION_FAILED = -5547  # our hash (i.e. the Secret Code, login or password) was refused

_PENDING_MARKERS = ("await", "pending", "in progress", "not yet", "does not exist")

#: The KHQR ACLEDA's page draws: ``<input id="qrPath" ... value="000201…">``.
#: Anchored on the id, so a redesign of their page fails loudly here rather
#: than picking up some other string that happens to look like a QR.
_QR_PATH_RE = re.compile(r'id=["\']qrPath["\'][^>]*?\bvalue=["\']([^"\']+)["\']', re.I)


def khqr_is_intact(payload: str) -> bool:
    """A whole KHQR whose tag-63 CRC checks out. A truncated one would scan
    and then fail in the bank app with nothing to explain why."""
    payload = (payload or "").strip()
    if len(payload) < 40 or not payload.startswith("000201") or payload[-8:-4] != "6304":
        return False
    return crc16(payload[:-4]) == payload[-4:].upper()
_CANCELLED_MARKERS = ("cancel", "abort", "reject", "declin")
_EXPIRED_MARKERS = ("expire", "timeout", "timed out")


def sign(message: str, secret: str) -> str:
    """Uppercase hex HMAC-SHA512 — the appendix's MerchantHashGenerator."""
    return hmac.new((secret or "").encode(), (message or "").encode(), hashlib.sha512).hexdigest().upper()


def format_amount(amount, currency: str) -> str:
    """KHR is zero-decimal; USD carries cents."""
    value = Decimal(str(amount or 0))
    if (currency or "").upper() == "KHR":
        return str(int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def clean_description(text: str) -> str:
    """purchaseDesc: short, plain ASCII (16 characters)."""
    ascii_only = re.sub(r"[^A-Za-z0-9 ._-]", "", str(text or "")).strip()
    return (ascii_only or "AI tokens")[:MAX_DESCRIPTION].strip()


def txid_for(payment, number: int) -> str:
    """"EPC-260928-1A2B3C4D", attempt 3 → "2609281A2B3C4D3" (≤ 16 characters).

    The date and random part of the reference are fixed-length, so the
    attempt number on the end can never make two orders' ids collide.
    """
    return f"{attempts.compact(payment.reference).removeprefix('EPC')}{number}"[:MAX_TXID]


def device_of(request) -> str:
    """oprDevice: 'ios' or 'android' (what ACLEDA mobile's deeplink needs)."""
    agent = (request.META.get("HTTP_USER_AGENT", "") if request is not None else "").lower()
    return "ios" if any(tag in agent for tag in ("iphone", "ipad", "ipod")) else "android"


def _redact(payload: Any) -> Any:
    """What may be logged or stored of a request or answer: never a secret."""
    if not isinstance(payload, dict):
        return payload
    return {key: ("***" if key in {"password", "hash", "loginId", "secret"} else value)
            for key, value in payload.items()}


def _result(data: Any) -> dict:
    """Both endpoints wrap their answer in a top-level ``result``."""
    if isinstance(data, dict):
        inner = data.get("result")
        return inner if isinstance(inner, dict) else data
    return {}


def _code(result: dict) -> int | None:
    try:
        return int(result.get("code"))
    except (TypeError, ValueError):
        return None


def _succeeded(result: dict) -> bool:
    return _code(result) == 0 and str(result.get("errorDetails") or "").strip().upper() == "SUCCESS"


class ACLEDAXPayProvider(PaymentProvider):
    key = "acleda_xpay"
    label = "ACLEDA Bank"
    description = "Pay on ACLEDA Bank's secure page — scan its KHQR with ACLEDA mobile or any Cambodian banking app."
    icon = "acleda"
    currencies = ("USD", "KHR")
    steps = (
        "Continue to ACLEDA Bank's secure payment page.",
        "Scan its KHQR with ACLEDA mobile or any Cambodian banking app.",
        "You come back here and the plan activates once ACLEDA confirms.",
    )
    #: When ACLEDA's KHQR is shown on our own page instead.
    qr_steps = (
        "Scan this KHQR with ACLEDA mobile or any Cambodian banking app. Paying on this phone? "
        "Screenshot it and pick \"scan from gallery\" in your bank app.",
        "This page updates by itself and the plan activates once ACLEDA confirms.",
    )
    required_env = ("ACLEDA_XPAY_BASE_URL", "ACLEDA_XPAY_MERCHANT_ID", "ACLEDA_XPAY_LOGIN_ID",
                    "ACLEDA_XPAY_PASSWORD", "ACLEDA_XPAY_SECRET")
    optional_env = ("ACLEDA_XPAY_MERCHANT_NAME", "ACLEDA_XPAY_CURRENCY")

    # -- configuration ------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        return (settings.ACLEDA_XPAY_BASE_URL or "").rstrip("/")

    @property
    def merchant_id(self) -> str:
        return settings.ACLEDA_XPAY_MERCHANT_ID

    @property
    def login_id(self) -> str:
        return settings.ACLEDA_XPAY_LOGIN_ID

    @property
    def password(self) -> str:
        return settings.ACLEDA_XPAY_PASSWORD

    @property
    def secret(self) -> str:
        return settings.ACLEDA_XPAY_SECRET

    @property
    def merchant_name(self) -> str:
        return settings.ACLEDA_XPAY_MERCHANT_NAME or self.login_id

    @property
    def is_configured(self) -> bool:
        return all([self.base_url, self.merchant_id, self.login_id, self.password, self.secret])

    @property
    def is_sandbox(self) -> bool:
        return "epayment.acledabank.com.kh" not in self.base_url or "uat" in self.base_url.lower()

    @property
    def supports_webhook(self) -> bool:
        return True

    @property
    def charge_currency(self) -> str:
        currency = (settings.ACLEDA_XPAY_CURRENCY or "USD").upper()
        return currency if currency in self.currencies else "USD"

    @property
    def payment_page(self) -> str:
        return f"{self.base_url}/{PAYMENT_PAGE_PATH}"

    # -- transport ----------------------------------------------------------------------------
    def _post(self, path: str, body: dict) -> dict:
        try:
            response = requests.post(
                f"{self.base_url}/{path}", json=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=settings.ACLEDA_XPAY_TIMEOUT,
            )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("XPAY %s failed: %s", path.rsplit("/", 1)[-1], type(exc).__name__)
            raise PaymentError("We could not reach ACLEDA Bank. Please try again shortly.", code="unreachable")
        return data if isinstance(data, dict) else {}

    # -- checkout ---------------------------------------------------------------------------------
    def create_checkout(self, payment) -> CheckoutSession:
        if not self.is_configured:
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
        # A session is opened only when the student presses Pay: it is
        # single-use, and one opened on every page view would be wasted.
        return CheckoutSession(kind="handoff", instructions=list(self.steps), order_id=payment.reference)

    def open_session(self, payment, txid: str, *, request=None) -> dict:
        currency = payment.pay_currency.upper()
        site = settings.SITE_URL.rstrip("/")
        transaction = {
            "txid": txid,
            "purchaseAmount": format_amount(payment.pay_amount, currency),
            "purchaseCurrency": currency,
            "purchaseDate": timezone.localtime().strftime("%d-%m-%Y"),
            "purchaseDesc": clean_description(payment.plan.name),
            "invoiceid": payment.reference[:MAX_INVOICE_ID],
            "item": "1",
            "quantity": "1",
            "expiryTime": str(self.expiry_minutes(payment)),
            # Asking for the deeplink as well: the same session still drives
            # the hosted page, so one session serves both.
            "oprDevice": device_of(request),
            "callBackUrl": f"{site}/payments/{payment.reference}/return/",
        }
        body = {
            "loginId": self.login_id,
            "password": self.password,
            "merchantID": self.merchant_id,
            "hash": sign(f"{self.merchant_id}{self.login_id}{self.password}{txid}", self.secret),
            "xpayTransaction": transaction,
        }
        data = self._post(OPEN_SESSION_PATH, body)
        result = _result(data)
        if not _succeeded(result):
            logger.error("XPAY openSessionV2 refused txid=%s: code=%s %s",
                         txid, result.get("code"), result.get("errorDetails"))
            raise PaymentError(
                "ACLEDA Bank could not start the payment. Please try again, or choose another method.",
                code="gateway_refused",
            )
        return {
            "session_id": str(result.get("sessionid") or ""),
            "token": str((result.get("xTran") or {}).get("paymentTokenid") or ""),
            "deeplink": str(result.get("deeplinkUrl") or ""),
            "amount": transaction["purchaseAmount"],
            "currency": currency,
        }

    @staticmethod
    def expiry_minutes(payment) -> int:
        if payment.expires_at:
            return max(3, int((payment.expires_at - timezone.now()).total_seconds() // 60) or 3)
        return 15

    def fetch_khqr(self, fields: dict) -> str:
        """Post the documented form to paymentPage.jsp ourselves and read the
        session's KHQR out of it. Empty when it cannot be read — never raises."""
        try:
            response = requests.post(self.payment_page, data=fields, timeout=settings.ACLEDA_XPAY_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("XPAY paymentPage.jsp unreachable: %s", type(exc).__name__)
            return ""
        page = response.text or ""
        found = _QR_PATH_RE.search(page) if response.status_code == 200 else None
        if not found:
            logger.warning("XPAY paymentPage.jsp carried no KHQR (HTTP %s, %s bytes): %s",
                           response.status_code, len(page), re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))[:160])
            return ""
        payload = found.group(1).strip()
        if not khqr_is_intact(payload):
            logger.warning("XPAY KHQR failed its checksum (%s characters)", len(payload))
            return ""
        return payload

    def start_attempt(self, payment, *, request=None, hosted: bool = False) -> Handoff:
        if not self.is_configured:
            raise PaymentError("ACLEDA Bank is not set up yet.", code="not_configured")
        number = attempts.count(payment) + 1
        if number > attempts.MAX_ATTEMPTS:
            raise PaymentError("This order has been tried too many times — please start a new one.",
                               code="too_many_attempts")
        txid = txid_for(payment, number)
        session = self.open_session(payment, txid, request=request)
        if not session["token"]:
            raise PaymentError("ACLEDA Bank did not return a payment session. Please try again.",
                               code="gateway_refused")
        attempts.record(payment, provider_ref=txid, token=session["token"], session_id=session["session_id"],
                        data={"deeplink": session["deeplink"], "amount": session["amount"],
                              "currency": session["currency"]})
        site = settings.SITE_URL.rstrip("/")
        back = f"{site}/payments/{payment.reference}/return/"
        # The exact hidden-input set the appendix's sample form posts. The
        # lowercase names are ACLEDA's, not a typo.
        fields = {
            "merchantID": self.merchant_id,
            "sessionid": session["session_id"],
            "paymenttokenid": session["token"],
            "description": clean_description(payment.plan.name),
            "expirytime": str(self.expiry_minutes(payment)),
            "amount": session["amount"],
            "quantity": "1",
            "item": "1",
            "invoiceid": payment.reference[:MAX_INVOICE_ID],
            "currencytype": session["currency"],
            "transactionID": txid,
            "successUrlToReturn": back,
            "errorUrl": f"{back}?outcome=error",
        }
        if not hosted:
            khqr = self.fetch_khqr(fields)
            if khqr:
                return Handoff(reference=txid, qr_payload=khqr)
            # The fetch spent this session (ACLEDA cancels one posted twice),
            # so the student's own trip to ACLEDA's page gets a fresh one.
            return self.start_attempt(payment, request=request, hosted=True)
        return Handoff(action=self.payment_page, fields=fields, reference=txid)

    # -- status -----------------------------------------------------------------------------------
    def transaction_status(self, token: str) -> VerificationResult:
        """getTxnStatus for one payment token — the only answer that settles."""
        body = {
            "loginId": self.login_id,
            "password": self.password,
            "merchantName": self.merchant_name,
            "hash": sign(f"{self.merchant_id}{self.login_id}{self.password}{token}", self.secret),
            "merchantId": self.merchant_id,
            "paymentTokenid": token,
        }
        data = self._post(TXN_STATUS_PATH, body)
        result = _result(data)
        code = _code(result)
        details = str(result.get("errorDetails") or "")
        if code == CODE_VERIFICATION_FAILED:
            logger.error("XPAY refused our credentials (code %s: %s)", code, details)
            raise PaymentError("ACLEDA Bank refused this site's credentials.", code="gateway_auth")

        transaction = result.get("xTran") or {}
        try:
            confirmed = int(transaction.get("confirmDate") or 0)
        except (TypeError, ValueError):
            confirmed = 0
        lowered = details.lower()
        if _succeeded(result) and confirmed > 0:
            status = "paid"
        elif _succeeded(result) or code == CODE_AWAITING_PAYMENT or any(m in lowered for m in _PENDING_MARKERS):
            status = "pending"
        elif any(m in lowered for m in _EXPIRED_MARKERS):
            status = "expired"
        elif any(m in lowered for m in _CANCELLED_MARKERS):
            status = "cancelled"
        else:
            status = "failed"

        amount = transaction.get("purchaseAmount")
        return VerificationResult(
            status=status,
            provider_txn_id=str(result.get("coreRefNum") or transaction.get("txid") or "") if status == "paid" else "",
            amount="" if amount in (None, "") or status != "paid" else str(amount),
            currency=str(transaction.get("purchaseCurrency") or "").upper(),
            message=f"ACLEDA: {details or 'no answer yet'}",
            raw={"result": _redact(result)},
        )

    def verify(self, payment) -> VerificationResult:
        if not self.is_configured:
            return VerificationResult(status="pending", message="Awaiting manual confirmation.")
        batch = [attempt for attempt in attempts.to_check(payment) if attempt.token]
        if not batch:
            return VerificationResult(status="pending", message="Waiting for the payment to be started.")
        newest = None
        for attempt in batch:
            result = self.transaction_status(attempt.token)
            if result.is_paid:
                return result
            newest = newest or result
        if newest.status != "pending":
            checked = {attempt.pk for attempt in batch}
            for attempt in payment.attempts.exclude(token="").order_by("-created_at", "-id")[:attempts.OLDER_LIMIT + 1]:
                if attempt.pk in checked:
                    continue
                result = self.transaction_status(attempt.token)
                if result.is_paid:
                    return result
        # A cancelled or expired *session* is not a failed order: the student
        # can press Pay again and get a new one until the order itself expires.
        if newest.status in {"cancelled", "expired"}:
            return VerificationResult(status="pending", message=newest.message, raw=newest.raw)
        return newest

    def health_check(self) -> tuple[bool, str]:
        """getTxnStatus for a token that cannot exist: "does not exist" means
        the login, password and Secret Code were all accepted."""
        if not self.is_configured:
            return False, "One of the five XPAY settings is missing."
        try:
            result = self.transaction_status(f"HEALTH{int(timezone.now().timestamp())}")
        except PaymentError as exc:
            if exc.code == "gateway_auth":
                return False, "ACLEDA rejected the credentials (transaction verification failed) — check the Secret Code, login and password."
            return False, exc.friendly_message
        answer = str((result.raw.get("result") or {}).get("errorDetails") or "")
        code = _code(result.raw.get("result") or {})
        if code == CODE_NOT_FOUND or "does not exist" in answer.lower():
            return True, f"ACLEDA answered: {answer} — credentials accepted."
        return False, f"Unexpected ACLEDA answer: {answer or 'none'}"

    # -- callback ---------------------------------------------------------------------------------
    def parse_webhook(self, request) -> tuple[str, VerificationResult]:
        """ACLEDA's push. Its claims are ignored: the token it names is looked
        up with getTxnStatus, signed with our own credentials."""
        if not self.is_configured:
            raise PaymentError("ACLEDA XPAY is not configured.", code="not_configured")
        data: dict = {}
        raw = (request.body or b"").decode("utf-8", "replace").strip()
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                data = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                data = {}
        if not data:
            data = (request.POST if request.method == "POST" else request.GET).dict()
        token = str(data.get("_paymenttokenid") or data.get("paymenttokenid") or "").strip()
        if not token:
            raise PaymentError("Callback is missing a payment token.", code="bad_callback")
        attempt = attempts.find(token=token)
        if attempt is None:
            return "", VerificationResult(status="pending", message="Unknown payment token.")
        return attempt.payment.reference, self.transaction_status(token)

    def webhook_ack(self, ok: bool = True) -> tuple[dict, int]:
        # Always 200, so ACLEDA stops retrying a token we will never know.
        return ({"ok": ok}, 200)
