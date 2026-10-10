"""Per-model token accounting.

Every movement of a student's tokens happens here, inside a transaction that
locks the one :class:`~AI.models.wallets.UserAIWallet` row being changed.  That
single choke point is what guarantees:

* **no cross-model leakage** — a Sonnet charge can only ever touch the Sonnet
  wallet, because the wallet is selected by ``(user, ai_model)`` before anything
  is read;
* **no double spend** — two chat requests arriving together serialise on
  ``SELECT ... FOR UPDATE``, so the second one sees the first one's debit;
* **no negative balances** — checked in Python and again by a DB constraint;
* **no duplicate credits** — payments, referrals and trials each pass a
  deterministic ``reference_id``;
* **a replayable ledger** — every movement writes an
  :class:`~AI.models.wallets.AITokenTransaction` with the balance either side.

The legacy :class:`~AI.models.users.TokenAccount` is kept in step as the *sum*
of a student's wallets.  It is a mirror, never a source of truth: the dashboard,
the exports and the older admin screens read it, and this module is what keeps
them honest now that spending is per model.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from AI.models import (
    AIModel,
    AITokenTransaction,
    AllocationSource,
    TokenAccount,
    TokenTransaction,
    TransactionType,
    User,
    UserAIWallet,
    WalletTransactionType,
)
from AI.services import usage_limit_service

logger = logging.getLogger("AI.wallets")


class InsufficientModelTokens(Exception):
    """A debit would push one model's wallet below zero."""

    def __init__(self, ai_model: AIModel, balance: int, required: int):
        self.ai_model = ai_model
        self.balance = balance
        self.required = required
        self.shortfall = max(0, required - balance)
        super().__init__(f"Insufficient {ai_model.key}: balance={balance}, required={required}")

    @property
    def friendly_message(self) -> str:
        """Names the model, so the student knows which balance ran out."""
        return (
            f"You do not have enough {self.ai_model.name} tokens to continue. "
            "Please contact the administrator."
        )


class ModelAccessDenied(Exception):
    """The student is not allowed to use this model at all."""

    def __init__(self, ai_model: AIModel, reason: str = ""):
        self.ai_model = ai_model
        self.friendly_message = reason or (
            f"You do not have access to {ai_model.name}. Please contact the administrator."
        )
        super().__init__(self.friendly_message)


@dataclass
class WalletResult:
    """What a completed movement did. ``created`` is False for a no-op."""

    wallet: UserAIWallet | None
    transaction: AITokenTransaction | None
    balance_before: int
    balance_after: int
    amount: int
    created: bool


#: Per-model movement -> the legacy ledger's vocabulary, so the old history
#: screens keep reading correctly while the new ones read the real thing.
LEGACY_TYPES = {
    WalletTransactionType.ADMIN_ALLOCATION: TransactionType.ADMIN_GRANT,
    WalletTransactionType.ADMIN_REVERSAL: TransactionType.ADMIN_DEDUCT,
    WalletTransactionType.CHAT_USAGE: TransactionType.USAGE,
    WalletTransactionType.REFUND: TransactionType.REFUND,
    WalletTransactionType.MANUAL_ADJUSTMENT: TransactionType.ADJUSTMENT,
    WalletTransactionType.EXPIRATION: TransactionType.EXPIRY,
    WalletTransactionType.PLAN_PURCHASE: TransactionType.SUBSCRIPTION,
    WalletTransactionType.FREE_TRIAL: TransactionType.FREE_TRIAL,
    WalletTransactionType.REFERRAL_BONUS: TransactionType.REFERRAL_BONUS,
}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def get_wallet(user: User, ai_model: AIModel) -> UserAIWallet:
    """The student's wallet for one model, created empty on first sight.

    A new wallet starts at zero and stays there until an administrator funds it:
    students own no tokens by default.
    """
    wallet, _ = UserAIWallet.objects.get_or_create(user=user, ai_model=ai_model)
    return wallet


def balance_of(user: User, ai_model: AIModel) -> int:
    wallet = UserAIWallet.objects.filter(user=user, ai_model=ai_model).first()
    return wallet.balance if wallet else 0


def wallets_of(user: User, *, active_only: bool = False) -> list[UserAIWallet]:
    """Every wallet a student holds, newest catalogue order first."""
    queryset = UserAIWallet.objects.filter(user=user).select_related("ai_model", "ai_model__provider_ref")
    if active_only:
        queryset = queryset.filter(ai_model__is_active=True)
    return list(queryset)


def wallet_map(user: User) -> dict[int, UserAIWallet]:
    """``{ai_model_id: wallet}`` — one query, for pages that render every model."""
    return {wallet.ai_model_id: wallet for wallet in wallets_of(user)}


def ensure_wallets(user: User, models=None) -> list[UserAIWallet]:
    """Make sure a row exists for every catalogue model, so the UI can show zeros."""
    models = list(
        models
        if models is not None
        # Joined because a wallet row reports its model's vendor, and asking a
        # model for its provider is a query unless it was fetched with one.
        else AIModel.objects.select_related("provider_ref").all()
    )
    existing = {wallet.ai_model_id: wallet for wallet in wallets_of(user)}
    missing = [
        UserAIWallet(user=user, ai_model=model) for model in models if model.pk not in existing
    ]
    if missing:
        UserAIWallet.objects.bulk_create(missing, ignore_conflicts=True)
        existing = {wallet.ai_model_id: wallet for wallet in wallets_of(user)}
    return [existing[model.pk] for model in models if model.pk in existing]


def overview(user: User, models=None) -> list[dict]:
    """One row per catalogue model: what this student has, used and may use.

    Built for both the chat model picker and the admin student panel, so the two
    can never disagree about whether a model is usable.
    """
    models = list(
        models
        if models is not None
        else AIModel.objects.select_related("provider_ref").order_by("sort_order", "name")
    )
    held = wallet_map(user)

    rows = []
    for model in models:
        wallet = held.get(model.pk)
        balance = wallet.balance if wallet else 0
        enabled = wallet.is_enabled if wallet else True
        reason = ""
        if not model.provider_is_active:
            reason = f"{model.provider_name} is temporarily unavailable."
        elif not model.is_active:
            reason = f"{model.name} has been switched off by your administrator."
        elif model.is_exhausted:
            reason = f"{model.name} has used its whole platform allocation."
        elif not enabled:
            reason = f"You do not have access to {model.name}."
        elif balance <= 0:
            reason = f"You have no {model.name} tokens. Please contact the administrator."

        rows.append(
            {
                "model": model,
                "wallet": wallet,
                "balance": balance,
                "allocated": wallet.allocated_tokens if wallet else 0,
                "used": wallet.used_tokens if wallet else 0,
                "percent_used": wallet.usage_percent if wallet else 0,
                "percent_left": wallet.remaining_percent if wallet else 0,
                "is_enabled": enabled,
                "usable": not reason,
                "reason": reason,
            }
        )
    return rows


def usable_models(user: User) -> list[AIModel]:
    """Models this student may actually send a message to, right now."""
    return [row["model"] for row in overview(user) if row["usable"]]


def held_by_students(ai_model: AIModel) -> int:
    """What students collectively hold for one model, right now.

    Read from the wallets rather than from the pool's ``total_distributed``,
    because the two answer different questions. The pool counter records what
    has flowed *out of the pool*; balances that arrived another way — carried
    forward by a migration, bought with a plan, granted as a trial — are real
    tokens students can spend but never passed through it.
    """
    return int(
        UserAIWallet.objects.filter(ai_model=ai_model).aggregate(total=Sum("balance"))["total"] or 0
    )


def totals_of(user: User) -> dict:
    """Aggregate across every wallet — the number the legacy screens show."""
    figures = UserAIWallet.objects.filter(user=user).aggregate(
        balance=Sum("balance"),
        allocated=Sum("allocated_tokens"),
        used=Sum("used_tokens"),
        lifetime_allocated=Sum("lifetime_allocated"),
        lifetime_used=Sum("lifetime_used"),
    )
    return {key: int(value or 0) for key, value in figures.items()}


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
class _DuplicateMovement(Exception):
    """A concurrent writer already applied this ``reference_id``."""


def _sync_account(user: User, *, entry: AITokenTransaction | None, reason: str, model_key: str) -> int:
    """Recompute the legacy aggregate and mirror one movement into its ledger.

    Runs inside the caller's transaction, straight after the wallet write, so the
    old and new views of a balance can never be observed out of step.
    """
    totals = totals_of(user)
    account, _ = TokenAccount.objects.get_or_create(user=user)
    account.balance = totals["balance"]
    account.total_allocated = totals["lifetime_allocated"]
    account.total_consumed = totals["lifetime_used"]
    account.cycle_allocated = totals["allocated"]
    account.cycle_consumed = totals["used"]
    if account.cycle_started_at is None and totals["lifetime_allocated"]:
        account.cycle_started_at = timezone.now()
    account.save(
        update_fields=[
            "balance", "total_allocated", "total_consumed",
            "cycle_allocated", "cycle_consumed", "cycle_started_at", "updated_at",
        ]
    )

    if entry is not None:
        TokenTransaction.objects.create(
            user=user,
            amount=entry.amount,
            transaction_type=LEGACY_TYPES.get(entry.transaction_type, TransactionType.ADJUSTMENT),
            source=model_key[:64],
            description=reason[:255],
            balance_after=account.balance,
            # Namespaced so a mirrored row can never collide with a legacy key.
            idempotency_key=f"wallet:{entry.pk}",
            created_by=entry.performed_by,
            conversation=entry.conversation,
            message=entry.message,
            payment=entry.payment,
        )
    return account.balance


#: Which kind of package a movement opens, when it opens one at all.
_ALLOCATION_SOURCE = {
    WalletTransactionType.PLAN_PURCHASE: AllocationSource.PURCHASE,
    WalletTransactionType.ADMIN_ALLOCATION: AllocationSource.ADMIN,
    WalletTransactionType.FREE_TRIAL: AllocationSource.TRIAL,
    WalletTransactionType.REFERRAL_BONUS: AllocationSource.REFERRAL,
    WalletTransactionType.MANUAL_ADJUSTMENT: AllocationSource.ADJUSTMENT,
}


def _mirror_allocation(user, ai_model, amount, transaction_type, *, plan, payment, reason):
    """Keep the student's packages in step with the wallet that just moved.

    Runs inside the wallet's own transaction and under its lock, so the balance
    and the packages that explain it are written together or not at all. The
    wallet stays authoritative — it is what the ledger proves — and the packages
    are a view of the same tokens broken down by what was bought and how fast
    it may be spent.
    """
    if amount > 0:
        usage_limit_service.open_allocation(
            user, ai_model, amount,
            plan=plan,
            payment=payment,
            source=_ALLOCATION_SOURCE.get(transaction_type, AllocationSource.ADJUSTMENT),
            label=(plan.name if plan is not None else reason)[:120],
        )
    elif transaction_type == WalletTransactionType.CHAT_USAGE:
        usage_limit_service.consume(user, ai_model, -amount)
    else:
        # A reclaim, a refund or a negative correction takes tokens back out of
        # the packages themselves; it is not consumption and must not count
        # against anybody's daily allowance.
        usage_limit_service.cancel_allocations(user, ai_model, -amount, reason=reason)


def _apply(
    user: User,
    ai_model: AIModel,
    amount: int,
    transaction_type: str,
    *,
    reason: str = "",
    performed_by: User | None = None,
    reference_id: str | None = None,
    conversation=None,
    message=None,
    payment=None,
    plan=None,
    allow_clamp: bool = False,
) -> WalletResult:
    """The one write path. ``amount`` is signed: positive credits, negative debits."""
    amount = int(amount)
    if amount == 0:
        current = balance_of(user, ai_model)
        return WalletResult(None, None, current, current, 0, False)

    if reference_id and AITokenTransaction.objects.filter(reference_id=reference_id).exists():
        logger.info("Skipping duplicate wallet movement reference=%s", reference_id)
        current = balance_of(user, ai_model)
        return WalletResult(None, None, current, current, 0, False)

    with transaction.atomic():
        UserAIWallet.objects.get_or_create(user=user, ai_model=ai_model)
        # Re-read under the lock: the row may have been created, or moved, by a
        # concurrent request between the two statements above.
        wallet = UserAIWallet.objects.select_for_update().get(user=user, ai_model=ai_model)
        before = wallet.balance

        if amount < 0:
            required = -amount
            if wallet.balance < required:
                if not allow_clamp:
                    raise InsufficientModelTokens(ai_model, wallet.balance, required)
                amount = -wallet.balance
                if amount == 0:
                    return WalletResult(wallet, None, before, before, 0, False)

        wallet.balance += amount
        now = timezone.now()

        if amount > 0:
            wallet.allocated_tokens += amount
            wallet.lifetime_allocated += amount
            if wallet.first_allocated_at is None:
                wallet.first_allocated_at = now
        elif transaction_type == WalletTransactionType.CHAT_USAGE:
            wallet.used_tokens += -amount
            wallet.lifetime_used += -amount
            wallet.last_used_at = now
        else:
            # An administrator taking tokens back shrinks the allocation rather
            # than counting as consumption, so "used of allocated" stays true.
            wallet.allocated_tokens = max(wallet.used_tokens, wallet.allocated_tokens + amount)

        wallet.save(
            update_fields=[
                "balance", "allocated_tokens", "used_tokens",
                "lifetime_allocated", "lifetime_used",
                "first_allocated_at", "last_used_at", "updated_at",
            ]
        )

        try:
            entry = AITokenTransaction.objects.create(
                user=user,
                ai_model=ai_model,
                amount=amount,
                transaction_type=transaction_type,
                balance_before=before,
                balance_after=wallet.balance,
                reason=reason[:255],
                reference_id=reference_id,
                performed_by=performed_by,
                conversation=conversation,
                message=message,
                payment=payment,
            )
        except IntegrityError:
            logger.info("Idempotency race resolved for reference=%s", reference_id)
            raise _DuplicateMovement(before)

        _sync_account(user, entry=entry, reason=reason, model_key=ai_model.key)
        _mirror_allocation(
            user, ai_model, amount, transaction_type,
            plan=plan, payment=payment, reason=reason,
        )

        return WalletResult(wallet, entry, before, wallet.balance, amount, True)


def _safe_apply(*args, **kwargs) -> WalletResult:
    try:
        return _apply(*args, **kwargs)
    except _DuplicateMovement as exc:
        balance = exc.args[0] if exc.args else 0
        return WalletResult(None, None, balance, balance, 0, False)


# ---------------------------------------------------------------------------
# Credits
# ---------------------------------------------------------------------------
def credit(
    user: User,
    ai_model: AIModel,
    amount: int,
    *,
    transaction_type: str = WalletTransactionType.ADMIN_ALLOCATION,
    reason: str = "",
    performed_by: User | None = None,
    reference_id: str | None = None,
    payment=None,
    plan=None,
) -> WalletResult:
    """Add tokens to one model's wallet. Safe to retry with a ``reference_id``.

    ``plan`` names the package this credit bought, when it bought one. It is
    what gives the resulting allocation its daily, weekly and monthly ceilings —
    a credit with no plan is an unmetered grant.
    """
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Credit amount must be positive.")
    result = _safe_apply(
        user, ai_model, amount, transaction_type,
        reason=reason, performed_by=performed_by, reference_id=reference_id,
        payment=payment, plan=plan,
    )

    # Topping a wallet up re-arms its low-balance warnings. Without this a
    # student warned at 10% would be warned once and never again, because the
    # de-duplication guard would still be holding the claim from last time —
    # so the second time they ran out they would hear nothing.
    #
    # Scoped to this model: refilling GPT-4o must not silence a Claude warning
    # that is still overdue. Never allowed to fail a credit that has already
    # been applied.
    try:
        from AI.services import token_alert_service

        token_alert_service.reset_for(user, ai_model)
    except Exception:  # pragma: no cover - a bookkeeping detail, not the credit
        logger.exception("could not reset low-token alerts for user=%s", user.pk)

    return result


def grant_free_trial(user: User, ai_model: AIModel, amount: int, *, performed_by: User | None = None):
    """One welcome grant per account per model, ever — guarded by the reference."""
    return credit(
        user, ai_model, amount,
        transaction_type=WalletTransactionType.FREE_TRIAL,
        reason=f"Welcome bonus — {amount:,} {ai_model.name} tokens",
        performed_by=performed_by,
        reference_id=f"free_trial:{user.pk}:{ai_model.pk}",
    )


def grant_referral_bonus(user: User, ai_model: AIModel, amount: int, *, referral_id: int, role: str, note: str = ""):
    return credit(
        user, ai_model, amount,
        transaction_type=WalletTransactionType.REFERRAL_BONUS,
        reason=note or f"Referral bonus — {ai_model.name}",
        reference_id=f"referral:{referral_id}:{role}:{ai_model.pk}",
    )


def credit_payment(payment, ai_model: AIModel, *, tokens: int | None = None) -> WalletResult:
    """Credit a paid order exactly once, into one of the plan's models.

    ``tokens`` names the amount for this particular model, which a bundle needs
    because each model in it has its own figure. Left out, it falls back to the
    payment's own total — the single-model case, unchanged.
    """
    return credit(
        payment.user, ai_model,
        payment.tokens_to_grant if tokens is None else tokens,
        transaction_type=WalletTransactionType.PLAN_PURCHASE,
        reason=f"{payment.plan.name} — {payment.reference}",
        reference_id=f"payment:{payment.pk}:{ai_model.pk}",
        payment=payment,
        plan=payment.plan,
    )


# ---------------------------------------------------------------------------
# Debits
# ---------------------------------------------------------------------------
def consume(
    user: User,
    ai_model: AIModel,
    amount: int,
    *,
    reason: str = "",
    conversation=None,
    message=None,
    clamp: bool = True,
) -> WalletResult:
    """Charge real AI usage against exactly one model's wallet.

    ``clamp`` exists because the provider's reported usage can slightly exceed
    the balance we pre-checked; we charge what is there rather than refusing to
    record spending that has already happened.
    """
    amount = int(amount)
    if amount <= 0:
        current = balance_of(user, ai_model)
        return WalletResult(None, None, current, current, 0, False)
    return _safe_apply(
        user, ai_model, -amount, WalletTransactionType.CHAT_USAGE,
        reason=reason, conversation=conversation, message=message, allow_clamp=clamp,
    )


def reclaim(
    user: User, ai_model: AIModel, amount: int, *, reason: str = "", performed_by: User | None = None
) -> WalletResult:
    """Administrative removal of tokens. Clamped, so it never goes below zero."""
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Reclaim amount must be positive.")
    return _safe_apply(
        user, ai_model, -amount, WalletTransactionType.ADMIN_REVERSAL,
        reason=reason or f"Reclaimed {amount:,} {ai_model.name} tokens",
        performed_by=performed_by, allow_clamp=True,
    )


def adjust(
    user: User, ai_model: AIModel, amount: int, *, reason: str, performed_by: User | None = None
) -> WalletResult:
    """Signed manual correction. A reason is required and is written to the ledger."""
    if not (reason or "").strip():
        raise ValueError("A manual adjustment must carry a reason.")
    return _safe_apply(
        user, ai_model, int(amount), WalletTransactionType.MANUAL_ADJUSTMENT,
        reason=reason, performed_by=performed_by, allow_clamp=True,
    )


def refund_payment(payment, ai_model: AIModel, *, performed_by: User | None = None) -> WalletResult:
    """Reverse a purchase: pull the granted tokens back out of the same wallet.

    Recorded as a ``REFUND`` rather than an admin reversal, so the history says
    *why* the tokens went — and keyed on the payment, so replaying a webhook or
    double-clicking "refund" cannot debit twice.

    Clamped: a student who has already spent some of what they bought ends at
    zero rather than going negative.
    """
    return _safe_apply(
        payment.user,
        ai_model,
        -int(payment.tokens_to_grant),
        WalletTransactionType.REFUND,
        reason=f"Refund — {payment.reference}",
        performed_by=performed_by,
        reference_id=f"refund:{payment.pk}:{ai_model.pk}",
        payment=payment,
        allow_clamp=True,
    )


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
def set_access(user: User, ai_model: AIModel, enabled: bool) -> UserAIWallet:
    """Grant or revoke one model for one student, without moving any tokens."""
    wallet = get_wallet(user, ai_model)
    if wallet.is_enabled != enabled:
        wallet.is_enabled = enabled
        wallet.save(update_fields=["is_enabled", "updated_at"])
    return wallet


def authorize(user: User, ai_model: AIModel, *, minimum: int = 1) -> UserAIWallet:
    """Server-side gate for one request. Raises rather than returning a flag.

    Both halves are checked here — access *and* balance — because the model
    picker in the browser is a convenience and is never trusted.
    """
    if not ai_model.provider_is_active:
        raise ModelAccessDenied(
            ai_model, f"{ai_model.provider_name} is temporarily unavailable. Please pick another model."
        )
    if not ai_model.is_active:
        raise ModelAccessDenied(
            ai_model, f"{ai_model.name} is not available right now. Please pick another model."
        )

    wallet = get_wallet(user, ai_model)
    if not wallet.is_enabled:
        raise ModelAccessDenied(ai_model)
    if wallet.balance < minimum:
        raise InsufficientModelTokens(ai_model, wallet.balance, minimum)
    return wallet


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------
def recalculate(user: User, ai_model: AIModel) -> UserAIWallet:
    """Rebuild one wallet from its ledger. A repair tool, not a hot path."""
    with transaction.atomic():
        UserAIWallet.objects.get_or_create(user=user, ai_model=ai_model)
        wallet = UserAIWallet.objects.select_for_update().get(user=user, ai_model=ai_model)
        entries = AITokenTransaction.objects.filter(user=user, ai_model=ai_model)
        credited = entries.filter(amount__gt=0).aggregate(total=Sum("amount"))["total"] or 0
        debited = entries.filter(amount__lt=0).aggregate(total=Sum("amount"))["total"] or 0
        used = (
            entries.filter(transaction_type=WalletTransactionType.CHAT_USAGE)
            .aggregate(total=Sum("amount"))["total"]
            or 0
        )
        wallet.lifetime_allocated = credited
        wallet.lifetime_used = -used
        wallet.used_tokens = -used
        wallet.balance = max(0, credited + debited)
        wallet.allocated_tokens = wallet.balance + wallet.used_tokens
        wallet.save(
            update_fields=[
                "balance", "allocated_tokens", "used_tokens",
                "lifetime_allocated", "lifetime_used", "updated_at",
            ]
        )
        _sync_account(user, entry=None, reason="", model_key=ai_model.key)
        # The packages describe the same tokens, so a rebuilt balance has to
        # take them with it or the two views drift apart.
        usage_limit_service.reconcile(user, ai_model, wallet.balance)
        return wallet
