"""Payment settlement, idempotency, refunds and webhook authentication."""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from AI.models import (
    AccountStatus,
    AITokenTransaction,
    Payment,
    PaymentStatus,
    Plan,
    Subscription,
    TokenTransaction,
    TransactionType,
    User,
    WalletTransactionType,
)
from AI.services import payment_service, settings_service, token_service, wallet_service
from AI.services.payments import PaymentError, get_provider
from AI.services.payments.acleda_khqr import build_khqr
from AI.services.payments.base import VerificationResult

PASSWORD = "Sup3rSecret!x"


class PaymentSettlementTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.user = User.objects.create_user(
            username="buyer", email="buyer@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.plan = Plan.objects.create(
            name="Student", tokens=100_000, price=Decimal("10.00"), duration_days=30
        )

    def test_new_payment_starts_pending_and_credits_nothing(self):
        payment = payment_service.create_payment(self.user, self.plan, "aba_payway")

        self.assertEqual(payment.status, PaymentStatus.PENDING)
        self.assertFalse(payment.tokens_credited)
        self.assertEqual(payment.tokens_to_grant, 100_000)
        self.assertEqual(token_service.balance_of(self.user), 0)
        self.assertTrue(payment.reference.startswith("EPC-"))

    def test_settling_credits_tokens_and_activates_a_subscription(self):
        payment = payment_service.create_payment(self.user, self.plan, "aba_payway")
        payment_service.settle(payment, provider_txn_id="TXN-1", source="test")

        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.PAID)
        self.assertTrue(payment.tokens_credited)
        self.assertIsNotNone(payment.paid_at)
        self.assertEqual(token_service.balance_of(self.user), 100_000)

        subscription = Subscription.objects.get(user=self.user)
        self.assertEqual(subscription.plan, self.plan)
        self.assertTrue(subscription.is_live)
        self.assertEqual(subscription.tokens_granted, 100_000)

        # The purchase credits the wallet for the model the plan sells, and the
        # per-model ledger is keyed on the payment so a retried webhook is a no-op.
        token_model = self.plan.token_model
        wallet_entry = AITokenTransaction.objects.get(
            user=self.user, transaction_type=WalletTransactionType.PLAN_PURCHASE
        )
        self.assertEqual(wallet_entry.amount, 100_000)
        self.assertEqual(wallet_entry.ai_model, token_model)
        self.assertEqual(wallet_entry.reference_id, f"payment:{payment.pk}:{token_model.pk}")
        self.assertEqual(wallet_service.balance_of(self.user, token_model), 100_000)

        # And the legacy aggregate ledger still records it for the old screens.
        entry = TokenTransaction.objects.get(user=self.user, transaction_type=TransactionType.SUBSCRIPTION)
        self.assertEqual(entry.amount, 100_000)

    def test_settling_twice_credits_only_once(self):
        payment = payment_service.create_payment(self.user, self.plan, "binance")

        payment_service.settle(payment, source="webhook")
        payment_service.settle(payment, source="webhook-retry")
        payment_service.settle(payment, source="admin")

        self.assertEqual(token_service.balance_of(self.user), 100_000)
        self.assertEqual(
            TokenTransaction.objects.filter(user=self.user, transaction_type=TransactionType.SUBSCRIPTION).count(), 1
        )
        self.assertEqual(Subscription.objects.filter(user=self.user).count(), 1)

    def test_renewal_extends_from_the_current_expiry(self):
        first = payment_service.create_payment(self.user, self.plan, "aba_payway")
        payment_service.settle(first, source="test")
        first_subscription = Subscription.objects.get(payment=first)

        second = payment_service.create_payment(self.user, self.plan, "aba_payway")
        payment_service.settle(second, source="test")
        second_subscription = Subscription.objects.get(payment=second)

        self.assertEqual(second_subscription.starts_at, first_subscription.ends_at)
        self.assertGreater(second_subscription.ends_at, first_subscription.ends_at)
        self.assertEqual(token_service.balance_of(self.user), 200_000)

    def test_failed_payment_credits_nothing(self):
        payment = payment_service.create_payment(self.user, self.plan, "acleda_khqr")
        payment_service.mark_failed(payment, "Bank declined")

        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.FAILED)
        self.assertEqual(token_service.balance_of(self.user), 0)
        self.assertFalse(Subscription.objects.filter(user=self.user).exists())

    def test_cancelling_a_paid_payment_is_refused(self):
        payment = payment_service.create_payment(self.user, self.plan, "aba_payway")
        payment_service.settle(payment, source="test")

        with self.assertRaises(PaymentError):
            payment_service.cancel(payment)

    def test_refund_removes_tokens_and_cancels_the_subscription(self):
        payment = payment_service.create_payment(self.user, self.plan, "aba_payway")
        payment_service.settle(payment, source="test")
        self.assertEqual(token_service.balance_of(self.user), 100_000)

        payment_service.refund(payment, note="Duplicate order")

        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.REFUNDED)
        self.assertEqual(token_service.balance_of(self.user), 0)
        self.assertEqual(Subscription.objects.get(payment=payment).status, Subscription.Status.CANCELLED)

    def test_refund_is_idempotent(self):
        payment = payment_service.create_payment(self.user, self.plan, "aba_payway")
        payment_service.settle(payment, source="test")
        payment_service.refund(payment)

        with self.assertRaises(PaymentError):
            payment_service.refund(payment)

        self.assertEqual(
            AITokenTransaction.objects.filter(
                user=self.user, transaction_type=WalletTransactionType.REFUND
            ).count(),
            1,
        )

    def test_refunding_after_spending_clamps_at_zero(self):
        payment = payment_service.create_payment(self.user, self.plan, "aba_payway")
        payment_service.settle(payment, source="test")
        wallet_service.consume(self.user, self.plan.token_model, 90_000, reason='chat')

        payment_service.refund(payment)
        self.assertEqual(token_service.balance_of(self.user), 0)

    def test_inactive_plan_cannot_be_purchased(self):
        self.plan.is_active = False
        self.plan.save(update_fields=["is_active"])
        with self.assertRaises(PaymentError):
            payment_service.create_payment(self.user, self.plan, "aba_payway")

    def test_unknown_method_is_rejected(self):
        with self.assertRaises(PaymentError):
            payment_service.create_payment(self.user, self.plan, "bitcoin_cash_app")


class WebhookSecurityTests(TestCase):
    """A callback with no valid signature must never settle a payment."""

    def setUp(self):
        settings_service.bootstrap()
        self.user = User.objects.create_user(
            username="hooked", email="hooked@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.plan = Plan.objects.create(name="Basic", tokens=50_000, price=Decimal("5.00"), duration_days=30)
        self.payment = payment_service.create_payment(self.user, self.plan, "aba_payway")

    def test_unsigned_callback_is_rejected(self):
        response = self.client.post(
            "/webhooks/payments/aba_payway/",
            data=json.dumps({"tran_id": self.payment.reference, "status": "00"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.PENDING)
        self.assertEqual(token_service.balance_of(self.user), 0)

    def _pushback(self, times: int = 1):
        response = None
        for _ in range(times):
            response = self.client.post(
                "/webhooks/payments/aba_payway/",
                data=json.dumps({"tran_id": self.payment.reference, "status": 0, "apv": "APV123"}),
                content_type="application/json",
            )
        return response

    def _payway_says(self, status: str, amount=None):
        from AI.services.payments.aba_payway import ABAPayWayProvider

        result = VerificationResult(
            status=status, provider_txn_id="APV123",
            amount=str(self.payment.charge_amount if amount is None else amount),
            currency=self.payment.charge_currency,
        )
        return patch.object(ABAPayWayProvider, "check_transaction", return_value=result)

    def test_pushback_is_not_trusted_without_payway_confirmation(self):
        with self.settings(ABA_PAYWAY_MERCHANT_ID="merchant", ABA_PAYWAY_API_KEY="realkey"), \
                self._payway_says("pending"):
            response = self._pushback()
        self.assertEqual(response.status_code, 200)

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.PENDING)
        self.assertEqual(token_service.balance_of(self.user), 0)

    def test_pushback_confirmed_by_payway_settles_the_payment(self):
        with self.settings(ABA_PAYWAY_MERCHANT_ID="merchant", ABA_PAYWAY_API_KEY="realkey"), \
                self._payway_says("paid"):
            response = self._pushback()

        self.assertEqual(response.status_code, 200)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.PAID)
        self.assertEqual(token_service.balance_of(self.user), 50_000)

    def test_confirmation_for_the_wrong_amount_does_not_settle(self):
        with self.settings(ABA_PAYWAY_MERCHANT_ID="merchant", ABA_PAYWAY_API_KEY="realkey"), \
                self._payway_says("paid", amount="100"):
            self._pushback()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.PENDING)
        self.assertEqual(token_service.balance_of(self.user), 0)

    def test_replayed_webhook_does_not_double_credit(self):
        with self.settings(ABA_PAYWAY_MERCHANT_ID="merchant", ABA_PAYWAY_API_KEY="realkey"), \
                self._payway_says("paid"):
            self._pushback(times=3)

        self.assertEqual(token_service.balance_of(self.user), 50_000)
        self.assertEqual(Subscription.objects.filter(user=self.user).count(), 1)

    def test_webhook_for_an_unknown_reference_is_acknowledged_and_ignored(self):
        with self.settings(ABA_PAYWAY_MERCHANT_ID="merchant", ABA_PAYWAY_API_KEY="realkey"), \
                self._payway_says("paid"):
            response = self.client.post(
                "/webhooks/payments/aba_payway/",
                data=json.dumps({"tran_id": "EPC-000000-NOPE"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"ok": True})
        self.assertEqual(token_service.balance_of(self.user), 0)


class ProviderVerificationTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.user = User.objects.create_user(
            username="verifier", email="verifier@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.plan = Plan.objects.create(name="Pro", tokens=75_000, price=Decimal("8.00"), duration_days=30)
        self.payment = payment_service.create_payment(self.user, self.plan, "binance")

    def test_frontend_claims_alone_never_settle_a_payment(self):
        """Hitting the status endpoint must not mark anything paid on its own."""
        self.client.force_login(self.user)
        with patch.object(type(get_provider("binance")), "verify",
                          return_value=VerificationResult(status="pending")):
            response = self.client.get(f"/payments/{self.payment.reference}/status/")

        self.assertEqual(response.status_code, 200)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.PENDING)
        self.assertEqual(token_service.balance_of(self.user), 0)

    def test_provider_confirmation_settles_the_payment(self):
        with patch.object(type(get_provider("binance")), "verify",
                          return_value=VerificationResult(status="paid", provider_txn_id="BN-9")):
            payment_service.verify_payment(self.payment)

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.PAID)
        self.assertEqual(self.payment.provider_txn_id, "BN-9")
        self.assertEqual(token_service.balance_of(self.user), 75_000)

    def test_uploading_proof_does_not_credit_tokens(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from io import BytesIO
        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (60, 60), "white").save(buffer, format="PNG")
        buffer.seek(0)

        self.client.force_login(self.user)
        response = self.client.post(
            f"/payments/{self.payment.reference}/proof/",
            {"proof": SimpleUploadedFile("receipt.png", buffer.read(), content_type="image/png")},
        )

        self.assertEqual(response.status_code, 302)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.PENDING)
        self.assertFalse(self.payment.tokens_credited)
        self.assertEqual(token_service.balance_of(self.user), 0)
        self.assertTrue(self.payment.proof)


class KHQRPayloadTests(TestCase):
    def test_payload_is_well_formed(self):
        payload = build_khqr(
            bakong_id="epc@aclb", merchant_name="EPC AI ASSISTANT", city="Phnom Penh",
            amount=Decimal("10.00"), currency="USD", bill_number="EPC-250101-ABCD",
        )
        self.assertTrue(payload.startswith("000201"))
        self.assertIn("5303840", payload)   # USD currency code
        self.assertIn("540510.00", payload)  # amount field
        self.assertIn("6304", payload)
        self.assertEqual(len(payload[-4:]), 4)
        int(payload[-4:], 16)  # CRC must be valid hexadecimal
