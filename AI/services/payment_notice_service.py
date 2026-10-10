"""What a student is told about their payment, everywhere at once.

In the app, in their private Telegram chat (when linked and allowed), and by
e-mail (unless they turned e-mail off). Every channel is best-effort: a
notification that fails must never undo or delay the payment it describes.
"""
from __future__ import annotations

import logging

from AI.models import EmailType, NotificationCategory, NotificationLevel
from AI.services import mail_service, notification_service, telegram_service

logger = logging.getLogger("AI.payments.notice")


def _line(payment) -> str:
    extra = f" (≈ {payment.currency} {payment.amount})" if payment.is_converted else ""
    return f"{payment.pay_display}{extra}"


def confirmed(payment) -> None:
    """Paid: the in-app notice (with its Telegram copy), then the e-mail receipt."""
    notification_service.payment_confirmed(payment)
    try:
        mail_service.send_notice(
            payment.user,
            email_type=EmailType.PAYMENT,
            subject=f"Payment received — {payment.plan.name} [{payment.reference}]",
            heading="Payment received — thank you",
            intro=f"your {payment.plan.name} plan is active.",
            body=(
                f"Amount: {_line(payment)}\n"
                f"Method: {payment.get_method_display()}\n"
                f"Tokens added: {payment.tokens_to_grant:,}\n"
                f"Reference: {payment.reference}"
            ),
            button_label="View receipt",
            button_path=f"/payments/{payment.reference}/receipt/",
            reason=f"because you paid for {payment.plan.name}.",
            trigger=f"payment {payment.reference} paid",
        )
    except Exception:  # pragma: no cover
        logger.exception("Receipt e-mail failed for %s", payment.reference)


OUTCOMES = {
    "failed": ("Payment not completed", "The payment did not go through, and nothing was charged by us."),
    "expired": ("Payment window closed", "The order expired before a payment arrived. Nothing was charged."),
}


def not_completed(payment, outcome: str, reason: str = "") -> None:
    """Failed or expired: say so plainly, and how to try again."""
    title, sentence = OUTCOMES.get(outcome, OUTCOMES["failed"])
    body = f"{payment.plan.name} · {_line(payment)} · {payment.reference}. {sentence}"
    notification_service.notify(
        payment.user, title, body,
        level=NotificationLevel.WARNING,
        category=NotificationCategory.PAYMENT,
        action_url="/app/pricing/",
        action_label="Try again",
        telegram=True,
        telegram_text=(
            f"<b>{telegram_service.escape_html(title)}</b>\n\n"
            f"Plan: <b>{telegram_service.escape_html(payment.plan.name)}</b>\n"
            f"Amount: {telegram_service.escape_html(_line(payment))}\n"
            f"Reference: <code>{telegram_service.escape_html(payment.reference)}</code>\n\n"
            f"{telegram_service.escape_html(sentence)}"
        ),
    )
    try:
        mail_service.send_notice(
            payment.user,
            email_type=EmailType.PAYMENT,
            subject=f"{title} [{payment.reference}]",
            heading=title,
            intro=f"about your {payment.plan.name} order.",
            body=f"{sentence}\nAmount: {_line(payment)}\nReference: {payment.reference}"
                 + (f"\nDetail: {reason}" if reason and outcome == "failed" else ""),
            button_label="Choose a plan again",
            button_path="/app/pricing/",
            reason=f"because you started a payment for {payment.plan.name}.",
            trigger=f"payment {payment.reference} {outcome}",
        )
    except Exception:  # pragma: no cover
        logger.exception("Payment e-mail failed for %s", payment.reference)
