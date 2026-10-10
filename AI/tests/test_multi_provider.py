"""Multi-provider routing and per-model token accounting.

The property under test throughout is **isolation**: spending on one model must
never move another model's balance, and a student must never reach a model they
were not given.  Everything else here exists to prove that property holds under
the conditions that would break it — a wrong model key, a revoked model, an
empty pool, a provider outage, a concurrent second request.
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from AI.models import (
    AccountStatus,
    AIModel,
    AIProvider,
    AITokenTransaction,
    AIUsageLog,
    ModelTokenPool,
    Role,
    User,
    UserAIWallet,
    WalletTransactionType,
)
from AI.services import (
    chat_service,
    distribution_service,
    model_pool_service,
    model_router,
    providers,
    settings_service,
    wallet_service,
)
from AI.services.chat_service import ChatBlocked
from AI.services.model_pool_service import InsufficientPool
from AI.services.providers.base import AIResponse, ChatTurn
from AI.services.wallet_service import InsufficientModelTokens, ModelAccessDenied

PASSWORD = "Sup3rSecret!x"

GPT = "gpt-5-mini"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5"
#: The fourth model. It is funded by an administrator rather than by a
#: signup, so in these fixtures it is always present and always zero — which
#: is the point: spending on the other three must never move it.
GPT4O = "gpt-4o"


def reply(model: str, *, input_tokens: int = 100, output_tokens: int = 200) -> AIResponse:
    return AIResponse(
        content=f"Answer from {model}.",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        model=model,
        request_id=f"req-{model}",
        latency_ms=400,
    )


class MultiProviderTestCase(TestCase):
    """Shared fixture: three models, three funded pools, one student."""

    def setUp(self):
        settings_service.bootstrap()
        self.admin = User.objects.create_superuser(
            username="root", email="root@test.local", password=PASSWORD
        )
        self.student = User.objects.create_user(
            username="student1", email="s1@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.models = {model.key: model for model in AIModel.objects.all()}
        for model in self.models.values():
            model_pool_service.get_pool(model)

    def fund_pool(self, key: str, amount: int = 1_000_000):
        model = self.models[key]
        pool = ModelTokenPool.objects.get(ai_model=model)
        if pool.balance < amount:
            model_pool_service.top_up(model, amount - pool.balance, created_by=self.admin)
        return model

    def give(self, key: str, amount: int, student=None):
        model = self.fund_pool(key)
        return distribution_service.distribute(
            student or self.student, model, amount,
            action="add", performed_by=self.admin, notify=False,
        )

    def balances(self, student=None) -> dict[str, int]:
        target = student or self.student
        return {
            key: wallet_service.balance_of(target, model)
            for key, model in self.models.items()
        }

    def expect(self, **named) -> dict[str, int]:
        """The balances named here; every other model in the catalogue at zero.

        Written this way rather than as a literal dict so these assertions keep
        meaning "and nothing else moved" as models are added. A frozen
        four-key literal stopped checking the fifth and sixth models the day
        they arrived — which is exactly when "nothing else moved" is worth
        checking.
        """
        expected = {key: 0 for key in self.models}
        expected.update(named)
        return expected


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
class CatalogueTests(MultiProviderTestCase):
    def test_every_model_is_seeded_against_its_own_provider(self):
        # Not an exact census: asserting the catalogue's contents here made this
        # test fail the day a model was added, which says nothing about
        # providers. What matters is that each model names the right vendor and
        # that no two share a currency — and that check now covers every model
        # in the catalogue, however many there are.
        self.assertTrue({GPT, SONNET, HAIKU, GPT4O} <= set(self.models))
        self.assertEqual(self.models[GPT].provider, "openai")
        self.assertEqual(self.models[GPT4O].provider, "openai")
        self.assertEqual(self.models[SONNET].provider, "anthropic")
        self.assertEqual(self.models[HAIKU].provider, "anthropic")

        # Each model is its own currency — including the two that share a
        # vendor, which is the case where a shared currency would go unnoticed.
        token_types = {model.token_type for model in self.models.values()}
        self.assertEqual(len(token_types), len(self.models))

    def test_every_model_names_a_provider_that_actually_exists(self):
        for model in self.models.values():
            self.assertIn(model.provider, providers.available_slugs())
            self.assertIsNotNone(model.provider_ref_id)

    def test_each_model_has_its_own_pool(self):
        pools = ModelTokenPool.objects.filter(ai_model__in=self.models.values())
        self.assertEqual(pools.count(), len(self.models))


# ---------------------------------------------------------------------------
# Ownership: admin holds everything, students start empty
# ---------------------------------------------------------------------------
class InitialOwnershipTests(MultiProviderTestCase):
    def test_a_new_student_holds_nothing_on_any_model(self):
        fresh = User.objects.create_user(
            username="fresh", email="fresh@test.local", password=PASSWORD
        )
        self.assertEqual(self.balances(fresh), self.expect())

    def test_a_student_with_no_tokens_can_use_no_model(self):
        self.assertEqual(wallet_service.usable_models(self.student), [])

    def test_the_admin_pool_holds_the_supply(self):
        for key in (GPT, SONNET, HAIKU):
            self.fund_pool(key, 1_000_000)
            self.assertEqual(model_pool_service.balance(self.models[key]), 1_000_000)


# ---------------------------------------------------------------------------
# Distribution
# ---------------------------------------------------------------------------
class DistributionTests(MultiProviderTestCase):
    def test_distributing_one_model_leaves_the_others_untouched(self):
        self.give(SONNET, 50_000)

        self.assertEqual(self.balances(), self.expect(**{SONNET: 50_000}))
        self.assertEqual(model_pool_service.balance(self.models[SONNET]), 950_000)
        # The other pools never moved.
        self.assertEqual(model_pool_service.balance(self.models[GPT]), 1_000_000)
        self.assertEqual(model_pool_service.balance(self.models[HAIKU]), 1_000_000)

    def test_set_draws_only_the_difference_from_the_pool(self):
        self.give(SONNET, 20_000)
        before = model_pool_service.balance(self.models[SONNET])

        distribution_service.distribute(
            self.student, self.models[SONNET], 70_000,
            action="set", performed_by=self.admin, notify=False,
        )

        self.assertEqual(wallet_service.balance_of(self.student, self.models[SONNET]), 70_000)
        # Topping 20,000 up to 70,000 costs the pool 50,000, not 70,000.
        self.assertEqual(model_pool_service.balance(self.models[SONNET]), before - 50_000)

    def test_reclaimed_tokens_go_back_to_that_model_pool(self):
        self.give(HAIKU, 30_000)
        before = model_pool_service.balance(self.models[HAIKU])

        distribution_service.distribute(
            self.student, self.models[HAIKU], 10_000,
            action="remove", performed_by=self.admin, notify=False,
        )

        self.assertEqual(wallet_service.balance_of(self.student, self.models[HAIKU]), 20_000)
        self.assertEqual(model_pool_service.balance(self.models[HAIKU]), before + 10_000)

    def test_an_empty_pool_refuses_rather_than_overdrawing(self):
        model = self.models[SONNET]
        ModelTokenPool.objects.filter(ai_model=model).update(balance=1_000)

        with self.assertRaises(InsufficientPool):
            distribution_service.distribute(
                self.student, model, 5_000, action="add",
                performed_by=self.admin, notify=False,
            )

        # Neither side moved.
        self.assertEqual(wallet_service.balance_of(self.student, model), 0)
        self.assertEqual(model_pool_service.balance(model), 1_000)

    def test_every_movement_is_written_to_the_ledger_with_both_balances(self):
        self.give(GPT, 25_000)

        entry = AITokenTransaction.objects.get(
            user=self.student, ai_model=self.models[GPT],
            transaction_type=WalletTransactionType.ADMIN_ALLOCATION,
        )
        self.assertEqual(entry.amount, 25_000)
        self.assertEqual(entry.balance_before, 0)
        self.assertEqual(entry.balance_after, 25_000)
        self.assertEqual(entry.performed_by, self.admin)

    def test_preview_reports_what_would_happen_without_writing(self):
        self.give(SONNET, 20_000)
        plan = distribution_service.preview(self.student, self.models[SONNET], 50_000, "add")

        self.assertEqual(plan["current_balance"], 20_000)
        self.assertEqual(plan["new_balance"], 70_000)
        self.assertEqual(plan["pool_after"], plan["pool_before"] - 50_000)
        self.assertTrue(plan["affordable"])
        # Nothing was written.
        self.assertEqual(wallet_service.balance_of(self.student, self.models[SONNET]), 20_000)

    def test_a_manual_adjustment_requires_a_reason(self):
        with self.assertRaises(ValueError):
            distribution_service.adjust(
                self.student, self.models[GPT], 500, reason="  ", performed_by=self.admin
            )


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
class RoutingTests(MultiProviderTestCase):
    def test_each_model_routes_to_its_own_provider_implementation(self):
        for key, expected in (
            (GPT, providers.OpenAICompatibleProvider),
            (SONNET, providers.AnthropicProvider),
            (HAIKU, providers.AnthropicProvider),
        ):
            self.give(key, 10_000)
            with patch.object(providers.REGISTRY[self.models[key].provider], "is_configured", True):
                route = model_router.route(self.student, key)
            self.assertIsInstance(route.provider, expected)
            self.assertEqual(route.api_model_id, key)

    def test_a_student_without_tokens_is_refused_before_any_provider_call(self):
        self.give(SONNET, 10_000)
        with self.assertRaises(InsufficientModelTokens) as caught:
            model_router.route(self.student, HAIKU)
        self.assertEqual(caught.exception.ai_model.pk, self.models[HAIKU].pk)
        self.assertIn(self.models[HAIKU].name, caught.exception.friendly_message)

    def test_revoked_access_is_refused_even_with_a_balance(self):
        self.give(SONNET, 10_000)
        wallet_service.set_access(self.student, self.models[SONNET], False)

        with self.assertRaises(ModelAccessDenied):
            model_router.route(self.student, SONNET)

    def test_the_landing_model_is_one_the_student_can_actually_use(self):
        # Only Haiku is funded, so the picker must not open on the default.
        self.give(HAIKU, 10_000)
        self.assertEqual(model_router.preferred_model(self.student).key, HAIKU)

    def test_an_unknown_model_key_falls_back_instead_of_erroring(self):
        self.give(GPT, 10_000)
        self.assertEqual(model_router.resolve_model(self.student, "not-a-model").key, GPT)

    def test_a_disabled_model_is_refused_rather_than_substituted(self):
        self.give(SONNET, 10_000)
        AIModel.objects.filter(key=SONNET).update(is_active=False)

        from AI.services.model_service import ModelUnavailable

        with self.assertRaises(ModelUnavailable):
            model_router.resolve_model(self.student, SONNET)


# ---------------------------------------------------------------------------
# Chat: the isolation guarantee
# ---------------------------------------------------------------------------
class ChatIsolationTests(MultiProviderTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student)

    def _send(self, model_key: str, message: str = "Explain gravity", conversation=None):
        payload = {"message": message, "model": model_key}
        if conversation:
            payload["conversation_id"] = conversation
        return self.client.post(
            "/api/chat/send/", payload, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )

    def test_gpt_spends_only_gpt_tokens(self):
        self.give(GPT, 10_000)
        self.give(SONNET, 20_000)
        self.give(HAIKU, 30_000)

        with patch.object(providers.OpenAICompatibleProvider, "is_configured", True), \
             patch.object(providers.OpenAICompatibleProvider, "complete") as complete:
            complete.return_value = reply(GPT, input_tokens=100, output_tokens=200)
            response = self._send(GPT)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["tokens_charged"], 300)
        self.assertEqual(self.balances(), self.expect(**{GPT: 9_700, SONNET: 20_000, HAIKU: 30_000}))

    def test_sonnet_spends_only_sonnet_tokens(self):
        self.give(GPT, 10_000)
        self.give(SONNET, 20_000)
        self.give(HAIKU, 30_000)

        with patch.object(providers.AnthropicProvider, "is_configured", True), \
             patch.object(providers.AnthropicProvider, "complete") as complete:
            complete.return_value = reply(SONNET, input_tokens=150, output_tokens=350)
            response = self._send(SONNET)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tokens_charged"], 500)
        self.assertEqual(self.balances(), self.expect(**{GPT: 10_000, SONNET: 19_500, HAIKU: 30_000}))

    def test_haiku_spends_only_haiku_tokens(self):
        self.give(GPT, 10_000)
        self.give(SONNET, 20_000)
        self.give(HAIKU, 30_000)

        with patch.object(providers.AnthropicProvider, "is_configured", True), \
             patch.object(providers.AnthropicProvider, "complete") as complete:
            complete.return_value = reply(HAIKU, input_tokens=40, output_tokens=60)
            response = self._send(HAIKU)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tokens_charged"], 100)
        self.assertEqual(self.balances(), self.expect(**{GPT: 10_000, SONNET: 20_000, HAIKU: 29_900}))

    def test_three_models_in_one_conversation_bill_three_wallets(self):
        """The cross-provider test: one student, one chat, zero leakage."""
        self.give(GPT, 10_000)
        self.give(SONNET, 20_000)
        self.give(HAIKU, 30_000)

        with patch.object(providers.OpenAICompatibleProvider, "is_configured", True), \
             patch.object(providers.OpenAICompatibleProvider, "complete") as openai_call, \
             patch.object(providers.AnthropicProvider, "is_configured", True), \
             patch.object(providers.AnthropicProvider, "complete") as anthropic_call:
            openai_call.return_value = reply(GPT, input_tokens=100, output_tokens=100)
            first = self._send(GPT, "First")
            self.assertEqual(first.status_code, 200)
            # One conversation, three models — the hardest case for isolation.
            thread = first.json()["conversation_id"]

            anthropic_call.return_value = reply(SONNET, input_tokens=200, output_tokens=200)
            self.assertEqual(self._send(SONNET, "Second", thread).status_code, 200)

            anthropic_call.return_value = reply(HAIKU, input_tokens=300, output_tokens=300)
            self.assertEqual(self._send(HAIKU, "Third", thread).status_code, 200)

        self.assertEqual(self.balances(), self.expect(**{GPT: 9_800, SONNET: 19_600, HAIKU: 29_400}))

        # Each provider was called for its own model, and only for its own.
        self.assertEqual(openai_call.call_count, 1)
        self.assertEqual(anthropic_call.call_count, 2)

        # And the usage log agrees about who answered.
        logged = dict(
            AIUsageLog.objects.filter(success=True).values_list("model_key", "provider")
        )
        self.assertEqual(logged[GPT], "openai")
        self.assertEqual(logged[SONNET], "anthropic")
        self.assertEqual(logged[HAIKU], "anthropic")

    def test_every_assistant_message_records_which_model_produced_it(self):
        self.give(GPT, 10_000)
        self.give(SONNET, 20_000)

        with patch.object(providers.OpenAICompatibleProvider, "is_configured", True), \
             patch.object(providers.OpenAICompatibleProvider, "complete") as openai_call, \
             patch.object(providers.AnthropicProvider, "is_configured", True), \
             patch.object(providers.AnthropicProvider, "complete") as anthropic_call:
            openai_call.return_value = reply(GPT)
            first = self._send(GPT, "First").json()
            anthropic_call.return_value = reply(SONNET)
            second = self._send(SONNET, "Second", first["conversation_id"]).json()

        # Same conversation, different providers — history must say which.
        from AI.models import Conversation, Message, MessageRole

        conversation = Conversation.objects.get(user=self.student)
        replies = list(
            Message.objects.filter(conversation=conversation, role=MessageRole.ASSISTANT)
            .order_by("id")
        )
        self.assertEqual([m.model_key for m in replies], [GPT, SONNET])
        self.assertTrue(all(m.request_id for m in replies))
        self.assertEqual(first["assistant_message"]["model"], GPT)
        self.assertEqual(second["assistant_message"]["model"], SONNET)

    def test_a_model_the_student_cannot_pay_for_is_never_called(self):
        """Sonnet is not called, and not billed, because it cannot be paid for.

        The message is not refused any more — this student holds GPT tokens, so
        it is answered with those and the transcript says so. The guarantee
        that matters is unchanged: a vendor is never called for a model the
        student cannot pay for.
        """
        self.give(GPT, 10_000)

        with patch.object(providers.AnthropicProvider, "is_configured", True), \
             patch.object(providers.AnthropicProvider, "complete") as claude, \
             patch.object(providers.OpenAICompatibleProvider, "is_configured", True), \
             patch.object(providers.OpenAICompatibleProvider, "complete") as gpt:
            gpt.return_value = reply(GPT, input_tokens=100, output_tokens=200)
            response = self._send(SONNET)

        self.assertEqual(response.status_code, 200)
        claude.assert_not_called()
        payload = response.json()
        self.assertEqual(payload["switched_from"], SONNET)
        self.assertEqual(payload["switched_to"], GPT)
        # Sonnet is untouched; only the model that answered was charged.
        self.assertEqual(self.balances(), self.expect(**{GPT: 9_700}))

    def test_a_model_the_student_cannot_pay_for_is_refused_when_nothing_else_can_answer(self):
        """With no other funded model, the refusal is exactly as it was."""
        with patch.object(providers.AnthropicProvider, "is_configured", True), \
             patch.object(providers.AnthropicProvider, "complete") as complete:
            response = self._send(SONNET)

        self.assertEqual(response.status_code, 402)
        self.assertIn(self.models[SONNET].name, response.json()["error"])
        complete.assert_not_called()
        self.assertEqual(self.balances(), self.expect())

    def test_a_failed_provider_call_charges_nothing(self):
        self.give(SONNET, 20_000)

        from AI.services.providers.base import AIError

        with patch.object(providers.AnthropicProvider, "is_configured", True), \
             patch.object(providers.AnthropicProvider, "complete") as complete:
            complete.side_effect = AIError("The assistant is busy.", code="rate_limited")
            response = self._send(SONNET)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(wallet_service.balance_of(self.student, self.models[SONNET]), 20_000)
        self.assertTrue(AIUsageLog.objects.filter(success=False, model_key=SONNET).exists())

    def test_one_provider_failing_leaves_the_other_working(self):
        """An Anthropic outage must not stop GPT answering."""
        self.give(GPT, 10_000)
        self.give(SONNET, 20_000)

        AIProvider.objects.filter(slug="anthropic").update(is_active=False)

        with patch.object(providers.OpenAICompatibleProvider, "is_configured", True), \
             patch.object(providers.OpenAICompatibleProvider, "complete") as complete:
            complete.return_value = reply(GPT, input_tokens=50, output_tokens=50)
            blocked = self._send(SONNET)
            working = self._send(GPT)

        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(working.status_code, 200)
        self.assertEqual(wallet_service.balance_of(self.student, self.models[GPT]), 9_900)
        self.assertEqual(wallet_service.balance_of(self.student, self.models[SONNET]), 20_000)

    def test_the_balance_payload_describes_the_selected_model(self):
        self.give(GPT, 10_000)
        self.give(SONNET, 20_000)

        payload = chat_service.balance_payload(self.student, ai_model=self.models[SONNET])
        self.assertEqual(payload["model"], SONNET)
        self.assertEqual(payload["balance"], 20_000)
        # Every model is listed, so the picker can show and explain each one.
        listed = {row["key"]: row for row in payload["models"]}
        self.assertEqual(listed[GPT]["balance"], 10_000)
        self.assertFalse(listed[HAIKU]["usable"])
        self.assertIn(self.models[HAIKU].name, listed[HAIKU]["reason"])


# ---------------------------------------------------------------------------
# Accounting invariants
# ---------------------------------------------------------------------------
class AccountingTests(MultiProviderTestCase):
    def test_a_balance_never_goes_negative(self):
        model = self.give(SONNET, 1_000).ai_model
        wallet_service.consume(self.student, model, 5_000, reason="overspend", clamp=True)
        self.assertEqual(wallet_service.balance_of(self.student, model), 0)

    def test_a_second_request_cannot_spend_the_same_tokens(self):
        """Two debits against one balance: the second sees the first."""
        self.give(SONNET, 1_000)
        model = self.models[SONNET]

        wallet_service.consume(self.student, model, 800, reason="first", clamp=False)
        with self.assertRaises(InsufficientModelTokens):
            wallet_service.consume(self.student, model, 800, reason="second", clamp=False)

        self.assertEqual(wallet_service.balance_of(self.student, model), 200)

    def test_allocated_minus_used_always_equals_the_balance(self):
        self.give(GPT, 50_000)
        model = self.models[GPT]
        wallet_service.consume(self.student, model, 12_000, reason="chat")

        wallet = UserAIWallet.objects.get(user=self.student, ai_model=model)
        self.assertEqual(wallet.balance, wallet.allocated_tokens - wallet.used_tokens)
        self.assertEqual(wallet.balance, 38_000)

    def test_a_credit_with_the_same_reference_is_applied_once(self):
        model = self.fund_pool(SONNET)
        for _ in range(3):
            wallet_service.credit(
                self.student, model, 5_000, reason="trial", reference_id="trial:once"
            )
        self.assertEqual(wallet_service.balance_of(self.student, model), 5_000)

    def test_the_legacy_total_mirrors_the_sum_of_every_wallet(self):
        self.give(GPT, 10_000)
        self.give(SONNET, 20_000)
        self.give(HAIKU, 30_000)

        from AI.models import TokenAccount

        account = TokenAccount.objects.get(user=self.student)
        self.assertEqual(account.balance, 60_000)
        self.assertEqual(wallet_service.totals_of(self.student)["balance"], 60_000)

    def test_a_wallet_can_be_rebuilt_from_its_ledger(self):
        self.give(GPT, 40_000)
        model = self.models[GPT]
        wallet_service.consume(self.student, model, 7_500, reason="chat")

        UserAIWallet.objects.filter(user=self.student, ai_model=model).update(balance=999_999)
        rebuilt = wallet_service.recalculate(self.student, model)
        self.assertEqual(rebuilt.balance, 32_500)
        self.assertEqual(rebuilt.used_tokens, 7_500)


# ---------------------------------------------------------------------------
# The Anthropic wire format
# ---------------------------------------------------------------------------
class AnthropicPayloadTests(TestCase):
    """What actually goes to Anthropic, without going to Anthropic."""

    def build(self, key: str = SONNET, **overrides):
        model = AIModel.objects.get(key=key)
        provider = providers.for_model(model)
        for attribute, value in overrides.items():
            setattr(provider, attribute, value)
        return provider

    def _capture(self, provider, turns, **kwargs):
        """Run complete() and return the kwargs it would have sent."""
        captured = {}

        class _Usage:
            input_tokens = 11
            output_tokens = 22

        class _Block:
            type = "text"
            text = "ok"

        class _Response:
            content = [_Block()]
            usage = _Usage()
            stop_reason = "end_turn"
            model = provider.model
            id = "msg_1"

        class _Messages:
            def create(self, **payload):
                captured.update(payload)
                return _Response()

        class _Client:
            messages = _Messages()

        with patch.object(provider, "_client", return_value=_Client()):
            provider.complete(turns, **kwargs)
        return captured

    def test_the_system_prompt_is_a_top_level_parameter_not_a_message(self):
        provider = self.build(api_key="test-key")
        payload = self._capture(
            provider, [ChatTurn(role="user", content="Hi")], system_prompt="Be helpful."
        )
        self.assertEqual(payload["system"], "Be helpful.")
        self.assertEqual([m["role"] for m in payload["messages"]], ["user"])

    def test_max_tokens_is_always_sent(self):
        provider = self.build(api_key="test-key")
        payload = self._capture(provider, [ChatTurn(role="user", content="Hi")])
        self.assertIn("max_tokens", payload)
        self.assertGreater(payload["max_tokens"], 0)

    def test_no_sampling_parameters_are_ever_sent(self):
        """The Messages API has no temperature or top_p — sending either fails.

        Regression guard: an earlier version sent a temperature to the older
        model family and every Haiku request died in the SDK before it left the
        machine. Both families are checked, because the mistake was to assume
        they differed here.
        """
        for key in (SONNET, HAIKU):
            provider = self.build(key, api_key="test-key")
            payload = self._capture(provider, [ChatTurn(role="user", content="Hi")])
            self.assertNotIn("temperature", payload, key)
            self.assertNotIn("top_p", payload, key)
            self.assertNotIn("top_k", payload, key)

    def test_only_the_thinking_dialect_differs_between_model_families(self):
        self.assertTrue(self.build(SONNET, api_key="test-key").uses_adaptive_thinking)
        self.assertFalse(self.build(HAIKU, api_key="test-key").uses_adaptive_thinking)

    def test_thinking_is_off_by_default_so_students_are_not_billed_for_it(self):
        provider = self.build(SONNET, api_key="test-key")
        payload = self._capture(provider, [ChatTurn(role="user", content="Hi")])
        self.assertNotIn("thinking", payload)

    def test_adaptive_thinking_uses_each_family_s_own_dialect(self):
        sonnet = self.build(SONNET, api_key="test-key", thinking_mode="adaptive")
        self.assertEqual(
            self._capture(sonnet, [ChatTurn(role="user", content="Hi")])["thinking"],
            {"type": "adaptive"},
        )

        haiku = self.build(HAIKU, api_key="test-key", thinking_mode="adaptive")
        thinking = self._capture(haiku, [ChatTurn(role="user", content="Hi")])["thinking"]
        self.assertEqual(thinking["type"], "enabled")
        self.assertGreaterEqual(thinking["budget_tokens"], 1024)

    def test_a_conversation_is_trimmed_to_start_with_the_user(self):
        provider = self.build(api_key="test-key")
        payload = self._capture(
            provider,
            [
                ChatTurn(role="assistant", content="Earlier reply"),
                ChatTurn(role="user", content="Question"),
            ],
        )
        self.assertEqual([m["role"] for m in payload["messages"]], ["user"])

    def test_an_image_becomes_a_base64_source_block(self):
        provider = self.build(api_key="test-key")
        tiny = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        payload = self._capture(
            provider, [ChatTurn(role="user", content="What is this?", image_data_urls=[tiny])]
        )
        blocks = payload["messages"][0]["content"]
        image = next(block for block in blocks if block["type"] == "image")
        self.assertEqual(image["source"]["type"], "base64")
        self.assertEqual(image["source"]["media_type"], "image/png")
        self.assertNotIn("data:", image["source"]["data"])

    def test_a_malformed_image_is_dropped_rather_than_sent(self):
        provider = self.build(api_key="test-key")
        payload = self._capture(
            provider,
            [ChatTurn(role="user", content="Look", image_data_urls=["https://example.com/x.png"])],
        )
        # Falls back to the text alone instead of producing a 400.
        content = payload["messages"][0]["content"]
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        self.assertEqual([block["type"] for block in blocks], ["text"])
        self.assertEqual(blocks[0]["text"], "Look")

    def test_usage_is_taken_from_the_provider_and_never_estimated(self):
        provider = self.build(api_key="test-key")

        class _Usage:
            input_tokens = 137
            output_tokens = 419

        class _Block:
            type = "text"
            text = "A real answer."

        class _Response:
            content = [_Block()]
            usage = _Usage()
            stop_reason = "end_turn"
            model = SONNET
            id = "msg_x"

        class _Messages:
            def create(self, **payload):
                return _Response()

        class _Client:
            messages = _Messages()

        with patch.object(provider, "_client", return_value=_Client()):
            result = provider.complete([ChatTurn(role="user", content="Hi")])

        self.assertEqual(result.input_tokens, 137)
        self.assertEqual(result.output_tokens, 419)
        self.assertEqual(result.total_tokens, 556)


# ---------------------------------------------------------------------------
# The OpenAI wire format
# ---------------------------------------------------------------------------
class OpenAIPayloadTests(TestCase):
    """Cost control on the reasoning family, which bills thinking as output."""

    def _capture(self, model_key: str, **overrides):
        model = AIModel.objects.get(key=model_key)
        provider = providers.for_model(model)
        provider.api_key = "test-key"
        for attribute, value in overrides.items():
            setattr(provider, attribute, value)

        captured = {}

        class _Response:
            status_code = 200
            headers = {}

            @staticmethod
            def json():
                return {
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    "model": model_key,
                    "id": "chatcmpl-test",
                }

        def fake_post(url, headers=None, data=None, timeout=None):
            captured.update(json.loads(data))
            return _Response()

        with patch("AI.services.providers.openai_provider.requests.post", fake_post):
            provider.complete([ChatTurn(role="user", content="Hi")])
        return captured

    def test_a_reasoning_model_is_asked_to_think_less(self):
        """Hidden reasoning is billed as output and ignores "be brief"."""
        payload = self._capture(GPT)
        self.assertEqual(payload["reasoning_effort"], "low")
        # Reasoning models renamed the output limit and reject temperature.
        self.assertIn("max_completion_tokens", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("temperature", payload)

    def test_the_catalogue_can_turn_the_reasoning_back_up(self):
        payload = self._capture(GPT, thinking_mode="adaptive")
        self.assertNotIn("reasoning_effort", payload)

    def test_a_non_reasoning_model_keeps_its_sampling_controls(self):
        model = AIModel.objects.create(
            key="gpt-4o-mini", name="Legacy", provider="openai",
            provider_ref=AIProvider.objects.get(slug="openai"),
        )
        payload = self._capture(model.key)
        self.assertIn("max_tokens", payload)
        self.assertIn("temperature", payload)
        self.assertNotIn("reasoning_effort", payload)


# ---------------------------------------------------------------------------
# Admin surfaces
# ---------------------------------------------------------------------------
class ConsoleTests(MultiProviderTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_the_dashboard_renders_a_separate_section_per_model(self):
        """Every model gets its own panel, not one shared figure.

        The old assertion looked for the phrase once and in one exact casing,
        so it failed the day the label was capitalised while still saying the
        same thing — and it would have passed if five of the six panels had
        vanished. Counting them checks what the test is named for.
        """
        response = self.client.get("/admin/ai-tokens/")
        self.assertEqual(response.status_code, 200)
        for name in (model.name for model in self.models.values()):
            self.assertContains(response, name)

        body = response.content.decode()
        panels = body.lower().count("available to distribute")
        self.assertEqual(
            panels,
            len(self.models),
            f"expected one 'available to distribute' panel per model "
            f"({len(self.models)}), found {panels}",
        )

    def test_the_distribution_page_renders(self):
        response = self.client.get(f"/admin/ai-tokens/distribute/?student={self.student.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.student.display_name)

    def test_preview_returns_server_computed_figures(self):
        self.give(SONNET, 20_000)
        response = self.client.get(
            "/admin/ai-tokens/distribute/preview/",
            {
                "user_id": self.student.pk,
                "model_id": self.models[SONNET].pk,
                "action": "add",
                "amount": 50_000,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["current_balance"], 20_000)
        self.assertEqual(data["new_balance"], 70_000)
        self.assertEqual(data["pool_after"], data["pool_before"] - 50_000)

    def test_applying_a_distribution_moves_only_the_chosen_model(self):
        self.fund_pool(SONNET)
        response = self.client.post(
            "/admin/ai-tokens/distribute/apply/",
            {
                "user_id": self.student.pk,
                "model_id": self.models[SONNET].pk,
                "action": "add",
                "amount": 50_000,
                "reason": "Term 2",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.balances(), self.expect(**{SONNET: 50_000}))

    def test_a_student_cannot_reach_the_admin_pages(self):
        self.client.force_login(self.student)
        for url in ("/admin/ai-tokens/", "/admin/ai-tokens/distribute/"):
            self.assertIn(self.client.get(url).status_code, (302, 403, 404))

    def test_a_student_cannot_distribute_tokens_to_themselves(self):
        self.client.force_login(self.student)
        self.fund_pool(SONNET)
        self.client.post(
            "/admin/ai-tokens/distribute/apply/",
            {
                "user_id": self.student.pk,
                "model_id": self.models[SONNET].pk,
                "action": "add",
                "amount": 999_999,
            },
        )
        self.assertEqual(wallet_service.balance_of(self.student, self.models[SONNET]), 0)


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
class CredentialTests(MultiProviderTestCase):
    def test_no_api_key_reaches_the_chat_page(self):
        from django.conf import settings

        self.give(GPT, 10_000)
        self.client.force_login(self.student)
        body = self.client.get("/chat/").content.decode()

        for secret in (settings.OPENAI_API_KEY, settings.ANTHROPIC_API_KEY):
            if secret:
                self.assertNotIn(secret, body)
        self.assertNotIn("sk-ant-", body)
        self.assertNotIn("sk-proj-", body)

    def test_provider_status_reports_configuration_without_revealing_it(self):
        from AI.services.ai_service import provider_status

        status = provider_status()
        self.assertIn("providers", status)
        self.assertEqual(set(status["providers"]), set(providers.available_slugs()))
        for value in status["providers"].values():
            self.assertIsInstance(value, bool)
