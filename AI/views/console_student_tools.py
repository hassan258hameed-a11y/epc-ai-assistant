"""Per-student tools: notes, tags, a message, a plan, sign-out — and the
warn-now and renewal actions on the Running low and Expiring access pages.

Each is a small POST that returns to the student's page, audited like every
other change an administrator makes to an account.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from AI.models import AuditAction, NotificationLevel, Plan, StudentNote, Subscription, User, UserAIWallet
from AI.services import account_service, admin_tools_service, audit_service, notification_service, token_alert_service
from AI.utils.decorators import admin_required
from AI.utils.pagination import clean_id, clean_int


def _back(student, anchor: str = ""):
    url = reverse("console:user_detail", args=[student.pk])
    return redirect(f"{url}#{anchor}" if anchor else url)


@admin_required
@require_POST
def note_add(request, pk: int):
    student = get_object_or_404(User, pk=pk)
    try:
        admin_tools_service.add_note(request.user, student, request.POST.get("body", ""))
        messages.success(request, "Note added.")
    except admin_tools_service.AdminToolError as exc:
        messages.error(request, exc.friendly_message)
    return _back(student, "notes")


@admin_required
@require_POST
def note_action(request, pk: int, note_id: int):
    student = get_object_or_404(User, pk=pk)
    note = get_object_or_404(StudentNote, pk=note_id, student=student)
    if request.POST.get("action") == "delete":
        note.delete()
        messages.success(request, "Note deleted.")
    else:
        admin_tools_service.toggle_pin(note)
    return _back(student, "notes")


@admin_required
@require_POST
def tags_save(request, pk: int):
    """Replace the student's tags with the comma-separated list posted."""
    student = get_object_or_404(User, pk=pk)
    tags = []
    for raw in (request.POST.get("tags") or "").split(","):
        tag = admin_tools_service.clean_tag(raw)
        if tag and tag not in tags:
            tags.append(tag)
    tags = tags[: admin_tools_service.MAX_TAGS]
    before = ", ".join(student.tags or [])
    User.objects.filter(pk=student.pk).update(tags=tags)
    audit_service.record(
        AuditAction.USER_UPDATE, actor=request.user, request=request, target=student,
        description="Updated tags", old_value=before[:255], new_value=", ".join(tags)[:255],
    )
    messages.success(request, "Tags saved.")
    return _back(student, "notes")


@admin_required
@require_POST
def message(request, pk: int):
    student = get_object_or_404(User, pk=pk)
    try:
        outcome = admin_tools_service.message_students(
            request.user, [student], title=request.POST.get("title", ""), body=request.POST.get("body", ""),
            level=request.POST.get("level", NotificationLevel.INFO), email=request.POST.get("email") == "on",
            request=request,
        )
    except admin_tools_service.AdminToolError as exc:
        messages.error(request, exc.friendly_message)
    else:
        by_mail = " and e-mailed" if outcome["mailed"] else ""
        messages.success(request, f"Message sent{by_mail}.")
    return _back(student)


def _local(moment):
    """A stored (UTC) moment in the site's own time zone, for anything a person reads.

    ``ends_at`` is kept in UTC, so formatting it directly printed the UTC date —
    for a plan ending just after midnight in Phnom Penh, the day before.
    """
    return timezone.localtime(moment)


@admin_required
@require_POST
def grant_plan(request, pk: int):
    """Put the student on a plan without a payment — stacked after any current one."""
    student = get_object_or_404(User, pk=pk)
    plan_id = clean_id(request.POST.get("plan_id"))
    plan = Plan.objects.filter(pk=plan_id).first() if plan_id else None
    if plan is None:
        messages.error(request, "Choose a plan.")
        return _back(student)
    reason = (request.POST.get("reason") or "").strip()[:200]
    subscription = account_service.grant_plan(
        student, plan, performed_by=request.user, reason=reason or f"{plan.name} plan granted by an administrator",
    )
    ends = _local(subscription.ends_at)
    credited = account_service.describe_credit(getattr(subscription, "credited", []))
    audit_service.record(
        AuditAction.USER_UPDATE, actor=request.user, request=request, target=student,
        description=f"Granted the {plan.name} plan" + (f" — {reason}" if reason else ""),
        new_value=f"until {ends:%Y-%m-%d}" + (f"; credited {credited}" if credited else ""),
    )
    notification_service.notify(
        student, f"{plan.name} plan added",
        f"An administrator added the {plan.name} plan to your account"
        + (f" with {credited}" if credited else "")
        + f". It runs until {ends:%d %B %Y}.",
        level=NotificationLevel.SUCCESS, action_url="/app/usage/", action_label="See usage", telegram=True,
    )
    messages.success(
        request,
        f"{plan.name} granted until {ends:%d %b %Y}"
        + (f" — {credited} credited." if credited else " — this plan credits no tokens."),
    )
    return _back(student)


@admin_required
@require_POST
def sign_out(request, pk: int):
    student = get_object_or_404(User, pk=pk)
    if student.pk == request.user.pk:
        messages.error(request, "Use Sign out in the menu to end your own session.")
        return _back(student)
    account_service.sign_out_everywhere(student)
    audit_service.record(AuditAction.USER_UPDATE, actor=request.user, request=request, target=student,
                         description="Signed the student out on every device")
    messages.success(request, f"{student.display_name} is signed out on every device.")
    return _back(student)


# ---------------------------------------------------------------------------
# From the Running low and Expiring access pages
# ---------------------------------------------------------------------------
def _back_to(request, name: str):
    target = request.POST.get("next") or ""
    base = reverse(name)
    return redirect(target if target.startswith(base) and "//" not in target else base)


@admin_required
@require_POST
def warn_now(request, wallet_id: int):
    """Send the low-balance e-mail for one wallet now, whatever was sent before."""
    wallet = get_object_or_404(UserAIWallet.objects.select_related("user", "ai_model"), pk=wallet_id)
    tier = token_alert_service.tier_for(wallet) or max(token_alert_service.thresholds_for(wallet.ai_model))
    log = token_alert_service.send_warning(wallet, tier, force=True)
    if log is None:
        messages.warning(request, f"{wallet.user.display_name} has no e-mail address or turned these alerts off.")
    elif log.status == "sent":
        messages.success(request, f"Warning e-mailed to {log.recipient}.")
    else:
        messages.error(request, f"The e-mail could not be sent: {log.error or log.status}.")
    return _back_to(request, "console:low_tokens")


@admin_required
@require_POST
def expiring_action(request, subscription_id: int):
    """Remind a student their plan is ending, or extend it."""
    sub = get_object_or_404(Subscription.objects.select_related("user", "plan"), pk=subscription_id)
    if request.POST.get("action") == "extend":
        days = clean_int(request.POST.get("days"), minimum=1, maximum=365) or 30
        change_end_date(sub, days, actor=request.user, request=request)
        messages.success(
            request,
            f"{sub.user.display_name}'s {sub.plan.name} extended to {_local(sub.ends_at):%d %b %Y}.",
        )
    else:
        notification_service.notify(
            sub.user, f"Your {sub.plan.name} plan ends soon",
            f"It ends on {_local(sub.ends_at):%d %B %Y}. Renew from Pricing to keep your limits and tokens flowing.",
            level=NotificationLevel.WARNING, action_url="/app/pricing/", action_label="Renew", telegram=True,
        )
        audit_service.record(AuditAction.USER_UPDATE, actor=request.user, request=request, target=sub.user,
                             description=f"Reminded about {sub.plan.name} ending")
        messages.success(request, f"Reminder sent to {sub.user.display_name}.")
    return _back_to(request, "console:expiring_tokens")


def change_end_date(sub: Subscription, days: int, *, actor, request=None) -> Subscription:
    """Move a subscription's end date by ``days`` (negative shortens it).

    Audited, and the student is told the new date in the site's own time
    zone, in the app and on Telegram. Shared by the Expiring access page and
    the Subscriptions page, so the two cannot word it differently.
    """
    before = sub.ends_at
    sub.ends_at = sub.ends_at + timedelta(days=days)
    sub.save(update_fields=["ends_at", "updated_at"])
    ends = _local(sub.ends_at)
    if days >= 0:
        title = f"{sub.plan.name} extended"
        body = f"Your {sub.plan.name} plan now runs until {ends:%d %B %Y}."
        verb = f"Extended {sub.plan.name} by {days} day{'s' if days != 1 else ''}"
    else:
        title = f"{sub.plan.name} end date changed"
        body = f"Your {sub.plan.name} plan now ends on {ends:%d %B %Y}."
        verb = f"Shortened {sub.plan.name} by {-days} day{'s' if days != -1 else ''}"
    notification_service.notify(
        sub.user, title, body, level=NotificationLevel.SUCCESS if days >= 0 else NotificationLevel.WARNING,
        action_url="/app/usage/", action_label="See your plan", telegram=True,
    )
    audit_service.record(
        AuditAction.USER_UPDATE, actor=actor, request=request, target=sub.user,
        description=verb,
        old_value=f"{_local(before):%Y-%m-%d %H:%M}", new_value=f"{ends:%Y-%m-%d %H:%M}",
    )
    return sub
