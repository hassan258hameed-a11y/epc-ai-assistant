"""Browser notifications: the service worker, the web app manifest and the
small JSON API the Settings card and the worker talk to.

``/sw.js`` and ``/manifest.webmanifest`` are served from the site root — a
service worker only controls pages at or below its own path, so it cannot
live under ``/static/``. Both are public: the browser fetches them itself.

The API is for signed-in students only, JSON in and out, CSRF-protected like
every other POST on the site. The service worker has no DOM to read a token
from, so ``/api/push/key/`` hands one back along with the public key.
"""
from __future__ import annotations

import json
import logging

from django.conf import settings
from django.http import HttpResponse
from django.middleware.csrf import get_token
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.views.decorators.http import require_GET, require_POST

from AI.services import push_service
from AI.utils import ratelimit
from AI.utils.decorators import login_required_student
from AI.utils.request import json_error, json_ok, user_agent

logger = logging.getLogger("AI.views.push")

#: The light chrome of the student app (``--canvas`` / the header's theme-color).
THEME_COLOR = "#F4F5F7"
BACKGROUND_COLOR = "#ECEEF2"


# ---------------------------------------------------------------------------
# Public files
# ---------------------------------------------------------------------------
@require_GET
def service_worker(request):
    """The worker script. Never cached, so a new version reaches browsers."""
    # JSON literals: valid JavaScript strings, whatever the brand name holds.
    body = render_to_string(
        "push/sw.js",
        {
            "icon_js": json.dumps(static("brand/icon-192.png")),
            "badge_js": json.dumps(static("brand/badge-96.png")),
            "app_name_js": json.dumps(settings.BRAND.get("full_name", "")),
        },
    )
    response = HttpResponse(body, content_type="application/javascript; charset=utf-8")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response


@require_GET
def manifest(request):
    """What "Add to Home Screen" / "Install app" reads."""
    brand = settings.BRAND
    data = {
        "id": "/chat/",
        "name": brand.get("full_name") or brand.get("name"),
        "short_name": brand.get("name") or brand.get("full_name"),
        "description": brand.get("tagline", ""),
        "start_url": "/chat/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "theme_color": THEME_COLOR,
        "background_color": BACKGROUND_COLOR,
        "prefer_related_applications": False,
        "icons": [
            {"src": static("brand/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": static("brand/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": static("brand/icon-maskable-192.png"), "sizes": "192x192", "type": "image/png",
             "purpose": "maskable"},
            {"src": static("brand/icon-maskable-512.png"), "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }
    response = HttpResponse(
        json.dumps(data, ensure_ascii=False, indent=2), content_type="application/manifest+json; charset=utf-8",
    )
    response["Cache-Control"] = "public, max-age=3600"
    return response


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def _json_body(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _limited(request, scope: str, *, limit: int, window: int):
    status = ratelimit.check(f"push_{scope}", f"{scope}:{request.user.pk}", limit=limit, window=window)
    if status.allowed:
        return None
    return json_error(
        "Too many attempts. Please wait a moment and try again.",
        status=429, code="rate_limited", retry_after=status.retry_after,
    )


@require_GET
@login_required_student
def key(request):
    """The public key to subscribe with, whether push is set up, and a CSRF token."""
    return json_ok(
        configured=push_service.is_configured(),
        public_key=push_service.public_key(),
        csrf=get_token(request),
    )


@require_POST
@login_required_student
def subscribe(request):
    if not push_service.is_configured():
        return json_error("Browser notifications aren't set up on this site yet.", status=503, code="not_configured")
    limited = _limited(request, "subscribe", limit=30, window=600)
    if limited:
        return limited
    data = _json_body(request)
    subscription = data.get("subscription", data)
    old_endpoint = data.get("old_endpoint") if isinstance(data.get("old_endpoint"), str) else ""
    try:
        push_service.subscribe(request.user, subscription, user_agent(request), old_endpoint=old_endpoint)
    except push_service.PushError as exc:
        return json_error(exc.friendly_message, status=400, code="invalid_subscription")
    return json_ok(subscribed=True, devices=push_service.device_count(request.user))


@require_POST
@login_required_student
def unsubscribe(request):
    limited = _limited(request, "unsubscribe", limit=30, window=600)
    if limited:
        return limited
    data = _json_body(request)
    endpoint = data.get("endpoint")
    if not endpoint and isinstance(data.get("subscription"), dict):
        endpoint = data["subscription"].get("endpoint")
    removed = push_service.unsubscribe(request.user, endpoint if isinstance(endpoint, str) else "")
    return json_ok(removed=removed, devices=push_service.device_count(request.user))


@require_POST
@login_required_student
def test(request):
    if not push_service.is_configured():
        return json_error("Browser notifications aren't set up on this site yet.", status=503, code="not_configured")
    limited = _limited(request, "test", limit=5, window=600)
    if limited:
        return limited
    if not push_service.device_count(request.user):
        return json_error("Turn notifications on for this device first.", status=409, code="no_devices")
    sent = push_service.send_test(request.user)
    if not sent:
        return json_error(
            "The test couldn't be delivered. Turn notifications off and on again, then retry.",
            status=502, code="not_delivered",
        )
    return json_ok(sent=sent)
