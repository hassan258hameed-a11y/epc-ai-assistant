"""One-off notices by e-mail: a support reply, a message from the team.

Every attempt is written to the e-mail log — sent, failed or skipped — because
"no e-mail arrived" and "we never tried" are different problems with different
fixes. A mail server that is down never takes the calling operation with it.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from AI.models import EmailLog, EmailStatus, NotificationPreference

logger = logging.getLogger("AI.mail")


def wants_email(user) -> bool:
    """The student's own switch. Read fresh, never from a cached relation."""
    prefs = NotificationPreference.objects.filter(user=user).values("email_enabled").first()
    return prefs is None or prefs["email_enabled"]


def send_notice(
    user,
    *,
    email_type: str,
    subject: str,
    heading: str,
    intro: str,
    body: str = "",
    button_label: str = "",
    button_path: str = "",
    reason: str = "",
    recipient: str = "",
    trigger: str = "",
) -> EmailLog:
    """Send one notice and log it. ``button_path`` is an in-site path."""
    recipient = (recipient or user.email or "").strip()
    log = EmailLog(user=user, recipient=recipient, email_type=email_type, subject=subject[:190],
                   trigger=trigger[:120])

    if not recipient:
        log.status = EmailStatus.SKIPPED
        log.error = "No e-mail address on the account."
        log.save()
        return log
    if not wants_email(user):
        log.status = EmailStatus.SKIPPED
        log.error = "The student turned e-mail off."
        log.save()
        return log

    site = settings.SITE_URL.rstrip("/")
    context = {
        "user": user,
        "brand": settings.BRAND,
        "heading": heading,
        "intro": intro,
        "body": body,
        "button_label": button_label,
        "button_url": f"{site}{button_path}" if button_path else "",
        "reason": reason,
        "recipient": recipient,
    }
    try:
        message = EmailMultiAlternatives(
            subject, render_to_string("emails/notice.txt", context), settings.DEFAULT_FROM_EMAIL, [recipient],
        )
        message.attach_alternative(render_to_string("emails/notice.html", context), "text/html")
        message.send(fail_silently=False)
        log.status = EmailStatus.SENT
    except Exception as exc:  # noqa: BLE001 - recorded, not raised
        logger.warning("notice e-mail to %s failed: %s", recipient, exc)
        log.status = EmailStatus.FAILED
        log.error = str(exc)[:255]
    log.save()
    return log
