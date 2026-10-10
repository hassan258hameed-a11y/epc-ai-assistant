"""Console pages for answer quality and the featured prompt library.

*Answer quality* is what students said about replies, per model — the one
signal in the console about whether the answers are any good, as opposed to
how many of them there were. *Prompt library* is where an administrator
publishes the prompts every student sees.
"""
from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from AI.models import AIModel, AuditAction, FeedbackReason, FeedbackStatus, MessageFeedback, SavedPrompt
from AI.services import audit_service, library_service
from AI.utils.decorators import admin_required
from AI.utils.pagination import page_range, paginate
from AI.views.console_ai import tone_for
from AI.views.console_insights import _context

RANGES = {"7": 7, "30": 30, "90": 90}
DEFAULT_RANGE = 30


@admin_required
def quality(request):
    days = RANGES.get(request.GET.get("range", ""), DEFAULT_RANGE)
    report = library_service.quality_report(days)

    tones = {m.key: tone_for(m) for m in AIModel.objects.all()}
    for row in report["rows"]:
        row["tone"] = tones.get(row["key"], tone_for(AIModel(key=row["key"])))

    negatives = library_service.recent_negative(days)
    model_key = (request.GET.get("model") or "").strip()
    if model_key:
        negatives = negatives.filter(model_key=model_key)
    reason = (request.GET.get("reason") or "").strip()
    if reason in FeedbackReason.values:
        negatives = negatives.filter(reason=reason)
    # The queue opens on what still needs a look; "all" shows everything.
    status = request.GET.get("status", FeedbackStatus.OPEN)
    if status in FeedbackStatus.values:
        negatives = negatives.filter(status=status)
    else:
        status = "all"
    page = paginate(request, negatives.order_by("-updated_at", "-id"), per_page=20)

    return render(
        request,
        "console/quality.html",
        _context(
            "quality",
            report=report,
            days=days,
            range_options=[("7", "7 days"), ("30", "30 days"), ("90", "90 days")],
            page=page,
            page_numbers=page_range(page),
            negatives=page.object_list,
            model_filter=model_key,
            reason_filter=reason,
            reasons=FeedbackReason.choices,
            rated_models=[row for row in report["rows"] if row["total"]],
            status_filter=status,
            status_choices=FeedbackStatus.choices,
            review_counts=library_service.review_counts(days),
        ),
    )


@admin_required
@require_POST
def feedback_review(request, pk: int):
    """Mark one thumbs-down reviewed or resolved, with a note."""
    feedback = get_object_or_404(MessageFeedback, pk=pk, rating__lt=0)
    try:
        library_service.review_feedback(
            request.user, feedback, status=request.POST.get("status", ""), note=request.POST.get("note", ""),
        )
    except library_service.LibraryError as exc:
        messages.error(request, exc.friendly_message)
    else:
        audit_service.record(
            AuditAction.SETTINGS_UPDATE, actor=request.user, request=request, target=feedback.user,
            description=f"Marked a thumbs-down on {feedback.model_key or 'a reply'} as {feedback.get_status_display().lower()}",
        )
        messages.success(request, f"Marked {feedback.get_status_display().lower()}.")
    back = request.POST.get("back") or ""
    url = reverse("console:quality")
    return redirect(back if back.startswith(url) and "//" not in back else f"{url}#complaints")


@admin_required
def prompt_library(request):
    editing = None
    edit_id = request.GET.get("edit")
    if edit_id and edit_id.isdigit():
        editing = library_service.featured_queryset().filter(pk=edit_id).first()

    return render(
        request,
        "console/prompt_library.html",
        _context(
            "prompt_library",
            prompts=library_service.featured_queryset().order_by("sort_order", "title"),
            editing=editing,
            stats=library_service.library_stats(),
        ),
    )


@admin_required
@require_POST
def prompt_save(request, pk: int | None = None):
    prompt = get_object_or_404(library_service.featured_queryset(), pk=pk) if pk else None
    try:
        saved = library_service.save_featured(
            request.user,
            title=request.POST.get("title", ""),
            body=request.POST.get("body", ""),
            category=request.POST.get("category", ""),
            sort_order=_int(request.POST.get("sort_order"), 100),
            prompt=prompt,
        )
    except library_service.LibraryError as exc:
        messages.error(request, exc.friendly_message)
        url = reverse("console:prompt_library")
        return redirect(f"{url}?edit={pk}" if pk else url)

    audit_service.record(
        AuditAction.SETTINGS_UPDATE, actor=request.user, request=request, target=saved,
        description=f"{'Updated' if pk else 'Published'} featured prompt “{saved.title}”",
    )
    messages.success(request, f"“{saved.title}” {'updated' if pk else 'published to every student'}.")
    return redirect("console:prompt_library")


@admin_required
@require_POST
def prompt_toggle(request, pk: int):
    prompt = get_object_or_404(library_service.featured_queryset(), pk=pk)
    prompt.is_active = not prompt.is_active
    prompt.save(update_fields=["is_active", "updated_at"])
    audit_service.record(
        AuditAction.SETTINGS_UPDATE, actor=request.user, request=request, target=prompt,
        description=f"{'Showed' if prompt.is_active else 'Hid'} featured prompt “{prompt.title}”",
    )
    messages.success(request, f"“{prompt.title}” is now {'visible' if prompt.is_active else 'hidden'}.")
    return redirect("console:prompt_library")


@admin_required
@require_POST
def prompt_delete(request, pk: int):
    prompt = get_object_or_404(library_service.featured_queryset(), pk=pk)
    title = prompt.title
    prompt.delete()
    audit_service.record(
        AuditAction.SETTINGS_UPDATE, actor=request.user, request=request,
        target_label=title[:140], description=f"Deleted featured prompt “{title}”",
    )
    messages.success(request, f"“{title}” deleted.")
    return redirect("console:prompt_library")


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
