"""GPT-6 Astra and GPT-5 Sol: a seventh and eighth model that behave like the rest.

As with GPT-4o, Fable 5.1 and Opus 5, the point is that there is almost nothing
special to test. Each is a catalogue row, a supply pool and three packages; the
wallet, the router, the limit windows, the chat runtime, the model selector and
the per-model analytics were not changed to accommodate them. So what is
asserted here is mostly *sameness* — that they route, bill, meter, block and
reset through the same code paths, and that adding them moved nothing belonging
to the six models already there.

What is genuinely their own:

* **the wire format.** Both are OpenAI reasoning models, so the request that
  leaves the server must carry ``max_completion_tokens`` and
  ``reasoning_effort`` and must not carry ``max_tokens`` or ``temperature`` —
  getting that wrong is an HTTP 400 on every message. It is pinned below
  against the real request body, not a mock of the provider.
* **the vendor credential.** ``api_key_setting`` is blank, which resolves to
  ``OPENAI_API_KEY`` — the key GPT-5 Mini already uses.
* **their place on the price ladder**, which is set from what each costs to run.
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

import requests
from django.test import SimpleTestCase, TestCase, override_settings

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
from AI.services.providers.base import AIError, AIResponse, ChatTurn

PASSWORD = "Sup3rSecret!x"

ASTRA = "gpt-6-astra"
SOL = "gpt-5.6-sol"
NEW_MODELS = (ASTRA, SOL)
#: The six that existed before these two arrived.
INCUMBENTS = (
    "gpt-5-mini", "gpt-4o", "claude-haiku-4-5", "claude-sonnet-5",
    "claude-fable-5-1", "claude-opus-5",
)
SIZES = (50_000, 100_000, 200_000)

COMPLETE = "AI.services.providers.openai_provider.OpenAICompatibleProvider.complete"
POST = "AI.services.providers.openai_provider.requests.post"

#: A 1x1 PNG, enough for the provider to accept it as an image.
PIXEL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def reply(model: str, *, inp: int = 1200, out: int = 2800) -> AIResponse:
    return AIResponse(
        content="Answer.", input_tokens=inp, output_tokens=out, total_tokens=inp + out,
        model=model, request_id="req-new", latency_ms=300,
    )


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def completion(model: str, content: str = "Hello.") -> dict:
    return {
        "id": "chatcmpl-test",
        "model": model,
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 40,
            "completion_tokens": 60,
            "total_tokens": 100,
            "completion_tokens_details": {"reasoning_tokens": 25},
        },
    }


class CatalogueTests(TestCase):
    """Both models exist and are configured the way the product expects."""

    def test_the_platform_now_offers_eight_models(self):
        keys = set(AIModel.objects.filter(is_active=True).values_list("key", flat=True))
        self.assertTrue(set(NEW_MODELS) <= keys)
        self.assertTrue(set(INCUMBENTS) <= keys)
        self.assertEqual(len(set(NEW_MODELS) | set(INCUMBENTS)), 8)

    def test_each_is_configured_for_its_vendor(self):
        for key, name in ((ASTRA, "GPT-6 Astra"), (SOL, "GPT-5 Sol")):
            with self.subTest(model=key):
                model = AIModel.objects.get(key=key)
                self.assertEqual(model.name, name)
                self.assertEqual(model.provider, "openai")
                self.assertEqual(model.provider_ref.slug, "openai")
                self.assertTrue(model.is_active)
                self.assertTrue(model.is_available)
                self.assertFalse(model.is_default, "neither may displace the default model")
                self.assertTrue(model.supports_vision)
                self.assertEqual(model.max_output_tokens, 4096)
                self.assertTrue(model.description)

    def test_the_picker_shows_them_in_price_order(self):
        """Sol between Claude 4.5 High and Fable; Astra after everything."""
        order = list(AIModel.objects.order_by("sort_order").values_list("key", flat=True))
        self.assertLess(order.index("claude-sonnet-5"), order.index(SOL))
        self.assertLess(order.index(SOL), order.index("claude-fable-5-1"))
        self.assertEqual(order[-1], ASTRA)

    def test_list_prices_are_recorded_for_cost_reporting(self):
        """Unlike Fable and Opus, these arrive priced — the margin page is live."""
        expected = {ASTRA: ("10", "50"), SOL: ("4", "20")}
        for key, (inbound, outbound) in expected.items():
            with self.subTest(model=key):
                model = AIModel.objects.get(key=key)
                self.assertEqual(model.input_cost_per_1m, Decimal(inbound))
                self.assertEqual(model.output_cost_per_1m, Decimal(outbound))

    def test_neither_stores_a_credential_of_its_own(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                model = AIModel.objects.get(key=key)
                self.assertEqual(model.api_key_setting, "")
                self.assertFalse(model.has_own_credential)

    @override_settings(OPENAI_API_KEY="shared-openai-key")
    def test_the_router_hands_them_the_shared_openai_key(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                provider = providers.for_model(AIModel.objects.get(key=key))
                self.assertIsInstance(provider, providers.OpenAICompatibleProvider)
                self.assertEqual(provider.api_key, "shared-openai-key")
                self.assertEqual(provider.model, key)
                self.assertTrue(provider.is_configured)

    def test_the_provider_treats_both_as_reasoning_models_that_see(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                provider = providers.for_model(AIModel.objects.get(key=key))
                self.assertTrue(provider.is_reasoning_model)
                self.assertTrue(provider.supports_vision)

    def test_thinking_is_low_until_an_administrator_turns_it_up(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                self.assertEqual(AIModel.objects.get(key=key).thinking_mode, "off")

    def test_their_token_currencies_are_distinct(self):
        types = [AIModel.objects.get(key=key).token_type for key in NEW_MODELS]
        self.assertEqual(types, ["gpt_6_astra", "gpt_5_6_sol"])
        everyone = list(AIModel.objects.values_list("token_type", flat=True))
        self.assertEqual(len(everyone), len(set(everyone)), "no two models may share a currency")


@override_settings(OPENAI_API_KEY="sk-test", OPENAI_BASE_URL="https://api.openai.test/v1")
class WireFormatTests(SimpleTestCase):
    """What actually leaves the server. A wrong field here is a 400 per message."""

    def send(self, key, *, turns=None, thinking_mode="off", response=None):
        provider = providers.build(
            "openai", model=key, max_output_tokens=4096, vision=True,
            thinking_mode=thinking_mode, temperature=0.7,
        )
        with patch(POST, return_value=response or FakeResponse(completion(key))) as post:
            result = provider.complete(
                turns or [ChatTurn(role="user", content="hi")], system_prompt="Be brief.",
            )
        return post, result

    def test_the_request_names_the_model_and_hits_chat_completions(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                post, _ = self.send(key)
                self.assertEqual(post.call_args.args[0], "https://api.openai.test/v1/chat/completions")
                self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer sk-test")
                payload = json.loads(post.call_args.kwargs["data"])
                self.assertEqual(payload["model"], key)
                self.assertEqual(payload["messages"][0], {"role": "system", "content": "Be brief."})

    def test_reasoning_parameters_replace_the_chat_ones(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                post, _ = self.send(key)
                payload = json.loads(post.call_args.kwargs["data"])
                self.assertEqual(payload["max_completion_tokens"], 4096)
                self.assertEqual(payload["reasoning_effort"], "low")
                for rejected in ("max_tokens", "temperature", "presence_penalty", "frequency_penalty"):
                    self.assertNotIn(rejected, payload)

    def test_adaptive_thinking_leaves_the_effort_to_the_model(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                post, _ = self.send(key, thinking_mode="adaptive")
                self.assertNotIn("reasoning_effort", json.loads(post.call_args.kwargs["data"]))

    def test_an_image_is_sent_as_a_content_part(self):
        turn = ChatTurn(role="user", content="What is this?", image_data_url=PIXEL)
        for key in NEW_MODELS:
            with self.subTest(model=key):
                post, _ = self.send(key, turns=[turn])
                parts = json.loads(post.call_args.kwargs["data"])["messages"][1]["content"]
                self.assertEqual(parts[0], {"type": "text", "text": "What is this?"})
                self.assertEqual(parts[1]["type"], "image_url")
                self.assertEqual(parts[1]["image_url"]["url"], PIXEL)

    def test_usage_is_read_from_the_vendor(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                _, result = self.send(key)
                self.assertEqual(result.content, "Hello.")
                self.assertEqual((result.input_tokens, result.output_tokens), (40, 60))
                self.assertEqual(result.total_tokens, 100)
                self.assertEqual(result.reasoning_tokens, 25)

    def test_vendor_failures_become_friendly_errors(self):
        cases = [
            (FakeResponse({"error": {"message": "slow down"}}, status=429), "rate_limited", True),
            (FakeResponse({"error": {"message": "bad key"}}, status=401), "auth", False),
            (FakeResponse({"error": {"message": "boom"}}, status=503), "provider_down", True),
        ]
        for key in NEW_MODELS:
            for response, code, retryable in cases:
                with self.subTest(model=key, code=code):
                    with self.assertRaises(AIError) as caught:
                        self.send(key, response=response)
                    self.assertEqual(caught.exception.code, code)
                    self.assertEqual(caught.exception.retryable, retryable)

    def test_a_timeout_is_retryable(self):
        provider = providers.build("openai", model=ASTRA, max_output_tokens=4096)
        with patch(POST, side_effect=requests.Timeout):
            with self.assertRaises(AIError) as caught:
                provider.complete([ChatTurn(role="user", content="hi")])
        self.assertEqual(caught.exception.code, "timeout")
        self.assertTrue(caught.exception.retryable)


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
                self.assertEqual(pool.input_allocation, 1_500_000)
                self.assertEqual(pool.output_allocation, 1_500_000)

    def test_a_new_signup_is_given_none_of_it(self):
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
        model = AIModel.objects.get(key=ASTRA)
        with self.assertRaises(model_pool_service.InsufficientPool):
            distribution_service.distribute(student, model, 99_000_000)
        self.assertEqual(self.pool(ASTRA).balance, 3_000_000)
        self.assertEqual(wallet_service.balance_of(student, model), 0)


class PackageTests(TestCase):
    """Three live packages each, at the sizes every other model is sold in."""

    def price(self, key, size) -> Decimal:
        return (
            Plan.objects.filter(ai_model__key=key, tokens=size, is_active=True)
            .order_by("-price").first().price
        )

    def test_three_live_packages_at_the_standard_sizes(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                plans = Plan.objects.filter(ai_model__key=key, is_active=True).order_by("tokens")
                self.assertEqual([p.tokens for p in plans], list(SIZES))

    def test_the_ladder_holds_at_every_size(self):
        """Sol sits between Claude 4.5 High and Fable; Astra above everything."""
        for size in SIZES:
            with self.subTest(size=size):
                sol, astra = self.price(SOL, size), self.price(ASTRA, size)
                self.assertGreater(sol, self.price("claude-sonnet-5", size))
                self.assertLess(sol, self.price("claude-fable-5-1", size))
                for key in INCUMBENTS:
                    self.assertGreater(astra, self.price(key, size), f"Astra must cost more than {key}")

    def test_a_bigger_package_is_always_better_value(self):
        for key in NEW_MODELS:
            per_million = [self.price(key, size) / size * 1_000_000 for size in SIZES]
            with self.subTest(model=key):
                self.assertGreater(per_million[0], per_million[1])
                self.assertGreater(per_million[1], per_million[2])

    def test_every_package_clears_its_worst_case_cost(self):
        """Even if every token sold were output — the dearest direction."""
        for key in NEW_MODELS:
            model = AIModel.objects.get(key=key)
            for size in SIZES:
                with self.subTest(model=key, size=size):
                    worst = model.output_cost_per_1m * size / 1_000_000
                    self.assertGreater(self.price(key, size), worst)

    def test_exactly_one_package_each_is_highlighted(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                featured = Plan.objects.filter(ai_model__key=key, is_active=True, is_featured=True)
                self.assertEqual(featured.count(), 1)
                self.assertEqual(featured.first().tokens, 100_000)

    def test_they_carry_the_same_ceilings_as_every_other_model(self):
        reference = AIModel.objects.get(key="claude-haiku-4-5")
        for key in NEW_MODELS:
            for size in SIZES:
                with self.subTest(model=key, size=size):
                    new = Plan.objects.get(ai_model__key=key, tokens=size)
                    other = Plan.objects.get(is_active=True, ai_model=reference, tokens=size)
                    self.assertEqual(
                        (new.daily_limit, new.weekly_limit, new.monthly_limit),
                        (other.daily_limit, other.weekly_limit, other.monthly_limit),
                    )

    def test_every_package_names_the_model_it_credits(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                for plan in Plan.objects.filter(ai_model__key=key):
                    self.assertEqual(plan.token_model.key, key)
                    self.assertNotIn(".", plan.slug, "slugs stay URL-safe")


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
                    chat_service.send_message(self.student, self.conversation, "Hello", model_key=key)
                after = wallet_service.balance_of(self.student, self.models[key])
                self.assertEqual(before - after, 4000)

    def test_spending_one_never_touches_another(self):
        others = {
            key: wallet_service.balance_of(self.student, AIModel.objects.get(key=key))
            for key in INCUMBENTS
        }
        sol_before = wallet_service.balance_of(self.student, self.models[SOL])

        with patch(COMPLETE, return_value=reply(ASTRA)):
            chat_service.send_message(self.student, self.conversation, "Hello", model_key=ASTRA)

        for key, balance in others.items():
            with self.subTest(untouched=key):
                self.assertEqual(
                    wallet_service.balance_of(self.student, AIModel.objects.get(key=key)), balance
                )
        self.assertEqual(
            wallet_service.balance_of(self.student, self.models[SOL]), sol_before,
            "spending Astra must not touch Sol",
        )

    def test_usage_is_logged_and_counted_under_its_own_key(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                with patch(COMPLETE, return_value=reply(key)):
                    chat_service.send_message(self.student, self.conversation, "Hello", model_key=key)
                log = AIUsageLog.objects.filter(user=self.student, model_key=key).latest("id")
                self.assertEqual(log.total_tokens, 4000)
                self.assertTrue(log.success)
                model = AIModel.objects.get(key=key)
                self.assertEqual(model.request_count, 1)
                self.assertEqual(model.input_tokens_total, 1200)
                self.assertEqual(model.output_tokens_total, 2800)
                self.assertGreater(model.estimated_cost, 0)

    def test_the_ledger_records_it_against_the_right_model(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                with patch(COMPLETE, return_value=reply(key)):
                    chat_service.send_message(self.student, self.conversation, "Hello", model_key=key)
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
                        chat_service.send_message(broke, conversation, "Hello", model_key=key)
                    vendor.assert_not_called()

    def test_a_failed_call_costs_the_student_nothing(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                before = wallet_service.balance_of(self.student, self.models[key])
                with patch(COMPLETE, side_effect=AIError("vendor is down")):
                    with self.assertRaises(Exception):
                        chat_service.send_message(self.student, self.conversation, "Hello", model_key=key)
                self.assertEqual(wallet_service.balance_of(self.student, self.models[key]), before)


@override_settings(OPENAI_API_KEY="sk-test")
class PageTests(ModelFixture):
    """The student and admin pages list them, and the chat endpoint serves them."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.student)

    def test_the_chat_endpoint_answers_with_each_model(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                before = wallet_service.balance_of(self.student, self.models[key])
                with patch(POST, return_value=FakeResponse(completion(key))) as post:
                    response = self.client.post(
                        "/api/chat/send/", {"message": "Explain gravity", "model": key},
                        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                    )
                self.assertEqual(response.status_code, 200, response.content[:300])
                self.assertTrue(response.json()["ok"])
                self.assertEqual(json.loads(post.call_args.kwargs["data"])["model"], key)
                self.assertEqual(
                    before - wallet_service.balance_of(self.student, self.models[key]), 100
                )

    def test_switching_between_them_mid_conversation(self):
        conversation = chat_service.create_conversation(self.student)
        for key in (ASTRA, SOL, ASTRA):
            with patch(POST, return_value=FakeResponse(completion(key))) as post:
                response = self.client.post(
                    "/api/chat/send/",
                    {"message": "Next", "model": key, "conversation_id": conversation.pk},
                    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(post.call_args.kwargs["data"])["model"], key)
        self.assertEqual(
            list(AIUsageLog.objects.filter(user=self.student).order_by("id")
                 .values_list("model_key", flat=True)),
            [ASTRA, SOL, ASTRA],
        )

    def test_the_chat_picker_offers_both(self):
        body = self.client.get("/chat/").content.decode()
        for key, name in ((ASTRA, "GPT-6 Astra"), (SOL, "GPT-5 Sol")):
            with self.subTest(model=key):
                self.assertIn(f'data-model="{key}"', body)
                self.assertIn(name, body)

    def test_the_pricing_pages_list_their_packages(self):
        """The student's billing view, and the public page a visitor sees.

        ``/plans/`` redirects a signed-in student to the first, so the second
        is fetched signed out.
        """
        pages = {"/app/pricing/": self.client.get("/app/pricing/").content.decode()}
        self.client.logout()
        pages["/plans/"] = self.client.get("/plans/").content.decode()
        for url, body in pages.items():
            for key in NEW_MODELS:
                with self.subTest(url=url, model=key):
                    self.assertIn("GPT-6 Astra" if key == ASTRA else "GPT-5 Sol", body)
                    for plan in Plan.objects.filter(ai_model__key=key, is_active=True):
                        self.assertIn(f"{plan.price}", body)


class LimitTests(ModelFixture):

    def test_a_package_opens_with_the_plans_ceilings(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                allocation = PackageAllocation.objects.get(user=self.student, ai_model=self.models[key])
                self.assertEqual(allocation.tokens_allocated, 100_000)
                self.assertEqual(allocation.daily_limit, 9_000)
                self.assertEqual(allocation.weekly_limit, 35_000)
                self.assertEqual(allocation.monthly_limit, 100_000)

    def test_an_exhausted_day_moves_to_the_other_model(self):
        allocation = PackageAllocation.objects.get(user=self.student, ai_model=self.models[ASTRA])
        allocation.daily_used = allocation.daily_limit
        allocation.save(update_fields=["daily_used"])

        with patch(COMPLETE, return_value=reply(SOL)):
            data = chat_service.send_message(self.student, self.conversation, "Hello", model_key=ASTRA)
        self.assertEqual(data["switched_from"], ASTRA)
        self.assertEqual(data["switched_to"], SOL)
        self.assertFalse(
            AIUsageLog.objects.filter(user=self.student, model_key=ASTRA).exists(),
            "a model over its ceiling must not be billed",
        )

    def test_an_exhausted_day_blocks_when_switching_is_off(self):
        settings_service.set_value("auto_switch_model", False)
        try:
            for key in NEW_MODELS:
                allocation = PackageAllocation.objects.get(user=self.student, ai_model=self.models[key])
                allocation.daily_used = allocation.daily_limit
                allocation.save(update_fields=["daily_used"])

            with patch(COMPLETE, return_value=reply(ASTRA)) as vendor:
                with self.assertRaises(ChatBlocked):
                    chat_service.send_message(self.student, self.conversation, "Hello", model_key=ASTRA)
                vendor.assert_not_called()
        finally:
            settings_service.set_value("auto_switch_model", True)


class RoutingTests(ModelFixture):

    def test_asking_for_each_model_resolves_to_it(self):
        for key in NEW_MODELS:
            with self.subTest(model=key):
                self.assertEqual(model_router.resolve_model(self.student, key).key, key)

    def test_the_picker_offers_them_once_the_student_holds_some(self):
        usable = {row["model"].key for row in wallet_service.overview(self.student) if row["balance"] > 0}
        self.assertTrue(set(NEW_MODELS) <= usable)

    def test_a_disabled_model_is_refused_rather_than_substituted(self):
        model = self.models[ASTRA]
        model.is_active = False
        model.save(update_fields=["is_active"])
        with self.assertRaises(Exception):
            model_router.resolve_model(self.student, ASTRA)

    def test_switching_off_openai_takes_both_offline(self):
        """A model is only as available as its vendor."""
        provider = self.models[ASTRA].provider_ref
        provider.is_active = False
        provider.save(update_fields=["is_active"])
        for key in NEW_MODELS:
            with self.subTest(model=key):
                self.assertFalse(AIModel.objects.get(key=key).is_available)


class ConsoleTests(ModelFixture):
    """The console picks both up without being told they exist."""

    def test_the_model_selector_offers_all_eight(self):
        from AI.services import model_scope

        keys = {model.key for model in model_scope.catalogue()}
        self.assertTrue(set(NEW_MODELS) | set(INCUMBENTS) <= keys)

    def test_each_has_its_own_colour_and_glyph(self):
        from AI.views.console_ai import FALLBACK_TONE, MODEL_TONES, tone_for

        for key in NEW_MODELS:
            with self.subTest(model=key):
                self.assertIsNot(tone_for(self.models[key]), FALLBACK_TONE)
        tones = [(t["to"], t["icon"]) for t in MODEL_TONES.values()]
        self.assertEqual(len({c for c, _ in tones}), len(tones), "no two models share a colour")
        self.assertEqual(len({i for _, i in tones}), len(tones), "no two models share a glyph")

    def test_per_model_analytics_still_sum_to_the_whole(self):
        from AI.services import analytics_service, model_scope

        with patch(COMPLETE, return_value=reply(ASTRA)):
            chat_service.send_message(self.student, self.conversation, "Hello", model_key=ASTRA)

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
        self.assertTrue(set(NEW_MODELS) | set(INCUMBENTS) <= rows)

    def test_low_token_warnings_cover_them(self):
        from AI.services import token_alert_service

        for key in NEW_MODELS:
            with self.subTest(model=key):
                wallet = UserAIWallet.objects.get(user=self.student, ai_model=self.models[key])
                wallet.balance = 100
                wallet.save(update_fields=["balance"])
                self.assertIsNotNone(token_alert_service.tier_for(wallet))


class NoRegressionTests(TestCase):
    """Adding two models moved nothing belonging to the six already there."""

    def test_the_incumbent_models_keep_their_settings(self):
        expected = {
            "gpt-5-mini": (True, True),        # (is_default, auto_grant)
            "gpt-4o": (False, False),
            "claude-haiku-4-5": (False, True),
            "claude-sonnet-5": (False, True),
            "claude-fable-5-1": (False, False),
            "claude-opus-5": (False, False),
        }
        for key, (is_default, auto_grant) in expected.items():
            with self.subTest(model=key):
                model = AIModel.objects.get(key=key)
                self.assertEqual((model.is_default, model.auto_grant), (is_default, auto_grant))
                self.assertTrue(model.is_active)

    def test_exactly_one_model_is_still_the_default(self):
        self.assertEqual(AIModel.objects.filter(is_default=True).count(), 1)

    def test_the_incumbent_headline_prices_are_unchanged(self):
        expected = {
            "gpt-5-mini": "2.49", "claude-haiku-4-5": "3.99", "gpt-4o": "4.99",
            "claude-sonnet-5": "6.49", "claude-fable-5-1": "7.49", "claude-opus-5": "8.99",
        }
        for key, price in expected.items():
            with self.subTest(model=key):
                self.assertEqual(
                    Plan.objects.filter(is_active=True, ai_model__key=key).count(), 3
                )
                self.assertEqual(
                    Plan.objects.filter(is_active=True, ai_model__key=key, tokens=100_000)
                    .order_by("-price").first().price,
                    Decimal(price),
                )

    def test_every_model_has_a_pool(self):
        self.assertEqual(ModelTokenPool.objects.count(), AIModel.objects.count())
