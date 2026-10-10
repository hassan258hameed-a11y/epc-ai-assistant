"""Payment gateways — is each one set up, where must its callbacks point, and do
its credentials actually work.

Credentials live in the environment (``.env``), never in the database, so this
page shows *whether* each variable is set and never what it holds.
"""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.db.models import Count
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from AI.models import AuditAction, Payment, PaymentEvent, PaymentStatus
from AI.services import audit_service, payment_service, settings_service
from AI.services.payments import PaymentError, available_providers, get_provider
from AI.utils.decorators import admin_required
from AI.views.console_insights import _context

HEALTH_SESSION_KEY = "gateway_health"

#: What an administrator does, in order, to take each gateway live.
GUIDES = {
    "aba_payway": [
        "Apply for an ABA PayWay merchant account at payway.com.kh (business registration and bank account at ABA).",
        "ABA sends sandbox credentials first: a merchant ID and an API key (PayWay calls it the \"public key\").",
        "Put them in .env as ABA_PAYWAY_MERCHANT_ID and ABA_PAYWAY_API_KEY; keep ABA_PAYWAY_BASE_URL on the sandbox.",
        "Give ABA your website domain to whitelist and the pushback URL below.",
        "Ask ABA to enable KHR on the account (ABA_PAYWAY_CURRENCY=KHR), or set it to USD.",
        "Pay a few sandbox orders, press Test connection, then ask ABA for production credentials.",
        "Swap in the production merchant ID and key, set ABA_PAYWAY_BASE_URL=https://checkout.payway.com.kh, restart.",
        "Put the key's expiry date from ABA's sheet in ABA_PAYWAY_KEY_EXPIRES (YYYY-MM-DD) so this page warns before it lapses.",
    ],
    "acleda_xpay": [
        "ACLEDA issues an XPAY merchant after its SIT review, with a \"Credential (Production) Document\".",
        "From the document: ACLEDA_XPAY_MERCHANT_ID (Merchant ID), ACLEDA_XPAY_LOGIN_ID (Remote login), "
        "ACLEDA_XPAY_PASSWORD (Remote Password), ACLEDA_XPAY_SECRET (Secret Code).",
        "ACLEDA_XPAY_BASE_URL is the document's URL up to, not including, /XPAYConnectorServiceInterfaceImplV2 — "
        "e.g. https://epayment.acledabank.com.kh/YOURMERCHANT.",
        "Restart, then press Test connection: it looks up a transaction that cannot exist, which only answers "
        "\"does not exist\" when the login, password and Secret Code are all right.",
        "Optional: give ACLEDA the callback URL below. Payments confirm without it — the site asks ACLEDA when "
        "the student comes back, while the page is open, and on a background sweep.",
        "Pay one small real order from ACLEDA mobile, check it activates by itself, and refund it in ACLEDA's merchant portal.",
    ],
    "acleda_khqr": [
        "Open (or use) an ACLEDA account with a Bakong account ID — it looks like yourname@aclb. A merchant KHQR "
        "from ACLEDA also gives you a merchant ID.",
        "Set ACLEDA_KHQR_BAKONG_ID, ACLEDA_KHQR_ACCOUNT_NAME (as shown on the account, 25 characters max) and, for "
        "a merchant QR, ACLEDA_KHQR_MERCHANT_ID. The QR works from this point, with manual confirmation.",
        "Register at api-bakong.nbc.gov.kh with the same e-mail to get a Bakong Open API token; set BAKONG_API_TOKEN.",
        "Press Test connection. The Bakong API may only answer from a Cambodian IP address — host in Cambodia or "
        "ask NBC to allow your server's IP.",
        "The token expires every 90 days. Renew it on the Bakong site and update .env before it lapses.",
        "Pay one small KHQR order from ACLEDA mobile and check it confirms by itself.",
    ],
    "binance": [
        "Apply at merchant.binance.com — a verified individual Binance account can apply, or a business "
        "(entity) account. One merchant account per Binance account.",
        "In the Merchant Admin portal → Developer → API Keys, create a key; copy the API key and the secret "
        "(the secret is shown once). If you bind the key to IP addresses, add this server's public IP.",
        "Set BINANCE_PAY_API_KEY and BINANCE_PAY_API_SECRET in .env (and BINANCE_PAY_MERCHANT_ID for reference); "
        "keep BINANCE_PAY_BASE_URL=https://bpay.binanceapi.com. Restart.",
        "Press Test connection — a signed call that fails on a wrong key or secret, or a server clock that is off.",
        "On a public HTTPS domain, add the webhook URL below in Developer → Webhooks. Each order also carries it, "
        "and payments confirm without it while the page is open and on the background sweep.",
        "Binance Pay has no sandbox: pay one small real order (the cheapest plan), check it activates, then refund "
        "it in the Merchant Admin portal and press Refund on the order here.",
    ],
}


def _mode(provider) -> str:
    if settings.PAYMENT_EMULATOR:
        return "emulator"
    if provider.is_configured:
        return "sandbox" if provider.is_sandbox else "live"
    if getattr(provider, "can_generate", False):
        return "qr_only"
    return "manual"


def _callbacks(key: str, site: str) -> list[dict]:
    if key == "aba_payway":
        return [
            {"label": "Pushback URL (give to ABA)", "url": f"{site}/webhooks/payments/aba_payway/"},
            {"label": "Domain to whitelist", "url": urlsplit(site).netloc or site},
            {"label": "Browser return (sent with each order)", "url": f"{site}/payments/<reference>/return/"},
        ]
    if key == "acleda_xpay":
        return [
            {"label": "Callback URL (optional — give to ACLEDA)", "url": f"{site}/webhooks/payments/acleda_xpay/"},
            {"label": "Browser return (sent with each session)", "url": f"{site}/payments/<reference>/return/"},
        ]
    if key == "binance":
        return [
            {"label": "Webhook URL (Merchant Admin → Webhook)", "url": f"{site}/webhooks/payments/binance/"},
            {"label": "Browser return (sent with each order)", "url": f"{site}/payments/<reference>/return/"},
        ]
    return []


@admin_required
def gateways(request):
    site = settings.SITE_URL.rstrip("/")
    week = timezone.now() - timedelta(days=7)
    health = request.session.get(HEALTH_SESSION_KEY, {})
    rows = []
    for provider in available_providers():
        env = [{"name": name, "set": bool(getattr(settings, name, "")), "required": True}
               for name in provider.required_env]
        env += [{"name": name, "set": bool(getattr(settings, name, "")), "required": False}
                for name in provider.optional_env]
        counts = dict(
            Payment.objects.filter(method=provider.key, created_at__gte=week)
            .values_list("status").annotate(n=Count("id")).values_list("status", "n")
        )
        rows.append({
            "provider": provider,
            "key": provider.key,
            "mode": _mode(provider),
            "currency": provider.charge_currency,
            "env": env,
            "missing": [row["name"] for row in env if row["required"] and not row["set"]],
            "callbacks": _callbacks(provider.key, site),
            "polls": provider.key == "acleda_khqr",
            "notices": provider.notices(),
            "last_webhook": PaymentEvent.objects.filter(payment__method=provider.key, event_type="webhook")
                                        .order_by("-created_at").first(),
            "last_paid": Payment.objects.filter(method=provider.key, status=PaymentStatus.PAID)
                                        .order_by("-paid_at").first(),
            "week": {
                "paid": counts.get(PaymentStatus.PAID, 0),
                "pending": counts.get(PaymentStatus.PENDING, 0),
                "expired": counts.get(PaymentStatus.EXPIRED, 0),
                "failed": counts.get(PaymentStatus.FAILED, 0) + counts.get(PaymentStatus.CANCELLED, 0),
            },
            "health": health.get(provider.key),
            "guide": GUIDES.get(provider.key, []),
        })

    flagged = (
        PaymentEvent.objects.filter(event_type__in=("amount_mismatch", "duplicate_transaction"),
                                    payment__status=PaymentStatus.PENDING)
        .select_related("payment", "payment__user").order_by("-created_at")[:10]
    )
    return render(
        request,
        "console/payment_gateways.html",
        _context(
            "gateways",
            rows=rows,
            site_url=site,
            site_is_https=site.startswith("https://"),
            site_is_local=any(host in site for host in ("127.0.0.1", "localhost", "0.0.0.0")),
            khr_rate=payment_service.khr_rate(),
            expiry_minutes=payment_service.expiry_minutes(),
            manual_review=settings_service.get("payment_manual_review", True),
            emulator=settings.PAYMENT_EMULATOR,
            flagged=flagged,
        ),
    )


@admin_required
@require_POST
def gateway_test(request, key: str):
    """Make one harmless authenticated call and remember what came back."""
    try:
        provider = get_provider(key)
    except PaymentError:
        messages.error(request, "Unknown payment gateway.")
        return redirect("console:gateways")
    ok, message = provider.health_check()
    results = request.session.get(HEALTH_SESSION_KEY, {})
    results[key] = {"ok": ok, "message": message[:300], "at": timezone.localtime().strftime("%d %b %Y, %H:%M")}
    request.session[HEALTH_SESSION_KEY] = results
    audit_service.record(
        AuditAction.SETTINGS_UPDATE, actor=request.user, request=request,
        description=f"Tested {provider.label} connection: {'ok' if ok else 'failed'}",
    )
    return redirect(reverse("console:gateways") + f"#gw-{key}")
