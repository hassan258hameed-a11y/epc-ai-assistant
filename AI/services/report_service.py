"""Revenue and growth, month by month — the numbers an owner asks for.

Everything is bucketed by calendar month in the site's timezone. A payment
counts in the month it was paid; a refunded payment counts there too, as gross,
with its refund taken off the same month, so each month's net is what that
month's sales were really worth. Retention reads the usage log, which survives
deleted chats, so a student who asked anything in a month counts as active.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from django.db.models import Count
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from AI.models import AIUsageLog, Payment, PaymentStatus, Role, User

ZERO = Decimal("0")


def _month_start(value: date) -> date:
    return date(value.year, value.month, 1)


def _shift(month: date, by: int) -> date:
    index = month.year * 12 + (month.month - 1) + by
    return date(index // 12, index % 12 + 1, 1)


def months_back(count: int) -> list[date]:
    """The first day of each of the last ``count`` months, oldest first, this month included."""
    this = _month_start(timezone.localdate())
    return [_shift(this, -offset) for offset in range(count - 1, -1, -1)]


def _aware(month: date) -> datetime:
    return timezone.make_aware(datetime(month.year, month.month, 1))


def _key(value) -> date:
    if isinstance(value, datetime):
        value = timezone.localtime(value) if timezone.is_aware(value) else value
        return date(value.year, value.month, 1)
    return date(value.year, value.month, 1)


def revenue(months: int = 6) -> dict:
    buckets = months_back(months)
    start = _aware(buckets[0])
    tz = timezone.get_current_timezone()

    payments = list(
        Payment.objects.filter(status__in=[PaymentStatus.PAID, PaymentStatus.REFUNDED])
        .annotate(when=Coalesce("paid_at", "created_at"))
        .filter(when__gte=start)
        .values("amount", "currency", "status", "method", "user_id", "plan__name", "when")
    )
    signups = (
        User.objects.filter(role=Role.STUDENT, date_joined__gte=start)
        .annotate(month=TruncMonth("date_joined", tzinfo=tz)).values("month").annotate(n=Count("id"))
    )
    new_by_month = {_key(row["month"]): row["n"] for row in signups}

    rows = {month: {"month": month, "gross": ZERO, "refunds": ZERO, "count": 0, "payers": set()} for month in buckets}
    plans: dict[str, dict] = defaultdict(lambda: {"count": 0, "net": ZERO})
    methods: dict[str, dict] = defaultdict(lambda: {"count": 0, "net": ZERO})
    currencies: dict[str, int] = defaultdict(int)
    for p in payments:
        month = _key(p["when"])
        if month not in rows:
            continue
        amount = p["amount"] or ZERO
        row = rows[month]
        row["gross"] += amount
        row["count"] += 1
        currencies[p["currency"] or ""] += 1
        refunded = p["status"] == PaymentStatus.REFUNDED
        if refunded:
            row["refunds"] += amount
        else:
            row["payers"].add(p["user_id"])
        net = ZERO if refunded else amount
        plans[p["plan__name"] or "No plan"]["count"] += 1
        plans[p["plan__name"] or "No plan"]["net"] += net
        methods[p["method"] or "other"]["count"] += 1
        methods[p["method"] or "other"]["net"] += net

    monthly = []
    for month in buckets:
        row = rows[month]
        net = row["gross"] - row["refunds"]
        payers = len(row["payers"])
        monthly.append({
            "month": month,
            "label": month.strftime("%b %Y"),
            "short": month.strftime("%b"),
            "gross": row["gross"],
            "refunds": row["refunds"],
            "net": net,
            "payments": row["count"],
            "payers": payers,
            "arppu": (net / payers).quantize(Decimal("0.01")) if payers else ZERO,
            "new_students": new_by_month.get(month, 0),
        })

    total_net = sum((m["net"] for m in monthly), ZERO)
    all_payers = {p["user_id"] for p in payments if p["status"] == PaymentStatus.PAID}
    previous, current = (monthly[-2]["net"] if len(monthly) > 1 else ZERO), monthly[-1]["net"]

    def share(items: dict) -> list[dict]:
        out = [{"name": name, "count": v["count"], "net": v["net"]} for name, v in items.items()]
        out.sort(key=lambda r: r["net"], reverse=True)
        for r in out:
            r["percent"] = round(float(r["net"] / total_net * 100), 1) if total_net else 0
        return out

    from AI.models import PaymentMethod

    method_rows = share(methods)
    labels = dict(PaymentMethod.choices)
    for r in method_rows:
        r["name"] = labels.get(r["name"], r["name"].replace("_", " ").title())

    return {
        "months": monthly,
        "currency": max(currencies, key=currencies.get) if currencies else "USD",
        "totals": {
            "gross": sum((m["gross"] for m in monthly), ZERO),
            "refunds": sum((m["refunds"] for m in monthly), ZERO),
            "net": total_net,
            "payments": sum(m["payments"] for m in monthly),
            "payers": len(all_payers),
            "arppu": (total_net / len(all_payers)).quantize(Decimal("0.01")) if all_payers else ZERO,
            "new_students": sum(m["new_students"] for m in monthly),
            "refund_rate": round(float(sum((m["refunds"] for m in monthly), ZERO)
                                       / sum((m["gross"] for m in monthly), ZERO) * 100), 1)
            if sum((m["gross"] for m in monthly), ZERO) else 0,
        },
        "this_month": current,
        "change": (round(float((current - previous) / previous * 100), 1) if previous else None),
        "by_plan": share(plans),
        "by_method": method_rows,
    }


def retention(months: int = 6) -> dict:
    """Of the students who joined in each month, the share active in each month since."""
    buckets = months_back(months)
    tz = timezone.get_current_timezone()
    start = _aware(buckets[0])

    joined = (
        User.objects.filter(role=Role.STUDENT, date_joined__gte=start)
        .annotate(month=TruncMonth("date_joined", tzinfo=tz)).values_list("id", "month")
    )
    cohorts: dict[date, set] = defaultdict(set)
    for user_id, month in joined:
        cohorts[_key(month)].add(user_id)

    activity = (
        AIUsageLog.objects.filter(success=True, created_at__gte=start, user_id__in=[u for s in cohorts.values() for u in s])
        .annotate(month=TruncMonth("created_at", tzinfo=tz)).values_list("user_id", "month").distinct()
    )
    active: dict[date, set] = defaultdict(set)
    for user_id, month in activity:
        active[_key(month)].add(user_id)

    rows = []
    for index, month in enumerate(buckets):
        members = cohorts.get(month, set())
        cells = []
        for offset in range(len(buckets) - index):
            seen = len(members & active.get(_shift(month, offset), set())) if members else 0
            cells.append({"offset": offset, "count": seen,
                          "percent": round(seen * 100 / len(members)) if members else None})
        rows.append({"label": month.strftime("%b %Y"), "size": len(members), "cells": cells})
    return {"rows": rows, "width": len(buckets)}


def monthly_csv_rows(report: dict) -> list[list]:
    header = ["Month", "Gross", "Refunds", "Net", "Payments", "Paying students", "Net per paying student",
              "New students"]
    body = [[m["label"], f"{m['gross']:.2f}", f"{m['refunds']:.2f}", f"{m['net']:.2f}", m["payments"], m["payers"],
             f"{m['arppu']:.2f}", m["new_students"]] for m in report["months"]]
    return [header, *body]
