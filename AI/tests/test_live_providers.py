"""End-to-end checks against the **real** OpenAI and Anthropic APIs.

Skipped by default: these call paid endpoints and cost real money, so they only
run when ``AI_LIVE_TESTS=1`` is set. Everything else in the suite mocks the
provider; this module exists so that "it works" can be verified rather than
assumed, and so a change to a vendor's wire format is caught by a test instead
of by a student.

They run against Django's throwaway test database and go through the ordinary
HTTP endpoint the chat UI uses — same routing, same authorisation, same token
accounting. Nothing here touches production data.

    AI_LIVE_TESTS=1 python manage.py test AI.tests.test_live_providers
"""
from __future__ import annotations

import os
import unittest

from django.conf import settings
from django.test import TestCase

from AI.models import AccountStatus, AIModel, AIUsageLog, Message, MessageRole, User
from AI.services import model_pool_service, settings_service, wallet_service

LIVE = os.environ.get("AI_LIVE_TESTS") == "1"

GPT = "gpt-5-mini"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5"

PASSWORD = "Sup3rSecret!x"


@unittest.skipUnless(LIVE, "Set AI_LIVE_TESTS=1 to call the real provider APIs.")
class LiveProviderTests(TestCase):
    """One real exchange per model, billed to that model's wallet and no other."""

    def setUp(self):
        settings_service.bootstrap()
        self.student = User.objects.create_user(
            username="live.student", email="live@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.models = {model.key: model for model in AIModel.objects.all()}

        # The cross-provider fixture from the specification: different amounts
        # per model, so a leak between them is impossible to miss.
        self.opening = {GPT: 10_000, SONNET: 20_000, HAIKU: 30_000}
        for key, amount in self.opening.items():
            model = self.models[key]
            model_pool_service.top_up(model, amount, description="Live test fixture")
            model_pool_service.draw(model, amount, student=self.student)
            wallet_service.credit(self.student, model, amount, reason="Live test allocation")

        self.client.force_login(self.student)

    def balances(self) -> dict[str, int]:
        return {
            key: wallet_service.balance_of(self.student, model)
            for key, model in self.models.items()
        }

    def ask(self, model_key: str, question: str, conversation=None):
        payload = {"message": question, "model": model_key}
        if conversation:
            payload["conversation_id"] = conversation
        return self.client.post(
            "/api/chat/send/", payload, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )

    def _exchange(self, model_key: str, question: str, conversation=None):
        """Send one real message and assert the reply is genuine."""
        response = self.ask(model_key, question, conversation)
        self.assertEqual(
            response.status_code, 200,
            f"{model_key} failed: {response.content[:400]!r}",
        )
        data = response.json()

        answer = data["assistant_message"]["content"]
        self.assertTrue(answer.strip(), f"{model_key} returned an empty answer")
        self.assertEqual(data["assistant_message"]["model"], model_key)

        # Usage must come from the provider, so it cannot be zero on a real call.
        self.assertGreater(data["tokens_used"], 0)
        self.assertGreater(data["tokens_charged"], 0)
        return data

    # -- one model at a time ------------------------------------------------
    @unittest.skipUnless(settings.OPENAI_API_KEY, "OPENAI_API_KEY is not configured.")
    def test_gpt_5_mini_answers_and_bills_only_gpt(self):
        before = self.balances()
        data = self._exchange(GPT, "In one sentence, what is photosynthesis?")
        after = self.balances()

        self.assertLess(after[GPT], before[GPT])
        self.assertEqual(after[SONNET], before[SONNET])
        self.assertEqual(after[HAIKU], before[HAIKU])

        log = AIUsageLog.objects.filter(model_key=GPT, success=True).latest("id")
        self.assertEqual(log.provider, "openai")
        self.assertEqual(log.total_tokens, data["tokens_used"])

    @unittest.skipUnless(settings.ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY is not configured.")
    def test_claude_sonnet_5_answers_and_bills_only_sonnet(self):
        before = self.balances()
        data = self._exchange(SONNET, "In one sentence, what is photosynthesis?")
        after = self.balances()

        self.assertLess(after[SONNET], before[SONNET])
        self.assertEqual(after[GPT], before[GPT])
        self.assertEqual(after[HAIKU], before[HAIKU])

        log = AIUsageLog.objects.filter(model_key=SONNET, success=True).latest("id")
        self.assertEqual(log.provider, "anthropic")
        self.assertEqual(log.total_tokens, data["tokens_used"])
        self.assertGreater(log.input_tokens, 0)
        self.assertGreater(log.output_tokens, 0)

    @unittest.skipUnless(settings.ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY is not configured.")
    def test_claude_haiku_45_answers_and_bills_only_haiku(self):
        before = self.balances()
        data = self._exchange(HAIKU, "In one sentence, what is photosynthesis?")
        after = self.balances()

        self.assertLess(after[HAIKU], before[HAIKU])
        self.assertEqual(after[GPT], before[GPT])
        self.assertEqual(after[SONNET], before[SONNET])

        log = AIUsageLog.objects.filter(model_key=HAIKU, success=True).latest("id")
        self.assertEqual(log.provider, "anthropic")
        # Anthropic echoes a dated snapshot id; the catalogue key stays canonical.
        self.assertTrue(log.model.startswith("claude-haiku-4-5"))

    # -- all three, one conversation ---------------------------------------
    @unittest.skipUnless(
        settings.OPENAI_API_KEY and settings.ANTHROPIC_API_KEY,
        "Both provider keys are required for the cross-provider test.",
    )
    def test_all_three_models_in_one_conversation_bill_three_wallets(self):
        """The acceptance test: zero cross-provider token leakage, for real."""
        question = "Reply with one short sentence about gravity."

        first = self._exchange(GPT, question)
        thread = first["conversation_id"]
        second = self._exchange(SONNET, question, thread)
        third = self._exchange(HAIKU, question, thread)

        after = self.balances()
        spent = {key: self.opening[key] - after[key] for key in self.opening}

        # Every model spent something, and each spent only its own tokens.
        for key in (GPT, SONNET, HAIKU):
            self.assertGreater(spent[key], 0, f"{key} was not charged")

        self.assertEqual(spent[GPT], first["tokens_charged"])
        self.assertEqual(spent[SONNET], second["tokens_charged"])
        self.assertEqual(spent[HAIKU], third["tokens_charged"])

        # One conversation, three providers, and history knows which is which.
        replies = list(
            Message.objects.filter(
                conversation_id=thread, role=MessageRole.ASSISTANT
            ).order_by("id")
        )
        self.assertEqual([m.model_key for m in replies], [GPT, SONNET, HAIKU])
        self.assertTrue(all(m.request_id for m in replies), "a request id is missing")

        providers_used = dict(
            AIUsageLog.objects.filter(success=True).values_list("model_key", "provider")
        )
        self.assertEqual(providers_used[GPT], "openai")
        self.assertEqual(providers_used[SONNET], "anthropic")
        self.assertEqual(providers_used[HAIKU], "anthropic")

    # -- refusal path -------------------------------------------------------
    @unittest.skipUnless(settings.ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY is not configured.")
    def test_an_unfunded_model_is_refused_without_spending_a_cent(self):
        model = self.models[SONNET]
        wallet_service.reclaim(
            self.student, model, self.opening[SONNET], reason="Emptied for the test"
        )

        before_calls = AIUsageLog.objects.count()
        response = self.ask(SONNET, "This must never reach Anthropic.")

        self.assertEqual(response.status_code, 402)
        self.assertIn(AIModel.objects.get(key=SONNET).name, response.json()["error"])
        # No provider call was made at all.
        self.assertEqual(AIUsageLog.objects.count(), before_calls)
