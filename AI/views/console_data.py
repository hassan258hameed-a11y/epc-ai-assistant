"""Console pages for the records that used to need Django's stock admin.

Conversations, their transcripts and subscriptions all live here, so the whole
administrative job can be done inside the product. Nothing here is a raw CRUD
form: each page is built around what an administrator actually needs to answer
— who is using the assistant, what did it cost, and whose plan is about to
lapse.

Opening a transcript is a read of a student's private messages, so it is
recorded in the audit log like any other consequential action.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib import messages as flash
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from AI.models import (
    ConversationShare,
    AIUsageLog,
    AuditAction,
    Conversation,
    Message,
    MessageRole,
    Payment,
    PaymentStatus,
    Plan,
    Role,
    Subscription,
    User,
)
from AI.services import audit_service, model_scope
from AI.utils.decorators import admin_required
from AI.utils.pagination import clean_id, clean_int, page_range, paginate

logger = logging.getLogger("AI.console.data")


def _context(active: str, **extra) -> dict:
    context = {
        "nav_active": active,
        "pending_payment_count": Payment.objects.filter(status=PaymentStatus.PENDING).count(),
    }
    context.update(extra)
    return context


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------
@admin_required
def conversations(request):
    """Every chat on the platform, newest activity first."""
    search = (request.GET.get("q") or "").strip()
    scope = request.GET.get("scope") or ""          # this page's activity filter
    # A student id too large for the database is not a student; it is ignored
    # rather than handed to SQLite, which raises OverflowError on it.
    student_pk = clean_id(request.GET.get("student"))
    student_id = str(student_pk) if student_pk else ""
    selected = model_scope.resolve(request)         # which model's chats

    # A conversation has no model of its own — it is whatever its messages were
    # answered by, and it can be more than one. So "GPT-4o conversations" means
    # the ones GPT-4o ever answered in, de-duplicated.
    queryset = selected.conversations(Conversation.objects.select_related("user").all())

    if search:
        matching = (
            Message.objects.filter(content__icontains=search)
            .values_list("conversation_id", flat=True)
            .distinct()[:500]
        )
        queryset = queryset.filter(
            Q(title__icontains=search)
            | Q(user__username__icontains=search)
            | Q(user__full_name__icontains=search)
            | Q(pk__in=list(matching))
        )
    if student_pk:
        queryset = queryset.filter(user_id=student_pk)

    # A tie-breaker on every ordering: rows sharing a timestamp (or a token
    # count) otherwise come back in whatever order the database likes, and
    # can appear on two pages or on none as the list is paged through.
    ordering = ("-is_pinned", "-updated_at", "-id")
    if scope == "active":
        queryset = queryset.filter(updated_at__gte=timezone.now() - timedelta(days=7))
    elif scope == "expensive":
        queryset = queryset.filter(total_tokens__gt=0)
        ordering = ("-total_tokens", "-id")
    elif scope == "empty":
        queryset = queryset.filter(message_count=0)
    elif scope == "archived":
        queryset = queryset.filter(is_archived=True)

    page = paginate(request, queryset.order_by(*ordering), per_page=25)

    if selected.is_all:
        totals = Conversation.objects.aggregate(
            count=Count("id"), tokens=Sum("total_tokens"), messages=Sum("message_count")
        )
    else:
        # A conversation's own counters cover every model that answered in it,
        # so summing them for a single model would credit GPT-4o with tokens
        # Claude spent. The message log is the only place that knows which
        # model each token belonged to.
        scoped_messages = Message.objects.filter(model_key=selected.model.key)
        totals = {
            "count": selected.conversations(Conversation.objects.all()).count(),
            "tokens": scoped_messages.aggregate(t=Sum("total_tokens"))["t"] or 0,
            "messages": scoped_messages.count(),
        }
    week_start = timezone.now() - timedelta(days=7)

    return render(
        request,
        "console/conversations.html",
        _context(
            "conversations",
            page=page,
            page_numbers=page_range(page),
            search=search,
            scope=scope,
            student_id=student_id,
            focus_student=User.objects.filter(pk=student_pk).first() if student_pk else None,
            totals={
                "count": totals["count"] or 0,
                "tokens": totals["tokens"] or 0,
                "messages": totals["messages"] or 0,
                "active": Conversation.objects.filter(updated_at__gte=week_start).count(),
                "students": Conversation.objects.values("user_id").distinct().count(),
            },
        ),
    )


@admin_required
def conversation_detail(request, pk: int):
    """The full transcript, with what each reply cost."""
    conversation = get_object_or_404(Conversation.objects.select_related("user"), pk=pk)
    chat_messages = list(conversation.messages.all().order_by("created_at", "id"))

    audit_service.record(
        AuditAction.USER_UPDATE,
        actor=request.user,
        request=request,
        target=conversation.user,
        description=f"Viewed conversation #{conversation.pk} ({conversation.title})",
    )

    usage = AIUsageLog.objects.filter(conversation=conversation).aggregate(
        requests=Count("id"),
        tokens=Sum("total_tokens", filter=Q(success=True)),
        inbound=Sum("input_tokens", filter=Q(success=True)),
        outbound=Sum("output_tokens", filter=Q(success=True)),
        failures=Count("id", filter=Q(success=False)),
    )

    share = ConversationShare.objects.filter(conversation=conversation, is_active=True).first()

    return render(
        request,
        "console/conversation_detail.html",
        _context(
            "conversations",
            share=share,
            conversation=conversation,
            chat_messages=chat_messages,
            usage={
                "requests": usage["requests"] or 0,
                "tokens": usage["tokens"] or 0,
                "inbound": usage["inbound"] or 0,
                "outbound": usage["outbound"] or 0,
                "failures": usage["failures"] or 0,
            },
            user_messages=sum(1 for m in chat_messages if m.role == MessageRole.USER),
        ),
    )


@admin_required
@require_POST
def conversation_delete(request, pk: int):
    conversation = get_object_or_404(Conversation, pk=pk)
    owner, title = conversation.user, conversation.title
    conversation.delete()

    audit_service.record(
        AuditAction.USER_UPDATE,
        actor=request.user,
        request=request,
        target=owner,
        description=f"Deleted conversation '{title}'",
    )
    flash.success(request, f"Deleted “{title}”.")
    return redirect("console:conversations")


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------
@admin_required
def subscriptions(request):
    """Who is on a plan, and whose plan is about to lapse."""
    search = (request.GET.get("q") or "").strip()
    status = request.GET.get("status") or ""
    plan_pk = clean_id(request.GET.get("plan"))
    plan_id = str(plan_pk) if plan_pk else ""

    # A subscription reaches a model through its plan, the same path a payment
    # takes — so filtering here and on the payments page cannot disagree.
    queryset = model_scope.resolve(request).subscriptions(
        Subscription.objects.select_related("user", "plan", "plan__ai_model", "payment").all()
    )
    now = timezone.now()

    if search:
        queryset = queryset.filter(
            Q(user__username__icontains=search)
            | Q(user__full_name__icontains=search)
            | Q(user__email__icontains=search)
        )
    if status == "live":
        queryset = queryset.filter(status=Subscription.Status.ACTIVE, ends_at__gt=now)
    elif status == "expiring":
        queryset = queryset.filter(
            status=Subscription.Status.ACTIVE, ends_at__gt=now, ends_at__lte=now + timedelta(days=7)
        )
    elif status == "lapsed":
        queryset = queryset.filter(Q(status=Subscription.Status.EXPIRED) | Q(ends_at__lte=now))
    elif status == "cancelled":
        queryset = queryset.filter(status=Subscription.Status.CANCELLED)
    if plan_pk:
        queryset = queryset.filter(plan_id=plan_pk)

    page = paginate(request, queryset.order_by("-created_at", "-id"), per_page=25)
    for sub in page.object_list:
        sub.can_change_end = sub.status == Subscription.Status.ACTIVE

    return render(
        request,
        "console/subscriptions.html",
        _context(
            "subscriptions",
            page=page,
            page_numbers=page_range(page),
            search=search,
            status=status,
            plan_id=plan_id,
            plans=Plan.objects.all().order_by("display_order", "name"),
            totals={
                "live": Subscription.objects.filter(
                    status=Subscription.Status.ACTIVE, ends_at__gt=now
                ).count(),
                "expiring": Subscription.objects.filter(
                    status=Subscription.Status.ACTIVE, ends_at__gt=now, ends_at__lte=now + timedelta(days=7)
                ).count(),
                "lapsed": Subscription.objects.filter(
                    Q(status=Subscription.Status.EXPIRED) | Q(ends_at__lte=now)
                ).count(),
                "tokens": Subscription.objects.aggregate(total=Sum("tokens_granted"))["total"] or 0,
                "students": User.objects.filter(
                    role=Role.STUDENT, subscriptions__status=Subscription.Status.ACTIVE,
                    subscriptions__ends_at__gt=now,
                ).distinct().count(),
            },
        ),
    )


@admin_required
@require_POST
def subscription_cancel(request, pk: int):
    subscription = get_object_or_404(Subscription.objects.select_related("user", "plan"), pk=pk)

    if subscription.status != Subscription.Status.ACTIVE:
        flash.warning(request, "That subscription is not active.")
        return redirect("console:subscriptions")

    subscription.status = Subscription.Status.CANCELLED
    subscription.save(update_fields=["status", "updated_at"])

    audit_service.record(
        AuditAction.USER_UPDATE,
        actor=request.user,
        request=request,
        target=subscription.user,
        description=f"Cancelled the {subscription.plan.name} subscription",
        old_value=Subscription.Status.ACTIVE,
        new_value=Subscription.Status.CANCELLED,
    )
    # Tokens already granted are deliberately left with the student: they paid
    # for them. Removing them is a separate, explicit decision on the token page.
    flash.success(
        request,
        f"Cancelled {subscription.user.display_name}'s {subscription.plan.name} plan. "
        "Tokens already credited were left in place.",
    )
    return redirect("console:subscriptions")



@admin_required
@require_POST
def subscription_extend(request, pk: int):
    """Move a live subscription's end date by N days — later, or earlier.

    Audited, and the student is told the new date (in the site's own time
    zone) in the app and on Telegram. A change that would end the plan before
    it began is refused rather than clamped, because clamping would record a
    date nobody asked for.
    """
    from AI.views.console_student_tools import _local, change_end_date

    subscription = get_object_or_404(Subscription.objects.select_related("user", "plan"), pk=pk)
    back = _subscriptions_back(request)
    if subscription.status != Subscription.Status.ACTIVE:
        flash.warning(request, "Only an active subscription's end date can be changed.")
        return redirect(back)

    days = clean_int(request.POST.get("days"), minimum=-365, maximum=365)
    if request.POST.get("direction") == "shorten" and days:
        days = -abs(days)
    if not days:
        flash.error(request, "Enter a number of days between 1 and 365.")
        return redirect(back)
    if subscription.ends_at + timedelta(days=days) <= subscription.starts_at:
        flash.error(
            request,
            f"That would end the plan before it started ({_local(subscription.starts_at):%d %b %Y}). "
            "Cancel it instead.",
        )
        return redirect(back)

    change_end_date(subscription, days, actor=request.user, request=request)
    verb = "extended" if days > 0 else "shortened"
    flash.success(
        request,
        f"{subscription.user.display_name}'s {subscription.plan.name} {verb} — it now ends "
        f"{_local(subscription.ends_at):%d %b %Y, %H:%M}. They have been notified.",
    )
    return redirect(back)


def _subscriptions_back(request) -> str:
    """Back to the list the form was on, filters and page intact — never off-site."""
    from django.urls import reverse

    target = request.POST.get("next") or ""
    base = reverse("console:subscriptions")
    return target if target.startswith(base) and "//" not in target else base


@admin_required
@require_POST
def conversation_unshare(request, pk: int):
    """Take a student's public link down — for content that should not be public."""
    conversation = get_object_or_404(Conversation, pk=pk)
    changed = ConversationShare.objects.filter(conversation=conversation, is_active=True).update(is_active=False)
    if changed:
        audit_service.record(
            AuditAction.USER_UPDATE, actor=request.user, request=request, target=conversation.user,
            description=f"Revoked the public link to conversation #{conversation.pk} ({conversation.title})",
        )
        flash.success(request, "The public link no longer works.")
    return redirect("console:conversation_detail", pk=pk)
