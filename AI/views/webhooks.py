"""Provider callbacks.

These endpoints are CSRF exempt because they are machine-to-machine, so each one
authenticates the caller itself: payment webhooks verify an HMAC signature and
the Telegram webhook checks the shared secret header.
"""
from __future__ import annotations

import json
import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from AI.services import payment_service, telegram_service
from AI.services.payments import PaymentError
from AI.utils import ratelimit
from AI.utils.ratelimit import RateLimited
from AI.utils.request import client_ip

logger = logging.getLogger("AI.webhooks")


@csrf_exempt
@require_POST
def payment_webhook(request, method: str):
    try:
        ratelimit.enforce("payment_create", f"webhook:{client_ip(request)}", limit=120, window=60)
    except RateLimited:
        return JsonResponse({"ok": False, "error": "rate_limited"}, status=429)

    try:
        body, status = payment_service.handle_webhook(method, request)
    except PaymentError as exc:
        logger.warning("Rejected %s webhook: %s", method, exc.code)
        # Do not echo details back to an unauthenticated caller. An unreachable
        # gateway on our side is a 503, so the gateway retries later.
        return JsonResponse({"ok": False}, status=503 if exc.code == "unreachable" else 400)
    except Exception:  # pragma: no cover
        logger.exception("Webhook handler crashed for method=%s", method)
        return JsonResponse({"ok": False}, status=500)

    # Each gateway expects its own acknowledgement (Binance: returnCode SUCCESS).
    return JsonResponse(body, status=status, json_dumps_params={"separators": (",", ":")})


@csrf_exempt
@require_POST
def telegram_webhook(request):
    """Updates from Telegram, authenticated by the secret we registered.

    The secret is what stops anyone on the internet from posting a forged
    ``/start <token>`` or "user blocked the bot" update. In production it is
    mandatory — no secret configured means every call is refused, rather than
    every call being trusted. Only a DEBUG machine may run without one.
    """
    import hmac

    secret = settings.TELEGRAM_WEBHOOK_SECRET
    if secret or not settings.DEBUG:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secret or not hmac.compare_digest(supplied.encode(), secret.encode()):
            logger.warning(
                "Rejected Telegram webhook: %s",
                "TELEGRAM_WEBHOOK_SECRET is not set" if not secret else "bad secret token",
            )
            return HttpResponse(status=403)

    try:
        update = json.loads(request.body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=400)

    try:
        status = telegram_service.handle_update(update)
    except Exception:  # pragma: no cover — Telegram retries on non-200
        logger.exception("Telegram update handling failed")
        return HttpResponse(status=200)

    return JsonResponse({"ok": True, "status": status})
