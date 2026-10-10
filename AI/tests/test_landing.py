"""The landing page: what it claims, and where the claims come from.

A marketing page is the one place where an invented number is cheapest to
write and most expensive to be wrong about, so every figure it prints is
tested against the thing it is drawn from — the catalogue, the settings, the
payment methods and the real answer times.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from AI.models import AIUsageLog, PaymentMethod, Plan
from AI.services import settings_service
from AI.tests.test_multi_provider import GPT, MultiProviderTestCase
from AI.views.public import TOKENS_PER_QUESTION, plan_cards, typical_reply


class PlanCardTests(TestCase):
    def make(self, name, tokens, price, **extra):
        return Plan.objects.create(
            name=name, tokens=tokens, price=Decimal(price), duration_days=30, is_active=True, **extra
        )

    def test_fit_is_a_rank_over_whatever_the_catalogue_holds(self):
        small = self.make("Small", 50_000, "1.99")
        mid = self.make("Mid", 100_000, "2.49", is_featured=True)
        big = self.make("Big", 500_000, "3.00")
        cards = {card["plan"].pk: card for card in plan_cards([small, mid, big])}
        self.assertEqual(cards[small.pk]["fit"], "Light")
        self.assertEqual(cards[mid.pk]["fit"], "Regular")
        self.assertEqual(cards[big.pk]["fit"], "Heavy")

    def test_the_meter_is_a_share_of_the_biggest_plan_on_the_page(self):
        small = self.make("Small", 50_000, "1.99")
        big = self.make("Big", 500_000, "3.00")
        cards = {card["plan"].pk: card for card in plan_cards([small, big])}
        self.assertEqual(cards[big.pk]["meter"], 100)
        self.assertEqual(cards[small.pk]["meter"], 10)

    def test_questions_and_price_are_formatted_from_the_plan(self):
        plan = self.make("Standard", 100_000, "2.49")
        card = plan_cards([plan])[0]
        self.assertEqual(card["questions"], f"{100_000 // TOKENS_PER_QUESTION:,}")
        self.assertEqual(card["price"], "$2.49")

    def test_the_tag_names_what_makes_the_plan_worth_a_look(self):
        small = self.make("Small", 50_000, "1.99")
        featured = self.make("Mid", 100_000, "2.49", is_featured=True)
        big = self.make("Big", 500_000, "3.00")
        cards = {card["plan"].pk: card for card in plan_cards([small, featured, big])}
        self.assertEqual(cards[small.pk]["tag"], "STARTER")
        self.assertEqual(cards[featured.pk]["tag"], "MOST POPULAR")
        self.assertEqual(cards[big.pk]["tag"], "BIGGEST BALANCE")
        self.assertEqual(cards[featured.pk]["ribbon"], "MOST POPULAR")
        self.assertEqual(cards[big.pk]["ribbon"], "BEST FIT")

    def test_an_empty_catalogue_draws_no_cards(self):
        self.assertEqual(plan_cards([]), [])


class TypicalReplyTests(MultiProviderTestCase):
    def setUp(self):
        super().setUp()
        cache.delete("landing_typical_reply")

    def tearDown(self):
        cache.delete("landing_typical_reply")
        super().tearDown()

    def log(self, latency, **extra):
        return AIUsageLog.objects.create(
            user=self.student, model="gpt", model_key=GPT, latency_ms=latency, success=True, **extra
        )

    def test_too_little_history_is_never_dressed_up_as_a_measurement(self):
        for _ in range(5):
            self.log(1200)
        answer = typical_reply()
        self.assertFalse(answer["measured"])
        self.assertEqual(answer["value"], "<5s")

    def test_the_figure_is_the_median_rounded_up_to_the_second(self):
        for latency in [1000] * 10 + [9000] * 5:
            self.log(latency)
        answer = typical_reply()
        self.assertTrue(answer["measured"])
        self.assertEqual(answer["value"], "<1s")
        self.assertIn("median of 15 answers", answer["sub"])

    def test_failures_and_ancient_history_are_left_out(self):
        for _ in range(12):
            self.log(1000)
        old = self.log(30_000)
        AIUsageLog.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=200))
        AIUsageLog.objects.create(
            user=self.student, model="gpt", model_key=GPT, latency_ms=99_000, success=False
        )
        self.assertEqual(typical_reply()["sub"], "median of 12 answers")


class LandingPageTests(MultiProviderTestCase):
    def setUp(self):
        super().setUp()
        cache.delete("landing_typical_reply")
        self.url = reverse("public:home")
        # The page shows the first three plans in the catalogue, so this test
        # owns the catalogue rather than guessing which three the fixture left.
        Plan.objects.all().delete()
        self.small = Plan.objects.create(
            name="GPT-5 Mini — Starter", tokens=50_000, price=Decimal("1.99"), duration_days=365,
            is_active=True, description="A few weeks of everyday study.",
            features="50,000 tokens, and they never expire\nFull chat, voice and file support",
        )
        self.mid = Plan.objects.create(
            name="GPT-5 Mini — Standard", tokens=100_000, price=Decimal("2.49"), duration_days=365,
            is_active=True, is_featured=True, display_order=1,
        )
        self.big = Plan.objects.create(
            name="Starter — 500K", tokens=500_000, price=Decimal("3.00"), duration_days=30,
            is_active=True, display_order=2,
        )

    def tearDown(self):
        cache.delete("landing_typical_reply")
        super().tearDown()

    def test_the_page_renders_the_design(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        for text in (
            "Study help that", "actually explains", "Built for", "EVERY SUBJECT, EVERY NIGHT",
            "WHAT YOU GET", "One assistant for", "HOW IT WORKS", "Three steps to your",
            "PLANS", "Pay for", "How much do you study?", "Compare every plan",
            "FAQ", "Questions,", "Your next study session", "See the plans",
            "TYPICAL REPLY", "Homework, explained", "Any hour, any day",
        ):
            self.assertContains(response, text)

    def test_the_plans_on_the_page_are_the_catalogue(self):
        response = self.client.get(self.url)
        cards = response.context["plan_cards"]
        self.assertEqual([card["plan"].pk for card in cards], [self.small.pk, self.mid.pk, self.big.pk])
        self.assertContains(response, "GPT-5 Mini — Starter")
        self.assertContains(response, "$1.99")
        self.assertContains(response, "A few weeks of everyday study.")
        self.assertContains(response, "50,000 tokens, and they never expire")
        # The middle plan is the one recommended before anybody touches the
        # switcher, so exactly one card starts marked.
        self.assertEqual(response.content.decode().count('class="lp-plan is-rec"'), 1)

    def test_a_plan_added_later_still_gets_a_fit_and_a_meter(self):
        Plan.objects.all().delete()
        Plan.objects.create(name="Only one", tokens=10_000, price=Decimal("1.00"), duration_days=30, is_active=True)
        response = self.client.get(self.url)
        card = response.context["plan_cards"][0]
        self.assertEqual(card["fit"], "Regular")
        self.assertEqual(card["meter"], 100)

    def test_the_numbers_it_quotes_come_from_settings_and_the_model(self):
        settings_service.set_value("free_trial_tokens", 750)
        response = self.client.get(self.url)
        self.assertEqual(response.context["free_trial_tokens"], 750)
        # The trial is paid in the default model, and the page says which.
        self.assertContains(response, "750 free GPT-5 Mini tokens")
        methods = [label for _value, label in PaymentMethod.choices]
        self.assertEqual(response.context["payment_methods"], methods)
        for label in methods:
            self.assertContains(response, label)
        self.assertContains(response, f"{len(methods)} methods")

    def test_the_faq_answers_agree_with_the_page(self):
        response = self.client.get(self.url)
        questions = [faq["q"] for faq in response.context["faqs"]]
        self.assertIn("What is a token?", questions)
        self.assertIn("Which payment methods do you accept?", questions)
        token_answer = response.context["faqs"][0]["a"]
        # The FAQ quotes the biggest plan on the page, at the same
        # tokens-per-question the cards are drawn with.
        biggest = max(card["plan"].tokens for card in response.context["plan_cards"])
        self.assertEqual(biggest, self.big.tokens)
        self.assertIn(f"{biggest // TOKENS_PER_QUESTION:,} questions", token_answer)

    def test_no_plans_leaves_the_page_standing(self):
        Plan.objects.all().delete()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["plan_cards"], [])
        self.assertContains(response, "Create an account to get started")

    def test_a_visitor_is_shown_the_way_in_and_a_student_the_way_back(self):
        anonymous = self.client.get(self.url)
        self.assertContains(anonymous, reverse("auth:signup"))
        self.assertContains(anonymous, "Sign in")

        self.client.force_login(self.student)
        student = self.client.get(self.url)
        self.assertContains(student, reverse("student:chat"))
        self.assertContains(student, "Open dashboard")


class PublicPlansReachabilityTests(MultiProviderTestCase):
    """`/plans/` is the one canonical plans URL, for everyone."""

    def test_a_logged_out_visitor_reaches_the_marketing_page(self):
        response = self.client.get("/plans/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "public/plans.html")

    def test_a_student_is_taken_to_their_own_pricing_panel(self):
        self.client.force_login(self.student)
        response = self.client.get("/plans/?page=2")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/app/pricing/?page=2")

    def test_an_administrator_sees_the_marketing_page(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/plans/").status_code, 200)
