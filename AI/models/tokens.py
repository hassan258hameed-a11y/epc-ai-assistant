"""Immutable token ledger.

Every movement of tokens — grants, purchases, bonuses, AI consumption — is a row
here.  Balances are never overwritten; they are the running total of this ledger
kept on :class:`AI.models.users.TokenAccount`.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class TransactionType(models.TextChoices):
    ADMIN_GRANT = "admin_grant", "Admin grant"
    ADMIN_DEDUCT = "admin_deduct", "Admin deduction"
    SUBSCRIPTION = "subscription", "Plan purchase"
    FREE_TRIAL = "free_trial", "Free trial"
    REFERRAL_BONUS = "referral_bonus", "Referral bonus"
    USAGE = "usage", "AI usage"
    REFUND = "refund", "Refund"
    ADJUSTMENT = "adjustment", "Adjustment"
    EXPIRY = "expiry", "Expired"


CREDIT_TYPES = {
    TransactionType.ADMIN_GRANT,
    TransactionType.SUBSCRIPTION,
    TransactionType.FREE_TRIAL,
    TransactionType.REFERRAL_BONUS,
    TransactionType.REFUND,
}


class TokenTransaction(models.Model):
    """One signed movement of tokens.

    ``amount`` is positive for credits and negative for debits.  ``balance_after``
    snapshots the resulting balance so history is auditable without replaying
    the whole ledger.  ``idempotency_key`` makes credits safe to retry: payments,
    referral bonuses and free trials each build a deterministic key.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="token_transactions"
    )
    amount = models.BigIntegerField(help_text="Positive = credit, negative = debit.")
    transaction_type = models.CharField(max_length=24, choices=TransactionType.choices, db_index=True)
    source = models.CharField(max_length=64, blank=True, help_text="Free-form origin, e.g. 'gpt-4o-mini'.")
    description = models.CharField(max_length=255, blank=True)
    balance_after = models.BigIntegerField(default=0)

    idempotency_key = models.CharField(max_length=120, unique=True, null=True, blank=True)

    conversation = models.ForeignKey(
        "AI.Conversation", null=True, blank=True, on_delete=models.SET_NULL, related_name="token_transactions"
    )
    message = models.ForeignKey(
        "AI.Message", null=True, blank=True, on_delete=models.SET_NULL, related_name="token_transactions"
    )
    payment = models.ForeignKey(
        "AI.Payment", null=True, blank=True, on_delete=models.SET_NULL, related_name="token_transactions"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="token_transactions_created",
        help_text="Admin who performed the action, when applicable.",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["transaction_type", "-created_at"]),
        ]

    def __str__(self) -> str:
        sign = "+" if self.amount >= 0 else ""
        return f"{self.user_id} {sign}{self.amount} ({self.transaction_type})"

    @property
    def is_credit(self) -> bool:
        return self.amount >= 0

    @property
    def abs_amount(self) -> int:
        return abs(self.amount)

    @property
    def tone(self) -> str:
        if self.transaction_type == TransactionType.USAGE:
            return "neutral"
        return "success" if self.is_credit else "danger"


class TokenPool(models.Model):
    """The platform's own supply of tokens — the stock an admin distributes from.

    There is exactly one row. Every token an administrator hands to a student is
    drawn from this balance, and every token taken back returns to it, so the
    pool answers a question the per-student accounts cannot: *how much is left
    to give out?*

    The pool governs **administrative** distribution only. Tokens a student buys
    with a plan are created by that purchase and credited to them directly, so a
    paying customer is never blocked by an empty pool.
    """

    balance = models.BigIntegerField(default=0, help_text="Tokens available to distribute.")
    total_added = models.BigIntegerField(default=0, help_text="Ever topped up.")
    total_distributed = models.BigIntegerField(default=0, help_text="Ever given to students.")
    total_reclaimed = models.BigIntegerField(default=0, help_text="Ever taken back from students.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(balance__gte=0), name="token_pool_non_negative"),
        ]

    def __str__(self) -> str:
        return f"Token pool: {self.balance:,} available"

    @property
    def outstanding(self) -> int:
        """Tokens handed out and not returned — what students collectively hold from the pool."""
        return max(0, self.total_distributed - self.total_reclaimed)

    @property
    def usage_percent(self) -> int:
        supply = self.total_added
        if supply <= 0:
            return 0
        return min(100, round(self.outstanding / supply * 100))

    @property
    def is_empty(self) -> bool:
        return self.balance <= 0


class PoolAction(models.TextChoices):
    TOPUP = "topup", "Added to pool"
    DISTRIBUTE = "distribute", "Given to a student"
    RECLAIM = "reclaim", "Returned from a student"
    ADJUSTMENT = "adjustment", "Adjustment"


class PoolTransaction(models.Model):
    """One movement of the platform's supply. Signed: positive in, negative out."""

    amount = models.BigIntegerField(help_text="Positive = into the pool, negative = out of it.")
    action = models.CharField(max_length=16, choices=PoolAction.choices, db_index=True)
    description = models.CharField(max_length=255, blank=True)
    balance_after = models.BigIntegerField(default=0)

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True, on_delete=models.SET_NULL, related_name="pool_movements",
        help_text="The student on the other side of the movement, when there is one.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True, on_delete=models.SET_NULL, related_name="pool_movements_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["action", "-created_at"])]

    def __str__(self) -> str:
        sign = "+" if self.amount >= 0 else ""
        return f"pool {sign}{self.amount} ({self.action})"

    @property
    def is_credit(self) -> bool:
        return self.amount >= 0

    @property
    def abs_amount(self) -> int:
        return abs(self.amount)
