"""GPT-4o: a fourth model that behaves exactly like the other three.

The point of these tests is that there is almost nothing special to test. GPT-4o
is a catalogue row, a supply pool and three packages; the wallet, the router,
the limit windows and the chat runtime were not changed to accommodate it. So
what is asserted here is mostly *sameness* — that it routes, bills, meters,
blocks and resets through the same code paths, and that adding it moved nothing
belonging to the models that were already there.

Two things genuinely are its own:

* **its credential**, resolved by name from the server environment, so a
  deployment can bill it to a separate vendor account;
* **admin-only supply**, so no signup hands out any of the three million tokens
  the organisation bought.
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
    usage_limit_service,
    wallet_service,
)
from AI.services.chat_service import ChatBlocked
from AI.services.providers.base import AIResponse

PASSWORD = "Sup3rSecret!x"
GPT4O = "gpt-4o"
OTHERS = ("gpt-5-mini", "claude-haiku-4-5", "claude-sonnet-5")

COMPLETE = "AI.services.providers.openai_provider.OpenAICompatibleProvider.complete"


def reply(model: str = GPT4O, *, inp: int = 1200, out: int = 2800) -> AIResponse:
    return AIResponse(
        content="Answer.", input_tokens=inp, output_tokens=out, total_tokens=inp + out,
        model=model, request_id="req-4o", latency_ms=300,
    )


class CatalogueTests(TestCase):
    """The row itself, and that it did not disturb the others."""

    def test_gpt4o_joined_the_catalogue_without_displacing_the_others(self):
        """All four are present and available.

        Deliberately not an exact census of the catalogue: asserting equality
        here made this test fail the day a fifth model was added, which says
        nothing about GPT-4o. What matters is that GPT-4o arrived and the three
        that preceded it are still there.
        """
        keys = set(AIModel.objects.filter(is_active=True).values_list("key", flat=True))
        self.assertIn(GPT4O, keys)
        self.assertTrue(set(OTHERS) <= keys)

    def test_gpt4o_is_configured_the_way_the_product_expects(self):
        model = AIModel.objects.get(key=GPT4O)
        self.assertEqual(model.name, "GPT-4o")
        self.assertEqual(model.provider, "openai")
        self.assertTrue(model.is_active)
        self.assertFalse(model.is_default)
        self.assertTrue(model.supports_vision)      # it genuinely reads images
        self.assertFalse(model.auto_grant)          # admin-distributed only
        self.assertEqual(model.token_type, "gpt_4o")

    def test_it_names_its_credential_rather_than_storing_one(self):
        """The database must never contain the key, only the name of a setting."""
        model = AIModel.objects.get(key=GPT4O)
        self.assertEqual(model.api_key_setting, "OPENAI_GPT4O_API_KEY")
        with override_settings(OPENAI_GPT4O_API_KEY="sk-test-4o"):
            self.assertEqual(model.api_key, "sk-test-4o")
            self.assertTrue(model.has_own_credential)
        # Nothing that reaches a page carries it.
        self.assertNotIn("sk-", str(model.__dict__))

    def test_the_router_hands_the_model_its_own_key(self):
        model = AIModel.objects.get(key=GPT4O)
        with override_settings(OPENAI_GPT4O_API_KEY="sk-test-4o", OPENAI_API_KEY="sk-shared"):
            client = providers.for_model(model)
            self.assertEqual(client.api_key, "sk-test-4o")
            self.assertEqual(client.model, GPT4O)

    def test_a_model_with_no_credential_of_its_own_still_uses_the_shared_one(self):
        """The three existing models must be completely unaffected."""
        model = AIModel.objects.get(key="gpt-5-mini")
        self.assertEqual(model.api_key_setting, "")
        with override_settings(OPENAI_GPT4O_API_KEY="sk-test-4o", OPENAI_API_KEY="sk-shared"):
            self.assertEqual(providers.for_model(model).api_key, "sk-shared")


class SupplyTests(TestCase):
    """Three million tokens, held by the administrator and nobody else."""

    def setUp(self):
        self.model = AIModel.objects.get(key=GPT4O)
        self.pool = ModelTokenPool.objects.get(ai_model=self.model)

    def test_the_opening_allocation_is_three_million(self):
        self.assertEqual(self.pool.total_added, 3_000_000)
        self.assertEqual(self.pool.balance, 3_000_000)
        self.assertEqual(self.pool.total_distributed, 0)
        self.assertEqual(self.pool.total_reclaimed, 0)

    def test_it_is_recorded_as_one_and_a_half_million_each_way(self):
        self.assertEqual(self.pool.input_allocation, 1_500_000)
        self.assertEqual(self.pool.output_allocation, 1_500_000)
        self.assertEqual(
            self.pool.input_allocation + self.pool.output_allocation, self.pool.total_added
        )

    def test_the_split_reports_against_what_has_actually_been_spent(self):
        rows = {row["label"]: row for row in self.pool.direction_rows()}
        self.assertEqual(rows["Input"]["allocated"], 1_500_000)
        self.assertEqual(rows["Input"]["used"], 0)
        self.assertEqual(rows["Input"]["remaining"], 1_500_000)
        self.assertEqual(rows["Output"]["allocated"], 1_500_000)

    def test_no_student_starts_with_any_of_it(self):
        self.assertEqual(
            sum(UserAIWallet.objects.filter(ai_model=self.model).values_list("balance", flat=True)),
            0,
        )

    def test_a_new_signup_is_given_none_of_it(self):
        """The welcome grant must skip a model the administrator hands out."""
        settings_service.set_value("free_trial_enabled", True)
        settings_service.set_value("free_trial_tokens", 500)
        student = User.objects.create_user(
            username="newbie", email="newbie@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        referral_service.grant_welcome_tokens(student)

        self.assertEqual(wallet_service.balance_of(student, self.model), 0)
        # …while the trial itself, in the default model, still arrives.
        self.assertEqual(wallet_service.balance_of(student, AIModel.objects.get(key="gpt-5-mini")), 500)

    def test_the_supply_is_finite_and_distribution_draws_from_it(self):
        student = User.objects.create_user(
            username="s1", email="s1@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        distribution_service.distribute(student, self.model, 200_000, reason="test")

        self.pool.refresh_from_db()
        self.assertEqual(self.pool.balance, 2_800_000)
        self.assertEqual(self.pool.total_distributed, 200_000)
        self.assertEqual(wallet_service.balance_of(student, self.model), 200_000)
        # Nothing was minted: what the admin holds plus what students hold is
        # still exactly what was bought.
        held = sum(UserAIWallet.objects.filter(ai_model=self.model).values_list("balance", flat=True))
        self.assertEqual(self.pool.balance + held, 3_000_000)

    def test_the_pool_cannot_be_overdrawn(self):
        student = User.objects.create_user(
            username="s2", email="s2@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        with self.assertRaises(model_pool_service.InsufficientPool):
            distribution_service.distribute(student, self.model, 4_000_000, reason="too much")
        self.pool.refresh_from_db()
        self.assertEqual(self.pool.balance, 3_000_000)


class Gpt4oFixture(TestCase):
    """One student holding a real GPT-4o package."""

    def setUp(self):
        settings_service.bootstrap()
        self.student = User.objects.create_user(
            username="s1", email="s1@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.model = AIModel.objects.get(key=GPT4O)
        self.plan = Plan.objects.get(slug="gpt-4o-100k")
        wallet_service.credit(
            self.student, self.model, self.plan.tokens,
            transaction_type="plan_purchase", reason=self.plan.name, plan=self.plan,
        )
        self.conversation = chat_service.create_conversation(self.student)


class PackageTests(Gpt4oFixture):

    def test_three_packages_are_on_sale_at_the_three_standard_sizes(self):
        plans = Plan.objects.filter(is_active=True, ai_model=self.model).order_by("tokens")
        self.assertEqual([p.tokens for p in plans], [50_000, 100_000, 200_000])

    def test_they_carry_the_same_ceilings_as_every_other_model(self):
        for size in (50_000, 100_000, 200_000):
            gpt4o = Plan.objects.get(is_active=True, ai_model=self.model, tokens=size)
            other = Plan.objects.get(
                is_active=True, ai_model=AIModel.objects.get(key="claude-haiku-4-5"), tokens=size
            )
            with self.subTest(size=size):
                self.assertEqual(gpt4o.daily_limit, other.daily_limit)
                self.assertEqual(gpt4o.weekly_limit, other.weekly_limit)
                self.assertEqual(gpt4o.monthly_limit, other.monthly_limit)

    def test_it_is_priced_between_the_cheaper_and_the_dearer_claude(self):
        """Its API cost sits between theirs, and so does its price."""
        for size in (50_000, 100_000, 200_000):
            price = Plan.objects.get(is_active=True, ai_model=self.model, tokens=size).price
            cheaper = Plan.objects.get(
                is_active=True, ai_model=AIModel.objects.get(key="claude-haiku-4-5"), tokens=size
            ).price
            dearer = Plan.objects.get(
                is_active=True, ai_model=AIModel.objects.get(key="claude-sonnet-5"), tokens=size
            ).price
            with self.subTest(size=size):
                self.assertGreater(price, cheaper)
                self.assertLess(price, dearer, "must undercut the dearer model")

    def test_buying_one_opens_a_metered_package_like_any_other(self):
        allocation = PackageAllocation.objects.get(user=self.student, ai_model=self.model)
        self.assertEqual(allocation.tokens_allocated, 100_000)
        self.assertEqual(allocation.daily_limit, 9_000)
        self.assertEqual(allocation.weekly_limit, 35_000)
        self.assertEqual(allocation.monthly_limit, 100_000)


class ChatTests(Gpt4oFixture):

    def test_a_reply_is_billed_to_the_gpt4o_wallet_at_the_reported_usage(self):
        before = wallet_service.balance_of(self.student, self.model)
        with patch(COMPLETE, return_value=reply()) as complete:
            result = chat_service.send_message(
                self.student, self.conversation, "Explain diffraction", model_key=GPT4O
            )

        complete.assert_called_once()
        self.assertEqual(result["tokens_charged"], 4_000)
        self.assertEqual(wallet_service.balance_of(self.student, self.model), before - 4_000)
        self.assertEqual(result["model"], GPT4O)

    def test_spending_gpt4o_never_touches_another_model(self):
        for key in OTHERS:
            other = AIModel.objects.get(key=key)
            wallet_service.credit(self.student, other, 10_000, reason="test")
        before = {key: wallet_service.balance_of(self.student, AIModel.objects.get(key=key))
                  for key in OTHERS}

        with patch(COMPLETE, return_value=reply()):
            chat_service.send_message(self.student, self.conversation, "Hello", model_key=GPT4O)

        for key in OTHERS:
            self.assertEqual(
                wallet_service.balance_of(self.student, AIModel.objects.get(key=key)),
                before[key], key,
            )

    def test_the_usage_log_records_it_under_its_own_key(self):
        with patch(COMPLETE, return_value=reply()):
            chat_service.send_message(self.student, self.conversation, "Hello", model_key=GPT4O)

        log = AIUsageLog.objects.get()
        self.assertEqual(log.model_key, GPT4O)
        self.assertEqual(log.provider, "openai")
        self.assertEqual(log.input_tokens, 1_200)
        self.assertEqual(log.output_tokens, 2_800)
        self.assertEqual(log.tokens_charged, 4_000)

    def test_the_ledger_records_it_against_gpt4o(self):
        with patch(COMPLETE, return_value=reply()):
            chat_service.send_message(self.student, self.conversation, "Hello", model_key=GPT4O)

        entry = AITokenTransaction.objects.filter(transaction_type="chat_usage").get()
        self.assertEqual(entry.ai_model_id, self.model.pk)
        self.assertEqual(entry.amount, -4_000)

    def test_the_models_running_totals_follow_the_direction_split(self):
        with patch(COMPLETE, return_value=reply()):
            chat_service.send_message(self.student, self.conversation, "Hello", model_key=GPT4O)

        self.model.refresh_from_db()
        self.assertEqual(self.model.input_tokens_total, 1_200)
        self.assertEqual(self.model.output_tokens_total, 2_800)

        rows = {r["label"]: r for r in ModelTokenPool.objects.get(ai_model=self.model).direction_rows()}
        self.assertEqual(rows["Input"]["used"], 1_200)
        self.assertEqual(rows["Output"]["used"], 2_800)
        self.assertEqual(rows["Input"]["remaining"], 1_500_000 - 1_200)

    def test_a_student_with_no_gpt4o_tokens_cannot_use_it(self):
        stranger = User.objects.create_user(
            username="s9", email="s9@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        conversation = chat_service.create_conversation(stranger)
        with patch(COMPLETE) as complete:
            with self.assertRaises(ChatBlocked):
                chat_service.send_message(stranger, conversation, "Hello", model_key=GPT4O)
            complete.assert_not_called()

    def test_a_failed_call_costs_the_student_nothing(self):
        from AI.services.providers.base import AIError

        before = wallet_service.balance_of(self.student, self.model)
        with patch(COMPLETE, side_effect=AIError("upstream", code="provider_down")):
            with self.assertRaises(AIError):
                chat_service.send_message(self.student, self.conversation, "Hello", model_key=GPT4O)

        self.assertEqual(wallet_service.balance_of(self.student, self.model), before)
        self.assertFalse(AIUsageLog.objects.get().success)


class LimitTests(Gpt4oFixture):
    """The same three windows, enforced by the same code."""

    def test_the_day_caps_what_can_be_spent(self):
        snapshot = usage_limit_service.snapshot(self.student, self.model)
        self.assertTrue(snapshot.metered)
        self.assertEqual(snapshot.balance, 100_000)
        self.assertEqual(snapshot.spendable, 9_000)

    def test_an_exhausted_day_blocks_before_the_vendor_is_called(self):
        usage_limit_service.consume(self.student, self.model, 9_000)
        with patch(COMPLETE) as complete:
            with self.assertRaises(ChatBlocked) as caught:
                chat_service.send_message(self.student, self.conversation, "Hello", model_key=GPT4O)
            complete.assert_not_called()

        self.assertEqual(caught.exception.code, "usage_limit")
        self.assertEqual(caught.exception.extra["windows"], ["daily"])
        # The tokens are still theirs.
        self.assertGreater(wallet_service.balance_of(self.student, self.model), 0)

    def test_all_three_counters_move_together(self):
        with patch(COMPLETE, return_value=reply()):
            chat_service.send_message(self.student, self.conversation, "Hello", model_key=GPT4O)

        allocation = PackageAllocation.objects.get(user=self.student, ai_model=self.model)
        self.assertEqual(allocation.daily_used, 4_000)
        self.assertEqual(allocation.weekly_used, 4_000)
        self.assertEqual(allocation.monthly_used, 4_000)

    def test_reaching_the_gpt4o_limit_leaves_the_other_models_usable(self):
        other = AIModel.objects.get(key="gpt-5-mini")
        other_plan = Plan.objects.get(slug="gpt-5-mini-100k")
        wallet_service.credit(
            self.student, other, other_plan.tokens,
            transaction_type="plan_purchase", reason=other_plan.name, plan=other_plan,
        )
        usage_limit_service.consume(self.student, self.model, 9_000)

        # GPT-4o is over its ceiling, so the message is answered by the other
        # model this student holds rather than refused. What the ceiling still
        # guarantees is that GPT-4o itself is neither called nor billed.
        with patch(COMPLETE, return_value=reply("gpt-5-mini", inp=100, out=100)):
            switched = chat_service.send_message(
                self.student, self.conversation, "Hi", model_key=GPT4O
            )
        self.assertEqual(switched["switched_from"], GPT4O)
        self.assertEqual(switched["model"], "gpt-5-mini")
        self.assertEqual(
            PackageAllocation.objects.get(user=self.student, ai_model=self.model).daily_used,
            9_000,
            "a model over its ceiling must not be spent any further",
        )

        # And asking for the other model directly is unaffected.
        with patch(COMPLETE, return_value=reply("gpt-5-mini", inp=100, out=100)):
            result = chat_service.send_message(
                self.student, self.conversation, "Hi", model_key="gpt-5-mini"
            )
        self.assertEqual(result["model"], "gpt-5-mini")
        self.assertEqual(result["switched_from"], "")


class RoutingTests(Gpt4oFixture):

    def test_the_picker_offers_gpt4o_once_the_student_holds_some(self):
        keys = [row["model"].key for row in wallet_service.overview(self.student) if row["usable"]]
        self.assertIn(GPT4O, keys)

    def test_asking_for_gpt4o_resolves_to_gpt4o(self):
        route = model_router.route(self.student, GPT4O)
        self.assertEqual(route.model.key, GPT4O)
        self.assertEqual(route.api_model_id, GPT4O)
        self.assertEqual(route.provider_slug, "openai")

    def test_a_disabled_gpt4o_is_refused_rather_than_substituted(self):
        AIModel.objects.filter(key=GPT4O).update(is_active=False)
        from AI.services.model_service import ModelUnavailable

        with self.assertRaises(ModelUnavailable):
            model_router.route(self.student, GPT4O)


class NoRegressionTests(TestCase):
    """Adding a fourth model moved nothing belonging to the first three."""

    def test_the_existing_models_keep_their_settings(self):
        expected = {
            "gpt-5-mini": ("GPT-5 Mini", True),
            "claude-haiku-4-5": ("Claude 4.5", True),
            "claude-sonnet-5": ("Claude 4.5 High", True),
        }
        for key, (name, auto) in expected.items():
            model = AIModel.objects.get(key=key)
            with self.subTest(key=key):
                self.assertEqual(model.name, name)
                self.assertTrue(model.is_active)
                self.assertEqual(model.auto_grant, auto)
                self.assertEqual(model.api_key_setting, "")

    def test_the_existing_pools_were_not_touched(self):
        for key in OTHERS:
            pool = ModelTokenPool.objects.get(ai_model__key=key)
            with self.subTest(key=key):
                self.assertEqual(pool.input_allocation, 0)
                self.assertEqual(pool.output_allocation, 0)
                self.assertFalse(pool.tracks_direction)

    def test_the_existing_packages_are_unchanged(self):
        """The three models that predate GPT-4o still sell three sizes each.

        Named rather than counted: "everything except GPT-4o" grew from nine to
        fifteen when two more models went on sale, which says nothing about
        whether the original three were disturbed.
        """
        plans = Plan.objects.filter(is_active=True, ai_model__key__in=OTHERS)
        self.assertEqual(plans.count(), 9)
        for plan in plans:
            with self.subTest(slug=plan.slug):
                self.assertIn(plan.tokens, (50_000, 100_000, 200_000))
                self.assertTrue(plan.daily_limit and plan.weekly_limit and plan.monthly_limit)

    def test_every_model_on_sale_offers_the_three_standard_sizes(self):
        """Counted per model, so a new model does not break this.

        It replaced an exact total, which failed the day Fable and Opus went on
        sale without anything about GPT-4o having changed.
        """
        on_sale = (
            Plan.objects.filter(is_active=True, ai_model__isnull=False)
            .values_list("ai_model__key", flat=True)
            .distinct()
        )
        self.assertIn(GPT4O, set(on_sale))
        for key in on_sale:
            with self.subTest(model=key):
                sizes = sorted(
                    Plan.objects.filter(is_active=True, ai_model__key=key)
                    .values_list("tokens", flat=True)
                )
                self.assertEqual(sizes, [50_000, 100_000, 200_000])
