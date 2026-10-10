"""Support Tickets — the administrator's queue.

Ordered by what needs attention rather than strictly by date: an unanswered
urgent ticket outranks a resolved one from this morning.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib import messages
from django.db.models import Case, Count, F, IntegerField, Q, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from AI.forms import AdminReplyForm, TicketFilterForm
from AI.models import (
    CannedReply,
    OPEN_STATUSES,
    AuditAction,
    Payment,
    PaymentStatus,
    Role,
    SupportTicket,
    TicketPriority,
    TicketStatus,
    User,
)
from AI.services import audit_service, support_service, token_service
from AI.utils.decorators import admin_required
from AI.utils.pagination import clean_id, page_range, paginate

logger = logging.getLogger("AI.console.support")


def _context(active: str, **extra) -> dict:
    context = {
        "nav_active": active,
        "pending_payment_count": Payment.objects.filter(status=PaymentStatus.PENDING).count(),
        "open_ticket_count": support_service.open_count(),
    }
    context.update(extra)
    return context


@admin_required
def tickets(request):
    form = TicketFilterForm(request.GET or None)
    queryset = SupportTicket.objects.select_related("user", "assigned_to").annotate(
        reply_count=Count("replies", filter=Q(replies__is_internal=False))
    )

    # Each filter stands on its own: a field that fails validation (a stale
    # ``?days=abc``) is left out of ``cleaned_data`` while the valid ones are
    # still applied, instead of the whole form being ignored.
    form.is_valid()
    data = getattr(form, "cleaned_data", {}) if form.is_bound else {}
    if data.get("q"):
        term = data["q"]
        queryset = queryset.filter(
            Q(reference__icontains=term)
            | Q(subject__icontains=term)
            | Q(description__icontains=term)
            | Q(full_name__icontains=term)
            | Q(email__icontains=term)
            | Q(user__username__icontains=term)
        )
    status = data.get("status")
    if status == "open":
        queryset = queryset.filter(status__in=OPEN_STATUSES)
    elif status:
        queryset = queryset.filter(status=status)
    if data.get("category"):
        queryset = queryset.filter(category=data["category"])
    if data.get("priority"):
        queryset = queryset.filter(priority=data["priority"])
    if data.get("days"):
        queryset = queryset.filter(
            created_at__gte=timezone.now() - timedelta(days=int(data["days"]))
        )
    # "Assigned to" is me, nobody, or any named administrator. The form knows
    # the first two; an administrator's id is read here, and checked.
    assignee = (request.GET.get("assignee") or "").strip()
    admins = _administrators()
    admin_ids = {admin.pk for admin in admins}
    if assignee == "me":
        queryset = queryset.filter(assigned_to=request.user)
    elif assignee == "unassigned":
        queryset = queryset.filter(assigned_to__isnull=True)
    elif clean_id(assignee) in admin_ids:
        queryset = queryset.filter(assigned_to_id=clean_id(assignee))
    else:
        assignee = ""

    # Unanswered and urgent first, then newest. Resolved work sinks.
    queryset = queryset.annotate(
        queue_rank=Case(
            When(status=TicketStatus.NEW, then=Value(0)),
            When(status=TicketStatus.OPEN, then=Value(1)),
            When(status=TicketStatus.IN_PROGRESS, then=Value(2)),
            When(status=TicketStatus.WAITING_USER, then=Value(3)),
            default=Value(4),
            output_field=IntegerField(),
        ),
        urgency=Case(
            When(priority=TicketPriority.URGENT, then=Value(0)),
            When(priority=TicketPriority.HIGH, then=Value(1)),
            When(priority=TicketPriority.NORMAL, then=Value(2)),
            default=Value(3),
            output_field=IntegerField(),
        ),
    ).order_by("queue_rank", "urgency", "-created_at", "-id")

    page = paginate(request, queryset, per_page=20)
    counts = {
        row["status"]: row["total"]
        for row in SupportTicket.objects.values("status").annotate(total=Count("id"))
    }
    day_ago = timezone.now() - timedelta(days=1)

    return render(
        request,
        "console/tickets.html",
        _context(
            "tickets",
            form=form,
            page=page,
            page_numbers=page_range(page),
            stats={
                "total": SupportTicket.objects.count(),
                "needs_attention": SupportTicket.objects.filter(status__in=OPEN_STATUSES).count(),
                "unanswered": SupportTicket.objects.filter(
                    first_response_at__isnull=True, status__in=OPEN_STATUSES
                ).count(),
                "today": SupportTicket.objects.filter(created_at__gte=day_ago).count(),
                "resolved": counts.get(TicketStatus.RESOLVED, 0) + counts.get(TicketStatus.CLOSED, 0),
            },
            statuses=TicketStatus.choices,
            admins=admins,
            assignee=assignee,
        ),
    )


def _administrators():
    """Everyone a ticket can be assigned to: active console administrators."""
    return list(
        User.objects.filter(Q(role=Role.ADMIN) | Q(is_superuser=True), is_active=True)
        .order_by("full_name", "username")
    )


def _assign(ticket, value: str, actor) -> str | None:
    """Apply an ``assign`` value — ``me``, ``none`` or an administrator's id.

    Returns what changed, for the audit line and the message, or None when
    nothing did. An id that is not an active administrator changes nothing.
    """
    if value == "me":
        target = actor
    elif value == "none":
        target = None
    else:
        pk = clean_id(value)
        target = next((admin for admin in _administrators() if admin.pk == pk), None) if pk else None
        if target is None:
            return None
    if (target.pk if target else None) == ticket.assigned_to_id:
        return None
    ticket.assigned_to = target
    ticket.save(update_fields=["assigned_to", "updated_at"])
    if target is None:
        return "unassigned"
    return "assigned to you" if target.pk == actor.pk else f"assigned to {target.display_name}"


@admin_required
@require_POST
def tickets_bulk(request):
    """Close, resolve or assign several tickets from the queue at once."""
    back = request.POST.get("next") or ""
    base = reverse("console:tickets")
    back = back if back.startswith(base) and "//" not in back else base

    ids = [pk for pk in (clean_id(v) for v in request.POST.getlist("ids")) if pk][:200]
    tickets = list(SupportTicket.objects.filter(pk__in=ids).select_related("user"))
    if not tickets:
        messages.error(request, "Tick at least one ticket first.")
        return redirect(back)

    action = request.POST.get("action") or ""
    changed = 0
    if action in {"close", "resolve"}:
        status = TicketStatus.CLOSED if action == "close" else TicketStatus.RESOLVED
        for ticket in tickets:
            if ticket.status == status:
                continue
            previous = ticket.get_status_display()
            support_service.set_status(ticket, status, actor=request.user)
            audit_service.record(
                AuditAction.USER_UPDATE, actor=request.user, request=request, target=ticket.user,
                description=f"Ticket {ticket.reference}: status {previous} → {ticket.get_status_display()} (bulk)",
            )
            changed += 1
        verb = "Closed" if action == "close" else "Resolved"
    elif action == "assign":
        value = (request.POST.get("assign") or "").strip()
        verb = "Updated"
        for ticket in tickets:
            what = _assign(ticket, value, request.user)
            if what:
                audit_service.record(
                    AuditAction.USER_UPDATE, actor=request.user, request=request, target=ticket.user,
                    description=f"Ticket {ticket.reference}: {what} (bulk)",
                )
                changed += 1
    else:
        messages.error(request, "Choose what to do with the selected tickets.")
        return redirect(back)

    skipped = len(tickets) - changed
    messages.success(
        request,
        f"{verb} {changed} ticket{'s' if changed != 1 else ''}"
        + (f"; {skipped} already {'so' if action != 'assign' else 'had that assignee'}." if skipped else "."),
    )
    return redirect(back)


@admin_required
def ticket_detail(request, reference: str):
    ticket = get_object_or_404(
        SupportTicket.objects.select_related("user", "assigned_to"), reference=reference
    )

    # The student's own context, so an administrator does not have to go looking.
    account = token_service.get_account(ticket.user)
    recent_payments = Payment.objects.filter(user=ticket.user).order_by("-created_at")[:5]

    return render(
        request,
        "console/ticket_detail.html",
        _context(
            "tickets",
            ticket=ticket,
            replies=support_service.visible_replies(ticket, for_staff=True),
            form=AdminReplyForm(),
            statuses=TicketStatus.choices,
            priorities=TicketPriority.choices,
            admins=_administrators(),
            account=account,
            recent_payments=recent_payments,
            other_tickets=SupportTicket.objects.filter(user=ticket.user)
            .exclude(pk=ticket.pk)
            .order_by("-created_at")[:5],
            # Saved replies, ready to drop into the reply box with the student's
            # first name filled in.
            canned=[{"id": r.pk, "title": r.title, "body": r.body} for r in CannedReply.objects.all()],
            first_name=(ticket.full_name or ticket.user.display_name or "").split(" ")[0],
        ),
    )


@admin_required
@require_POST
def ticket_reply(request, reference: str):
    ticket = get_object_or_404(SupportTicket, reference=reference)
    form = AdminReplyForm(request.POST, request.FILES)

    if not form.is_valid():
        messages.error(request, next(iter(form.errors.values()))[0])
        return redirect("console:ticket_detail", reference=reference)

    internal = form.cleaned_data.get("is_internal", False)
    support_service.add_reply(
        ticket, request.user, form.cleaned_data["body"], internal=internal, from_staff=True,
        attachment=form.cleaned_data.get("attachment"),
    )
    # A saved reply counts as used only when a reply built from it is sent.
    canned_id = request.POST.get("canned_id", "")
    if canned_id.isdigit() and not internal:
        CannedReply.objects.filter(pk=int(canned_id)).update(use_count=F("use_count") + 1)

    audit_service.record(
        AuditAction.USER_UPDATE,
        actor=request.user,
        request=request,
        target=ticket.user,
        description=f"{'Noted on' if internal else 'Replied to'} ticket {ticket.reference}",
    )
    messages.success(
        request, "Internal note saved." if internal else "Reply sent — the student has been notified."
    )
    return redirect("console:ticket_detail", reference=reference)


@admin_required
@require_POST
def ticket_update(request, reference: str):
    """Change status, priority or assignment."""
    ticket = get_object_or_404(SupportTicket, reference=reference)
    changed = []

    status = request.POST.get("status")
    if status and status != ticket.status and status in TicketStatus.values:
        previous = ticket.get_status_display()
        support_service.set_status(ticket, status, actor=request.user)
        changed.append(f"status {previous} → {ticket.get_status_display()}")

    priority = request.POST.get("priority")
    if priority and priority != ticket.priority and priority in TicketPriority.values:
        ticket.priority = priority
        ticket.save(update_fields=["priority", "updated_at"])
        changed.append(f"priority {ticket.get_priority_display()}")

    # Me, nobody, or any administrator by id.
    assign = (request.POST.get("assign") or "").strip()
    if assign:
        what = _assign(ticket, assign, request.user)
        if what:
            changed.append(what)

    if changed:
        audit_service.record(
            AuditAction.USER_UPDATE,
            actor=request.user,
            request=request,
            target=ticket.user,
            description=f"Ticket {ticket.reference}: {', '.join(changed)}",
        )
        messages.success(request, f"Ticket updated — {', '.join(changed)}.")
    else:
        messages.info(request, "Nothing changed.")

    return redirect("console:ticket_detail", reference=reference)
