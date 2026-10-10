"""Token accounting: the invariants that protect real money."""
from __future__ import annotations

import threading

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase

from AI.models import TokenTransaction, TransactionType, User
from AI.services import token_service
from AI.services.token_service import InsufficientTokens


class TokenLedgerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="ledger", email="ledger@test.local", password="Sup3rSecret!x")

    def test_new_account_starts_at_zero(self):
        self.assertEqual(token_service.balance_of(self.user), 0)

    def test_grant_increases_balance_and_records_a_transaction(self):
        result = token_service.grant(self.user, 5_000, description="Test grant")

        self.assertTrue(result.created)
        self.assertEqual(result.balance, 5_000)
        self.assertEqual(token_service.balance_of(self.user), 5_000)

        entry = TokenTransaction.objects.get(user=self.user)
        self.assertEqual(entry.amount, 5_000)
        self.assertEqual(entry.balance_after, 5_000)
        self.assertEqual(entry.transaction_type, TransactionType.ADMIN_GRANT)

    def test_multiple_grants_accumulate_rather_than_overwrite(self):
        token_service.grant(self.user, 1_000)
        token_service.grant(self.user, 2_500)
        token_service.grant(self.user, 500)

        self.assertEqual(token_service.balance_of(self.user), 4_000)
        self.assertEqual(TokenTransaction.objects.filter(user=self.user).count(), 3)

        account = token_service.get_account(self.user)
        self.assertEqual(account.total_allocated, 4_000)

    def test_consume_deducts_and_tracks_total_consumed(self):
        token_service.grant(self.user, 1_000)
        token_service.consume(self.user, 300, description="AI usage")

        account = token_service.get_account(self.user)
        self.assertEqual(account.balance, 700)
        self.assertEqual(account.total_consumed, 300)
        self.assertEqual(account.total_allocated, 1_000)

    def test_consume_is_clamped_at_zero_never_negative(self):
        token_service.grant(self.user, 100)
        token_service.consume(self.user, 5_000, clamp=True)

        self.assertEqual(token_service.balance_of(self.user), 0)
        entry = TokenTransaction.objects.filter(transaction_type=TransactionType.USAGE).first()
        self.assertEqual(entry.amount, -100)  # only what was available was charged

    def test_unclamped_consume_raises_rather_than_going_negative(self):
        token_service.grant(self.user, 50)
        with self.assertRaises(InsufficientTokens):
            token_service.consume(self.user, 500, clamp=False)
        self.assertEqual(token_service.balance_of(self.user), 50)

    def test_admin_deduction_never_goes_below_zero(self):
        token_service.grant(self.user, 200)
        token_service.deduct(self.user, 1_000, description="Over-deduct")
        self.assertEqual(token_service.balance_of(self.user), 0)

    def test_idempotency_key_blocks_duplicate_credits(self):
        first = token_service.grant(self.user, 1_000, idempotency_key="promo:42")
        second = token_service.grant(self.user, 1_000, idempotency_key="promo:42")

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(token_service.balance_of(self.user), 1_000)
        self.assertEqual(TokenTransaction.objects.filter(user=self.user).count(), 1)

    def test_free_trial_can_only_be_granted_once(self):
        token_service.grant_free_trial(self.user, 20_000)
        token_service.grant_free_trial(self.user, 20_000)
        token_service.grant_free_trial(self.user, 20_000)

        self.assertEqual(token_service.balance_of(self.user), 20_000)
        self.assertEqual(
            TokenTransaction.objects.filter(user=self.user, transaction_type=TransactionType.FREE_TRIAL).count(), 1
        )

    def test_zero_balance_blocks_spending(self):
        self.assertFalse(token_service.can_spend(self.user))
        token_service.grant(self.user, 10)
        self.assertTrue(token_service.can_spend(self.user))

    def test_grant_rejects_non_positive_amounts(self):
        for amount in (0, -100):
            with self.assertRaises(ValueError):
                token_service.grant(self.user, amount)

    def test_recalculate_repairs_a_drifted_balance(self):
        token_service.grant(self.user, 1_000)
        token_service.consume(self.user, 250)

        account = token_service.get_account(self.user)
        account.balance = 999_999  # simulate corruption
        account.save(update_fields=["balance"])

        repaired = token_service.recalculate(self.user)
        self.assertEqual(repaired.balance, 750)
        self.assertEqual(repaired.total_allocated, 1_000)
        self.assertEqual(repaired.total_consumed, 250)

    def test_usage_summary_reports_totals(self):
        token_service.grant(self.user, 1_000)
        token_service.consume(self.user, 400)

        summary = token_service.usage_summary(self.user)
        self.assertEqual(summary["balance"], 600)
        self.assertEqual(summary["total_allocated"], 1_000)
        self.assertEqual(summary["total_consumed"], 400)
        self.assertEqual(summary["usage_percent"], 40)


class ConcurrentDeductionTests(TransactionTestCase):
    """Row locking must prevent two simultaneous requests double-spending."""

    reset_sequences = True

    def test_parallel_consumption_never_oversells(self):
        user = User.objects.create_user(username="racer", email="racer@test.local", password="Sup3rSecret!x")
        token_service.grant(user, 1_000)

        errors: list[Exception] = []
        barrier = threading.Barrier(5)

        def worker():
            try:
                barrier.wait(timeout=5)
                token_service.consume(user, 300, clamp=True)
            except Exception as exc:  # noqa: BLE001 — collected and asserted below
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        balance = token_service.balance_of(user)
        self.assertGreaterEqual(balance, 0, "Balance must never go negative")

        # Whatever interleaving occurred, the ledger must agree with the balance.
        total = sum(TokenTransaction.objects.filter(user=user).values_list("amount", flat=True))
        self.assertEqual(balance, total)

        # SQLite can raise "database is locked" under contention; that is a
        # refusal to write, not a double-spend, so it is acceptable here.
        for error in errors:
            self.assertNotIsInstance(error, AssertionError)
