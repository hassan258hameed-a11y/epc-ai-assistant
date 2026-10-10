"""The platform's token supply.

Tokens an administrator gives a student come out of a single shared pool, and
tokens taken back go into it. That makes the obvious question answerable — *how
much is left to give out?* — and stops a distracted admin handing out more than
the organisation actually bought.

Every mutation locks the pool row inside a transaction, so two admins assigning
tokens at the same moment cannot both spend the same balance. Callers are
expected to wrap the pool movement and the matching student credit in one
``transaction.atomic()`` block, so neither can land without the other.

The pool governs **administrative distribution only**. Tokens a student buys
with a plan are created by that purchase and credited directly, so a paying
customer is never blocked by an empty pool.
"""
from __future__ import annotations

import logging

from django.db import transaction

from AI.models import PoolAction, PoolTransaction, TokenPool, User

logger = logging.getLogger("AI.pool")


class InsufficientPoolTokens(Exception):
    """The pool cannot cover a distribution."""

    def __init__(self, balance: int, required: int):
        self.balance = balance
        self.required = required
        self.shortfall = max(0, required - balance)
        super().__init__(f"Pool has {balance}, needs {required}")

    @property
    def friendly_message(self) -> str:
        return (
            f"Your token pool holds {self.balance:,} tokens, so it cannot cover "
            f"{self.required:,}. Add at least {self.shortfall:,} more to the pool first."
        )


def get_pool() -> TokenPool:
    """The single pool row, created on first use."""
    pool, _ = TokenPool.objects.get_or_create(pk=1)
    return pool


def balance() -> int:
    pool = TokenPool.objects.filter(pk=1).first()
    return pool.balance if pool else 0


def _move(
    amount: int,
    action: str,
    *,
    description: str = "",
    student: User | None = None,
    created_by: User | None = None,
    allow_overdraw: bool = False,
) -> PoolTransaction | None:
    """Apply one signed movement to the locked pool row."""
    if amount == 0:
        return None

    with transaction.atomic():
        TokenPool.objects.get_or_create(pk=1)
        pool = TokenPool.objects.select_for_update().get(pk=1)

        if amount < 0 and pool.balance < -amount and not allow_overdraw:
            raise InsufficientPoolTokens(pool.balance, -amount)

        pool.balance += amount
        if amount > 0:
            if action == PoolAction.RECLAIM:
                pool.total_reclaimed += amount
            else:
                pool.total_added += amount
        else:
            if action == PoolAction.DISTRIBUTE:
                pool.total_distributed += -amount

        pool.save(update_fields=[
            "balance", "total_added", "total_distributed", "total_reclaimed", "updated_at",
        ])

        return PoolTransaction.objects.create(
            amount=amount,
            action=action,
            description=description[:255],
            balance_after=pool.balance,
            student=student,
            created_by=created_by,
        )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
def top_up(amount: int, *, description: str = "", created_by: User | None = None) -> PoolTransaction | None:
    """Add tokens to the supply — what the organisation bought from the provider."""
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Top-up amount must be positive.")
    return _move(
        amount, PoolAction.TOPUP,
        description=description or f"Added {amount:,} tokens to the pool",
        created_by=created_by,
    )


def draw(amount: int, *, student: User, created_by: User | None = None, description: str = "") -> PoolTransaction | None:
    """Take tokens out of the pool to give to a student.

    Raises :class:`InsufficientPoolTokens` rather than overdrawing.
    """
    amount = int(amount)
    if amount <= 0:
        return None
    return _move(
        -amount, PoolAction.DISTRIBUTE,
        description=description or f"Assigned {amount:,} tokens to {student.display_name}",
        student=student,
        created_by=created_by,
    )


def give_back(amount: int, *, student: User, created_by: User | None = None, description: str = "") -> PoolTransaction | None:
    """Return tokens taken back from a student to the pool."""
    amount = int(amount)
    if amount <= 0:
        return None
    return _move(
        amount, PoolAction.RECLAIM,
        description=description or f"Reclaimed {amount:,} tokens from {student.display_name}",
        student=student,
        created_by=created_by,
    )


def adjust(amount: int, *, description: str = "", created_by: User | None = None) -> PoolTransaction | None:
    """Signed correction. Clamped at zero so the pool can never go negative."""
    amount = int(amount)
    if amount < 0:
        amount = -min(-amount, balance())
    return _move(
        amount, PoolAction.ADJUSTMENT,
        description=description or "Manual adjustment",
        created_by=created_by,
        allow_overdraw=True,
    )


def can_cover(amount: int) -> bool:
    return balance() >= int(amount)


def summary() -> dict:
    pool = get_pool()
    return {
        "balance": pool.balance,
        "total_added": pool.total_added,
        "total_distributed": pool.total_distributed,
        "total_reclaimed": pool.total_reclaimed,
        "outstanding": pool.outstanding,
        "usage_percent": pool.usage_percent,
        "is_empty": pool.is_empty,
        "never_funded": pool.total_added <= 0,
    }


def recent(limit: int = 12):
    return list(
        PoolTransaction.objects.select_related("student", "created_by").order_by("-created_at")[:limit]
    )
