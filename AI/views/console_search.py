"""Search across the whole console from the ⌘K palette.

The palette used to filter the sidebar's own links, which answered "where is
the payments page" but not "where is this student". This answers the second:
one box for a person, a payment reference, a ticket number, a chat title or a
promo code, grouped by what was found. Read-only, admin-only, a handful of
indexed lookups — fast enough to run on every keystroke after a short pause.
"""
from __future__ import annotations

from django.db.models import Q
from django.urls import reverse

from AI.models import Conversation, Payment, Plan, PromoCode, SupportTicket, User
from AI.utils.decorators import admin_required
from AI.utils.request import json_ok

PER_GROUP = 6


def _student_rows(q: str) -> list[dict]:
    users = (
        User.objects.filter(
            Q(username__icontains=q) | Q(full_name__icontains=q) | Q(email__icontains=q)
            | Q(phone__icontains=q) | Q(referral_code__iexact=q)
        )
        .order_by("-last_seen_at", "-date_joined")[:PER_GROUP]
    )
    return [
        {
            "title": user.display_name,
            "sub": " · ".join(part for part in (
                f"@{user.username}", user.email, "admin" if user.is_admin_user else "",
                "disabled" if user.status == "disabled" else "",
            ) if part),
            "url": reverse("console:user_detail", args=[user.pk]),
            "icon": "#i-users",
        }
        for user in users
    ]


def _payment_rows(q: str) -> list[dict]:
    payments = (
        Payment.objects.select_related("user", "plan")
        .filter(Q(reference__icontains=q) | Q(provider_txn_id__icontains=q)
                | Q(user__username__icontains=q) | Q(user__email__icontains=q))
        .order_by("-created_at")[:PER_GROUP]
    )
    return [
        {
            "title": payment.reference,
            "sub": f"{payment.user.display_name} · {payment.amount} {payment.currency} · {payment.get_status_display()}",
            "url": reverse("console:payment_detail", args=[payment.reference]),
            "icon": "#i-card",
        }
        for payment in payments
    ]


def _ticket_rows(q: str) -> list[dict]:
    tickets = (
        SupportTicket.objects.select_related("user")
        .filter(Q(reference__icontains=q) | Q(subject__icontains=q) | Q(email__icontains=q)
                | Q(user__username__icontains=q))
        .order_by("-created_at")[:PER_GROUP]
    )
    return [
        {
            "title": f"{ticket.reference} — {ticket.subject}",
            "sub": f"{ticket.user.display_name} · {ticket.get_status_display()}",
            "url": reverse("console:ticket_detail", args=[ticket.reference]),
            "icon": "#i-inbox",
        }
        for ticket in tickets
    ]


def _conversation_rows(q: str) -> list[dict]:
    chats = (
        Conversation.objects.select_related("user").filter(title__icontains=q)
        .order_by("-updated_at")[:PER_GROUP]
    )
    return [
        {
            "title": chat.title,
            "sub": f"{chat.user.display_name} · {chat.message_count} messages",
            "url": reverse("console:conversation_detail", args=[chat.pk]),
            "icon": "#i-chat",
        }
        for chat in chats
    ]


def _promo_rows(q: str) -> list[dict]:
    return [
        {
            "title": promo.code,
            "sub": "Promo code" + ("" if promo.is_active else " · inactive"),
            "url": reverse("console:promo_detail", args=[promo.pk]),
            "icon": "#i-tag",
        }
        for promo in PromoCode.objects.filter(code__icontains=q).order_by("code")[:PER_GROUP]
    ]


def _plan_rows(q: str) -> list[dict]:
    return [
        {
            "title": plan.name,
            "sub": "Plan" + ("" if plan.is_active else " · hidden"),
            "url": reverse("console:plan_edit", args=[plan.pk]),
            "icon": "#i-layers",
        }
        for plan in Plan.objects.filter(Q(name__icontains=q) | Q(slug__icontains=q)).order_by("name")[:PER_GROUP]
    ]


@admin_required
def search(request):
    q = " ".join((request.GET.get("q") or "").split())[:80]
    if len(q) < 2:
        return json_ok(query=q, groups=[])
    groups = [
        ("Students", _student_rows(q)),
        ("Payments", _payment_rows(q)),
        ("Support tickets", _ticket_rows(q)),
        ("Conversations", _conversation_rows(q)),
        ("Promo codes", _promo_rows(q)),
        ("Plans", _plan_rows(q)),
    ]
    return json_ok(query=q, groups=[{"label": label, "items": rows} for label, rows in groups if rows])
