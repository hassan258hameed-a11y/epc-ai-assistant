"""Authorisation decorators.

Permissions are enforced here, in the backend, on every protected view — hiding
a button in a template is never treated as a security control.
"""
from __future__ import annotations

from functools import wraps

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect

from AI.utils.request import is_ajax


def _deny(request, message: str, redirect_to: str):
    if is_ajax(request):
        return JsonResponse({"ok": False, "error": message, "code": "forbidden"}, status=403)
    messages.error(request, message)
    return redirect(redirect_to)


def login_required_student(view):
    """Authenticated, active, non-admin users only."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            if is_ajax(request):
                return JsonResponse({"ok": False, "error": "Please sign in.", "code": "unauthenticated"}, status=401)
            return redirect(f"/login/?next={request.path}")
        if user.is_admin_user:
            return redirect("console:dashboard")
        if not user.is_active_account:
            return _deny(request, "Your account has been disabled. Contact support for help.", "/login/")
        return view(request, *args, **kwargs)

    return wrapper


def login_required_any(view):
    """Any authenticated, active account (student or admin)."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            if is_ajax(request):
                return JsonResponse({"ok": False, "error": "Please sign in.", "code": "unauthenticated"}, status=401)
            return redirect(f"/login/?next={request.path}")
        if not user.is_active_account:
            return _deny(request, "Your account has been disabled.", "/login/")
        return view(request, *args, **kwargs)

    return wrapper


def admin_required(view):
    """Administrators only — students never reach the console."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect(f"/admin/login/?next={request.path}")
        if not user.is_admin_user:
            # Do not reveal that the console exists.
            if is_ajax(request):
                return JsonResponse({"ok": False, "error": "Not found.", "code": "forbidden"}, status=404)
            messages.error(request, "You do not have access to that area.")
            return redirect("student:chat")
        if not user.is_active_account:
            return _deny(request, "Your admin account has been disabled.", "/admin/login/")
        return view(request, *args, **kwargs)

    return wrapper


def anonymous_required(view):
    """Public auth pages redirect signed-in users to their home surface."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.user.is_authenticated and request.user.is_active_account:
            return redirect("console:dashboard" if request.user.is_admin_user else "student:chat")
        return view(request, *args, **kwargs)

    return wrapper
