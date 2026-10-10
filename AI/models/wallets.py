"""Per-model token accounting: providers, admin pools, student wallets, ledger.

The platform bills students in tokens, and a token is only ever meaningful
*for one model*.  50,000 Claude Sonnet 5 tokens are not 50,000 GPT-5 Mini
tokens: they cost the organisation a different amount, they are bought from a
different vendor, and spending one must never touch the other.  Every model
therefore gets its own supply chain::

    AIProvider
        AIModel
            ModelTokenPool      what the admin has left to give out
            UserAIWallet        what one student holds
            AITokenTransaction  every movement, ever

Balances are never written outside ``AI.services.wallet_service`` and
``AI.services.model_pool_service``, both of which lock the row they mutate
inside a transaction.  That single choke point is what makes concurrent chat
requests safe and what lets the ledger be replayed to prove a balance.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class AIProvider(models.Model):
    """One vendor the platform can call.

    Rows here exist so an administrator can take a whole vendor offline — every
    Anthropic model at once, say — without touching models that belong to
    anyone else.  Adding a vendor later is a row plus a service class; nothing
    in the chat runtime changes.
    """

    slug = models.SlugField(
        max_length=32, unique=True,
        help_text="Routing key. Must match a provider registered in AI.services.providers.",
    )
    name = models.CharField(max_length=64)
    description = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(
        default=True, help_text="Uncheck to stop every model from this vendor immediately."
    )
    website = models.URLField(blank=True)

    sort_order = models.PositiveSmallIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name

    @property
    def status_badge(self) -> tuple[str, str]:
        return ("Active", "success") if self.is_active else ("Disabled", "neutral")


class ModelTokenPool(models.Model):
    """The administrator's own supply of tokens *for one model*.

    This is the number the distribution page means by "available to distribute".
    It is deliberately not shared between models: running out of Sonnet tokens
    must not stop an admin handing out Haiku tokens.

    Distinct from the provider's dollar balance at Anthropic or OpenAI — that is
    real money in a vendor account, and nothing in this table knows about it.
    """

    ai_model = models.OneToOneField("AI.AIModel", on_delete=models.CASCADE, related_name="pool")
    balance = models.BigIntegerField(default=0, help_text="Tokens available to distribute.")
    total_added = models.BigIntegerField(default=0, help_text="Ever topped up.")
    total_distributed = models.BigIntegerField(default=0, help_text="Ever given to students.")
    total_reclaimed = models.BigIntegerField(default=0, help_text="Ever taken back from students.")

    #: How the supply was bought, split the way the vendor prices it. Students
    #: still spend one undifferentiated token — an answer costs input *and*
    #: output and nobody can choose the ratio — but an operator buying capacity
    #: buys the two separately and needs to see both against what has been used.
    #: Zero on either side means "not tracked separately for this model".
    input_allocation = models.BigIntegerField(
        default=0, help_text="Input tokens this model's supply was bought with. 0 = not tracked."
    )
    output_allocation = models.BigIntegerField(
        default=0, help_text="Output tokens this model's supply was bought with. 0 = not tracked."
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(balance__gte=0), name="model_pool_non_negative"),
        ]

    def __str__(self) -> str:
        return f"{self.ai_model_id}: {self.balance:,} available"

    @property
    def outstanding(self) -> int:
        """Handed out and not returned — what students collectively hold."""
        return max(0, self.total_distributed - self.total_reclaimed)

    @property
    def usage_percent(self) -> int:
        if self.total_added <= 0:
            return 0
        return min(100, round(self.outstanding / self.total_added * 100))

    @property
    def is_empty(self) -> bool:
        return self.balance <= 0

    # -- the input/output split -------------------------------------------
    @property
    def tracks_direction(self) -> bool:
        return bool(self.input_allocation or self.output_allocation)

    def direction_rows(self) -> list[dict]:
        """Input and output, each against what has actually been spent.

        The consumed figures come from the model's own running totals, which
        ``model_service.record_usage`` keeps in step with the usage log — so
        this reports what the vendor was really billed for, not an estimate.
        """
        if not self.tracks_direction:
            return []
        model = self.ai_model
        rows = []
        for label, allocated, used in (
            ("Input", self.input_allocation, model.input_tokens_total),
            ("Output", self.output_allocation, model.output_tokens_total),
        ):
            remaining = max(0, allocated - used)
            rows.append(
                {
                    "label": label,
                    "allocated": allocated,
                    "used": used,
                    "remaining": remaining,
                    "percent": min(100, round(used / allocated * 100)) if allocated else 0,
                }
            )
        return rows


class ModelPoolAction(models.TextChoices):
    TOPUP = "topup", "Added to pool"
    DISTRIBUTE = "distribute", "Given to a student"
    RECLAIM = "reclaim", "Returned from a student"
    ADJUSTMENT = "adjustment", "Adjustment"


class ModelPoolTransaction(models.Model):
    """One signed movement of a single model's admin supply."""

    ai_model = models.ForeignKey("AI.AIModel", on_delete=models.CASCADE, related_name="pool_movements")
    amount = models.BigIntegerField(help_text="Positive = into the pool, negative = out of it.")
    action = models.CharField(max_length=16, choices=ModelPoolAction.choices, db_index=True)
    description = models.CharField(max_length=255, blank=True)
    balance_after = models.BigIntegerField(default=0)

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="model_pool_movements",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="model_pool_movements_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["ai_model", "-created_at"]),
            models.Index(fields=["action", "-created_at"]),
        ]

    def __str__(self) -> str:
        sign = "+" if self.amount >= 0 else ""
        return f"pool[{self.ai_model_id}] {sign}{self.amount} ({self.action})"

    @property
    def is_credit(self) -> bool:
        return self.amount >= 0

    @property
    def abs_amount(self) -> int:
        return abs(self.amount)


class UserAIWallet(models.Model):
    """What one student holds for one model.

    The invariant ``balance == allocated_tokens - used_tokens`` is maintained by
    :mod:`AI.services.wallet_service`, so "50,000 allocated, 2,450 used, 47,550
    left" always adds up on screen.

    A wallet row existing does not by itself grant access: ``is_enabled`` lets an
    administrator revoke one model for one student while leaving their balance
    intact, and the chat runtime requires both a positive balance *and* access.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_wallets"
    )
    ai_model = models.ForeignKey("AI.AIModel", on_delete=models.CASCADE, related_name="wallets")

    balance = models.BigIntegerField(default=0, help_text="Tokens left for this model.")
    allocated_tokens = models.BigIntegerField(default=0, help_text="Currently allocated, net of reclaims.")
    used_tokens = models.BigIntegerField(default=0, help_text="Spent on this model.")

    #: Lifetime figures, which survive a reclaim and so answer "how much has this
    #: student ever been given / ever spent" for reporting.
    lifetime_allocated = models.BigIntegerField(default=0)
    lifetime_used = models.BigIntegerField(default=0)

    is_enabled = models.BooleanField(
        default=True,
        help_text="Uncheck to block this student from this model without removing their tokens.",
    )

    first_allocated_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["ai_model__sort_order", "ai_model__name"]
        constraints = [
            models.UniqueConstraint(fields=["user", "ai_model"], name="one_wallet_per_user_model"),
            models.CheckConstraint(condition=models.Q(balance__gte=0), name="wallet_non_negative"),
        ]
        indexes = [
            models.Index(fields=["user", "ai_model"]),
            models.Index(fields=["ai_model", "-balance"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}/{self.ai_model_id}: {self.balance:,}"

    # -- display -----------------------------------------------------------
    @property
    def remaining_tokens(self) -> int:
        """Alias for :attr:`balance`, so templates can read the spec's name."""
        return self.balance

    @property
    def usage_percent(self) -> int:
        if self.allocated_tokens <= 0:
            return 0
        return min(100, round(self.used_tokens / self.allocated_tokens * 100))

    @property
    def remaining_percent(self) -> int:
        if self.allocated_tokens <= 0:
            return 0
        return max(0, min(100, round(self.balance / self.allocated_tokens * 100)))

    @property
    def has_tokens(self) -> bool:
        return self.balance > 0

    @property
    def is_usable(self) -> bool:
        """Access *and* money. Both are re-checked server-side on every request."""
        return self.is_enabled and self.balance > 0


class WalletTransactionType(models.TextChoices):
    """Why a wallet moved. Kept explicit so the audit history reads in English."""

    ADMIN_ALLOCATION = "admin_allocation", "Admin allocation"
    ADMIN_REVERSAL = "admin_reversal", "Admin reversal"
    CHAT_USAGE = "chat_usage", "Chat usage"
    REFUND = "refund", "Refund"
    MANUAL_ADJUSTMENT = "manual_adjustment", "Manual adjustment"
    EXPIRATION = "expiration", "Expired"
    PLAN_PURCHASE = "plan_purchase", "Plan purchase"
    FREE_TRIAL = "free_trial", "Free trial"
    REFERRAL_BONUS = "referral_bonus", "Referral bonus"


#: Movement types that normally add tokens to a wallet, for grouping history.
#: The sign of ``amount`` is always authoritative — ``REFUND`` is deliberately
#: absent because refunding a purchase *removes* the tokens it granted.
WALLET_CREDIT_TYPES = {
    WalletTransactionType.ADMIN_ALLOCATION,
    WalletTransactionType.PLAN_PURCHASE,
    WalletTransactionType.FREE_TRIAL,
    WalletTransactionType.REFERRAL_BONUS,
}


class AITokenTransaction(models.Model):
    """One signed movement of one student's tokens for one model.

    ``balance_before`` / ``balance_after`` snapshot the wallet either side of the
    movement, so a disputed balance can be explained without replaying the whole
    ledger — and a replay can prove those snapshots right.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_token_transactions"
    )
    ai_model = models.ForeignKey(
        "AI.AIModel", on_delete=models.CASCADE, related_name="token_transactions"
    )

    amount = models.BigIntegerField(help_text="Positive = credit, negative = debit.")
    transaction_type = models.CharField(
        max_length=24, choices=WalletTransactionType.choices, db_index=True
    )
    balance_before = models.BigIntegerField(default=0)
    balance_after = models.BigIntegerField(default=0)

    reason = models.CharField(max_length=255, blank=True)
    reference_id = models.CharField(
        max_length=120, unique=True, null=True, blank=True,
        help_text="Idempotency key. A retried credit with the same key is ignored.",
    )

    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="ai_token_transactions_performed",
        help_text="The administrator who did this, when a person did it.",
    )
    conversation = models.ForeignKey(
        "AI.Conversation", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="ai_token_transactions",
    )
    message = models.ForeignKey(
        "AI.Message", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="ai_token_transactions",
    )
    payment = models.ForeignKey(
        "AI.Payment", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="ai_token_transactions",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["ai_model", "-created_at"]),
            models.Index(fields=["user", "ai_model", "-created_at"]),
            models.Index(fields=["transaction_type", "-created_at"]),
        ]

    def __str__(self) -> str:
        sign = "+" if self.amount >= 0 else ""
        return f"{self.user_id} {sign}{self.amount} {self.ai_model_id} ({self.transaction_type})"

    # The global ledger and this one record the same kinds of event and are
    # rendered by the same templates, but were written with different field
    # names. Rather than teach every template to ask two questions, this one
    # answers to both names. The stored columns are unchanged.
    @property
    def description(self) -> str:
        """Alias for ``reason`` — what the global ledger calls this field."""
        return self.reason

    @property
    def created_by(self):
        """Alias for ``performed_by`` — likewise."""
        return self.performed_by

    @property
    def is_credit(self) -> bool:
        return self.amount >= 0

    @property
    def abs_amount(self) -> int:
        return abs(self.amount)

    @property
    def tone(self) -> str:
        if self.transaction_type == WalletTransactionType.CHAT_USAGE:
            return "neutral"
        return "success" if self.is_credit else "danger"
