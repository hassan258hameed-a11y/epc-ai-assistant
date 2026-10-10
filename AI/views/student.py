"""Referrals and the notification feed.

Everything else a student sees is a panel inside the chat shell — see
:mod:`AI.views.panels`. What is left here is the two surfaces that are not:
the referral page, which has a link people share, and the header feed, which
is JSON rather than a page at all.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from AI.models import Notification
from AI.services import notification_service, referral_service, settings_service
from AI.utils.decorators import login_required_student
from AI.templatetags.ai_extras import timeago
from AI.utils.request import json_ok


def _greeting() -> str:
    hour = timezone.localtime().hour
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


@login_required_student
def referrals(request):
    user = request.user
    stats = referral_service.stats_for(user)
    referral_link = f"{request.build_absolute_uri('/signup/')}?ref={user.referral_code}"
    return render(
        request,
        "student/referrals.html",
        {
            "nav_active": "referrals",
            "stats": stats,
            "referral_link": referral_link,
            "referral_enabled": settings_service.get("referral_enabled", True),
        },
    )


@login_required_student
@require_POST
def notification_read(request, pk: int):
    notification = get_object_or_404(Notification, pk=pk, user=request.user)
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=["is_read", "read_at"])
    if request.headers.get("X-Mark-Only"):
        # Following a notification's own button: nothing to repaint.
        return json_ok(unread=Notification.objects.filter(user=request.user, is_read=False).count())
    return redirect(_notifications_return(request))


@login_required_student
@require_POST
def notification_delete(request, pk: int):
    get_object_or_404(Notification, pk=pk, user=request.user).delete()
    return redirect(_notifications_return(request))


def _notifications_return(request) -> str:
    """Back to the tab and page the student was on — and only ever there."""
    base = reverse("student:notifications")
    parts = urlsplit(request.POST.get("return") or "")
    # Exactly the notifications page (with its tab/page query), never a
    # neighbour such as the POST-only ".../read-all/" — redirecting there
    # answered 405 and left the panel blank.
    if parts.scheme or parts.netloc or parts.path != base:
        return base
    return f"{base}?{parts.query}" if parts.query else base


@login_required_student
def notifications_feed(request):
    """The header dropdown's contents, so the panel does not need a page load."""
    rows = list(notification_service.inbox(request.user)[:8])
    return json_ok(
        unread=Notification.objects.filter(user=request.user, is_read=False).count(),
        notifications=[
            {
                "id": row.pk,
                "title": row.title,
                "body": (row.body or "")[:160],
                "level": row.level,
                "is_read": row.is_read,
                "is_pinned": row.is_pinned and not row.is_read,
                "action_url": row.action_url,
                "action_label": row.action_label,
                "when": timeago(row.created_at),
            }
            for row in rows
        ],
    )


@login_required_student
@require_POST
def notifications_read_all(request):
    Notification.objects.filter(user=request.user, is_read=False).update(
        is_read=True, read_at=timezone.now()
    )
    return json_ok(unread=0)
