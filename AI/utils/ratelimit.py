"""Server-side sliding-window rate limiting.

Every limit is enforced in the backend; the UI only mirrors the result.  The
window is stored in the database so limits hold across processes and restarts
even when Redis is not configured.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from AI.models import RateLimitEvent


class RateLimited(Exception):
    """Raised when a caller exceeds a configured limit."""

    def __init__(self, scope: str, retry_after: int):
        self.scope = scope
        self.retry_after = max(1, int(retry_after))
        super().__init__(f"Rate limit exceeded for '{scope}'. Try again in {self.retry_after}s.")

    @property
    def friendly_message(self) -> str:
        seconds = self.retry_after
        if seconds >= 90:
            return f"Too many attempts. Please wait about {round(seconds / 60)} minutes and try again."
        return f"Too many attempts. Please wait {seconds} seconds and try again."


@dataclass(frozen=True)
class RateStatus:
    allowed: bool
    remaining: int
    retry_after: int


def _config(scope: str) -> tuple[int, int]:
    limit, window = settings.RATE_LIMITS.get(scope, (30, 60))
    return int(limit), int(window)


def check(scope: str, identity: str, *, limit: int | None = None, window: int | None = None) -> RateStatus:
    """Record a hit and report whether it is within the limit."""
    default_limit, default_window = _config(scope)
    limit = default_limit if limit is None else limit
    window = default_window if window is None else window

    identity = (identity or "anonymous")[:190]
    now = timezone.now()
    cutoff = now - timedelta(seconds=window)

    # Opportunistic cleanup keeps the table small without a scheduled job.
    RateLimitEvent.objects.filter(scope=scope, created_at__lt=now - timedelta(seconds=max(window * 4, 3600))).delete()

    recent = list(
        RateLimitEvent.objects.filter(scope=scope, identity=identity, created_at__gte=cutoff)
        .order_by("created_at")
        .values_list("created_at", flat=True)
    )

    if len(recent) >= limit:
        retry_after = int((recent[0] + timedelta(seconds=window) - now).total_seconds()) + 1
        return RateStatus(allowed=False, remaining=0, retry_after=max(1, retry_after))

    RateLimitEvent.objects.create(scope=scope, identity=identity)
    return RateStatus(allowed=True, remaining=limit - len(recent) - 1, retry_after=0)


def enforce(scope: str, identity: str, *, limit: int | None = None, window: int | None = None) -> None:
    """Same as :func:`check` but raises :class:`RateLimited` when exceeded."""
    status = check(scope, identity, limit=limit, window=window)
    if not status.allowed:
        raise RateLimited(scope, status.retry_after)


def reset(scope: str, identity: str) -> None:
    """Clear a window — used after a successful login so a user is not punished."""
    RateLimitEvent.objects.filter(scope=scope, identity=(identity or "")[:190]).delete()
