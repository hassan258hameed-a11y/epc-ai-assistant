"""How students reach a person.

One place that turns the number an administrator typed into the several shapes
the Help page needs — a readable label, a ``wa.me`` link, and a QR code that
opens the same chat from a phone camera.

The number is configuration, not a constant, so changing it is a field in the
admin console rather than an edit here.  The QR is drawn server-side as inline
SVG, so it needs no image file, no external service, and no request to a third
party that would tell them who is looking at our Help page.
"""
from __future__ import annotations

import re

from AI.services import settings_service
from AI.utils import qr


def digits(raw: str) -> str:
    """Just the digits, with a leading ``00`` international prefix collapsed.

    ``wa.me`` wants a bare international number: no plus, no spaces, no dashes,
    and no trunk zero. Everything a person might reasonably type is normalised
    here rather than being the administrator's problem to get exactly right.
    """
    cleaned = re.sub(r"\D", "", raw or "")
    if cleaned.startswith("00"):
        cleaned = cleaned[2:]
    return cleaned


def display(raw: str) -> str:
    """The number as a person should read it: ``+92 316 2211830``.

    Country code, then the operator prefix, then the rest in one block. Deliberately
    not grouped every three digits — that leaves a stray digit dangling on the
    end of most real numbers and reads worse than not grouping at all.
    """
    cleaned = digits(raw)
    if not cleaned:
        return ""
    if len(cleaned) <= 5:
        return f"+{cleaned}"
    return f"+{cleaned[:2]} {cleaned[2:5]} {cleaned[5:]}"


def whatsapp() -> dict:
    """Everything the Help page needs to offer WhatsApp, or ``available`` False.

    A blank or unusable number produces a row with ``available`` set to False
    rather than a broken link — offering a button that goes nowhere is worse
    than not offering one.
    """
    raw = str(settings_service.get("support_whatsapp", "") or "")
    number = digits(raw)
    message = str(settings_service.get("support_whatsapp_message", "") or "").strip()

    if len(number) < 8:
        return {"available": False, "number": "", "display": "", "url": "", "qr": ""}

    url = f"https://wa.me/{number}"
    if message:
        from urllib.parse import quote

        url = f"{url}?text={quote(message)}"

    return {
        "available": True,
        "number": number,
        "display": display(raw),
        "url": url,
        # The QR encodes the same link the button opens, so scanning it and
        # tapping it land in exactly the same conversation.
        "qr": qr.svg(url, scale=4, border=2, dark="currentColor"),
        "message": message,
    }


def support() -> dict:
    """The whole contact block: WhatsApp, e-mail and when anybody is reading."""
    return {
        "whatsapp": whatsapp(),
        "email": str(settings_service.get("support_email", "") or "").strip(),
        "telegram": str(settings_service.get("support_telegram", "") or "").strip(),
        "hours": str(settings_service.get("support_hours", "") or "").strip(),
    }


__all__ = ["digits", "display", "support", "whatsapp"]
