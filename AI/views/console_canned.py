"""Saved replies — the answers support gives often enough to write once.

Listed and edited here; inserted into a reply from any ticket. The use count is
bumped when a reply that started from one is actually sent, so the list sorts
itself by what the team really reaches for.
"""
from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from AI.models import AuditAction, CannedReply
from AI.services import audit_service
from AI.utils.decorators import admin_required
from AI.views.console_insights import _context

TITLE_MAX, BODY_MAX = 80, 4000


@admin_required
def canned_replies(request):
    editing = None
    edit_id = request.GET.get("edit", "")
    if edit_id.isdigit():
        editing = CannedReply.objects.filter(pk=edit_id).first()
    replies = list(CannedReply.objects.select_related("created_by"))
    return render(request, "console/canned_replies.html", _context(
        "canned_replies", replies=replies, editing=editing,
        total_uses=sum(r.use_count for r in replies),
    ))


@admin_required
@require_POST
def canned_save(request, pk: int | None = None):
    reply = get_object_or_404(CannedReply, pk=pk) if pk else CannedReply(created_by=request.user)
    title = " ".join((request.POST.get("title") or "").split())[:TITLE_MAX]
    body = (request.POST.get("body") or "").strip()[:BODY_MAX]
    if not title or not body:
        messages.error(request, "A saved reply needs a title and the reply itself.")
        url = reverse("console:canned_replies")
        return redirect(f"{url}?edit={pk}#cannedForm" if pk else f"{url}#cannedForm")
    reply.title, reply.body = title, body
    reply.save()
    audit_service.record(
        AuditAction.SETTINGS_UPDATE, actor=request.user, request=request, target_label=title[:140],
        description=f"{'Updated' if pk else 'Added'} saved reply “{title}”",
    )
    messages.success(request, f"“{title}” {'updated' if pk else 'saved'}.")
    return redirect("console:canned_replies")


@admin_required
@require_POST
def canned_delete(request, pk: int):
    reply = get_object_or_404(CannedReply, pk=pk)
    title = reply.title
    reply.delete()
    audit_service.record(
        AuditAction.SETTINGS_UPDATE, actor=request.user, request=request, target_label=title[:140],
        description=f"Deleted saved reply “{title}”",
    )
    messages.success(request, f"“{title}” deleted.")
    return redirect("console:canned_replies")
