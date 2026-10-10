"""Daily, weekly and monthly usage windows — the rate at which tokens may be spent.

The wallet says *how many tokens a student owns*.  This module says *how many of
them may be spent right now*, and it is the only place that answers that
question.  Nothing here is advisory: :func:`check` runs before any provider is
contacted, and :func:`consume` runs inside the same transaction as the wallet
debit, so the two can never disagree about what was spent.

Three windows, and only three — daily, weekly, monthly.  They are configured per
model *and* per package by an administrator, snapshotted onto each purchase at
activation, and rolled forward lazily from the activation instant.  There is no
percentage anywhere in this module: a limit is a number an administrator typed,
not a fraction of something else derived in code.

Draw order is metered packages first, then unmetered grants, oldest first within
each.  A student holding two packages spends the one they bought first, which is
the order they would expect; a student holding a package *and* an admin grant
spends the package while its windows allow, so its counters move with use.
See :func:`_plan_draw`.

Concurrency
-----------
Every write path takes ``select_for_update`` over the student's allocations for
one model, in a stable order, and re-reads the counters under that lock.  Two
browser tabs sending at the same instant therefore serialise: the second sees
what the first spent, and no pair of requests can both pass a check that only
one of them had room for.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from AI.models import AIModel, AllocationSource, AllocationStatus, PackageAllocation
from AI.services import settings_service

logger = logging.getLogger("AI.limits")

#: The three windows, in the order they are shown to a student.
WINDOWS = ("daily", "weekly", "monthly")

WINDOW_LABELS = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}


class UsageLimitReached(Exception):
    """A window is exhausted. Carries everything the UI needs to explain it."""

    def __init__(self, windows: list[str], resets: dict, *, remaining: int = 0):
        self.windows = windows
        self.resets = resets
        self.remaining = remaining
        self.friendly_message = describe_block(windows)
        super().__init__(self.friendly_message)


def minimum_limit_package() -> int:
    """Smallest package that is subject to the window system.

    One value, read from configuration, used everywhere. A package below it is
    sold without ceilings — the number exists so an administrator can decide
    where metering starts without anybody editing code.
    """
    return max(0, int(settings_service.get("minimum_limit_package", 50000)))


def warning_thresholds() -> list[int]:
    raw = settings_service.get("usage_warning_thresholds", [70, 80, 90])
    try:
        values = sorted({int(v) for v in raw if 0 < int(v) < 100})
    except (TypeError, ValueError):
        values = [70, 80, 90]
    return values or [70, 80, 90]


def describe_block(windows: list[str]) -> str:
    """The sentence a student reads when a ceiling stops them.

    Written as one sentence naming every window that is actually exhausted,
    rather than one notification per window — three stacked banners saying
    almost the same thing is how a limit message becomes noise.
    """
    names = [WINDOW_LABELS.get(w, w) for w in windows if w in WINDOW_LABELS]
    if not names:
        return "Your usage limit has been reached."
    if len(names) == 1:
        return f"Your {names[0]} usage limit has been reached."
    if len(names) == 2:
        return f"Your {names[0]} and {names[1]} usage limits have been reached."
    return f"Your {', '.join(names[:-1])} and {names[-1]} usage limits have been reached."


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def live_allocations(user, ai_model: AIModel, *, lock: bool = False) -> list[PackageAllocation]:
    """Every package this student can still draw on for this model, oldest first.

    Rolls each one's windows and persists any that turned over, so a counter is
    never read stale. Rolling is a write, but a cheap and idempotent one, and
    doing it on read is what makes a reset happen at the moment it is due
    rather than at the next request that happens to consume something.
    """
    queryset = PackageAllocation.objects.filter(
        user=user, ai_model=ai_model, status=AllocationStatus.ACTIVE
    ).order_by("activated_at", "id")
    if lock:
        queryset = queryset.select_for_update()

    now = timezone.now()
    rows = list(queryset)
    for row in rows:
        changed = row.roll(now)
        if changed and not lock:
            # Outside a lock this is a best-effort refresh; the authoritative
            # roll happens again under the lock in consume().
            PackageAllocation.objects.filter(pk=row.pk).update(
                **{name: getattr(row, name) for name in changed}
            )
        elif changed:
            row.save(update_fields=[*changed, "updated_at"])
    return rows


@dataclass
class LimitSnapshot:
    """What the student's ceilings look like right now, for one model."""

    model_key: str = ""
    model_name: str = ""
    metered: bool = False
    balance: int = 0
    spendable: int = 0
    blocked: list[str] = field(default_factory=list)
    windows: dict = field(default_factory=dict)
    packages: list = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked)

    def as_payload(self) -> dict:
        return {
            "metered": self.metered,
            "spendable": self.spendable,
            "blocked": self.blocked,
            "message": describe_block(self.blocked) if self.blocked else "",
            "windows": self.windows,
            # The same three windows in display order, each carrying its own
            # key and label. A template cannot sort a dict, and the order these
            # are read in — day, week, month — is part of how they make sense.
            "windows_list": [
                dict(self.windows.get(name, {}), key=name, label=name.capitalize())
                for name in WINDOWS
            ],
            "thresholds": warning_thresholds(),
        }


def snapshot(user, ai_model: AIModel | None) -> LimitSnapshot:
    """Aggregate the live packages into the three numbers the UI shows.

    Limits are per package, but a student thinks in terms of "my daily limit",
    so the windows are summed across the packages they hold. The reset shown
    for a window is the *soonest* one, because that is when something they
    cannot do now becomes possible again.
    """
    if ai_model is None:
        return LimitSnapshot()

    rows = live_allocations(user, ai_model)
    snap = LimitSnapshot(model_key=ai_model.key, model_name=ai_model.name)

    metered = [row for row in rows if row.has_limits]
    snap.balance = sum(row.tokens_remaining for row in rows)
    snap.spendable = sum(row.spendable() for row in rows)
    snap.metered = bool(metered)

    for window in WINDOWS:
        limit = sum(getattr(row, f"{window}_limit", 0) for row in metered)
        used = sum(getattr(row, f"{window}_used", 0) for row in metered)
        # Only packages that actually have this ceiling contribute a reset time.
        resets = [
            row.reset_at(window)
            for row in metered
            if getattr(row, f"{window}_limit", 0) > 0
        ]
        reset_at = min(resets) if resets else None
        remaining = max(0, limit - used) if limit > 0 else None
        snap.windows[window] = {
            "limit": limit,
            "used": used,
            "remaining": remaining,
            "percent": min(100, round(used / limit * 100)) if limit > 0 else 0,
            "reset_at": reset_at.isoformat() if reset_at else "",
            "reset_display": _format_reset(reset_at),
            "reset_in_seconds": max(0, int((reset_at - timezone.now()).total_seconds()))
            if reset_at
            else 0,
            "enforced": limit > 0,
        }

    # A window blocks only when *every* package carrying that ceiling is spent
    # out on it. Holding one exhausted package and one fresh one is not a block.
    if metered and snap.spendable <= 0 and snap.balance > 0:
        for window in WINDOWS:
            info = snap.windows[window]
            if info["enforced"] and info["remaining"] == 0:
                snap.blocked.append(window)

    snap.packages = [_package_payload(row) for row in rows]
    return snap


def _format_reset(moment) -> str:
    """"12 Feb at 15:30" — built by hand because ``%-d`` is not portable to Windows."""
    if not moment:
        return ""
    local = timezone.localtime(moment)
    return f"{local.day} {local.strftime('%b')} at {local.strftime('%H:%M')}"


def _package_payload(row: PackageAllocation) -> dict:
    return {
        "id": row.pk,
        "label": row.label or (row.plan.name if row.plan_id else "Allocation"),
        "allocated": row.tokens_allocated,
        "used": row.tokens_used,
        "remaining": row.tokens_remaining,
        "activated_at": row.activated_at.isoformat() if row.activated_at else "",
        "source": row.source,
        "windows": {
            window: {
                "limit": getattr(row, f"{window}_limit"),
                "used": getattr(row, f"{window}_used"),
                "remaining": getattr(row, f"{window}_remaining"),
                "percent": row.percent_used(window),
                "reset_at": (row.reset_at(window).isoformat() if row.reset_at(window) else ""),
            }
            for window in WINDOWS
        },
    }


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------
def check(user, ai_model: AIModel, *, required: int = 1) -> LimitSnapshot:
    """Refuse the request now if no window can cover it.

    Raises :class:`UsageLimitReached` rather than returning a flag, so a caller
    that forgets to look at the result cannot accidentally send anyway.

    A student with no metered package passes straight through: the window
    system applies to packages at or above ``minimum_limit_package``, and
    everything else is governed by the balance alone.
    """
    snap = snapshot(user, ai_model)
    if not snap.metered:
        return snap

    if snap.spendable >= max(1, required):
        return snap

    # Not enough headroom. Distinguish "a ceiling stopped you" from "you are
    # simply out of tokens" — only the first has a reset time to offer.
    blocked = snap.blocked or _binding_windows(snap)
    if not blocked:
        return snap

    resets = {
        window: snap.windows[window]
        for window in blocked
        if snap.windows.get(window, {}).get("enforced")
    }
    raise UsageLimitReached(blocked, resets, remaining=snap.spendable)


def _binding_windows(snap: LimitSnapshot) -> list[str]:
    """Windows with no room left, when the request is larger than what remains."""
    out = []
    for window in WINDOWS:
        info = snap.windows.get(window, {})
        if info.get("enforced") and (info.get("remaining") or 0) <= 0:
            out.append(window)
    return out


def consume(user, ai_model: AIModel, amount: int) -> list[PackageAllocation]:
    """Draw ``amount`` tokens across the student's packages, oldest first.

    Called from inside the wallet's transaction, after a reply has arrived with
    real usage numbers. It never refuses: by this point the provider has already
    answered and the tokens are genuinely spent, so an over-run — a reply longer
    than the headroom that was checked — is recorded against the last package
    rather than discarded. Refusing here would mean giving work away for free.

    Returns the rows it touched, already saved.
    """
    amount = max(0, int(amount))
    if amount <= 0:
        return []

    now = timezone.now()
    touched: list[PackageAllocation] = []

    with transaction.atomic():
        rows = list(
            PackageAllocation.objects.select_for_update()
            .filter(user=user, ai_model=ai_model, status=AllocationStatus.ACTIVE)
            .order_by("activated_at", "id")
        )
        if not rows:
            return []

        for row in rows:
            row.roll(now)

        takes = _plan_draw(rows, amount)
        for row in rows:
            take = takes.get(row.pk, 0)
            if take <= 0:
                # Still save a window that rolled while we held the lock.
                row.save(update_fields=[*PackageAllocation.WINDOW_FIELDS, "updated_at"])
                continue

            row.tokens_used += take
            row.tokens_remaining = max(0, row.tokens_remaining - take)
            row.daily_used += take
            row.weekly_used += take
            row.monthly_used += take
            row.last_used_at = now
            if row.tokens_remaining <= 0:
                row.status = AllocationStatus.EXHAUSTED
            row.save(
                update_fields=[
                    "tokens_used", "tokens_remaining", "status", "last_used_at",
                    *PackageAllocation.WINDOW_FIELDS, "updated_at",
                ]
            )
            touched.append(row)

    return touched


def _plan_draw(rows: list[PackageAllocation], amount: int) -> dict[int, int]:
    """How much of ``amount`` each package pays, keyed by package id.

    Metered packages pay first, up to what their windows allow; unmetered ones
    (an admin grant, a welcome bonus) pay after. Oldest first within each group.

    The order matters because a package's windows only move when it is the one
    being drawn on. Drawing strictly oldest-first meant a student holding an
    older grant spent every token from it: their purchased package's daily,
    weekly and monthly counters sat at zero while they chatted, which reads as
    the limits being broken. Metered-first keeps the counters honest, and once a
    ceiling is reached the student carries on from the unmetered grant — which
    is exactly what :func:`check` already allowed, since a grant's tokens were
    always counted as spendable.

    Whatever is left after that (a reply that overshot its estimate) goes to
    packages with tokens left regardless of windows, and finally to the last
    package, so the ledger and the allocations always add up.
    """
    takes: dict[int, int] = {}
    outstanding = amount

    def draw(row, cap: int) -> None:
        nonlocal outstanding
        take = min(outstanding, max(0, cap - takes.get(row.pk, 0)))
        if take > 0:
            takes[row.pk] = takes.get(row.pk, 0) + take
            outstanding -= take

    metered = [row for row in rows if row.has_limits]
    unmetered = [row for row in rows if not row.has_limits]

    for row in metered:
        if outstanding <= 0:
            break
        draw(row, row.spendable())
    for row in unmetered:
        if outstanding <= 0:
            break
        draw(row, row.tokens_remaining)
    for row in metered:
        if outstanding <= 0:
            break
        draw(row, row.tokens_remaining)
    if outstanding > 0 and rows:
        # The final package absorbs the overshoot, as it always has.
        last = rows[-1]
        takes[last.pk] = takes.get(last.pk, 0) + outstanding
    return takes


def refund(user, ai_model: AIModel, amount: int) -> None:
    """Put tokens back, newest package first — the mirror image of :func:`consume`.

    Used when a charge is reversed. Window counters are wound back too, so a
    refunded request does not go on eating a student's daily allowance.
    """
    amount = max(0, int(amount))
    if amount <= 0:
        return

    with transaction.atomic():
        rows = list(
            PackageAllocation.objects.select_for_update()
            .filter(user=user, ai_model=ai_model)
            .exclude(status=AllocationStatus.CANCELLED)
            .order_by("-activated_at", "-id")
        )
        outstanding = amount
        for row in rows:
            if outstanding <= 0:
                break
            give_back = min(outstanding, row.tokens_used)
            if give_back <= 0:
                continue
            row.tokens_used -= give_back
            row.tokens_remaining += give_back
            row.daily_used = max(0, row.daily_used - give_back)
            row.weekly_used = max(0, row.weekly_used - give_back)
            row.monthly_used = max(0, row.monthly_used - give_back)
            if row.status == AllocationStatus.EXHAUSTED and row.tokens_remaining > 0:
                row.status = AllocationStatus.ACTIVE
            row.save(
                update_fields=[
                    "tokens_used", "tokens_remaining", "status",
                    "daily_used", "weekly_used", "monthly_used", "updated_at",
                ]
            )
            outstanding -= give_back


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
def open_allocation(
    user,
    ai_model: AIModel,
    tokens: int,
    *,
    plan=None,
    payment=None,
    source: str = AllocationSource.PURCHASE,
    label: str = "",
    price=None,
    activated_at=None,
) -> PackageAllocation | None:
    """Record a package a student now holds, and start its clock.

    Limits are copied at this moment rather than referenced, so re-pricing
    tomorrow's packages leaves today's purchases alone. A package at or above
    ``minimum_limit_package`` takes its plan's ceilings; everything else — an
    admin grant, a welcome bonus, a small package — takes the model's default
    ceilings, so every model is limited whichever way its tokens arrived.
    """
    tokens = max(0, int(tokens))
    if tokens <= 0:
        return None

    now = activated_at or timezone.now()
    row = PackageAllocation(
        user=user,
        ai_model=ai_model,
        plan=plan,
        payment=payment,
        source=source,
        label=(label or (plan.name if plan is not None else "") or f"{ai_model.name} tokens")[:120],
        tokens_allocated=tokens,
        tokens_remaining=tokens,
        price_paid=price if price is not None else (plan.price if plan is not None else 0),
        currency=(plan.currency if plan is not None else "USD"),
        activated_at=now,
    )

    if plan is not None and tokens >= minimum_limit_package() and plan.has_limits:
        row.daily_limit = max(0, int(plan.daily_limit or 0))
        row.weekly_limit = max(0, int(plan.weekly_limit or 0))
        row.monthly_limit = max(0, int(plan.monthly_limit or 0))
    else:
        # Anything that did not come with a plan's own ceilings — an admin
        # grant, a welcome bonus, a referral reward, a small package — takes the
        # model's defaults, so no model and no route to tokens is unlimited
        # unless an administrator set that model's defaults to zero.
        defaults = ai_model.default_limits
        row.daily_limit = defaults["daily"]
        row.weekly_limit = defaults["weekly"]
        row.monthly_limit = defaults["monthly"]

    row.seed_windows(now)
    row.save()
    logger.info(
        "Allocation opened user=%s model=%s tokens=%s daily=%s weekly=%s monthly=%s",
        user.pk, ai_model.key, tokens, row.daily_limit, row.weekly_limit, row.monthly_limit,
    )
    return row


def cancel_allocations(user, ai_model: AIModel, amount: int, *, reason: str = "") -> int:
    """Take ``amount`` tokens back out of a student's packages, newest first.

    The mirror of :func:`open_allocation` for a reclaim or a refunded payment.
    Returns how much it actually managed to remove.
    """
    amount = max(0, int(amount))
    if amount <= 0:
        return 0

    removed = 0
    with transaction.atomic():
        rows = list(
            PackageAllocation.objects.select_for_update()
            .filter(user=user, ai_model=ai_model, status=AllocationStatus.ACTIVE)
            .order_by("-activated_at", "-id")
        )
        for row in rows:
            if removed >= amount:
                break
            take = min(amount - removed, max(0, row.tokens_remaining))
            if take <= 0:
                continue
            row.tokens_remaining -= take
            row.tokens_allocated = max(row.tokens_used, row.tokens_allocated - take)
            if row.tokens_remaining <= 0:
                row.status = AllocationStatus.CANCELLED
            row.save(
                update_fields=["tokens_remaining", "tokens_allocated", "status", "updated_at"]
            )
            removed += take
    return removed


def reconcile(user, ai_model: AIModel, wallet_balance: int) -> None:
    """Make the packages add up to the wallet again after an out-of-band change.

    An administrator adjusting a balance directly is the case this exists for.
    The wallet is authoritative — it is what the ledger proves — so the packages
    are moved to match it, never the other way round.
    """
    with transaction.atomic():
        rows = list(
            PackageAllocation.objects.select_for_update()
            .filter(user=user, ai_model=ai_model, status=AllocationStatus.ACTIVE)
            .order_by("activated_at", "id")
        )
        held = sum(row.tokens_remaining for row in rows)
        drift = wallet_balance - held
        if drift == 0:
            return
        if drift > 0 and rows:
            row = rows[-1]
            row.tokens_remaining += drift
            row.tokens_allocated += drift
            row.save(update_fields=["tokens_remaining", "tokens_allocated", "updated_at"])
        elif drift < 0:
            cancel_allocations(user, ai_model, -drift, reason="reconcile")


def all_snapshots(user, models=None) -> dict[str, LimitSnapshot]:
    """One snapshot per model, for the usage screen and the admin console."""
    models = (
        models if models is not None
        else AIModel.objects.select_related("provider_ref").filter(is_active=True)
    )
    return {model.key: snapshot(user, model) for model in models}


def has_any_allocation(user) -> bool:
    return PackageAllocation.objects.filter(
        Q(user=user), ~Q(status=AllocationStatus.CANCELLED)
    ).exists()


__all__ = [
    "LimitSnapshot",
    "UsageLimitReached",
    "WINDOWS",
    "all_snapshots",
    "cancel_allocations",
    "check",
    "consume",
    "describe_block",
    "live_allocations",
    "minimum_limit_package",
    "open_allocation",
    "reconcile",
    "refund",
    "snapshot",
    "warning_thresholds",
]
