"""Falling back to another model, and saying so.

A student who reaches a ceiling used to be refused mid-thought and left to work
out which of their other models still had room. Now the platform picks one —
but switching what somebody is billed for is not a quiet thing to do, so what
is tested here is as much about the fences as the feature: only capacity
refusals, only models they already hold, and never without telling them.
"""
from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from AI.models import (
    AccountStatus,
    AIModel,
    AIUsageLog,
    PackageAllocation,
    Plan,
    User,
    UserAIWallet,
)
from AI.services import chat_service, settings_service, wallet_service
from AI.services.chat_service import ChatBlocked
from AI.services.providers.base import AIResponse

PASSWORD = "Sup3rSecret!x"
PRIMARY = "gpt-5-mini"
FALLBACK = "claude-haiku-4-5"

OPENAI = "AI.services.providers.openai_provider.OpenAICompatibleProvider.complete"
ANTHROPIC = "AI.services.providers.anthropic_provider.AnthropicProvider.complete"


def reply(model: str, *, inp: int = 100, out: int = 200) -> AIResponse:
    return AIResponse(
        content="Answer.", input_tokens=inp, output_tokens=out, total_tokens=inp + out,
        model=model, request_id="req", latency_ms=100,
    )


class SwitchFixture(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        settings_service.set_value("auto_switch_model", True)
        self.student = User.objects.create_user(
            username="switcher", email="s@switch.test", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.primary = AIModel.objects.get(key=PRIMARY)
        self.fallback = AIModel.objects.get(key=FALLBACK)
        self.conversation = chat_service.create_conversation(self.student)

    def fund(self, model, amount, plan_tokens=None):
        plan = Plan.objects.filter(is_active=True, ai_model=model).order_by("tokens").first()
        wallet_service.credit(
            self.student, model, plan_tokens or amount,
            transaction_type="plan_purchase", reason="test", plan=plan,
        )
        if plan_tokens and plan_tokens != amount:
            wallet = UserAIWallet.objects.get(user=self.student, ai_model=model)
            wallet.balance = amount
            wallet.save(update_fields=["balance"])

    def exhaust_window(self, model):
        allocation = PackageAllocation.objects.get(user=self.student, ai_model=model)
        allocation.daily_used = allocation.daily_limit
        allocation.save(update_fields=["daily_used"])

    def send(self, model_key=PRIMARY):
        with patch(OPENAI, side_effect=lambda *a, **k: reply(PRIMARY)), \
             patch(ANTHROPIC, side_effect=lambda *a, **k: reply(FALLBACK)):
            return chat_service.send_message(
                self.student, self.conversation, "Hello", model_key=model_key,
            )


class FallbackTests(SwitchFixture):

    def test_a_used_up_window_moves_the_answer_to_another_model(self):
        self.fund(self.primary, 100_000, plan_tokens=100_000)
        self.fund(self.fallback, 100_000, plan_tokens=100_000)
        self.exhaust_window(self.primary)

        data = self.send()
        self.assertEqual(data["switched_from"], PRIMARY)
        self.assertEqual(data["switched_to"], FALLBACK)
        self.assertEqual(data["switched_to_name"], self.fallback.name)
        # And it was billed to the model that actually answered.
        self.assertEqual(
            AIUsageLog.objects.filter(user=self.student).latest("id").model_key, FALLBACK
        )

    def test_an_empty_balance_moves_the_answer_too(self):
        self.fund(self.primary, 0, plan_tokens=100_000)
        self.fund(self.fallback, 100_000, plan_tokens=100_000)
        data = self.send()
        self.assertEqual(data["switched_to"], FALLBACK)

    def test_nothing_is_reported_when_no_switch_happened(self):
        self.fund(self.primary, 100_000, plan_tokens=100_000)
        data = self.send()
        self.assertEqual(data["switched_from"], "")
        self.assertEqual(data["switched_to"], "")
        self.assertEqual(
            AIUsageLog.objects.filter(user=self.student).latest("id").model_key, PRIMARY
        )

    def test_the_fallback_is_never_a_model_the_student_has_no_tokens_for(self):
        """It may only spend a currency they already own."""
        self.fund(self.primary, 0, plan_tokens=100_000)
        with self.assertRaises(ChatBlocked):
            self.send()

    def test_the_original_refusal_survives_when_nothing_can_answer(self):
        """The real reason is more useful than "no model is available"."""
        self.fund(self.primary, 100_000, plan_tokens=100_000)
        self.fund(self.fallback, 100_000, plan_tokens=100_000)
        self.exhaust_window(self.primary)
        self.exhaust_window(self.fallback)
        with self.assertRaises(ChatBlocked) as caught:
            self.send()
        self.assertEqual(caught.exception.code, "usage_limit")
        self.assertEqual(caught.exception.extra.get("model"), PRIMARY)

    def test_a_disabled_model_is_refused_rather_than_substituted(self):
        """Being switched off is not the same as being out of room.

        Answering a request for a model an administrator has disabled by
        quietly using a different one would defeat the point of disabling it.
        A disabled model raises ``ModelUnavailable`` rather than ``ChatBlocked``
        — a different kind of refusal, which the fallback deliberately does not
        catch, and which the chat view already turns into its own message.
        """
        from AI.services.model_service import ModelUnavailable

        self.fund(self.primary, 100_000, plan_tokens=100_000)
        self.fund(self.fallback, 100_000, plan_tokens=100_000)
        self.primary.is_active = False
        self.primary.save(update_fields=["is_active"])
        with self.assertRaises(ModelUnavailable):
            self.send()
        self.assertFalse(
            AIUsageLog.objects.filter(user=self.student).exists(),
            "nothing may be billed when the request was refused",
        )

    def test_the_administrator_can_turn_it_off(self):
        settings_service.set_value("auto_switch_model", False)
        self.fund(self.primary, 100_000, plan_tokens=100_000)
        self.fund(self.fallback, 100_000, plan_tokens=100_000)
        self.exhaust_window(self.primary)
        with self.assertRaises(ChatBlocked):
            self.send()
        settings_service.set_value("auto_switch_model", True)

    def test_the_choice_is_stable(self):
        """The same student in the same state always lands on the same model."""
        self.fund(self.primary, 0, plan_tokens=100_000)
        self.fund(self.fallback, 100_000, plan_tokens=100_000)
        first = self.send()["switched_to"]
        second = self.send()["switched_to"]
        self.assertEqual(first, second)


class TranscriptMarkerTests(TestCase):
    """The divider is derived from the transcript, so a reload still shows it."""

    def setUp(self):
        from AI.views.chat import _mark_model_switches

        self.mark = _mark_model_switches
        self.student = User.objects.create_user(
            username="reader", email="r@switch.test", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.conversation = chat_service.create_conversation(self.student)

    def message(self, role, model_key=""):
        from AI.models import Message

        return Message.objects.create(
            conversation=self.conversation, role=role, content="x", model_key=model_key,
        )

    def test_a_change_of_model_is_marked(self):
        rows = [
            self.message("user"),
            self.message("assistant", PRIMARY),
            self.message("user"),
            self.message("assistant", FALLBACK),
        ]
        self.mark(rows)
        self.assertEqual(rows[1].switched_to, "", "the first reply cannot be a switch")
        # The divider goes above the question the new model answered, so the
        # transcript reads "from here on, another model" — not between a
        # question and its own answer.
        self.assertEqual(rows[2].switched_to, self.fallback_name())
        self.assertEqual(rows[3].switched_to, "")

    def fallback_name(self):
        return AIModel.objects.get(key=FALLBACK).name

    def test_staying_on_one_model_marks_nothing(self):
        rows = [
            self.message("assistant", PRIMARY),
            self.message("assistant", PRIMARY),
        ]
        self.mark(rows)
        self.assertTrue(all(row.switched_to == "" for row in rows))

    def test_a_reply_with_no_model_recorded_is_skipped(self):
        """An image reply records no chat model; it is not a switch."""
        rows = [
            self.message("assistant", PRIMARY),
            self.message("assistant", ""),
            self.message("assistant", PRIMARY),
        ]
        self.mark(rows)
        self.assertTrue(all(row.switched_to == "" for row in rows))

    def test_a_retired_model_is_named_by_its_key(self):
        """Better than saying nothing about a model that no longer exists."""
        rows = [
            self.message("assistant", PRIMARY),
            self.message("assistant", "gpt-4o-mini-retired"),
        ]
        self.mark(rows)
        self.assertEqual(rows[1].switched_to, "gpt-4o-mini-retired")
