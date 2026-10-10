"""Small helpers for working with the incoming request."""
from __future__ import annotations

from django.http import JsonResponse


def client_ip(request) -> str | None:
    """Best-effort client IP, honouring a single proxy hop."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        candidate = forwarded.split(",")[0].strip()
        if candidate:
            return candidate
    return request.META.get("REMOTE_ADDR") or None


def user_agent(request) -> str:
    return (request.META.get("HTTP_USER_AGENT") or "")[:255]


def is_ajax(request) -> bool:
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
    )


def json_error(message: str, *, status: int = 400, code: str = "error", **extra) -> JsonResponse:
    payload = {"ok": False, "error": message, "code": code}
    payload.update(extra)
    return JsonResponse(payload, status=status)


def json_ok(**payload) -> JsonResponse:
    data = {"ok": True}
    data.update(payload)
    return JsonResponse(data)


def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return email or ""
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        masked = name[0] + "*"
    else:
        masked = name[0] + "*" * (len(name) - 2) + name[-1]
    return f"{masked}@{domain}"
