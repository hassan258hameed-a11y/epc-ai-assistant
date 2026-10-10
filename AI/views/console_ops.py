"""Operations pages: system health, and getting the data out.

Both are administrator-only surfaces with no student-facing counterpart, and
both are about running the platform rather than serving it:

* **Health** answers "is anything wrong right now, and what do I do about it".
  It reads settings and the database only — opening it never calls a provider,
  so it cannot cost money or hang.
* **Exports** hand the operational record to a spreadsheet. Bulk personal data
  leaving the platform is consequential, so every download is written to the
  audit trail with the dataset, the range and the row count.
"""
from __future__ import annotations

import logging

from django.contrib import messages as flash
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render

from AI.models import AuditAction, AuditLog, Payment, PaymentStatus
from AI.services import audit_service, export_service, health_service
from AI.utils.decorators import admin_required

logger = logging.getLogger("AI.console.ops")


def _context(active: str, **extra) -> dict:
    context = {
        "nav_active": active,
        "pending_payment_count": Payment.objects.filter(status=PaymentStatus.PENDING).count(),
    }
    context.update(extra)
    return context


# ---------------------------------------------------------------------------
# System health
# ---------------------------------------------------------------------------
@admin_required
def health(request):
    """Every check, grouped, worst first."""
    with health_service.runway_memo():
        report = health_service.report()
        runways = health_service.pool_runways()
    # The page has just run every check fresh, so the navigation badge (which
    # the context processor would otherwise work out by running them all a
    # second time when its short cache is cold) is set from this result.
    try:
        from django.core.cache import cache

        cache.set(health_service.ALERT_CACHE_KEY, report["fail_count"], health_service.ALERT_CACHE_TTL)
    except Exception:  # pragma: no cover - a badge must never break the page
        logger.exception("Could not refresh the health badge")
    return render(
        request,
        "console/health.html",
        _context(
            "health",
            report=report,
            groups=report["groups"],
            # One forecast per model: supply is not fungible between them.
            runways=runways,
            window_days=30,
        ),
    )


@admin_required
def health_data(request):
    """The same checks as JSON, so the page can refresh without a reload."""
    return JsonResponse({"ok": True, **health_service.summary()})


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------
@admin_required
def exports(request):
    """Pick a dataset and a range; see how many rows that comes to."""
    datasets = export_service.catalogue()
    start = export_service.parse_date(request.GET.get("from"))
    end = export_service.parse_date(request.GET.get("to"))

    counts = {}
    for dataset in datasets:
        try:
            counts[dataset.key] = export_service.build_queryset(dataset, start, end).count()
        except Exception:  # pragma: no cover — a broken count must not hide the page
            logger.exception("Could not count the %s dataset", dataset.key)
            counts[dataset.key] = None

    recent = (
        AuditLog.objects.filter(action=AuditAction.DATA_EXPORT)
        .select_related("actor")
        .order_by("-created_at")[:8]
    )

    return render(
        request,
        "console/exports.html",
        _context(
            "exports",
            datasets=datasets,
            counts=counts,
            date_from=request.GET.get("from", ""),
            date_to=request.GET.get("to", ""),
            invalid_range=bool(start and end and start > end),
            recent=recent,
        ),
    )


@admin_required
def export_download(request, key: str):
    """Stream one dataset as CSV, and record that it left the platform."""
    dataset = export_service.DATASETS.get(key)
    if dataset is None:
        flash.error(request, "That export does not exist.")
        return redirect("console:exports")

    start = export_service.parse_date(request.GET.get("from"))
    end = export_service.parse_date(request.GET.get("to"))
    if start and end and start > end:
        flash.error(request, "The start of the range is after its end.")
        return redirect("console:exports")

    queryset = export_service.build_queryset(dataset, start, end)
    # Counted before streaming: once the response is being written the status
    # line is already sent, and an audit entry after the fact could never be
    # trusted to have run.
    total = queryset.count()

    span = f"{start or 'the beginning'} to {end or 'now'}"
    audit_service.record(
        AuditAction.DATA_EXPORT,
        actor=request.user,
        request=request,
        target_label=dataset.label,
        description=f"Exported {total} {dataset.label.lower()} row(s), {span}, as CSV.",
        new_value=dataset.key,
    )

    response = StreamingHttpResponse(
        export_service.stream(dataset, queryset), content_type="text/csv; charset=utf-8"
    )
    response["Content-Disposition"] = f'attachment; filename="{export_service.filename(dataset, start, end)}"'
    # The file is a snapshot of live data — never let a proxy keep a copy.
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response["X-Row-Count"] = str(total)
    return response
