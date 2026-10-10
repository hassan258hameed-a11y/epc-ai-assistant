"""Revenue & growth — the monthly report, its CSV, and cohort retention."""
from __future__ import annotations

import csv
import io
import json

from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from AI.services import report_service
from AI.utils.decorators import admin_required
from AI.views.console_insights import _context

RANGES = {"3": 3, "6": 6, "12": 12}
DEFAULT_RANGE = 6


def _months(request) -> int:
    return RANGES.get(request.GET.get("range", ""), DEFAULT_RANGE)


@admin_required
def reports(request):
    months = _months(request)
    report = report_service.revenue(months)
    cohorts = report_service.retention(months)
    labels = [m["short"] for m in report["months"]]
    change = report["change"]
    totals, currency = report["totals"], report["currency"]
    # Display strings for the KPI tiles, built here rather than chained
    # through template filters.
    kpi = {
        "net": f"{totals['net']:,.2f}",
        "net_foot": f"{currency} after refunds, last {months} months",
        "this": f"{report['this_month']:,.2f}",
        "delta": f"{'+' if change and change > 0 else ''}{change}%" if change is not None else "",
        "this_foot": "net, against last month" if change is not None else "net so far this month",
        "arppu": f"{totals['arppu']:,.2f}",
        "arppu_foot": f"{totals['payers']:,} paying student{'s' if totals['payers'] != 1 else ''}",
        "refunds": f"{totals['refunds']:,.2f}",
        "refunds_foot": f"{totals['refund_rate']}% of gross",
    }
    return render(
        request,
        "console/reports.html",
        _context(
            "reports",
            months=months,
            range_options=[("3", "3 months"), ("6", "6 months"), ("12", "12 months")],
            report=report,
            cohorts=cohorts,
            change=change,
            kpi=kpi,
            change_tone="flat" if change is None or change == 0 else ("up" if change > 0 else "down"),
            revenue_chart=json.dumps({
                "labels": labels,
                "series": [
                    {"name": "Net revenue", "values": [float(m["net"]) for m in report["months"]], "color": "#2563EB"},
                    {"name": "Refunds", "values": [float(m["refunds"]) for m in report["months"]], "color": "#E11D48"},
                ],
            }),
            growth_chart=json.dumps({
                "labels": labels,
                "series": [
                    {"name": "New students", "values": [m["new_students"] for m in report["months"]], "color": "#60A5FA"},
                    {"name": "Paying students", "values": [m["payers"] for m in report["months"]], "color": "#10B981"},
                ],
            }),
        ),
    )


@admin_required
def reports_export(request):
    months = _months(request)
    buffer = io.StringIO()
    csv.writer(buffer).writerows(report_service.monthly_csv_rows(report_service.revenue(months)))
    response = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="revenue-{months}-months-{timezone.localdate().isoformat()}.csv"'
    )
    return response
