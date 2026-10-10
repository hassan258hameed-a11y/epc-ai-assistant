"""Payment provider contract.

Adding a provider means subclassing :class:`PaymentProvider` and registering it
in ``__init__.py`` — nothing else in the application changes.

Three rules hold for every provider:

* An order is *never* marked paid from a browser redirect, a success page or a
  screenshot. Only the gateway's own answer counts: a callback whose signature
  checks out, or a status query the server made itself.
* The amount and currency the gateway reports must match what was asked for.
* Crediting is idempotent — settlement runs once, however many confirmations
  arrive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass
class CheckoutSession:
    """What the student needs in order to pay.

    ``kind`` drives the payment page:
      ``handoff``  — a "Pay" button that opens a fresh attempt at a hosted
                     gateway (see :meth:`PaymentProvider.start_attempt`)
      ``redirect`` — POST ``fields`` to ``redirect_url`` (a hosted checkout page)
      ``qr``       — show ``qr_payload`` as a QR code (and any ``deeplink``)
      ``manual``   — transfer instructions for an administrator to confirm
    """

    kind: str
    redirect_url: str = ""
    qr_payload: str = ""
    qr_image_url: str = ""
    deeplink: str = ""
    checkout_url: str = ""
    instructions: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)
    expires_in: int = 900
    #: The gateway's id for the order, kept on the payment for queries.
    order_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "redirect_url": self.redirect_url,
            "qr_payload": self.qr_payload,
            "qr_image_url": self.qr_image_url,
            "deeplink": self.deeplink,
            "checkout_url": self.checkout_url,
            "instructions": self.instructions,
            "fields": self.fields,
            "expires_in": self.expires_in,
        }


@dataclass
class Handoff:
    """One attempt at a hosted gateway: a form the browser POSTs to it, or —
    when the gateway's own QR could be read — that QR to show on our page."""

    action: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    #: The gateway's reference for this attempt (tran_id, txid).
    reference: str = ""
    #: How the form is encoded. PayWay takes multipart; ACLEDA's page only
    #: reads a plain urlencoded form (multipart gets its "system updating" page).
    enctype: str = "application/x-www-form-urlencoded"
    #: The gateway's KHQR for this attempt, when it is shown on our page.
    qr_payload: str = ""


@dataclass
class VerificationResult:
    """Outcome of asking the provider whether a payment really happened."""

    status: str  # pending | paid | failed | expired | cancelled
    provider_txn_id: str = ""
    amount: str = ""
    currency: str = ""
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    #: The purchase as we sent it, echoed back — for a gateway that reports
    #: the *settled* pair in ``amount``/``currency`` (a riel payment settled
    #: in dollars, say). Either pair matching the order is a match.
    original_amount: str = ""
    original_currency: str = ""

    @property
    def is_paid(self) -> bool:
        return self.status == "paid"


class PaymentError(Exception):
    def __init__(self, message: str, *, code: str = "payment_error"):
        self.friendly_message = message
        self.code = code
        super().__init__(message)


class PaymentProvider:
    key: str = "base"
    label: str = "Payment"
    description: str = ""
    icon: str = "card"
    currencies: tuple[str, ...] = ("USD",)
    #: Short lines for the checkout's "how it works" under each method.
    steps: tuple[str, ...] = ()
    #: Environment variables needed for automatic confirmation (names only —
    #: the setup page shows whether each is set, never its value).
    required_env: tuple[str, ...] = ()
    #: Optional environment variables the method also reads.
    optional_env: tuple[str, ...] = ()

    @property
    def is_configured(self) -> bool:
        """True when live (or sandbox) credentials are present in the environment."""
        return False

    @property
    def is_sandbox(self) -> bool:
        """True when pointed at a test environment rather than production."""
        return False

    @property
    def supports_webhook(self) -> bool:
        return False

    @property
    def charge_currency(self) -> str:
        """The currency this method charges in: KHR, USD or USDT."""
        return self.currencies[0]

    def create_checkout(self, payment) -> CheckoutSession:
        raise NotImplementedError

    def start_attempt(self, payment, *, request=None, hosted: bool = False) -> Handoff:
        """A fresh, signed hand-off to the gateway for one press of "Pay".

        Only for ``handoff`` checkouts. Records a :class:`PaymentAttempt` and
        raises :class:`PaymentError` when the gateway refuses to open one.
        ``hosted`` asks for the gateway's own page even where its QR could be
        shown on ours.
        """
        raise NotImplementedError

    def notices(self) -> list[str]:
        """Warnings for the gateways page (a key about to expire, say)."""
        return []

    def result_matches(self, payment, result: "VerificationResult") -> bool:
        """The gateway's figures describe this order, in either pair it reports."""
        pairs = [(result.amount, result.currency)]
        if result.original_amount not in (None, ""):
            pairs.append((result.original_amount, result.original_currency))
        if all(amount in (None, "") for amount, _ in pairs):
            return self.amount_matches(payment.pay_amount, payment.pay_currency, "", result.currency)
        return any(
            amount not in (None, "")
            and self.amount_matches(payment.pay_amount, payment.pay_currency, amount, currency)
            for amount, currency in pairs
        )

    def verify(self, payment) -> VerificationResult:
        """Ask the provider for the authoritative status of a payment."""
        raise NotImplementedError

    def parse_webhook(self, request) -> tuple[str, VerificationResult]:
        """Return ``(payment_reference, result)`` from an authenticated callback.

        An implementation must authenticate the callback — a signature check,
        or a server-side status query that does not trust the callback's own
        claims — and raise :class:`PaymentError` when it cannot.
        """
        raise NotImplementedError

    def webhook_ack(self, ok: bool = True) -> tuple[dict, int]:
        """The body and status the gateway expects back from our callback URL."""
        return ({"ok": ok}, 200 if ok else 400)

    def close_order(self, payment) -> None:
        """Tell the gateway an order will not be paid (cancel/expiry). Best effort."""

    def health_check(self) -> tuple[bool, str]:
        """One harmless authenticated call: can we reach the gateway, and does it
        accept our credentials? Returns ``(ok, what happened)``."""
        return False, "Not configured."

    # -- shared helpers ---------------------------------------------------
    def manual_fallback(self, payment, instructions: list[str], fields: dict[str, str]) -> CheckoutSession:
        return CheckoutSession(kind="manual", instructions=instructions, fields=fields)

    @staticmethod
    def amount_matches(expected, currency: str, reported_amount, reported_currency: str) -> bool:
        """True when the gateway's figure is the one that was asked for.

        A missing figure is treated as a match only when the gateway does not
        report one at all — some confirmations carry no amount.
        """
        if reported_currency and currency and reported_currency.upper() != currency.upper():
            return False
        if reported_amount in (None, ""):
            return True
        try:
            return abs(Decimal(str(reported_amount)) - Decimal(str(expected))) < Decimal("0.01")
        except (InvalidOperation, TypeError):
            return False
