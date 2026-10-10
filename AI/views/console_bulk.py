"""Bulk actions from the students list.

Enabling, disabling and exporting act at once (the list asks first). Giving
tokens, sending a message and tagging go through a review page that lists who
is selected and asks for the details — a bulk action is easy to start and hard
to undo, so it is shown before it is done.

Administrators and the acting admin are never touched by enable or disable:
a bulk bar that could lock someone out of the console by accident would be a
trap, not a tool.
"""
from __future__ import annotations

import csv
import io

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from AI.models import AIModel, AuditAction, NotificationLevel, Role, User
from AI.services import account_service, admin_tools_service, audit_service, distribution_service
from AI.services.model_pool_service import InsufficientPool
from AI.utils.decorators import admin_required
from AI.utils.pagination import clean_id
from AI.views.console_insights import _context

MAX_SELECTED = 500
REVIEWED = {"tokens", "message", "tag"}


def _back(request) -> str:
    target = request.POST.get("next") or ""
    base = reverse("console:users")
    return target if target.startswith(base) and "//" not in target else base


def _selected(request):
    ids = [pk for pk in (clean_id(v) for v in request.POST.getlist("ids")) if pk][:MAX_SELECTED]
    return list(User.objects.filter(pk__in=ids, role=Role.STUDENT).order_by("full_name", "username"))


@admin_required
@require_POST
def users_bulk(request):
    action = request.POST.get("action", "")
    students = _selected(request)
    if not students:
        messages.error(request, "Select at least one student first.")
        return redirect(_back(request))

    if action in {"enable", "disable"}:
        enable = action == "enable"
        changed = 0
        for student in students:
            if student.pk == request.user.pk or student.is_admin_user:
                continue
            if (student.status == "active") == enable:
                continue
            account_service.set_enabled(
                student, enable, actor=request.user, request=request,
                reason="" if enable else (request.POST.get("reason") or "Disabled in bulk")[:255],
            )
            changed += 1
        messages.success(request, f"{'Enabled' if enable else 'Disabled'} {changed} account{'s' if changed != 1 else ''}.")
        return redirect(_back(request))

    if action == "export":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["Username", "Full name", "E-mail", "Phone", "Status", "Source", "Joined", "Tags"])
        for s in students:
            # The day they joined where the school is, not in UTC: somebody who
            # signed up at 6am in Phnom Penh joined "today", not "yesterday".
            writer.writerow([s.username, s.full_name, s.email, s.phone, s.status, s.source,
                             timezone.localtime(s.date_joined).date().isoformat(), ", ".join(s.tags or [])])
        # Personal data leaving the platform is recorded, as every other export is.
        audit_service.record(
            AuditAction.DATA_EXPORT,
            actor=request.user,
            request=request,
            target_label="Students",
            description=f"Exported {len(students)} selected student(s) as CSV from the students list.",
            new_value="students_selection",
        )
        response = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="students-{len(students)}-{timezone.localdate().isoformat()}.csv"'
        )
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        return response

    if action not in REVIEWED:
        messages.error(request, "Choose what to do with the selected students.")
        return redirect(_back(request))

    if request.POST.get("confirm") == "1":
        try:
            _apply(request, action, students)
        except admin_tools_service.AdminToolError as exc:
            messages.error(request, exc.friendly_message)
            return _review(request, action, students)
        return redirect(_back(request))
    return _review(request, action, students)


def _review(request, action, students):
    return render(
        request,
        "console/users_bulk.html",
        _context(
            "users",
            action=action,
            students=students,
            back=_back(request),
            models=AIModel.objects.filter(is_active=True).order_by("sort_order", "name"),
            levels=NotificationLevel.choices,
            known_tags=admin_tools_service.all_tags(),
            form=request.POST,
        ),
    )


def _apply(request, action, students) -> None:
    post = request.POST
    if action == "tokens":
        model_id = clean_id(post.get("model"))
        model = AIModel.objects.filter(pk=model_id, is_active=True).first() if model_id else None
        if model is None:
            raise admin_tools_service.AdminToolError("Choose a model.")
        try:
            amount = int(str(post.get("amount") or "0").replace(",", ""))
        except ValueError:
            amount = 0
        if amount <= 0:
            raise admin_tools_service.AdminToolError("Enter a token amount greater than zero.")
        try:
            result = distribution_service.bulk_distribute(
                students, model, amount, action="add",
                reason=(post.get("reason") or f"Bulk top-up of {model.name}")[:255],
                performed_by=request.user, request=request,
            )
        except InsufficientPool as exc:
            raise admin_tools_service.AdminToolError(str(exc) or "That model's pool cannot cover this.")
        if result["stopped_short"]:
            messages.warning(
                request,
                f"{model.name}'s pool ran out after {result['changed']} of {result['attempted']} students. "
                "Top up the pool, then give the rest.",
            )
        else:
            messages.success(request, f"Gave {amount:,} {model.name} tokens to {result['changed']} students.")
    elif action == "message":
        outcome = admin_tools_service.message_students(
            request.user, students, title=post.get("title", ""), body=post.get("body", ""),
            level=post.get("level", NotificationLevel.INFO), email=post.get("email") == "on", request=request,
        )
        extra = f", {outcome['mailed']} by e-mail" if post.get("email") == "on" else ""
        messages.success(request, f"Message sent to {outcome['sent']} student{'s' if outcome['sent'] != 1 else ''}{extra}.")
    elif action == "tag":
        changed = admin_tools_service.tag_students(
            request.user, students, add=post.get("add_tag", ""), remove=post.get("remove_tag", ""), request=request,
        )
        messages.success(request, f"Updated tags on {changed} student{'s' if changed != 1 else ''}.")
