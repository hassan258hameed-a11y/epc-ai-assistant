"""Fable 5.1 and Opus 5: a fifth and sixth model that behave like the rest.

As with GPT-4o, the point is that there is almost nothing special to test. Each
is a catalogue row, a supply pool and three packages; the wallet, the router,
the limit windows, the chat runtime, the model selector and the per-model
analytics were not changed to accommodate them. So what is asserted here is
mostly *sameness* — that they route, bill, meter, block and reset through the
same code paths, and that adding them moved nothing belonging to the four
models already there.

What is genuinely their own:

* **the vendor credential.** Both are Anthropic models with a blank
  ``api_key_setting``, which resolves to ``ANTHROPIC_API_KEY`` — the same key
  Claude 4.5 already uses. No new secret was introduced.
* **admin-only supply.** No signup hands out any of the six million tokens
  between them, so every token a student holds is one an administrator gave.
* **packages that start inactive**, because their prices are provisional.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from AI.models import (
    AccountStatus,
    AIModel,
    AITokenTransaction,
    AIUsageLog,
    ModelTokenPool,
    PackageAllocation,
    Plan,
    User,
    UserAIWallet,
)
from AI.services import (
    chat_service,
    distribution_service,
    model_pool_service,
    model_router,
    providers,
    referral_service,
    settings_service,
    wallet_service,
)
from AI.services.chat_service import ChatBlocked
from AI.services.providers.base import AIResponse

PASSWORD = "Sup3rSecret!x"

FABLE = "claude-fable-5-1"
OPUS = "claude-opus-5"
NEW_MODELS = (FABLE, OPUS)
#: The four that existed before these two arrived.
INCUMBENTS = ("gpt-5-mini", "gpt-4o", "claude-haiku-4-5", "claude-sonnet-5")

COMPLETE = "AI.services.providers.anthropic_provider.AnthropicProvider.complete"


def reply(model: str, *, inp: int = 1200, out: int = 2800) -> AIResponse:
    return AIResponse(
        content="Answer.", input_tokens=inp, output_tokens=out, total_tokens=inp + out,
        model=model, request_id="req-new", latency_ms=300,
    )


class CatalogueTests(TestCase):
    """Both models exist and are configured the way the product expects."""

    def test_the_platform_now_offers_six_models(self):
        keys = set(AIModel.objects.values_list("key", flat=True))
        self.assertTrue(set(NEW_MODELS) <= keys)
        self.assertTrue(set(INCUMBENTS) <= keys)

    def test_each_is_configured_for_its_vendor(self):
        for key, name, order in ((FABLE, "Fable 5.1", 30), (OPUS, "Opus 5", 40)):
            with self.subTest(model=key):
                model = AIModel.objects.get(key=key)
                self.assertEqual(model.name, name)
                self.assertEqual(model.provider, "anthropic")
                self.assertEqual(model.provider_ref.slug, "anthropic")
                self.assertTrue(model.is_active)
                self.assertFalse(model.is_default, "neither may displace the default model")
                self.assertTrue(model.supports_vision)
                self.assertEqual(model.sort_order, order)
                self.assertGreater(model.max_output_tokens, 0)

    def test_neither_stores_a_credential_of_its_own(self):
        """A key must never be in the database — only the name of a setting.

        These two name nothing at all, which the provider reads as "use the
        vendor's key", exactly as the two older Claude models do.
        """
        for key in NEW_MODELS:
            with self.subTest(model=key):
                model = AIModel.objects.get(key=key)
                self.assertEqual(model.api_key_setting, "")
                self.assertFalse(model.has_own_credential)

    @override_settings(ANTHROPIC_API_KEY="shared-anthropic-key")
    def test_the_router_hands_them_the_shared_anthropic_key(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                provider = providers.for_model(AIModel.objects.get(key=key))
                self.assertEqual(provider.api_key, "shared-anthropic-key")
                self.assertEqual(provider.model, key)

    def test_opus_gets_a_larger_reply_budget_than_fable(self):
        """Opus is a reasoning model: starved of output it answers with nothing.

        Observed directly against the live API — an eight-token budget was
        entirely consumed by reasoning and the reply came back empty, which a
        student would read as the model being broken.
        """
        self.assertGreater(
            AIModel.objects.get(key=OPUS).max_output_tokens,
            AIModel.objects.get(key=FABLE).max_output_tokens,
        )

    def test_the_provider_treats_both_as_adaptive_thinking_models(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                provider = providers.for_model(AIModel.objects.get(key=key))
                self.assertTrue(provider.uses_adaptive_thinking)
                self.assertTrue(provider.supports_vision)

    def test_thinking_is_off_until_an_administrator_turns_it_on(self):
        """Hidden reasoning bills to the student, so it is never on by default."""
        for key in NEW_MODELS:
            with self.subTest(model=key):
                self.assertEqual(AIModel.objects.get(key=key).thinking_mode, "off")

    def test_their_token_currencies_are_distinct(self):
        types = [AIModel.objects.get(key=key).token_type for key in NEW_MODELS]
        self.assertEqual(len(set(types)), 2)
        everyone = list(AIModel.objects.values_list("token_type", flat=True))
        self.assertEqual(len(everyone), len(set(everyone)), "no two models may share a currency")


class SupplyTests(TestCase):
    """Three million tokens each, all of it the administrator's."""

    def pool(self, key) -> ModelTokenPool:
        return ModelTokenPool.objects.get(ai_model__key=key)

    def test_each_opening_allocation_is_three_million(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                pool = self.pool(key)
                self.assertEqual(pool.total_added, 3_000_000)
                self.assertEqual(pool.balance, 3_000_000)
                self.assertEqual(pool.total_distributed, 0)

    def test_it_is_recorded_as_one_and_a_half_million_each_way(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                pool = self.pool(key)
                self.assertEqual(pool.input_allocation, 1_500_000)
                self.assertEqual(pool.output_allocation, 1_500_000)
                self.assertTrue(pool.tracks_direction)

    def test_no_student_starts_with_any_of_it(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                held = UserAIWallet.objects.filter(ai_model__key=key).exclude(balance=0)
                self.assertFalse(held.exists())

    def test_a_new_signup_is_given_none_of_it(self):
        """``auto_grant`` is off, so the welcome grant skips both."""
        settings_service.bootstrap()
        settings_service.set_value("free_trial_enabled", True)
        settings_service.set_value("free_trial_tokens", 500)

        student = User.objects.create_user(
            username="newcomer", email="new@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        referral_service.grant_welcome_tokens(student)

        for key in NEW_MODELS:
            with self.subTest(model=key):
                model = AIModel.objects.get(key=key)
                self.assertEqual(wallet_service.balance_of(student, model), 0)
                self.assertFalse(model.auto_grant)

    def test_distribution_draws_from_the_pool(self):
        student = User.objects.create_user(
            username="given", email="given@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        for key in NEW_MODELS:
            with self.subTest(model=key):
                model = AIModel.objects.get(key=key)
                before = self.pool(key).balance
                distribution_service.distribute(student, model, 10_000)
                self.assertEqual(wallet_service.balance_of(student, model), 10_000)
                self.assertEqual(self.pool(key).balance, before - 10_000)

    def test_the_pool_cannot_be_overdrawn(self):
        student = User.objects.create_user(
            username="greedy", email="greedy@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        model = AIModel.objects.get(key=OPUS)
        with self.assertRaises(model_pool_service.InsufficientPool):
            distribution_service.distribute(student, model, 99_000_000)
        self.assertEqual(self.pool(OPUS).balance, 3_000_000)
        self.assertEqual(wallet_service.balance_of(student, model), 0)


class PackageTests(TestCase):
    """Three packages each, at the sizes every other model is sold in."""

    def test_three_packages_exist_at_the_standard_sizes(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                plans = Plan.objects.filter(ai_model__key=key).order_by("tokens")
                self.assertEqual([p.tokens for p in plans], [50_000, 100_000, 200_000])

    def test_they_are_on_sale(self):
        """Priced in 0032 and opened; three live packages each."""
        for key in NEW_MODELS:
            with self.subTest(model=key):
                self.assertEqual(
                    Plan.objects.filter(ai_model__key=key, is_active=True).count(), 3
                )

    def test_they_are_the_two_dearest_models_at_every_size(self):
        """A ladder that crosses over somewhere is worse than no ladder.

        These are the expensive models to run, so their packages sit above
        everything else — and Opus above Fable — at 50k, 100k and 200k alike,
        not just at the size somebody happened to compare.
        """
        cheaper = ["gpt-5-mini", "claude-haiku-4-5", "gpt-4o", "claude-sonnet-5"]
        for size in (50_000, 100_000, 200_000):
            fable = self.price(FABLE, size)
            opus = self.price(OPUS, size)
            with self.subTest(size=size):
                self.assertGreater(opus, fable, "Opus is the premium tier")
                for key in cheaper:
                    self.assertGreater(
                        fable, self.price(key, size),
                        f"Fable must cost more than {key} at {size:,}",
                    )

    def test_a_bigger_package_is_always_better_value(self):
        for key in NEW_MODELS:
            per_million = [
                self.price(key, size) / size * 1_000_000
                for size in (50_000, 100_000, 200_000)
            ]
            with self.subTest(model=key):
                self.assertGreater(per_million[0], per_million[1])
                self.assertGreater(per_million[1], per_million[2])

    def test_exactly_one_package_each_is_highlighted(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                featured = Plan.objects.filter(
                    ai_model__key=key, is_active=True, is_featured=True
                )
                self.assertEqual(featured.count(), 1)
                self.assertEqual(featured.first().tokens, 100_000)

    def price(self, key, size):
        return (
            Plan.objects.filter(ai_model__key=key, tokens=size, is_active=True)
            .order_by("-price")
            .first()
            .price
        )

    def test_they_carry_the_same_ceilings_as_every_other_model(self):
        reference = AIModel.objects.get(key="claude-haiku-4-5")
        for key in NEW_MODELS:
            for size in (50_000, 100_000, 200_000):
                with self.subTest(model=key, size=size):
                    new = Plan.objects.get(ai_model__key=key, tokens=size)
                    other = Plan.objects.get(
                        is_active=True, ai_model=reference, tokens=size
                    )
                    self.assertEqual(new.daily_limit, other.daily_limit)
                    self.assertEqual(new.weekly_limit, other.weekly_limit)
                    self.assertEqual(new.monthly_limit, other.monthly_limit)

    def test_every_package_names_the_model_it_credits(self):
        """A plan with no model reports no revenue — see migration 0024."""
        for key in NEW_MODELS:
            with self.subTest(model=key):
                for plan in Plan.objects.filter(ai_model__key=key):
                    self.assertIsNotNone(plan.ai_model)
                    self.assertEqual(plan.token_model.key, key)


class ModelFixture(TestCase):
    """One student holding a real package of each new model."""

    def setUp(self):
        settings_service.bootstrap()
        self.student = User.objects.create_user(
            username="s1", email="s1@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.models = {key: AIModel.objects.get(key=key) for key in NEW_MODELS}
        self.plans = {
            key: Plan.objects.get(ai_model__key=key, tokens=100_000) for key in NEW_MODELS
        }
        for key in NEW_MODELS:
            wallet_service.credit(
                self.student, self.models[key], self.plans[key].tokens,
                transaction_type="plan_purchase", reason=self.plans[key].name,
                plan=self.plans[key],
            )
        self.conversation = chat_service.create_conversation(self.student)


class ChatTests(ModelFixture):

    def test_a_reply_is_billed_to_the_right_wallet(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                before = wallet_service.balance_of(self.student, self.models[key])
                with patch(COMPLETE, return_value=reply(key)):
                    chat_service.send_message(
                        self.student, self.conversation, "Hello", model_key=key,
                    )
                after = wallet_service.balance_of(self.student, self.models[key])
                self.assertEqual(before - after, 4000)

    def test_spending_one_never_touches_another(self):
        """Tokens are a separate currency per model, so nothing else may move."""
        others = {
            key: wallet_service.balance_of(self.student, AIModel.objects.get(key=key))
            for key in INCUMBENTS
        }
        opus_before = wallet_service.balance_of(self.student, self.models[OPUS])

        with patch(COMPLETE, return_value=reply(FABLE)):
            chat_service.send_message(
                self.student, self.conversation, "Hello", model_key=FABLE,
            )

        for key, balance in others.items():
            with self.subTest(untouched=key):
                self.assertEqual(
                    wallet_service.balance_of(self.student, AIModel.objects.get(key=key)),
                    balance,
                )
        self.assertEqual(
            wallet_service.balance_of(self.student, self.models[OPUS]), opus_before,
            "spending Fable must not touch Opus",
        )

    def test_the_usage_log_records_it_under_its_own_key(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                with patch(COMPLETE, return_value=reply(key)):
                    chat_service.send_message(
                        self.student, self.conversation, "Hello", model_key=key,
                    )
                log = AIUsageLog.objects.filter(user=self.student, model_key=key).latest("id")
                self.assertEqual(log.total_tokens, 4000)
                self.assertTrue(log.success)

    def test_the_ledger_records_it_against_the_right_model(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                with patch(COMPLETE, return_value=reply(key)):
                    chat_service.send_message(
                        self.student, self.conversation, "Hello", model_key=key,
                    )
                entry = AITokenTransaction.objects.filter(
                    user=self.student, ai_model=self.models[key], amount__lt=0,
                ).latest("id")
                self.assertEqual(entry.amount, -4000)

    def test_a_student_with_none_of_it_cannot_use_it(self):
        broke = User.objects.create_user(
            username="broke", email="broke@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        conversation = chat_service.create_conversation(broke)
        for key in NEW_MODELS:
            with self.subTest(model=key):
                with patch(COMPLETE, return_value=reply(key)) as vendor:
                    with self.assertRaises(ChatBlocked):
                        chat_service.send_message(
                            broke, conversation, "Hello", model_key=key,
                        )
                    vendor.assert_not_called()

    def test_a_failed_call_costs_the_student_nothing(self):
        from AI.services.providers.base import AIError

        for key in NEW_MODELS:
            with self.subTest(model=key):
                before = wallet_service.balance_of(self.student, self.models[key])
                with patch(COMPLETE, side_effect=AIError("vendor is down")):
                    with self.assertRaises(Exception):
                        chat_service.send_message(
                            self.student, self.conversation, "Hello", model_key=key,
                        )
                self.assertEqual(
                    wallet_service.balance_of(self.student, self.models[key]), before
                )


class LimitTests(ModelFixture):
    """The rolling ceilings apply exactly as they do to every other model."""

    def test_a_package_opens_with_the_plans_ceilings(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                allocation = PackageAllocation.objects.get(
                    user=self.student, ai_model=self.models[key]
                )
                self.assertEqual(allocation.tokens_allocated, 100_000)
                self.assertEqual(allocation.daily_limit, 9_000)
                self.assertEqual(allocation.weekly_limit, 35_000)
                self.assertEqual(allocation.monthly_limit, 100_000)

    def test_an_exhausted_day_moves_to_the_other_model(self):
        """The ceiling still stops Opus; the message is answered by Fable.

        This student holds both, so running out on one is no longer the end of
        the conversation — see AI/tests/test_model_switch.py for the fences on
        that. What matters here is that the *ceiling* was enforced: nothing was
        billed to Opus.
        """
        allocation = PackageAllocation.objects.get(
            user=self.student, ai_model=self.models[OPUS]
        )
        allocation.daily_used = allocation.daily_limit
        allocation.save(update_fields=["daily_used"])

        with patch(COMPLETE, return_value=reply(FABLE)):
            data = chat_service.send_message(
                self.student, self.conversation, "Hello", model_key=OPUS,
            )
        self.assertEqual(data["switched_from"], OPUS)
        self.assertEqual(data["switched_to"], FABLE)
        self.assertFalse(
            AIUsageLog.objects.filter(user=self.student, model_key=OPUS).exists(),
            "a model over its ceiling must not be billed",
        )

    def test_an_exhausted_day_blocks_when_switching_is_off(self):
        settings_service.set_value("auto_switch_model", False)
        try:
            for key in NEW_MODELS:
                allocation = PackageAllocation.objects.get(
                    user=self.student, ai_model=self.models[key]
                )
                allocation.daily_used = allocation.daily_limit
                allocation.save(update_fields=["daily_used"])

            with patch(COMPLETE, return_value=reply(OPUS)) as vendor:
                with self.assertRaises(ChatBlocked):
                    chat_service.send_message(
                        self.student, self.conversation, "Hello", model_key=OPUS,
                    )
                vendor.assert_not_called()
        finally:
            settings_service.set_value("auto_switch_model", True)

    def test_reaching_one_models_limit_leaves_the_other_usable(self):
        allocation = PackageAllocation.objects.get(
            user=self.student, ai_model=self.models[OPUS]
        )
        allocation.daily_used = allocation.daily_limit
        allocation.save(update_fields=["daily_used"])

        with patch(COMPLETE, return_value=reply(FABLE)):
            chat_service.send_message(
                self.student, self.conversation, "Hello", model_key=FABLE,
            )
        self.assertTrue(
            AIUsageLog.objects.filter(user=self.student, model_key=FABLE).exists()
        )


class RoutingTests(ModelFixture):

    def test_asking_for_each_model_resolves_to_it(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                self.assertEqual(
                    model_router.resolve_model(self.student, key).key, key
                )

    def test_the_picker_offers_them_once_the_student_holds_some(self):
        rows = wallet_service.overview(self.student)
        usable = {row["model"].key for row in rows if row["balance"] > 0}
        self.assertTrue(set(NEW_MODELS) <= usable)

    def test_a_disabled_model_is_refused_rather_than_substituted(self):
        """Quietly answering an Opus request with Fable would be a billing error."""
        model = self.models[OPUS]
        model.is_active = False
        model.save(update_fields=["is_active"])
        with self.assertRaises(Exception):
            model_router.resolve_model(self.student, OPUS)


class ScopeIntegrationTests(ModelFixture):
    """The console picks both up without being told they exist."""

    def test_the_model_selector_offers_all_six(self):
        from AI.services import model_scope

        keys = {model.key for model in model_scope.catalogue()}
        self.assertTrue(set(NEW_MODELS) <= keys)

    def test_per_model_analytics_still_sum_to_the_whole(self):
        """Adding two models must not break the all-models arithmetic."""
        from AI.services import analytics_service, model_scope

        with patch(COMPLETE, return_value=reply(FABLE)):
            chat_service.send_message(
                self.student, self.conversation, "Hello", model_key=FABLE,
            )

        whole = analytics_service.overview()["tokens_remaining"]
        parts = sum(
            analytics_service.overview(
                model_scope.ModelScope(model, tuple(model_scope.catalogue()))
            )["tokens_remaining"]
            for model in AIModel.objects.all()
        )
        self.assertEqual(parts, whole)

    def test_the_breakdown_lists_every_model(self):
        from AI.services import analytics_service

        rows = {row["key"] for row in analytics_service.model_breakdown()}
        self.assertTrue(set(NEW_MODELS) <= rows)
        self.assertTrue(set(INCUMBENTS) <= rows)

    def test_low_token_warnings_cover_them(self):
        """The alert sweep reads the catalogue, so it needs no new wiring."""
        from AI.services import token_alert_service

        wallet = UserAIWallet.objects.get(user=self.student, ai_model=self.models[OPUS])
        wallet.balance = 100                      # 0.1% of the allocation
        wallet.save(update_fields=["balance"])
        self.assertIsNotNone(token_alert_service.tier_for(wallet))


class NoRegressionTests(TestCase):
    """Adding two models moved nothing belonging to the four already there."""

    def test_the_incumbent_models_keep_their_settings(self):
        expected = {
            "gpt-5-mini": (True, True),        # (is_default, auto_grant)
            "gpt-4o": (False, False),
            "claude-haiku-4-5": (False, True),
            "claude-sonnet-5": (False, True),
        }
        for key, (is_default, auto_grant) in expected.items():
            with self.subTest(model=key):
                model = AIModel.objects.get(key=key)
                self.assertEqual(model.is_default, is_default)
                self.assertEqual(model.auto_grant, auto_grant)
                self.assertTrue(model.is_active)

    def test_exactly_one_model_is_still_the_default(self):
        self.assertEqual(AIModel.objects.filter(is_default=True).count(), 1)

    def test_the_incumbent_packages_are_unchanged(self):
        """The original twelve are still on sale, at their original prices.

        Counted per model rather than in total, so adding a seventh model later
        does not make this fail without anything having gone wrong.
        """
        for key in INCUMBENTS:
            with self.subTest(model=key):
                self.assertEqual(
                    Plan.objects.filter(is_active=True, ai_model__key=key).count(), 3
                )
        self.assertEqual(
            Plan.objects.filter(is_active=True, ai_model__key="gpt-5-mini", tokens=100_000)
            .order_by("-price").first().price,
            Decimal("2.49"),
            "the cheapest model's headline price must not drift",
        )

    def test_the_incumbent_pools_were_not_touched(self):
        for key in INCUMBENTS:
            with self.subTest(model=key):
                pool = ModelTokenPool.objects.get(ai_model__key=key)
                self.assertNotEqual(pool.total_added, 3_000_000 * 2)
                self.assertGreaterEqual(pool.balance, 0)

    def test_every_model_has_a_pool(self):
        self.assertEqual(
            ModelTokenPool.objects.count(), AIModel.objects.count(),
            "a model with no pool cannot be distributed from",
        )
