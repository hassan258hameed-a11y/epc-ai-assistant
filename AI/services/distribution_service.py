"""Admin token distribution: pool out, wallet in — atomically, per model.

An allocation is two movements that must both happen or neither: the model's
pool goes down and the student's wallet for that same model goes up.  Every
function here wraps the pair in one ``transaction.atomic()`` block, so a
half-applied distribution cannot exist.

This module is the *only* supported way for an administrator to move tokens.
Views call it, the audit log records it, and the student is notified from here,
so all three can never disagree about what happened.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction

from AI.models import AIModel, AuditAction, User, UserAIWallet, WalletTransactionType
from AI.services import audit_service, model_pool_service, wallet_service
from AI.services.model_pool_service import InsufficientPool

logger = logging.getLogger("AI.distribution")


@dataclass
class Distribution:
    """The before/after of one completed allocation, ready for a flash message."""

    student: User
    ai_model: AIModel
    amount: int
    balance_before: int
    balance_after: int
    pool_before: int
    pool_after: int
    action: str

    @property
    def verb(self) -> str:
        return {
            "add": "Added",
            "set": "Assigned",
            "remove": "Removed",
            "adjust": "Adjusted",
        }.get(self.action, "Updated")

    @property
    def summary(self) -> str:
        return (
            f"{self.verb} {abs(self.amount):,} {self.ai_model.name} tokens "
            f"for {self.student.display_name}."
        )


def preview(student: User, ai_model: AIModel, amount: int, action: str = "add") -> dict:
    """What *would* happen, for the confirmation step. Writes nothing."""
    amount = max(0, int(amount))
    current = wallet_service.balance_of(student, ai_model)
    pool = model_pool_service.balance(ai_model)

    if action == "set":
        delta = amount - current
        new_balance = amount
    elif action == "remove":
        delta = -min(amount, current)
        new_balance = current + delta
    else:
        delta = amount
        new_balance = current + amount

    # Only a net increase costs the pool anything; a reduction returns to it.
    pool_after = pool - delta if delta > 0 else pool + (-delta)
    return {
        "student": student,
        "model": ai_model,
        "action": action,
        "amount": amount,
        "delta": delta,
        "current_balance": current,
        "new_balance": max(0, new_balance),
        "pool_before": pool,
        "pool_after": max(0, pool_after),
        "affordable": delta <= pool,
        "shortfall": max(0, delta - pool),
    }


def _apply(
    student: User,
    ai_model: AIModel,
    delta: int,
    *,
    reason: str,
    performed_by: User | None,
    action: str,
) -> Distribution:
    """Move ``delta`` tokens between the model's pool and the student's wallet.

    Called inside the caller's transaction, with the wallet row already locked,
    so the balance read here is the one the movement is applied to.
    """
    pool_before = model_pool_service.balance(ai_model)
    before = wallet_service.balance_of(student, ai_model)

    with transaction.atomic():
        if delta > 0:
            # Draw first: it is the movement that can fail, and failing before
            # the wallet is touched keeps the pair all-or-nothing.
            model_pool_service.draw(
                ai_model, delta, student=student, created_by=performed_by,
                description=reason or f"Assigned {delta:,} {ai_model.name} tokens to {student.display_name}",
            )
            result = wallet_service.credit(
                student, ai_model, delta,
                transaction_type=WalletTransactionType.ADMIN_ALLOCATION,
                reason=reason or f"Admin allocation — {ai_model.name}",
                performed_by=performed_by,
            )
        else:
            result = wallet_service.reclaim(
                student, ai_model, -delta,
                reason=reason or f"Reclaimed {ai_model.name} tokens",
                performed_by=performed_by,
            )
            # A reclaim is clamped at the student's balance, so only what was
            # actually taken goes back into the pool — the signed amount the
            # wallet movement itself recorded, never a difference between two
            # balances read at different moments.
            taken = -int(result.amount or 0) if result.created else 0
            if taken > 0:
                model_pool_service.give_back(
                    ai_model, taken, student=student, created_by=performed_by,
                    description=reason or f"Reclaimed from {student.display_name}",
                )

    return Distribution(
        student=student,
        ai_model=ai_model,
        amount=result.amount,
        balance_before=result.balance_before if result.created else before,
        balance_after=result.balance_after if result.created else before,
        pool_before=pool_before,
        pool_after=model_pool_service.balance(ai_model),
        action=action,
    )


def distribute(
    student: User,
    ai_model: AIModel,
    amount: int,
    *,
    action: str = "add",
    reason: str = "",
    performed_by: User | None = None,
    request=None,
    notify: bool = True,
) -> Distribution:
    """Give, set or take back tokens for exactly one model.

    ``action`` is ``add`` (top up), ``set`` (make the balance exactly ``amount``)
    or ``remove`` (take back up to ``amount``).  Only the *difference* is drawn
    from the pool for ``set``: topping a student up to 50,000 when they already
    hold 20,000 costs the pool 30,000, not 50,000.

    Raises :class:`~AI.services.model_pool_service.InsufficientPool` when that
    model's supply cannot cover the increase.  No other model is ever touched.
    """
    amount = int(amount)
    if amount < 0:
        raise ValueError("Amount cannot be negative.")
    if action not in ("set", "remove") and amount == 0:
        raise ValueError("Enter a token amount greater than zero.")

    # "Set" and "remove" are relative to the balance the student holds *now*,
    # so that balance is read under the wallet's row lock and the movement is
    # applied in the same transaction. Read outside it, a chat reply landing
    # between the read and the write made "set to 50,000" leave some other
    # figure behind.
    with transaction.atomic():
        wallet_service.get_wallet(student, ai_model)
        current = (
            UserAIWallet.objects.select_for_update()
            .values_list("balance", flat=True)
            .get(user=student, ai_model=ai_model)
        )
        if action == "set":
            delta = amount - current
        elif action == "remove":
            delta = -min(amount, current)
        else:
            delta = amount

        if delta == 0:
            pool = model_pool_service.balance(ai_model)
            return Distribution(
                student=student, ai_model=ai_model, amount=0,
                balance_before=current, balance_after=current,
                pool_before=pool, pool_after=pool, action=action,
            )

        result = _apply(student, ai_model, delta, reason=reason, performed_by=performed_by, action=action)

        audit_service.record(
            AuditAction.TOKENS_ADD if delta > 0 else AuditAction.TOKENS_REMOVE,
            actor=performed_by,
            request=request,
            target=student,
            description=(reason or f"{result.verb} {ai_model.name} tokens")[:255],
            old_value=f"{ai_model.key}:{result.balance_before}",
            new_value=f"{ai_model.key}:{result.balance_after}",
        )

    if notify and delta > 0:
        _notify_credit(student, ai_model, result.balance_after)

    return result


def adjust(
    student: User,
    ai_model: AIModel,
    delta: int,
    *,
    reason: str,
    performed_by: User | None = None,
    request=None,
) -> Distribution:
    """A signed manual correction that does **not** move the pool.

    Used when the pool and the wallets have drifted apart and one side needs
    fixing on its own.  A reason is mandatory and lands in both the token ledger
    and the audit log, because an unexplained adjustment is indistinguishable
    from a mistake.
    """
    if not (reason or "").strip():
        raise ValueError("A manual adjustment must carry a reason.")

    before = wallet_service.balance_of(student, ai_model)
    result = wallet_service.adjust(
        student, ai_model, int(delta), reason=reason, performed_by=performed_by
    )
    audit_service.record(
        AuditAction.TOKENS_ADD if delta > 0 else AuditAction.TOKENS_REMOVE,
        actor=performed_by,
        request=request,
        target=student,
        description=f"Manual adjustment ({ai_model.name}) — {reason}"[:255],
        old_value=f"{ai_model.key}:{before}",
        new_value=f"{ai_model.key}:{result.balance_after}",
    )
    return Distribution(
        student=student, ai_model=ai_model, amount=result.amount,
        balance_before=before, balance_after=result.balance_after,
        pool_before=model_pool_service.balance(ai_model),
        pool_after=model_pool_service.balance(ai_model),
        action="adjust",
    )


def bulk_distribute(
    students,
    ai_model: AIModel,
    amount: int,
    *,
    action: str = "add",
    reason: str = "",
    performed_by: User | None = None,
    request=None,
) -> dict:
    """Apply the same allocation to many students, one transaction each.

    Stops at the first empty pool rather than skipping quietly: a half-applied
    batch the administrator does not know about is worse than one that reports
    exactly where it stopped.
    """
    changed = 0
    stopped_short = False
    students = list(students)

    for student in students:
        try:
            distribute(
                student, ai_model, amount,
                action=action, reason=reason, performed_by=performed_by,
                request=None, notify=True,
            )
            changed += 1
        except InsufficientPool:
            stopped_short = True
            break
        except Exception:  # one bad row must not abort the batch
            logger.exception("Bulk allocation failed for user=%s model=%s", student.pk, ai_model.key)

    audit_service.record(
        AuditAction.TOKENS_ADD if action != "remove" else AuditAction.TOKENS_REMOVE,
        actor=performed_by,
        request=request,
        description=f"Bulk {action} of {amount:,} {ai_model.name} tokens for {changed} students"[:255],
        new_value=f"{ai_model.key}:{amount}",
    )
    return {
        "changed": changed,
        "attempted": len(students),
        "stopped_short": stopped_short,
        "pool_balance": model_pool_service.balance(ai_model),
    }


def set_model_access(
    student: User,
    ai_model: AIModel,
    enabled: bool,
    *,
    performed_by: User | None = None,
    request=None,
):
    """Grant or revoke one model for one student, leaving their tokens alone."""
    wallet = wallet_service.set_access(student, ai_model, enabled)
    audit_service.record(
        AuditAction.SETTINGS_UPDATE,
        actor=performed_by,
        request=request,
        target=student,
        description=f"{'Enabled' if enabled else 'Disabled'} {ai_model.name} for {student.display_name}",
        new_value=str(enabled),
    )
    return wallet


def _notify_credit(student: User, ai_model: AIModel, balance: int) -> None:
    """Tell the student, naming the model so the message is actionable."""
    try:
        from AI.services import notification_service

        notification_service.reset_low_balance_alerts(student)
        notification_service.notify(
            student,
            f"{ai_model.name} tokens added",
            f"You now have {balance:,} {ai_model.name} tokens.",
            level="success",
            category="tokens",
            action_url="/chat/",
            action_label="Start chatting",
            telegram=True,
        )
    except Exception:  # pragma: no cover — never fail a distribution over an alert
        logger.exception("Could not notify user=%s about a %s allocation", student.pk, ai_model.key)
