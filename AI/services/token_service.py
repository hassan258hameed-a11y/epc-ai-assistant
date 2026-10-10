"""Token accounting.

Every balance change happens here, inside a database transaction that locks the
user's :class:`TokenAccount` row.  That single choke point is what guarantees:

* no negative balances (enforced again by a DB check constraint),
* no double deduction under concurrent AI requests,
* no duplicate credits for payments, referrals or free trials (idempotency keys),
* a complete, replayable ledger for every movement.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from AI.models import TokenAccount, TokenTransaction, TransactionType, User

logger = logging.getLogger("AI.tokens")


class InsufficientTokens(Exception):
    """Raised when a debit would push the balance below zero."""

    def __init__(self, balance: int, required: int):
        self.balance = balance
        self.required = required
        super().__init__(f"Insufficient tokens: balance={balance}, required={required}")


@dataclass
class LedgerResult:
    transaction: TokenTransaction | None
    balance: int
    created: bool  # False when an idempotency key short-circuited the write


def get_account(user: User) -> TokenAccount:
    account, _ = TokenAccount.objects.get_or_create(user=user)
    return account


def balance_of(user: User) -> int:
    account = TokenAccount.objects.filter(user=user).first()
    return account.balance if account else 0


def _apply(
    user: User,
    amount: int,
    transaction_type: str,
    *,
    description: str = "",
    source: str = "",
    created_by: User | None = None,
    idempotency_key: str | None = None,
    conversation=None,
    message=None,
    payment=None,
    allow_negative_clamp: bool = False,
    reset_cycle: bool = False,
) -> LedgerResult:
    """Core ledger write. ``amount`` is signed: positive credits, negative debits."""
    if amount == 0:
        return LedgerResult(transaction=None, balance=balance_of(user), created=False)

    if idempotency_key and TokenTransaction.objects.filter(idempotency_key=idempotency_key).exists():
        logger.info("Skipping duplicate token transaction key=%s", idempotency_key)
        return LedgerResult(transaction=None, balance=balance_of(user), created=False)

    with transaction.atomic():
        account = (
            TokenAccount.objects.select_for_update().filter(user=user).first()
            or TokenAccount.objects.create(user=user)
        )
        # Re-read under the lock in case the row was created between the two calls.
        account = TokenAccount.objects.select_for_update().get(pk=account.pk)

        if amount < 0:
            required = -amount
            if account.balance < required:
                if not allow_negative_clamp:
                    raise InsufficientTokens(account.balance, required)
                amount = -account.balance
                if amount == 0:
                    return LedgerResult(transaction=None, balance=0, created=False)

        account.balance += amount

        # Lifetime counters record everything that ever moved.
        if amount > 0:
            account.total_allocated += amount
        else:
            account.total_consumed += -amount

        # Cycle counters describe the *current* allocation, which is what the
        # student is shown. Consumption spends the cycle; an administrator
        # taking tokens back shrinks it instead, so the invariant
        # balance == cycle_allocated - cycle_consumed always holds.
        if reset_cycle:
            account.cycle_allocated = account.balance
            account.cycle_consumed = 0
            account.cycle_started_at = timezone.now()
        elif amount > 0:
            if not account.cycle_started_at:
                account.cycle_started_at = timezone.now()
            account.cycle_allocated += amount
        elif transaction_type == TransactionType.USAGE:
            account.cycle_consumed += -amount
        else:
            account.cycle_allocated = max(account.cycle_consumed, account.cycle_allocated + amount)

        account.save(
            update_fields=[
                "balance", "total_allocated", "total_consumed",
                "cycle_allocated", "cycle_consumed", "cycle_started_at", "updated_at",
            ]
        )

        try:
            entry = TokenTransaction.objects.create(
                user=user,
                amount=amount,
                transaction_type=transaction_type,
                source=source[:64],
                description=description[:255],
                balance_after=account.balance,
                idempotency_key=idempotency_key,
                created_by=created_by,
                conversation=conversation,
                message=message,
                payment=payment,
            )
        except IntegrityError:
            # A concurrent writer won the idempotency race — roll back this credit.
            logger.info("Idempotency race resolved for key=%s", idempotency_key)
            raise _DuplicateCredit(account.balance - amount)

        return LedgerResult(transaction=entry, balance=account.balance, created=True)


class _DuplicateCredit(Exception):
    def __init__(self, balance: int):
        self.balance = balance


def _safe_apply(*args, **kwargs) -> LedgerResult:
    try:
        return _apply(*args, **kwargs)
    except _DuplicateCredit as exc:
        return LedgerResult(transaction=None, balance=exc.balance, created=False)


# ---------------------------------------------------------------------------
# Credits
# ---------------------------------------------------------------------------
def grant(
    user: User,
    amount: int,
    *,
    transaction_type: str = TransactionType.ADMIN_GRANT,
    description: str = "",
    source: str = "",
    created_by: User | None = None,
    idempotency_key: str | None = None,
    payment=None,
    reset_cycle: bool = False,
) -> LedgerResult:
    """Credit tokens. Safe to retry when an ``idempotency_key`` is supplied."""
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Grant amount must be positive.")
    return _safe_apply(
        user,
        amount,
        transaction_type,
        description=description,
        source=source,
        created_by=created_by,
        idempotency_key=idempotency_key,
        payment=payment,
        reset_cycle=reset_cycle,
    )


def grant_free_trial(user: User, amount: int, *, created_by: User | None = None) -> LedgerResult:
    """One welcome grant per account, ever — guarded by the idempotency key."""
    return grant(
        user,
        amount,
        transaction_type=TransactionType.FREE_TRIAL,
        description=f"Welcome bonus — {amount:,} tokens",
        source="free_trial",
        created_by=created_by,
        idempotency_key=f"free_trial:{user.pk}",
    )


def grant_referral_bonus(user: User, amount: int, *, referral_id: int, role: str, note: str = "") -> LedgerResult:
    return grant(
        user,
        amount,
        transaction_type=TransactionType.REFERRAL_BONUS,
        description=note or "Referral bonus",
        source="referral",
        idempotency_key=f"referral:{referral_id}:{role}",
    )


def credit_payment(payment) -> LedgerResult:
    """Credit a paid order exactly once."""
    return grant(
        payment.user,
        payment.tokens_to_grant,
        transaction_type=TransactionType.SUBSCRIPTION,
        description=f"{payment.plan.name} — {payment.reference}",
        source=payment.method,
        payment=payment,
        idempotency_key=f"payment:{payment.pk}",
    )


# ---------------------------------------------------------------------------
# Debits
# ---------------------------------------------------------------------------
def consume(
    user: User,
    amount: int,
    *,
    description: str = "",
    source: str = "",
    conversation=None,
    message=None,
    clamp: bool = True,
) -> LedgerResult:
    """Charge actual AI usage against the balance.

    ``clamp`` exists because the provider's real usage may slightly exceed the
    balance we pre-checked; we charge what is available rather than refusing to
    record consumption that already happened.
    """
    amount = int(amount)
    if amount <= 0:
        return LedgerResult(transaction=None, balance=balance_of(user), created=False)
    return _safe_apply(
        user,
        -amount,
        TransactionType.USAGE,
        description=description,
        source=source,
        conversation=conversation,
        message=message,
        allow_negative_clamp=clamp,
    )


def deduct(
    user: User, amount: int, *, description: str = "", created_by: User | None = None,
    reset_cycle: bool = False,
) -> LedgerResult:
    """Administrative removal of tokens. Never goes below zero."""
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Deduction amount must be positive.")
    return _safe_apply(
        user,
        -amount,
        TransactionType.ADMIN_DEDUCT,
        description=description,
        source="admin",
        created_by=created_by,
        allow_negative_clamp=True,
        reset_cycle=reset_cycle,
    )


def adjust(user: User, amount: int, *, description: str = "", created_by: User | None = None) -> LedgerResult:
    """Signed correction entry."""
    return _safe_apply(
        user,
        int(amount),
        TransactionType.ADJUSTMENT,
        description=description,
        source="admin",
        created_by=created_by,
        allow_negative_clamp=True,
    )


def set_balance(user: User, target: int, *, description: str = "", created_by: User | None = None) -> LedgerResult:
    """Assign an exact balance, writing the difference as a single ledger entry.

    This is what "assign 50,000 tokens to Student A" means: the student ends up
    holding exactly 50,000, whatever they held before. The move is recorded as a
    grant or a deduction so history still explains how they got there.
    """
    target = int(target)
    if target < 0:
        raise ValueError("A balance cannot be negative.")

    # The read and the write share one transaction so the lock taken while
    # measuring the difference is still held when it is applied.
    with transaction.atomic():
        account = (
            TokenAccount.objects.select_for_update().filter(user=user).first()
            or TokenAccount.objects.create(user=user)
        )
        delta = target - account.balance
        if delta == 0:
            # No tokens move, but re-assigning the same number is still a new
            # allocation: the student should see "0 used of 50,000" again.
            account.cycle_allocated = target
            account.cycle_consumed = 0
            account.cycle_started_at = timezone.now()
            account.save(update_fields=["cycle_allocated", "cycle_consumed", "cycle_started_at", "updated_at"])
            return LedgerResult(transaction=None, balance=target, created=False)

        # A fresh assignment starts a new allocation cycle, so the student sees
        # "0 used of 50,000" rather than a lifetime total they cannot act on.
        note = description or f"Balance set to {target:,} tokens"
        if delta > 0:
            return grant(user, delta, description=note, source="admin", created_by=created_by, reset_cycle=True)
        return deduct(user, -delta, description=note, created_by=created_by, reset_cycle=True)


def refund(user: User, amount: int, *, payment=None, description: str = "", created_by: User | None = None):
    """Reverse a purchase: remove the granted tokens (clamped at zero)."""
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Refund amount must be positive.")
    key = f"refund:{payment.pk}" if payment is not None else None
    if key and TokenTransaction.objects.filter(idempotency_key=key).exists():
        return LedgerResult(transaction=None, balance=balance_of(user), created=False)
    return _safe_apply(
        user,
        -amount,
        TransactionType.REFUND,
        description=description or "Refund",
        source="refund",
        created_by=created_by,
        payment=payment,
        idempotency_key=key,
        allow_negative_clamp=True,
    )


# ---------------------------------------------------------------------------
# Guards & reporting
# ---------------------------------------------------------------------------
def can_spend(user: User, minimum: int = 1) -> bool:
    return balance_of(user) >= minimum


def usage_summary(user: User) -> dict:
    account = get_account(user)
    rows = TokenTransaction.objects.filter(user=user).values("transaction_type").annotate(total=Sum("amount"))
    by_type = {row["transaction_type"]: row["total"] or 0 for row in rows}
    return {
        "balance": account.balance,
        # The current allocation — what the student is shown as "x of y".
        "allocated": account.allocation,
        "used": account.used_in_cycle,
        "usage_percent": account.usage_percent,
        "remaining_percent": account.remaining_percent,
        # Lifetime, for the history views.
        "total_allocated": account.total_allocated,
        "total_consumed": account.total_consumed,
        "by_type": by_type,
    }


def recalculate(user: User) -> TokenAccount:
    """Rebuild a balance from the ledger — a repair tool, not a hot path."""
    with transaction.atomic():
        account = (
            TokenAccount.objects.select_for_update().filter(user=user).first()
            or TokenAccount.objects.create(user=user)
        )
        entries = TokenTransaction.objects.filter(user=user)
        credited = entries.filter(amount__gt=0).aggregate(total=Sum("amount"))["total"] or 0
        debited = entries.filter(amount__lt=0).aggregate(total=Sum("amount"))["total"] or 0
        account.total_allocated = credited
        account.total_consumed = -debited
        account.balance = max(0, credited + debited)
        account.save(update_fields=["balance", "total_allocated", "total_consumed", "updated_at"])
        return account
