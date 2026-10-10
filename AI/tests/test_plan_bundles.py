"""A plan that includes more than one model.

Tokens are a separate currency per model, so a bundle is not one bigger number —
it is several balances credited at once. The things that can go wrong are
crediting the wrong wallet, crediting one twice, or letting a bundle quietly
break the rule that per-model revenue adds up to total revenue.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from AI.models import (
    AccountStatus,
    AIModel,
    AIProvider,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Plan,
    PlanModelGrant,
    Role,
    StaffRole,
)
from AI.services import analytics_service, model_scope, payment_service, settings_service, wallet_service

User = get_user_model()


class PlanBundleTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        vendor = AIProvider.objects.create(slug="bun", name="Bundle Vendor")
        self.gpt = AIModel.objects.create(
            key="bun-gpt", name="Bun GPT", provider="openai", provider_ref=vendor, sort_order=1
        )
        self.claude = AIModel.objects.create(
            key="bun-claude", name="Bun Claude", provider="anthropic",
            provider_ref=vendor, sort_order=2,
        )
        self.extra = AIModel.objects.create(
            key="bun-extra", name="Bun Extra", provider="openai", provider_ref=vendor, sort_order=3
        )
        self.plan = Plan.objects.create(
            name="Bundle Pro", slug="bundle-pro", tokens=100_000,
            price=Decimal("30.00"), ai_model=self.gpt,
        )
        self.student = User.objects.create_user(
            username="bun_student", email="s@bun.test", password="x",
            role=Role.STUDENT, status=AccountStatus.ACTIVE,
        )
        self.admin = User.objects.create_user(
            username="bun_admin", email="a@bun.test", password="pw",
            role=Role.ADMIN, staff_role=StaffRole.OWNER, status=AccountStatus.ACTIVE, is_staff=True,
        )

    def bundle(self):
        PlanModelGrant.objects.create(plan=self.plan, ai_model=self.claude, tokens=50_000)
        PlanModelGrant.objects.create(plan=self.plan, ai_model=self.extra, tokens=25_000)

    # -- what a plan says it includes --------------------------------------
    def test_a_single_model_plan_reports_one_row(self):
        rows = self.plan.grant_rows
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], self.gpt)
        self.assertEqual(rows[0]["tokens"], 100_000)
        self.assertTrue(rows[0]["is_primary"])
        self.assertFalse(self.plan.is_bundle)

    def test_a_bundle_lists_every_model_primary_first(self):
        self.bundle()
        rows = self.plan.grant_rows
        self.assertEqual([row["model"] for row in rows], [self.gpt, self.claude, self.extra])
        self.assertTrue(rows[0]["is_primary"])
        self.assertFalse(rows[1]["is_primary"])
        self.assertTrue(self.plan.is_bundle)
        self.assertEqual(self.plan.total_tokens, 175_000)

    def test_a_grant_for_the_plans_own_model_is_not_counted_twice(self):
        """It would otherwise credit that model both from `tokens` and here."""
        PlanModelGrant.objects.create(plan=self.plan, ai_model=self.gpt, tokens=999)
        rows = self.plan.grant_rows
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tokens"], 100_000)

    def test_a_zero_grant_is_not_listed(self):
        PlanModelGrant.objects.create(plan=self.plan, ai_model=self.claude, tokens=0)
        self.assertEqual(len(self.plan.grant_rows), 1)

    # -- buying one --------------------------------------------------------
    def buy(self):
        payment = payment_service.create_payment(
            self.student, self.plan, PaymentMethod.ABA_PAYWAY
        )
        return payment_service.settle(payment, actor=self.admin, source="test")

    def test_buying_a_bundle_credits_every_model_separately(self):
        self.bundle()
        self.buy()
        self.assertEqual(wallet_service.balance_of(self.student, self.gpt), 100_000)
        self.assertEqual(wallet_service.balance_of(self.student, self.claude), 50_000)
        self.assertEqual(wallet_service.balance_of(self.student, self.extra), 25_000)

    def test_buying_a_single_model_plan_is_unchanged(self):
        self.buy()
        self.assertEqual(wallet_service.balance_of(self.student, self.gpt), 100_000)
        self.assertEqual(wallet_service.balance_of(self.student, self.claude), 0)

    def test_settling_twice_credits_each_wallet_once(self):
        """The reference key includes the model, so a repeat is a no-op."""
        self.bundle()
        payment = payment_service.create_payment(
            self.student, self.plan, PaymentMethod.ABA_PAYWAY
        )
        payment_service.settle(payment, actor=self.admin, source="test")
        for row in self.plan.grant_rows:
            wallet_service.credit_payment(payment, row["model"], tokens=row["tokens"])

        self.assertEqual(wallet_service.balance_of(self.student, self.gpt), 100_000)
        self.assertEqual(wallet_service.balance_of(self.student, self.claude), 50_000)

    # -- the sum rule survives ---------------------------------------------
    def test_a_bundle_does_not_break_per_model_revenue(self):
        """One sale, one plan, one model's revenue — never split three ways.

        Splitting $30 across three models would be an invention: nobody bought
        them separately. So the money stays with the plan's primary model, and
        selecting each model in turn still adds up to the total.
        """
        self.bundle()
        self.buy()

        whole = analytics_service.overview()["revenue_total"]
        parts = sum(
            analytics_service.overview(
                model_scope.ModelScope(m, tuple(model_scope.catalogue()))
            )["revenue_total"]
            for m in AIModel.objects.all()
        )
        self.assertEqual(parts, whole)
        # And it is attributed to the model the plan is sold for.
        gpt_only = analytics_service.overview(
            model_scope.ModelScope(self.gpt, ())
        )["revenue_total"]
        self.assertEqual(gpt_only, Decimal("30.00"))

    # -- the console form --------------------------------------------------
    def test_the_plan_form_saves_and_clears_bundle_amounts(self):
        self.client.force_login(self.admin)
        payload = {
            "name": "Bundle Pro", "description": "",
            "ai_model": str(self.gpt.pk), "tokens": "100000",
            "price": "30.00", "currency": "USD", "duration_days": "30",
            "daily_limit": "0", "weekly_limit": "0", "monthly_limit": "0",
            "features": "", "is_active": "on", "display_order": "0",
            f"bundle_{self.claude.pk}": "50000",
            f"bundle_{self.extra.pk}": "0",
        }
        response = self.client.post(
            reverse("console:plan_edit", args=[self.plan.pk]), payload, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.plan.refresh_from_db()
        self.assertEqual(
            {(g.ai_model_id, g.tokens) for g in self.plan.model_grants.all()},
            {(self.claude.pk, 50_000)},
        )

        # Setting it back to zero removes the row rather than leaving a stale one.
        payload[f"bundle_{self.claude.pk}"] = "0"
        self.client.post(reverse("console:plan_edit", args=[self.plan.pk]), payload, follow=True)
        self.assertEqual(self.plan.model_grants.count(), 0)

    def test_the_form_ignores_a_bundle_entry_for_the_plans_own_model(self):
        self.client.force_login(self.admin)
        payload = {
            "name": "Bundle Pro", "description": "",
            "ai_model": str(self.gpt.pk), "tokens": "100000",
            "price": "30.00", "currency": "USD", "duration_days": "30",
            "daily_limit": "0", "weekly_limit": "0", "monthly_limit": "0",
            "features": "", "is_active": "on", "display_order": "0",
            f"bundle_{self.gpt.pk}": "12345",
        }
        self.client.post(reverse("console:plan_edit", args=[self.plan.pk]), payload, follow=True)
        self.assertFalse(
            self.plan.model_grants.filter(ai_model=self.gpt).exists(),
            "a plan must not grant its own model twice",
        )

    def test_the_plans_page_lists_every_included_model(self):
        self.bundle()
        self.client.force_login(self.admin)
        html = self.client.get(reverse("console:plans")).content.decode()
        self.assertIn("Bun GPT", html)
        self.assertIn("Bun Claude", html)
        self.assertIn("Bun Extra", html)
