"""Package allocations: what one purchase bought, and how fast it may be spent.

Two different questions are answered by two different tables, on purpose:

* :class:`AI.models.wallets.UserAIWallet` answers *how many tokens does this
  student own for this model* — one number, the balance they paid for;
* :class:`PackageAllocation` answers *how much of that may be spent in the
  current day, week and month* — a rate, not a quantity.

Conflating them is the classic mistake in a metered product: a student who
bought 200,000 tokens has not bought the right to spend all 200,000 this
afternoon, and a student who has hit today's ceiling has not lost the tokens,
only the ability to spend them until the window turns over.

One row per purchase.  Limits are **snapshotted at activation** rather than read
live from the plan, so an administrator re-tuning next month's packages never
silently re-writes the terms of one somebody already paid for.

Windows are rolling and anchored to the activation instant, and they are
advanced lazily — :meth:`PackageAllocation.roll` is called by every read and
every write, so a window that expired while nobody was looking is still correct
the moment anyone asks.
"""
from __future__ import annotations

import calendar
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

#: A limit of zero means "no ceiling of this kind", not "may spend nothing".
#: Chosen so an unconfigured window is permissive: a missing setting must never
#: silently lock a paying student out of tokens they own.
UNLIMITED = 0


def add_months(moment, months: int):
    """``moment`` shifted by whole months, clamped to the last valid day.

    January 31 + 1 month is February 28 (or 29), not an invalid February 31.
    """
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def anchored_month_end(start, anchor_day: int):
    """End of the monthly cycle beginning at ``start`` for a package bought on
    ``anchor_day``.

    The anchor is carried separately from the window start so a package bought
    on the 31st bills 31 Jan → 28 Feb → 31 Mar, rather than collapsing to the
    28th for the rest of its life after one short month.
    """
    nxt = add_months(start, 1)
    day = min(max(1, anchor_day), calendar.monthrange(nxt.year, nxt.month)[1])
    return nxt.replace(day=day)


class AllocationStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    EXHAUSTED = "exhausted", "Fully spent"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Expired"


class AllocationSource(models.TextChoices):
    PURCHASE = "purchase", "Purchase"
    ADMIN = "admin", "Admin allocation"
    TRIAL = "trial", "Free trial"
    REFERRAL = "referral", "Referral bonus"
    ADJUSTMENT = "adjustment", "Adjustment"


class PackageAllocation(models.Model):
    """One package a student holds, with its own windows and its own counters."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="allocations"
    )
    ai_model = models.ForeignKey(
        "AI.AIModel", on_delete=models.CASCADE, related_name="allocations"
    )
    #: The package that was sold. Nullable so retiring a package from the
    #: catalogue never deletes the record of somebody having bought it.
    plan = models.ForeignKey(
        "AI.Plan", null=True, blank=True, on_delete=models.SET_NULL, related_name="allocations"
    )
    payment = models.ForeignKey(
        "AI.Payment", null=True, blank=True, on_delete=models.SET_NULL, related_name="allocations"
    )

    source = models.CharField(
        max_length=16, choices=AllocationSource.choices, default=AllocationSource.PURCHASE,
        db_index=True,
    )
    status = models.CharField(
        max_length=12, choices=AllocationStatus.choices, default=AllocationStatus.ACTIVE,
        db_index=True,
    )

    label = models.CharField(max_length=120, blank=True, help_text="What the student bought.")
    tokens_allocated = models.BigIntegerField(default=0)
    tokens_used = models.BigIntegerField(default=0)
    tokens_remaining = models.BigIntegerField(default=0)

    price_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    currency = models.CharField(max_length=8, default="USD")

    #: The instant every window is measured from. Not the row's creation time —
    #: a payment confirmed by an administrator days later activates when it is
    #: confirmed, and that is when the student's first day begins.
    activated_at = models.DateTimeField(default=timezone.now, db_index=True)
    #: Day of month the monthly cycle returns to. See :func:`anchored_month_end`.
    cycle_anchor_day = models.PositiveSmallIntegerField(default=1)

    # -- ceilings, snapshotted at activation -------------------------------
    daily_limit = models.BigIntegerField(default=UNLIMITED)
    weekly_limit = models.BigIntegerField(default=UNLIMITED)
    monthly_limit = models.BigIntegerField(default=UNLIMITED)

    # -- counters, reset when their window turns over ----------------------
    daily_used = models.BigIntegerField(default=0)
    weekly_used = models.BigIntegerField(default=0)
    monthly_used = models.BigIntegerField(default=0)

    daily_window_start = models.DateTimeField(default=timezone.now)
    daily_window_end = models.DateTimeField(default=timezone.now)
    weekly_window_start = models.DateTimeField(default=timezone.now)
    weekly_window_end = models.DateTimeField(default=timezone.now)
    monthly_window_start = models.DateTimeField(default=timezone.now)
    monthly_window_end = models.DateTimeField(default=timezone.now)

    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["activated_at", "id"]
        indexes = [
            models.Index(fields=["user", "ai_model", "status", "activated_at"]),
            models.Index(fields=["status", "-activated_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(tokens_remaining__gte=0), name="allocation_non_negative"
            ),
            models.CheckConstraint(
                condition=models.Q(daily_used__gte=0)
                & models.Q(weekly_used__gte=0)
                & models.Q(monthly_used__gte=0),
                name="allocation_usage_non_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}/{self.ai_model_id} {self.tokens_remaining:,} left"

    # -- windows -----------------------------------------------------------
    #: Every field :meth:`roll` may touch, so a caller can save exactly these.
    WINDOW_FIELDS = (
        "daily_used", "daily_window_start", "daily_window_end",
        "weekly_used", "weekly_window_start", "weekly_window_end",
        "monthly_used", "monthly_window_start", "monthly_window_end",
    )

    def seed_windows(self, now=None) -> None:
        """Line every window up with the activation instant."""
        now = now or self.activated_at or timezone.now()
        self.activated_at = now
        self.cycle_anchor_day = now.day
        self.daily_window_start = now
        self.daily_window_end = now + timedelta(days=1)
        self.weekly_window_start = now
        self.weekly_window_end = now + timedelta(days=7)
        self.monthly_window_start = now
        self.monthly_window_end = anchored_month_end(now, now.day)

    def roll(self, now=None) -> list[str]:
        """Advance any window whose end has passed, zeroing its counter.

        Returns the names of the fields that changed, so a caller inside a
        transaction can save exactly those. Windows advance by whole periods
        rather than restarting from *now*: a student who does not log in for a
        week should not get a fresh day at whatever hour they happen to return,
        because that would let anyone reset their own ceiling by waiting.
        """
        now = now or timezone.now()
        rolled: list[str] = []

        if now >= self.daily_window_end:
            # Jump straight to the window containing `now`. Looping one day at a
            # time would be O(days) for an account dormant for a year.
            periods = (now - self.daily_window_start) // timedelta(days=1)
            self.daily_window_start = self.daily_window_start + timedelta(days=periods)
            self.daily_window_end = self.daily_window_start + timedelta(days=1)
            self.daily_used = 0
            rolled += ["daily_used", "daily_window_start", "daily_window_end"]

        if now >= self.weekly_window_end:
            periods = (now - self.weekly_window_start) // timedelta(days=7)
            self.weekly_window_start = self.weekly_window_start + timedelta(weeks=periods)
            self.weekly_window_end = self.weekly_window_start + timedelta(days=7)
            self.weekly_used = 0
            rolled += ["weekly_used", "weekly_window_start", "weekly_window_end"]

        # Calendar months are uneven, so this one does have to step. The guard
        # caps a pathological clock skew at forty years of stepping.
        guard = 0
        while now >= self.monthly_window_end and guard < 480:
            self.monthly_window_start = self.monthly_window_end
            self.monthly_window_end = anchored_month_end(
                self.monthly_window_start, self.cycle_anchor_day
            )
            self.monthly_used = 0
            guard += 1
        if guard:
            rolled += ["monthly_used", "monthly_window_start", "monthly_window_end"]

        return rolled

    # -- headroom ----------------------------------------------------------
    @property
    def has_limits(self) -> bool:
        return bool(self.daily_limit or self.weekly_limit or self.monthly_limit)

    @staticmethod
    def _remaining(limit: int, used: int) -> int | None:
        """``None`` means there is no ceiling of this kind."""
        if limit <= UNLIMITED:
            return None
        return max(0, limit - used)

    @property
    def daily_remaining(self) -> int | None:
        return self._remaining(self.daily_limit, self.daily_used)

    @property
    def weekly_remaining(self) -> int | None:
        return self._remaining(self.weekly_limit, self.weekly_used)

    @property
    def monthly_remaining(self) -> int | None:
        return self._remaining(self.monthly_limit, self.monthly_used)

    def spendable(self) -> int:
        """The most that could be drawn from this package right now.

        The smallest of what is left in it and what each live window still
        allows. Callers must have rolled the windows first.
        """
        caps = [max(0, self.tokens_remaining)]
        for value in (self.daily_remaining, self.weekly_remaining, self.monthly_remaining):
            if value is not None:
                caps.append(value)
        return max(0, min(caps))

    def blocked_windows(self) -> list[str]:
        """Which ceilings are currently at zero — e.g. ``["daily", "weekly"]``."""
        out = []
        for name, value in (
            ("daily", self.daily_remaining),
            ("weekly", self.weekly_remaining),
            ("monthly", self.monthly_remaining),
        ):
            if value is not None and value <= 0:
                out.append(name)
        return out

    def percent_used(self, window: str) -> int:
        limit = getattr(self, f"{window}_limit", 0)
        used = getattr(self, f"{window}_used", 0)
        if limit <= UNLIMITED:
            return 0
        return min(100, round(used / limit * 100))

    def reset_at(self, window: str):
        return getattr(self, f"{window}_window_end", None)

    @property
    def is_live(self) -> bool:
        return self.status == AllocationStatus.ACTIVE and self.tokens_remaining > 0


class ImageRequest(models.Model):
    """One picture asked for, whether or not it arrived.

    Separate from :class:`AI.models.chat.AIUsageLog` because an image is priced
    per picture and per quality rather than per token, and because a failed
    drawing still has to be visible to an administrator diagnosing an outage.
    """

    class Status(models.TextChoices):
        OK = "ok", "Delivered"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="image_requests"
    )
    conversation = models.ForeignKey(
        "AI.Conversation", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="image_requests",
    )
    message = models.ForeignKey(
        "AI.Message", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="image_requests",
    )
    #: The wallet that paid. One vendor serves images whatever model the student
    #: is talking to, so this is the *currency*, not the drawing engine — that
    #: is :attr:`image_model`.
    ai_model = models.ForeignKey(
        "AI.AIModel", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="image_requests",
    )
    allocation = models.ForeignKey(
        PackageAllocation, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="image_requests",
    )

    prompt = models.TextField(blank=True)
    revised_prompt = models.TextField(blank=True)
    provider = models.CharField(max_length=32, blank=True)
    image_model = models.CharField(max_length=64, blank=True)
    size = models.CharField(max_length=16, blank=True)
    quality = models.CharField(max_length=16, blank=True)

    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OK, db_index=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.CharField(max_length=255, blank=True)

    tokens_charged = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    #: Where the picture ended up, so an audit can find the asset again.
    attachment = models.ForeignKey(
        "AI.MessageAttachment", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="image_requests",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} image {self.status}"

    @property
    def status_tone(self) -> str:
        return "success" if self.status == self.Status.OK else "danger"
