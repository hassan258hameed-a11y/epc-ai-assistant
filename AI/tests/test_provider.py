"""Provider request shaping.

Reasoning models (the gpt-5 family, o-series) reject the parameters older chat
models require. Getting this wrong means every request fails with HTTP 400, so
the payload rules are pinned here. These tests never touch the network.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from AI.services.ai_service import AIError, ChatTurn, OpenAICompatibleProvider, get_provider


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def completion(content="Hello.", *, reasoning=0, finish="stop", completion_tokens=20):
    return {
        "id": "chatcmpl-test",
        "model": "gpt-5-mini-2025-08-07",
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": completion_tokens,
            "total_tokens": 10 + completion_tokens,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    }


def sent_payload(mock_post) -> dict:
    return json.loads(mock_post.call_args.kwargs["data"])


@override_settings(OPENAI_API_KEY="sk-test", OPENAI_MAX_OUTPUT_TOKENS=1024)
class ReasoningModelPayloadTests(SimpleTestCase):
    """gpt-5 and o-series take max_completion_tokens and no temperature."""

    def _complete(self, model, response=None, **options):
        provider = OpenAICompatibleProvider(model=model, **options)
        with patch("AI.services.ai_service.requests.post",
                   return_value=FakeResponse(response or completion())) as post:
            result = provider.complete([ChatTurn(role="user", content="hi")])
        return post, result

    def test_gpt5_uses_max_completion_tokens_and_omits_temperature(self):
        post, _ = self._complete("gpt-5-mini")
        payload = sent_payload(post)

        self.assertIn("max_completion_tokens", payload)
        self.assertNotIn("max_tokens", payload, "gpt-5 rejects max_tokens with HTTP 400")
        self.assertNotIn("temperature", payload, "gpt-5 accepts only the default temperature")

    def test_older_chat_models_keep_max_tokens_and_temperature(self):
        post, _ = self._complete("gpt-4o-mini")
        payload = sent_payload(post)

        self.assertIn("max_tokens", payload)
        self.assertNotIn("max_completion_tokens", payload)
        # Study answers should be accurate and repeatable rather than inventive,
        # so the default sits well below 1.0.
        self.assertEqual(payload["temperature"], settings.AI_TEMPERATURE)
        self.assertIn("presence_penalty", payload)
        self.assertIn("frequency_penalty", payload)

    def test_a_model_can_override_the_sampling_defaults(self):
        """A catalogue row's temperature reaches the provider payload."""
        post, _ = self._complete("gpt-4o-mini", temperature=0.9)
        self.assertEqual(sent_payload(post)["temperature"], 0.9)

    def test_every_reasoning_family_is_detected(self):
        for model in ("gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-5.1", "o1-mini", "o3", "o4-mini"):
            self.assertTrue(OpenAICompatibleProvider(model=model).is_reasoning_model, model)
        for model in ("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-3.5-turbo"):
            self.assertFalse(OpenAICompatibleProvider(model=model).is_reasoning_model, model)

    @override_settings(OPENAI_MAX_OUTPUT_TOKENS=200)
    def test_reasoning_models_get_a_minimum_budget(self):
        """A small budget is spent entirely on thinking, returning nothing."""
        post, _ = self._complete("gpt-5-mini")
        self.assertGreaterEqual(
            sent_payload(post)["max_completion_tokens"],
            OpenAICompatibleProvider.MIN_REASONING_BUDGET,
        )

    @override_settings(OPENAI_MAX_OUTPUT_TOKENS=200)
    def test_non_reasoning_models_keep_the_configured_budget(self):
        post, _ = self._complete("gpt-4o-mini")
        self.assertEqual(sent_payload(post)["max_tokens"], 200)


@override_settings(OPENAI_API_KEY="sk-test")
class ReasoningUsageTests(SimpleTestCase):
    def test_reasoning_tokens_are_reported(self):
        provider = OpenAICompatibleProvider(model="gpt-5-mini")
        with patch("AI.services.ai_service.requests.post",
                   return_value=FakeResponse(completion(reasoning=192, completion_tokens=254))):
            result = provider.complete([ChatTurn(role="user", content="hi")])

        self.assertEqual(result.reasoning_tokens, 192)
        self.assertEqual(result.output_tokens, 254)   # reasoning is included in this
        self.assertEqual(result.total_tokens, 264)

    def test_an_answer_starved_by_reasoning_is_a_clear_retryable_error(self):
        """Empty + finish_reason=length means our budget was too small."""
        provider = OpenAICompatibleProvider(model="gpt-5-mini")
        starved = completion("", reasoning=200, finish="length", completion_tokens=200)

        with patch("AI.services.ai_service.requests.post", return_value=FakeResponse(starved)):
            with self.assertRaises(AIError) as ctx:
                provider.complete([ChatTurn(role="user", content="hi")])

        self.assertEqual(ctx.exception.code, "reasoning_budget_exhausted")
        self.assertTrue(ctx.exception.retryable)

    def test_a_plain_empty_reply_is_still_reported_separately(self):
        provider = OpenAICompatibleProvider(model="gpt-4o-mini")
        with patch("AI.services.ai_service.requests.post",
                   return_value=FakeResponse(completion("", finish="stop"))):
            with self.assertRaises(AIError) as ctx:
                provider.complete([ChatTurn(role="user", content="hi")])
        self.assertEqual(ctx.exception.code, "empty_response")


@override_settings(OPENAI_API_KEY="sk-test", OPENAI_MODEL="gpt-5-mini", AI_VISION_ENABLED=True)
class VisionTests(SimpleTestCase):
    def test_gpt5_is_treated_as_vision_capable(self):
        self.assertTrue(get_provider().supports_vision)

    def test_an_image_is_sent_as_a_content_part(self):
        provider = OpenAICompatibleProvider(model="gpt-5-mini")
        turn = ChatTurn(role="user", content="What is this?", image_data_url="data:image/png;base64,AAAA")

        with patch("AI.services.ai_service.requests.post", return_value=FakeResponse(completion())) as post:
            provider.complete([turn])

        content = sent_payload(post)["messages"][-1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual({part["type"] for part in content}, {"text", "image_url"})

    @override_settings(OPENAI_MODEL="gpt-3.5-turbo")
    def test_a_text_only_model_is_told_about_the_image_in_words(self):
        provider = OpenAICompatibleProvider(model="gpt-3.5-turbo")
        turn = ChatTurn(role="user", content="What is this?", image_data_url="data:image/png;base64,AAAA")

        with patch("AI.services.ai_service.requests.post", return_value=FakeResponse(completion())) as post:
            provider.complete([turn])

        content = sent_payload(post)["messages"][-1]["content"]
        self.assertIsInstance(content, str)
        self.assertIn("cannot read images", content)
