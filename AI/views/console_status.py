"""Model status — the page an administrator opens during an incident.

For every model: what the administrator has declared (working normally, slow
or unreliable, paused — with a note students see), next to what the request
log measures over the last 15 minutes, hour and day. The two are kept apart:
the measurement only ever *suggests* a change, a person makes it.

Also here: the site-wide "pause all chat" switch, and the daily digest.
Provider-level detail (credentials, vendor error rates) stays on System health.
"""
from __future__ import annotations

import copy
from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from AI.models import AIModel, AIUsageLog, AuditAction
from AI.services import audit_service, digest_service, model_status_service, settings_service, telegram_service
from AI.utils.decorators import admin_required
from AI.views.console_ai import tone_for
from AI.views.console_insights import _context

#: (minutes, table label, phrase for "in the last …") — shown for every model.
WINDOWS = ((15, "15 min", "15 minutes"), (60, "1 h", "hour"), (24 * 60, "24 h", "24 hours"))

EMPTY = {
    "requests": 0, "failures": 0, "error_rate": 0.0, "p50_ms": 0, "p95_ms": 0,
    "looks_unhealthy": False, "last_error": "", "last_error_code": "", "last_error_at": None,
}

STATUS_CHOICES = [
    {"value": AIModel.ServiceStatus.OPERATIONAL, "label": "Working normally", "tone": "ok"},
    {"value": AIModel.ServiceStatus.DEGRADED, "label": "Slow or unreliable", "tone": "warn"},
    {"value": AIModel.ServiceStatus.PAUSED, "label": "Paused", "tone": "stop"},
]
STATUS_BADGE = {
    AIModel.ServiceStatus.OPERATIONAL: "success",
    AIModel.ServiceStatus.DEGRADED: "warning",
    AIModel.ServiceStatus.PAUSED: "danger",
}


def _draft_notice(model: AIModel, status: str) -> dict:
    """What ``model`` would show under ``status`` with no note of its own."""
    clone = copy.copy(model)
    clone.service_status = status
    clone.status_note = ""
    return model_status_service.model_notice(clone) or {}


def _looks_recovered(model: AIModel, recent: dict) -> bool:
    """A degraded model whose last 15 minutes look fine again. Only a hint."""
    return (
        model.is_degraded
        and recent["requests"] >= model_status_service.MIN_SAMPLE
        and recent["error_rate"] < 5
    )


def _latency(ms: int) -> str:
    if not ms:
        return "—"
    return f"{ms:,} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


def _window(minutes: int, label: str, phrase: str, figures: dict | None) -> dict:
    window = {"minutes": minutes, "label": label, "phrase": phrase, **(figures or EMPTY)}
    window["p50"] = _latency(window["p50_ms"])
    window["p95"] = _latency(window["p95_ms"])
    window["bad"] = window["requests"] >= model_status_service.MIN_SAMPLE and window["error_rate"] >= 25
    return window


def _rows() -> list[dict]:
    models = list(AIModel.objects.select_related("provider_ref").order_by("-is_active", "sort_order", "name"))
    measured = {minutes: model_status_service.measured(minutes) for minutes, _, _ in WINDOWS}
    rows = []
    for model in models:
        windows = [
            _window(minutes, label, phrase, measured[minutes].get(model.key)) for minutes, label, phrase in WINDOWS
        ]
        day = windows[-1]
        unhealthy_in = next((w for w in windows[:2] if w["looks_unhealthy"]), None)
        rows.append({
            "model": model,
            "tone": tone_for(model),
            "badge": STATUS_BADGE.get(model.service_status, "neutral"),
            "windows": windows,
            "last_error": day if day["last_error_at"] else None,
            "unhealthy": unhealthy_in if model.service_status == AIModel.ServiceStatus.OPERATIONAL else None,
            "recovered": _looks_recovered(model, windows[0]),
            "notice": model_status_service.model_notice(model),
            "draft": {
                "degraded": _draft_notice(model, AIModel.ServiceStatus.DEGRADED),
                "paused": _draft_notice(model, AIModel.ServiceStatus.PAUSED),
            },
        })
    return rows


@admin_required
def model_status(request):
    rows = _rows()
    active = [row for row in rows if row["model"].is_active]
    chat_paused, chat_note = model_status_service.chat_pause()
    paused_at, paused_by = model_status_service.chat_pause_changed() if chat_paused else (None, None)

    hour = AIUsageLog.objects.filter(created_at__gte=timezone.now() - timedelta(hours=1)).aggregate(
        requests=Count("id"), failures=Count("id", filter=Q(success=False)),
    )
    hour_rate = round(hour["failures"] * 100 / hour["requests"], 1) if hour["requests"] else 0.0

    return render(
        request,
        "console/model_status.html",
        _context(
            "model_status",
            rows=rows,
            status_choices=STATUS_CHOICES,
            note_max=model_status_service.NOTE_MAX,
            counts={
                "active": len(active),
                "operational": sum(1 for row in active if row["model"].service_status == AIModel.ServiceStatus.OPERATIONAL),
                "degraded": sum(1 for row in active if row["model"].is_degraded),
                "paused": sum(1 for row in active if row["model"].is_paused),
                "unhealthy": sum(1 for row in active if row["unhealthy"]),
            },
            hour={"requests": hour["requests"], "failures": hour["failures"], "rate": hour_rate},
            chat_paused=chat_paused,
            chat_note=chat_note,
            chat_paused_at=paused_at,
            chat_paused_by=paused_by,
            maintenance=model_status_service.maintenance_notice(chat_note),
            maintenance_default=model_status_service.MAINTENANCE_TEXT,
            digest={
                "enabled": digest_service.is_enabled(),
                "telegram_ready": telegram_service.is_configured(),
                "telegram_linked": telegram_service.private_target(request.user) is not None,
                "email": request.user.email,
                "last_sent": digest_service.last_sent(request.user),
                "hour": digest_service.SEND_AFTER_HOUR,
            },
        ),
    )


def _clean_note(raw) -> str:
    """One line: newlines and runs of spaces collapse to single spaces."""
    return " ".join(str(raw or "").split())


@admin_required
@require_POST
def model_status_set(request, pk: int):
    """Declare one model's status and its student-facing note."""
    model = get_object_or_404(AIModel, pk=pk)
    back = reverse("console:model_status") + f"#model-{model.pk}"
    status = (request.POST.get("status") or "").strip()
    note = _clean_note(request.POST.get("note"))

    if status not in AIModel.ServiceStatus.values:
        messages.error(request, "Choose Working normally, Slow or unreliable, or Paused.")
        return redirect(back)
    if len(note) > model_status_service.NOTE_MAX:
        messages.error(request, f"Keep the note to {model_status_service.NOTE_MAX} characters — it has {len(note)}.")
        return redirect(back)

    stored_note = note if status != AIModel.ServiceStatus.OPERATIONAL else ""
    if status == model.service_status and stored_note == model.status_note:
        messages.info(request, f"{model.name} is already set that way.")
        return redirect(back)

    model_status_service.set_status(model, status, note, actor=request.user, request=request)
    label = AIModel.ServiceStatus(status).label
    if status == AIModel.ServiceStatus.PAUSED:
        messages.success(request, f"{model.name} is paused. Students can't send it new messages until you change this.")
    elif status == AIModel.ServiceStatus.DEGRADED:
        messages.success(request, f"{model.name} is marked “{label}”. Students see your note and can still use it.")
    else:
        messages.success(request, f"{model.name} is back to “{label}”. The student notice is gone.")
    return redirect(back)


@admin_required
@require_POST
def chat_pause(request):
    """Pause or resume every model at once, or change the maintenance note."""
    back = reverse("console:model_status")
    action = request.POST.get("action", "")
    note = _clean_note(request.POST.get("note"))
    if len(note) > model_status_service.NOTE_MAX:
        messages.error(request, f"Keep the message to {model_status_service.NOTE_MAX} characters — it has {len(note)}.")
        return redirect(back + "#chat-pause")

    paused, _old_note = model_status_service.chat_pause()
    if action == "pause":
        if request.POST.get("confirm") != "yes":
            messages.error(request, "Confirm the pause first — nothing was changed.")
            return redirect(back + "#chat-pause")
        model_status_service.set_chat_pause(True, note, actor=request.user, request=request)
        messages.success(request, "All chat is paused. Students can read their chats but can't send new messages.")
    elif action == "resume":
        model_status_service.set_chat_pause(False, None, actor=request.user, request=request)
        messages.success(request, "Chat is back on. Students can send messages again.")
    elif action == "note":
        if "note" not in request.POST:
            messages.error(request, "Type the message students should see.")
            return redirect(back + "#chat-pause")
        model_status_service.set_chat_pause(paused, note, actor=request.user, request=request)
        messages.success(request, "Maintenance message saved.")
    else:
        messages.error(request, "Unknown action — nothing was changed.")
    return redirect(back + "#chat-pause")


@admin_required
@require_POST
def digest_action(request):
    """Send a test digest to the requesting administrator, or switch it on/off."""
    back = reverse("console:model_status") + "#digest"
    action = request.POST.get("action", "")

    if action == "test":
        result = digest_service.send_test(request.user)
        audit_service.record(
            AuditAction.SETTINGS_UPDATE, actor=request.user, request=request,
            description="Sent a test admin digest to themselves",
            new_value=f"telegram={result['telegram']}; email={result['email']}",
        )
        reached = []
        if result["telegram"]:
            reached.append("your Telegram")
        if result["email"] == "sent":
            reached.append(f"your e-mail ({request.user.email})")
        if reached:
            messages.success(request, "Test digest sent to " + " and ".join(reached) + ".")
        else:
            reason = result["email_error"] or "no e-mail address on your account"
            messages.error(request, f"The test digest reached nobody: Telegram isn't linked and e-mail failed ({reason}).")
        return redirect(back)

    if action in {"enable", "disable"}:
        enabled = action == "enable"
        if bool(settings_service.get("admin_digest_enabled", False)) != enabled:
            settings_service.set_value("admin_digest_enabled", enabled, actor=request.user)
            audit_service.record(
                AuditAction.SETTINGS_UPDATE, actor=request.user, request=request,
                description=f"{'Turned on' if enabled else 'Turned off'} the daily admin digest",
                old_value=str(not enabled), new_value=str(enabled),
            )
        messages.success(
            request,
            "Daily digest is on — it goes out each morning after the hourly job runs." if enabled
            else "Daily digest is off.",
        )
        return redirect(back)

    messages.error(request, "Unknown action — nothing was changed.")
    return redirect(back)
