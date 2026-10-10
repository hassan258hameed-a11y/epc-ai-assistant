"""The administrator's per-model token supply.

Every token an admin hands to a student comes out of *that model's* pool, and
every token taken back goes into it.  Keeping one pool per model is what makes
the distribution page answerable — "how much Sonnet is left to give out?" — and
what stops an empty Sonnet supply from blocking a Haiku allocation.

Each mutation locks the model's pool row inside a transaction, so two admins
distributing at the same moment cannot both spend the same balance.  Callers are
expected to wrap the pool movement and the matching wallet credit in one
``transaction.atomic()`` block, so neither can land without the other.

This is **application** accounting, not vendor accounting.  It has no connection
to the dollar balance in the OpenAI or Anthropic account; topping one up does
not top up the other.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Sum

from AI.models import AIModel, ModelPoolAction, ModelPoolTransaction, ModelTokenPool, User

logger = logging.getLogger("AI.pool.model")


class InsufficientPool(Exception):
    """One model's pool cannot cover a distribution."""

    def __init__(self, ai_model: AIModel, balance: int, required: int):
        self.ai_model = ai_model
        self.balance = balance
        self.required = required
        self.shortfall = max(0, required - balance)
        super().__init__(f"Pool[{ai_model.key}] has {balance}, needs {required}")

    @property
    def friendly_message(self) -> str:
        return (
            f"Your {self.ai_model.name} pool holds {self.balance:,} tokens, so it cannot "
            f"cover {self.required:,}. Add at least {self.shortfall:,} more to that "
            f"model's pool first."
        )


def get_pool(ai_model: AIModel) -> ModelTokenPool:
    """The pool row for one model, created empty on first use."""
    pool, _ = ModelTokenPool.objects.get_or_create(ai_model=ai_model)
    return pool


def balance(ai_model: AIModel) -> int:
    pool = ModelTokenPool.objects.filter(ai_model=ai_model).first()
    return pool.balance if pool else 0


def ensure_pools(models=None) -> None:
    """Guarantee a pool row for every catalogue model. Idempotent."""
    models = list(
        models if models is not None
        else AIModel.objects.select_related("provider_ref").all()
    )
    existing = set(
        ModelTokenPool.objects.filter(ai_model__in=models).values_list("ai_model_id", flat=True)
    )
    missing = [ModelTokenPool(ai_model=model) for model in models if model.pk not in existing]
    if missing:
        ModelTokenPool.objects.bulk_create(missing, ignore_conflicts=True)


def _move(
    ai_model: AIModel,
    amount: int,
    action: str,
    *,
    description: str = "",
    student: User | None = None,
    created_by: User | None = None,
    allow_overdraw: bool = False,
) -> ModelPoolTransaction | None:
    """Apply one signed movement to the locked pool row for ``ai_model``."""
    amount = int(amount)
    if amount == 0:
        return None

    with transaction.atomic():
        ModelTokenPool.objects.get_or_create(ai_model=ai_model)
        pool = ModelTokenPool.objects.select_for_update().get(ai_model=ai_model)

        if amount < 0 and pool.balance < -amount and not allow_overdraw:
            raise InsufficientPool(ai_model, pool.balance, -amount)

        pool.balance += amount
        if amount > 0:
            if action == ModelPoolAction.RECLAIM:
                pool.total_reclaimed += amount
            else:
                pool.total_added += amount
        elif action == ModelPoolAction.DISTRIBUTE:
            pool.total_distributed += -amount

        pool.save(
            update_fields=["balance", "total_added", "total_distributed", "total_reclaimed", "updated_at"]
        )

        return ModelPoolTransaction.objects.create(
            ai_model=ai_model,
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
def top_up(ai_model: AIModel, amount: int, *, description: str = "", created_by: User | None = None):
    """Add tokens to one model's supply — what the organisation bought for it."""
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Top-up amount must be positive.")
    return _move(
        ai_model, amount, ModelPoolAction.TOPUP,
        description=description or f"Added {amount:,} {ai_model.name} tokens to the pool",
        created_by=created_by,
    )


def draw(ai_model: AIModel, amount: int, *, student: User, created_by: User | None = None, description: str = ""):
    """Take tokens out of one model's pool to give to a student.

    Raises :class:`InsufficientPool` rather than overdrawing.
    """
    amount = int(amount)
    if amount <= 0:
        return None
    return _move(
        ai_model, -amount, ModelPoolAction.DISTRIBUTE,
        description=description or f"Assigned {amount:,} {ai_model.name} tokens to {student.display_name}",
        student=student, created_by=created_by,
    )


def give_back(ai_model: AIModel, amount: int, *, student: User, created_by: User | None = None, description: str = ""):
    """Return tokens taken back from a student to that model's pool."""
    amount = int(amount)
    if amount <= 0:
        return None
    return _move(
        ai_model, amount, ModelPoolAction.RECLAIM,
        description=description or f"Reclaimed {amount:,} {ai_model.name} tokens from {student.display_name}",
        student=student, created_by=created_by,
    )


def adjust(ai_model: AIModel, amount: int, *, description: str = "", created_by: User | None = None):
    """Signed correction. Clamped at zero so a pool can never go negative."""
    amount = int(amount)
    if amount < 0:
        amount = -min(-amount, balance(ai_model))
    return _move(
        ai_model, amount, ModelPoolAction.ADJUSTMENT,
        description=description or f"Manual adjustment to the {ai_model.name} pool",
        created_by=created_by, allow_overdraw=True,
    )


def can_cover(ai_model: AIModel, amount: int) -> bool:
    return balance(ai_model) >= int(amount)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def summary(ai_model: AIModel) -> dict:
    pool = get_pool(ai_model)
    return {
        "model": ai_model,
        "balance": pool.balance,
        "total_added": pool.total_added,
        "total_distributed": pool.total_distributed,
        "total_reclaimed": pool.total_reclaimed,
        "outstanding": pool.outstanding,
        "usage_percent": pool.usage_percent,
        "is_empty": pool.is_empty,
        "never_funded": pool.total_added <= 0,
    }


def summaries(models=None) -> list[dict]:
    """One summary per model, in catalogue order. The admin dashboard's backbone."""
    models = list(
        models if models is not None
        else AIModel.objects.select_related("provider_ref").order_by("sort_order", "name")
    )
    ensure_pools(models)
    pools = {pool.ai_model_id: pool for pool in ModelTokenPool.objects.filter(ai_model__in=models)}

    rows = []
    for model in models:
        pool = pools.get(model.pk)
        rows.append(
            {
                "model": model,
                "balance": pool.balance if pool else 0,
                "total_added": pool.total_added if pool else 0,
                "total_distributed": pool.total_distributed if pool else 0,
                "total_reclaimed": pool.total_reclaimed if pool else 0,
                "outstanding": pool.outstanding if pool else 0,
                "usage_percent": pool.usage_percent if pool else 0,
                "is_empty": (pool.balance if pool else 0) <= 0,
                "never_funded": (pool.total_added if pool else 0) <= 0,
            }
        )
    return rows


def grand_total() -> dict:
    """Everything across every pool. Shown only as a footnote, never as *the* number."""
    figures = ModelTokenPool.objects.aggregate(
        balance=Sum("balance"),
        total_added=Sum("total_added"),
        total_distributed=Sum("total_distributed"),
        total_reclaimed=Sum("total_reclaimed"),
    )
    return {key: int(value or 0) for key, value in figures.items()}


def recent(ai_model: AIModel | None = None, limit: int = 12):
    queryset = ModelPoolTransaction.objects.select_related("ai_model", "student", "created_by")
    if ai_model is not None:
        queryset = queryset.filter(ai_model=ai_model)
    return list(queryset.order_by("-created_at", "-id")[:limit])
