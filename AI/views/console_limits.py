"""The administrator's view of who holds what, and how fast they may spend it.

One row per package a student holds, carrying every number the support desk is
actually asked about: what was bought, what is left, how much of today's, this
week's and this month's allowance has gone, and when each of those comes back.

Read-only on purpose.  Balances move through
:mod:`AI.services.wallet_service` and nowhere else, so a screen that both
reports and edits would be a second write path to keep in step with the first.
Editing lives where it already lived: the distribution page for tokens, the
package form for ceilings.
"""
from __future__ import annotations

from django.db.models import F, Q
from django.shortcuts import render
from django.utils import timezone

from AI.models import AIModel, AllocationStatus, PackageAllocation, Payment, PaymentStatus
from AI.services import model_scope, usage_limit_service
from AI.utils.decorators import admin_required
from AI.utils.pagination import page_range, paginate

PAGE_SIZE = 25

#: What the status filter offers, beyond "everything".
STATUS_CHOICES = [
    ("active", "Active"),
    ("exhausted", "Fully spent"),
    ("cancelled", "Cancelled"),
    ("blocked", "Limit reached"),
]


def _blocked_now() -> Q:
    """Rows that are at one of their ceilings *right now*, as a database filter.

    Expressed in SQL rather than in Python so the page count and the page
    contents agree: filtering after pagination gives a paginator that counts
    rows it then does not show, which reports the wrong number of pages and can
    land somebody on an empty one.

    ``window_end > now`` is what makes it *now* rather than *last time anybody
    looked*. Counters are reset lazily, so a spent window whose end has already
    passed is not a block — the next request would roll it and go through.
    """
    now = timezone.now()
    condition = Q()
    for name in usage_limit_service.WINDOWS:
        condition |= Q(
            **{
                f"{name}_limit__gt": 0,
                f"{name}_used__gte": F(f"{name}_limit"),
                f"{name}_window_end__gt": now,
            }
        )
    return condition


def _window(row: PackageAllocation, name: str) -> dict:
    limit = getattr(row, f"{name}_limit", 0)
    used = getattr(row, f"{name}_used", 0)
    reset_at = row.reset_at(name)
    return {
        "limit": limit,
        "used": used,
        "remaining": max(0, limit - used) if limit else None,
        "percent": row.percent_used(name),
        "enforced": limit > 0,
        "reset_at": reset_at,
        "at_limit": bool(limit) and used >= limit,
    }


@admin_required
def usage_limits(request):
    """Every package, filterable by student, model and state."""
    query = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()

    # This page had a model dropdown of its own, on the same `?model=` key the
    # console-wide selector now uses. Reading the shared scope instead means one
    # control rather than two that could show different things, and the choice
    # follows you here from wherever you made it.
    selected = model_scope.resolve(request)

    rows = selected.allocations(
        PackageAllocation.objects.select_related("user", "ai_model", "plan")
        .order_by("-activated_at", "-id")
    )

    if query:
        rows = rows.filter(
            Q(user__email__icontains=query)
            | Q(user__username__icontains=query)
            | Q(user__full_name__icontains=query)
            | Q(label__icontains=query)
        )
    if status == "blocked":
        rows = rows.filter(_blocked_now())
    elif status:
        rows = rows.filter(status=status)

    page = paginate(request, rows, PAGE_SIZE)

    # Windows are rolled lazily, so a row read straight from the table may show
    # a window whose reset time has already passed. Rolling the in-memory copy
    # shows what is true now. It is deliberately *not* written back: this page
    # is read-only, and an unlocked write of the counters read a moment ago
    # could overwrite usage a chat request recorded in between. The next
    # request that spends from the package rolls it for real, under its lock.
    now = timezone.now()
    prepared = []
    for row in page.object_list:
        row.roll(now)
        windows = {name: _window(row, name) for name in usage_limit_service.WINDOWS}
        blocked = [name for name, info in windows.items() if info["at_limit"]]
        prepared.append(
            {
                "row": row,
                "windows": windows,
                "blocked": blocked,
                "spendable": row.spendable(),
                "percent_spent": (
                    min(100, round(row.tokens_used / row.tokens_allocated * 100))
                    if row.tokens_allocated
                    else 0
                ),
            }
        )

    return render(
        request,
        "console/usage_limits.html",
        {
            "nav_active": "usage_limits",
            "pending_payment_count": Payment.objects.filter(status=PaymentStatus.PENDING).count(),
            "rows": prepared,
            "page": page,
            "page_numbers": page_range(page),
            "query": query,
            "status": status,
            "status_choices": STATUS_CHOICES,
            # The cards count the selected model's packages, so they describe
            # the same set as the table beneath them.
            "totals": {
                "packages": selected.allocations(PackageAllocation.objects.all()).count(),
                "active": selected.allocations(
                    PackageAllocation.objects.filter(status=AllocationStatus.ACTIVE)
                ).count(),
                "minimum": usage_limit_service.minimum_limit_package(),
            },
        },
    )
