"""Request middleware: single-session enforcement and live account checks."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import get_user_model, logout
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone

from AI.services import settings_service
from AI.utils.request import is_ajax

#: ``/sw.js`` and the manifest are fetched by the browser itself (an update
#: check can come from a session that was since replaced); redirecting them to
#: the login page would break the installed service worker's update.
EXEMPT_PREFIXES = (
    "/static/", "/media/", "/webhooks/", "/logout/", "/favicon.ico", "/sw.js", "/manifest.webmanifest",
)


def _exempt(path: str) -> bool:
    return path.startswith(EXEMPT_PREFIXES)


class SingleSessionMiddleware:
    """Only one active session per account.

    Signing in elsewhere rotates ``active_session_key``; any older session is
    terminated on its next request.  Enforced server-side — a stale cookie has
    no value.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and not _exempt(request.path)
            and settings_service.get("single_session", True)
        ):
            current_key = request.session.session_key
            stored_key = user.active_session_key
            if stored_key and current_key and stored_key != current_key:
                logout(request)
                if is_ajax(request):
                    return JsonResponse(
                        {
                            "ok": False,
                            "code": "session_replaced",
                            "error": "You signed in on another device. Please sign in again.",
                        },
                        status=401,
                    )
                messages.warning(
                    request, "You were signed out because your account was used on another device."
                )
                return redirect("/login/")

        return self.get_response(request)


class AccountStatusMiddleware:
    """Terminate the session the moment an account is disabled.

    Also records a coarse ``last_seen_at`` used by the admin activity metrics —
    written at most once every five minutes to avoid a write per request.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and not _exempt(request.path):
            if not user.is_active_account:
                logout(request)
                if is_ajax(request):
                    return JsonResponse(
                        {"ok": False, "code": "account_disabled", "error": "This account has been disabled."},
                        status=403,
                    )
                messages.error(request, "This account has been disabled. Please contact support.")
                return redirect("/login/")

            now = timezone.now()
            last_seen = user.last_seen_at
            if last_seen is None or (now - last_seen).total_seconds() > 300:
                # request.user is lazy, so go through the model rather than type().
                get_user_model().objects.filter(pk=user.pk).update(last_seen_at=now)
                user.last_seen_at = now

        return self.get_response(request)


class StaffAreaMiddleware:
    """Keep each administrator inside the console areas their staff role allows.

    The one place the staff-role matrix is enforced. It runs after the URL is
    resolved and before the view, so every console route — page, form post,
    JSON endpoint, CSV download — is covered by the same check, and a view
    added later is covered without anybody remembering to decorate it. What a
    role may open is decided by ``AI/services/staff_permissions.py``.

    It only acts on signed-in, active administrators inside the ``console``
    namespace. Anonymous visitors, students and disabled accounts fall through
    to ``admin_required`` and the middleware above, which handle them exactly as
    before; the admin sign-in and sign-out pages live outside the namespace.

    The ⌘K search is the one response it touches on the way out: results are
    trimmed to records the role could open, so a search never lists a payment
    to somebody who would get this page for clicking it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        match = getattr(request, "resolver_match", None)
        if (
            match is not None
            and match.view_name == "console:search"
            and getattr(response, "status_code", 0) == 200
            and response.get("Content-Type", "").startswith("application/json")
        ):
            response = self._trim_search(request, response)
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = getattr(request, "resolver_match", None)
        if match is None or match.namespace != "console":
            return None
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or not user.is_admin_user or not user.is_active_account:
            return None

        from AI.services import staff_permissions

        post = request.POST if request.method == "POST" and staff_permissions.reads_form(match.view_name) else None
        decision = staff_permissions.check(user, match.view_name, post=post, kwargs=view_kwargs)
        if decision.allowed:
            return None
        return self._forbidden(request, decision)

    @staticmethod
    def _forbidden(request, decision):
        from django.shortcuts import render

        from AI.services import staff_permissions

        if is_ajax(request):
            return JsonResponse(
                {"ok": False, "code": "forbidden_area", "area": decision.area, "error": decision.message},
                status=403,
            )
        role = staff_permissions.role_of(request.user)
        owners = list(
            staff_permissions.active_owners().exclude(pk=request.user.pk).order_by("full_name", "username")[:3]
        )
        return render(
            request,
            "console/_forbidden.html",
            {
                "nav_active": "",
                # Not a page about a model, whatever the blocked URL was.
                "show_model_scope": False,
                "decision": decision,
                "forbidden_area": staff_permissions.area_label(decision.area),
                "forbidden_role": staff_permissions.role_label(role),
                "forbidden_owners": owners,
            },
            status=403,
        )

    @staticmethod
    def _trim_search(request, response):
        import json

        from AI.services import staff_permissions

        user = getattr(request, "user", None)
        if user is None or staff_permissions.is_owner(user):
            return response
        try:
            payload = json.loads(response.content)
        except (TypeError, ValueError):  # pragma: no cover — not ours to fix here
            return response
        return JsonResponse(staff_permissions.filter_search_payload(user, payload), status=response.status_code)
