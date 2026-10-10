"""Is a model working right now — as measured, and as an administrator says.

Two sources, kept apart on purpose:

* **Measured** health comes from ``AIUsageLog``: requests, failures and latency
  over a recent window. It is a hint for administrators.
* **Declared** status is what an administrator sets on ``AIModel`` during an
  incident (degraded or paused, with a note for students). Only the declared
  status changes what students can do: a paused model takes no new messages,
  and "pause all chat" (``chat_paused``) stops every model at once.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count, Q
from django.utils import timezone

from AI.models import AIModel, AIUsageLog, AuditAction
from AI.services import audit_service, settings_service

#: Minimum requests in the window before an error rate means anything.
MIN_SAMPLE = 5

#: Longest student-facing note, for a model or for the site-wide pause.
NOTE_MAX = 200

MAINTENANCE_TITLE = "Chat is paused for maintenance"
MAINTENANCE_TEXT = "New messages are paused for a short while. Your chats are safe."


def chat_pause() -> tuple[bool, str]:
    """``(paused, note)`` for the site-wide maintenance switch."""
    paused = bool(settings_service.get("chat_paused", False))
    note = (settings_service.get("chat_paused_note", "") or "").strip()
    return paused, note


def maintenance_notice(note: str = "") -> dict:
    """The notice every student sees while all chat is paused."""
    return {"level": "maintenance", "title": MAINTENANCE_TITLE, "text": (note or "").strip() or MAINTENANCE_TEXT}


def notice_for(model: AIModel | None) -> dict | None:
    """What a student should be told about ``model`` right now, or ``None``.

    ``{"level": "paused"|"degraded"|"maintenance", "title": …, "text": …}``.
    """
    paused, note = chat_pause()
    if paused:
        return maintenance_notice(note)
    return model_notice(model)


def model_notice(model: AIModel | None) -> dict | None:
    """The model's own notice, ignoring the site-wide pause.

    ``notice_for`` is this plus the pause; the console uses it on its own to
    preview a model's note while all chat happens to be paused.
    """
    if model is None:
        return None
    if model.is_paused:
        return {
            "level": "paused",
            "title": f"{model.name} is paused",
            "text": model.status_note or f"{model.name} isn't taking new messages right now. Try another model.",
        }
    if model.is_degraded:
        return {
            "level": "degraded",
            "title": f"{model.name} is slow right now",
            "text": model.status_note or "Answers may be slow or fail. Another model may be quicker.",
        }
    return None


def blocks_sending(model: AIModel | None) -> dict | None:
    """The notice when a message to ``model`` must not be sent, else ``None``."""
    notice = notice_for(model)
    if notice and notice["level"] in {"maintenance", "paused"}:
        return notice
    return None


def set_status(model: AIModel, status: str, note: str = "", *, actor=None, request=None) -> AIModel:
    if status not in AIModel.ServiceStatus.values:
        raise ValueError(f"Unknown status {status!r}")
    old = model.service_status
    model.service_status = status
    model.status_note = (note or "").strip()[:200] if status != AIModel.ServiceStatus.OPERATIONAL else ""
    model.status_changed_at = timezone.now()
    model.save(update_fields=["service_status", "status_note", "status_changed_at", "updated_at"])
    forget_attention()
    audit_service.record(
        AuditAction.SETTINGS_UPDATE, actor=actor, request=request, target=model,
        description=f"{model.name}: status {old} → {status}" + (f" ({model.status_note})" if model.status_note else ""),
        old_value=old, new_value=status,
    )
    return model


def measured(minutes: int = 60) -> dict[str, dict]:
    """Per model key: requests, failures, error rate and median/p95 latency."""
    since = timezone.now() - timedelta(minutes=minutes)
    logs = AIUsageLog.objects.filter(created_at__gte=since)
    counts = {
        row["model_key"]: row
        for row in logs.values("model_key").annotate(
            requests=Count("id"), failures=Count("id", filter=Q(success=False)),
        )
    }
    result = {}
    for key, row in counts.items():
        latencies = sorted(
            logs.filter(model_key=key, success=True, latency_ms__gt=0).values_list("latency_ms", flat=True)[:2000]
        )
        p50 = latencies[len(latencies) // 2] if latencies else 0
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else 0
        rate = row["failures"] / row["requests"] if row["requests"] else 0
        last_error = None
        if row["failures"]:
            last_error = (
                logs.filter(model_key=key, success=False).order_by("-created_at")
                .values("error_code", "error_message", "created_at").first()
            )
        result[key] = {
            "requests": row["requests"],
            "failures": row["failures"],
            "error_rate": round(rate * 100, 1),
            "p50_ms": p50,
            "p95_ms": p95,
            # A suggestion for the administrator, never applied automatically.
            "looks_unhealthy": row["requests"] >= MIN_SAMPLE and rate >= 0.25,
            # The newest failure in the window, so an administrator can read
            # what the vendor actually said without opening the logs.
            "last_error": (last_error or {}).get("error_message", ""),
            "last_error_code": (last_error or {}).get("error_code", ""),
            "last_error_at": (last_error or {}).get("created_at"),
        }
    return result


def set_chat_pause(paused: bool, note: str | None = None, *, actor=None, request=None) -> tuple[bool, str]:
    """Turn the site-wide pause on or off, and/or change its note. Audit-logged.

    ``note=None`` leaves the stored note as it is.
    """
    old_paused, old_note = chat_pause()
    new_note = old_note if note is None else " ".join(str(note).split())[:NOTE_MAX]
    if bool(paused) != old_paused:
        settings_service.set_value("chat_paused", bool(paused), actor=actor)
    if new_note != old_note:
        settings_service.set_value("chat_paused_note", new_note, actor=actor)
    if bool(paused) != old_paused:
        description = "Paused all chat" if paused else "Resumed chat"
    else:
        description = "Changed the maintenance message"
    if bool(paused) != old_paused or new_note != old_note:
        audit_service.record(
            AuditAction.SETTINGS_UPDATE, actor=actor, request=request, target_label="Chat (all models)",
            description=description + (f": {new_note}" if new_note and paused else ""),
            old_value=f"paused={old_paused}; note={old_note}", new_value=f"paused={bool(paused)}; note={new_note}",
        )
    return bool(paused), new_note


def chat_pause_changed():
    """``(when, by)`` the pause switch was last flipped, or ``(None, None)``."""
    from AI.models import AppSetting

    row = AppSetting.objects.filter(key="chat_paused").select_related("updated_by").first()
    if row is None:
        return None, None
    return row.updated_at, row.updated_by


ATTENTION_CACHE_KEY = "model_status:attention"
ATTENTION_CACHE_TTL = 30


def forget_attention() -> None:
    """Drop the cached badge counts, after a status change."""
    cache.delete(ATTENTION_CACHE_KEY)


def attention(force: bool = False) -> dict:
    """What the console navigation badge needs, for every console page.

    Counts active models only — an inactive model is already hidden from
    students, so its status is not an incident. The model counts are cached
    briefly (and dropped by ``set_status``); the pause switch is read from the
    settings cache, so flipping it on the Settings page shows at once.
    """
    counts = None if force else cache.get(ATTENTION_CACHE_KEY)
    if counts is None:
        counts = AIModel.objects.filter(is_active=True).aggregate(
            paused=Count("id", filter=Q(service_status=AIModel.ServiceStatus.PAUSED)),
            degraded=Count("id", filter=Q(service_status=AIModel.ServiceStatus.DEGRADED)),
        )
        cache.set(ATTENTION_CACHE_KEY, counts, ATTENTION_CACHE_TTL)
    chat_paused = chat_pause()[0]
    paused, degraded = counts["paused"] or 0, counts["degraded"] or 0
    return {
        "chat_paused": chat_paused,
        "paused": paused,
        "degraded": degraded,
        "count": paused + degraded + (1 if chat_paused else 0),
        "tone": "danger" if (chat_paused or paused) else "warning",
    }
