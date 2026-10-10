"""The Cost & margin page and its CSV export.

The page is arithmetic on real rows, so the tests put known usage and a known
payment behind one model and check that every figure the page derives from
them — cost, margin, margin share, bars, ring — is the one a spreadsheet
would give. The export has to be the same table, and has to be audited.
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from AI.models import AIUsageLog, AuditAction, AuditLog, Payment, PaymentStatus, Plan
from AI.tests.test_multi_provider import GPT, SONNET, MultiProviderTestCase
from AI.views.console_insights import _margin_percent


class MarginPercentTests(MultiProviderTestCase):
    def test_no_revenue_means_no_share(self):
        self.assertIsNone(_margin_percent(Decimal("-1"), Decimal("0"), Decimal("1")))

    def test_a_cost_never_rounds_up_to_a_full_margin(self):
        self.assertEqual(_margin_percent(Decimal("37.88"), Decimal("38"), Decimal("0.12")), 99)
        self.assertEqual(_margin_percent(Decimal("38"), Decimal("38"), Decimal("0")), 100)

    def test_a_loss_is_negative(self):
        self.assertLess(_margin_percent(Decimal("-2"), Decimal("1"), Decimal("3")), 0)


class CostMarginPageTests(MultiProviderTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.url = reverse("console:model_costs")
        gpt = self.models[GPT]
        gpt.input_cost_per_1m = Decimal("0.25")
        gpt.output_cost_per_1m = Decimal("2.00")
        gpt.save(update_fields=["input_cost_per_1m", "output_cost_per_1m"])
        sonnet = self.models[SONNET]
        sonnet.input_cost_per_1m = Decimal("0")
        sonnet.output_cost_per_1m = Decimal("0")
        sonnet.save(update_fields=["input_cost_per_1m", "output_cost_per_1m"])
        # 1M input + 1M output on GPT: $0.25 + $2.00 = $2.25 of cost.
        AIUsageLog.objects.create(
            user=self.student, model_key=GPT, input_tokens=1_000_000, output_tokens=1_000_000,
            total_tokens=2_000_000,
        )
        plan = Plan.objects.create(
            name="Starter", tokens=1_000, ai_model=gpt, price=Decimal("38.00"), duration_days=30, is_active=True,
        )
        Payment.objects.create(
            user=self.student, plan=plan, amount=Decimal("38.00"), status=PaymentStatus.PAID, paid_at=timezone.now(),
        )

    def row(self, response, key):
        return next(row for row in response.context["rows"] if row["model"].key == key)

    def test_the_figures_are_the_arithmetic(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        gpt = self.row(response, GPT)
        self.assertTrue(gpt["priced"])
        self.assertEqual(gpt["cost"], Decimal("2.25"))
        self.assertEqual(gpt["revenue"], Decimal("38.00"))
        self.assertEqual(gpt["margin"], Decimal("35.75"))
        self.assertEqual(gpt["margin_percent"], 94)
        self.assertEqual(gpt["requests"], 1)
        self.assertEqual(gpt["cost_per_request"], Decimal("2.25"))
        # The busiest model draws a full bar, split by its token mix.
        self.assertAlmostEqual(gpt["in_pct"] + gpt["out_pct"], 100, delta=0.2)
        totals = response.context["totals"]
        self.assertEqual(totals["cost"], Decimal("2.25"))
        self.assertEqual(totals["margin"], Decimal("35.75"))
        self.assertEqual(totals["plans_sold"], 1)
        self.assertEqual(totals["per_request"], Decimal("2.25"))
        self.assertEqual(totals["priced"], totals["models"] - len(totals["unpriced"]))
        self.assertEqual(response.context["window_index"], 1)
        self.assertAlmostEqual(response.context["ring_length"], 345.6 * 0.94, delta=0.5)

    def test_an_unpriced_model_is_named_and_kept_out_of_the_totals(self):
        response = self.client.get(self.url)
        sonnet = self.row(response, SONNET)
        self.assertFalse(sonnet["priced"])
        self.assertIn(self.models[SONNET].name, response.context["totals"]["unpriced"])
        self.assertContains(response, "have no list price yet")
        self.assertContains(response, "unpriced")

    def test_the_page_renders_the_design(self):
        response = self.client.get(self.url)
        for text in (
            "What students paid against what the tokens they burned actually cost you.",
            "WINDOW", "MARGIN &middot; LAST 30 DAYS", "Revenue collected", "Estimated provider cost",
            "PER REQUEST", "PRICE COVERAGE", "TOTAL REVENUE", "TOKENS BILLED",
            "Per-model economics", "SORT BY", "TOKEN MIX", "Edit list price", "Export", "Refresh",
        ):
            self.assertContains(response, text)

    def test_the_window_is_clamped_and_reflected(self):
        self.assertEqual(self.client.get(self.url, {"days": "90"}).context["days"], 90)
        self.assertEqual(self.client.get(self.url, {"days": "9000"}).context["days"], 365)
        self.assertEqual(self.client.get(self.url, {"days": "x"}).context["days"], 30)

    def test_the_export_is_the_same_table_and_is_audited(self):
        response = self.client.get(reverse("console:model_costs_export"), {"days": "30"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("cost-margin-30d.csv", response["Content-Disposition"])
        reader = list(csv.reader(io.StringIO(response.content.decode())))
        header, body = reader[0], reader[1:]
        self.assertEqual(header[0], "model")
        gpt = next(row for row in body if row[0] == self.models[GPT].name)
        self.assertEqual(gpt[5], "2.2500")
        self.assertEqual(gpt[6], "38.00")
        self.assertEqual(gpt[8], "94")
        sonnet = next(row for row in body if row[0] == self.models[SONNET].name)
        self.assertEqual(sonnet[5], "unpriced")
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.DATA_EXPORT, new_value="model_costs").exists())

    def test_students_cannot_open_either(self):
        self.client.force_login(self.student)
        self.assertNotEqual(self.client.get(self.url).status_code, 200)
        self.assertNotEqual(self.client.get(reverse("console:model_costs_export")).status_code, 200)
