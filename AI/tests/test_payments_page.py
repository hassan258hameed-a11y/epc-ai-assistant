"""The Payments page and its inline actions.

The page confirms, declines and refunds from a row and comes back to itself
with a toast, so the tests prove the three actions reach the same backend the
detail page uses, that the round trip only follows a local URL, and that the
queue figures the page draws are the ledger's.
"""
from __future__ import annotations

from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from AI.models import Payment, PaymentMethod, PaymentStatus, Plan
from AI.tests.test_multi_provider import GPT, MultiProviderTestCase


class PaymentsPageTests(MultiProviderTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.url = reverse("console:payments")
        self.fund_pool(GPT)
        self.plan = Plan.objects.create(
            name="Starter", tokens=500, ai_model=self.models[GPT], price=Decimal("3.00"), duration_days=30, is_active=True,
        )
        self.pending = Payment.objects.create(
            user=self.student, plan=self.plan, amount=Decimal("3.00"), method=PaymentMethod.BINANCE,
            status=PaymentStatus.PENDING, tokens_to_grant=500,
        )
        self.paid = Payment.objects.create(
            user=self.student, plan=self.plan, amount=Decimal("10.00"), method=PaymentMethod.ABA_PAYWAY,
            status=PaymentStatus.PAID, tokens_to_grant=500, paid_at=timezone.now(),
        )
        self.failed = Payment.objects.create(
            user=self.student, plan=self.plan, amount=Decimal("3.00"), method=PaymentMethod.ACLEDA_KHQR,
            status=PaymentStatus.FAILED, tokens_to_grant=500,
        )

    def test_the_page_draws_the_queue_from_the_ledger(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        totals = response.context["totals"]
        self.assertEqual(totals["revenue"], Decimal("10.00"))
        self.assertEqual(totals["count"], 1)
        self.assertEqual(totals["pending"], 1)
        self.assertEqual(totals["declined"], 1)
        self.assertEqual(totals["all"], 3)
        for text in (
            "Confirm what landed, release the tokens, and keep refunds tidy.",
            "CONFIRMED REVENUE", "PROVIDER STATUS", "AWAITING REVIEW", "MATCHING ORDERS", "DECLINED",
            "1 awaiting you", "Confirm &amp; release", "Refund", "TOKENS ON RELEASE",
            "1 payment still awaiting confirmation",
        ):
            self.assertContains(response, text)
        states = {p.reference: p.state for p in response.context["page"].object_list}
        self.assertEqual(states[self.pending.reference], "pending")
        self.assertEqual(states[self.paid.reference], "paid")
        self.assertEqual(states[self.failed.reference], "declined")
        split = {row["key"]: row for row in response.context["method_split"]}
        self.assertEqual(split[PaymentMethod.ABA_PAYWAY]["share"], 100.0)

    def test_filters_narrow_the_list_and_mark_it_dirty(self):
        response = self.client.get(self.url, {"status": "pending"})
        self.assertEqual([p.pk for p in response.context["page"].object_list], [self.pending.pk])
        self.assertTrue(response.context["dirty"])
        self.assertContains(response, "under the current filters")
        response = self.client.get(self.url, {"q": self.paid.reference[-6:]})
        self.assertEqual([p.pk for p in response.context["page"].object_list], [self.paid.pk])

    def test_confirming_from_the_row_releases_and_comes_back_with_a_toast(self):
        before = self.balances()[GPT]
        response = self.client.post(
            reverse("console:payment_action", args=[self.pending.reference]),
            {"action": "confirm", "next": self.url + "?status=pending"},
        )
        self.assertRedirects(response, self.url + "?status=pending", fetch_redirect_response=False)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, PaymentStatus.PAID)
        self.assertEqual(self.balances()[GPT] - before, 500)
        page = self.client.get(self.url)
        toast = page.context["toast"]
        self.assertEqual(toast["kind"], "ok")
        self.assertIn("500 tokens released", toast["text"])
        self.assertIsNone(self.client.get(self.url).context["toast"], "shown once")

    def test_declining_and_refunding_from_the_row(self):
        response = self.client.post(
            reverse("console:payment_action", args=[self.pending.reference]), {"action": "reject", "next": self.url},
        )
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, PaymentStatus.FAILED)
        self.assertEqual(self.client.get(self.url).context["toast"]["kind"], "bad")

        # A paid one is refunded from its row; the tokens come back.
        self.give(GPT, 500)
        self.paid.tokens_credited = True
        self.paid.save(update_fields=["tokens_credited"])
        response = self.client.post(
            reverse("console:payment_action", args=[self.paid.reference]), {"action": "refund", "next": self.url},
        )
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.paid.refresh_from_db()
        self.assertEqual(self.paid.status, PaymentStatus.REFUNDED)
        self.assertEqual(self.client.get(self.url).context["toast"]["kind"], "warn")

    def test_only_a_local_next_is_followed(self):
        response = self.client.post(
            reverse("console:payment_action", args=[self.pending.reference]),
            {"action": "confirm", "next": "https://evil.example/steal"},
        )
        self.assertRedirects(
            response, reverse("console:payment_detail", args=[self.pending.reference]), fetch_redirect_response=False,
        )

    def test_without_next_the_detail_page_flow_is_unchanged(self):
        response = self.client.post(
            reverse("console:payment_action", args=[self.pending.reference]), {"action": "confirm"},
        )
        self.assertRedirects(
            response, reverse("console:payment_detail", args=[self.pending.reference]), fetch_redirect_response=False,
        )
        self.assertIsNone(self.client.get(self.url).context["toast"])

    def test_students_cannot_open_it(self):
        self.client.force_login(self.student)
        self.assertNotEqual(self.client.get(self.url).status_code, 200)
