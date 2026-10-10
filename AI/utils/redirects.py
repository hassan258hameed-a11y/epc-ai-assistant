"""Where to send someone after a POST, when the form says where it came from.

A console form often carries ``<input name="next">`` so the administrator lands
back on the page they acted from. That value arrives from the browser, so it is
only honoured when it points at this site — otherwise a crafted link could
bounce a signed-in administrator to anywhere on the internet.
"""
from __future__ import annotations

from django.shortcuts import resolve_url
from django.utils.http import url_has_allowed_host_and_scheme


def next_url(request) -> str:
    """The posted ``next`` if it stays on this site, or ``""`` when it does not."""
    target = (request.POST.get("next") or request.GET.get("next") or "").strip()
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return ""


def safe_next(request, fallback: str, *args, **kwargs) -> str:
    """The posted ``next`` if it is local, otherwise ``fallback``.

    ``fallback`` is anything ``redirect()`` accepts — a URL name such as
    ``"console:ai_tokens"`` (with its arguments) or a path — so the result can
    be handed straight to ``redirect``.
    """
    return next_url(request) or resolve_url(fallback, *args, **kwargs)
