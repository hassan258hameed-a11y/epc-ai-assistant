"""Image generation: recognising the ask, drawing it, and paying for it.

The bug this whole path exists to fix is a chat model answering "I can't create
images" to somebody who asked for one. So the first thing under test is the
recognition, in the words people actually use — and, just as importantly, the
things that merely *sound* like a request and must still be answered as text.

After that: a picture is drawn by the dedicated endpoint and never by the chat
model, a failure costs the student nothing, and a missing credential is an
operator's problem that never reaches a student as a stack trace.
"""
from __future__ import annotations

import base64
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from AI.models import (
    AccountStatus,
    AIModel,
    ImageRequest,
    MessageRole,
    PackageAllocation,
    Plan,
    User,
)
from AI.services import chat_service, image_service, settings_service, wallet_service
from AI.services.chat_service import ChatBlocked
from AI.services.providers.base import AIError

PASSWORD = "Sup3rSecret!x"
GPT = "gpt-5-mini"

#: The smallest valid PNG, so the pipeline gets real bytes to sniff and store.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeResponse:
    """Just enough of ``requests.Response`` for the image client."""

    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text
        self.content = b""

    def json(self):
        return self._payload


def drawn(revised: str = "") -> FakeResponse:
    """An OpenAI-shaped success."""
    item = {"b64_json": base64.b64encode(PNG).decode()}
    if revised:
        item["revised_prompt"] = revised
    return FakeResponse({"data": [item], "model": "gpt-image-1"})


def gemini_drawn() -> FakeResponse:
    """A Gemini-shaped success: the picture is a part of a content response."""
    return FakeResponse(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "Here you go"},
                            {"inlineData": {"mimeType": "image/png",
                                            "data": base64.b64encode(PNG).decode()}},
                        ]
                    }
                }
            ]
        }
    )


def stability_drawn() -> FakeResponse:
    """A Stability-shaped success: the raw bytes, with no JSON around them."""
    response = FakeResponse({}, status_code=200)
    response.content = PNG
    return response


class IntentTests(TestCase):
    """What counts as asking for a picture."""

    def test_the_reported_case_is_recognised(self):
        """The exact message that used to be answered with "I am text-based"."""
        self.assertTrue(image_service.looks_like_image_request("Create a monkey eating a banana."))

    def test_natural_requests_are_recognised(self):
        for text in [
            "Generate a futuristic city.",
            "Make an image of a football stadium.",
            "Create a cartoon character.",
            "Generate a realistic mountain landscape.",
            "draw a cat",
            "make a logo for a coffee shop",
            "imagine a dragon flying over a castle",
            "Create a poster for our school science fair",
            "/image a lighthouse at dawn",
        ]:
            with self.subTest(text=text):
                self.assertTrue(image_service.looks_like_image_request(text))

    def test_questions_and_documents_are_not_drawings(self):
        for text in [
            "What is a monkey?",
            "How do I generate an image in Python?",
            "Create a Python function that reverses a string",
            "make a table of contents for my essay",
            "create an account for me",
            "Write me an essay about climate change",
            "design a database schema for a school",
            "Produce a summary of chapter 4",
            "generate a report on my usage",
            "Create a study plan for my exams",
            "Solve 2x + 5 = 15",
            "Can you explain how photosynthesis works?",
        ]:
            with self.subTest(text=text):
                self.assertFalse(image_service.looks_like_image_request(text))

    def test_a_message_containing_code_is_never_a_drawing(self):
        self.assertFalse(
            image_service.looks_like_image_request("create a chart\n```python\nprint(1)\n```")
        )

    def test_the_prompt_drops_the_asking_and_keeps_the_picture(self):
        self.assertEqual(
            image_service.build_prompt("Please can you generate an image of a red bicycle"),
            "a red bicycle",
        )
        self.assertEqual(
            image_service.build_prompt("/image a lighthouse at dawn"), "a lighthouse at dawn"
        )

    def test_the_browser_gets_the_same_rules_the_server_uses(self):
        hints = image_service.client_hints()
        self.assertIn("create", hints["make"])
        self.assertIn("function", hints["notPicture"])
        self.assertEqual(hints["maxChars"], image_service._MAX_PROMPT_CHARS)


class ImageFixture(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        settings_service.set_value("image_enabled", True)
        settings_service.set_value("image_api_key", "test-key")
        self.student = User.objects.create_user(
            username="s1", email="s1@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.model = AIModel.objects.get(key=GPT)
        self.plan = Plan.objects.create(
            name="Images 100k", tokens=100_000, ai_model=self.model, price=Decimal("2.49"),
            daily_limit=9_000, weekly_limit=35_000, monthly_limit=100_000,
        )
        wallet_service.credit(
            self.student, self.model, self.plan.tokens,
            transaction_type="plan_purchase", reason=self.plan.name, plan=self.plan,
        )
        self.conversation = chat_service.create_conversation(self.student)


class GenerationTests(ImageFixture):

    def test_a_request_for_a_picture_never_reaches_the_chat_model(self):
        with patch("AI.services.imaging.base.requests.post", return_value=drawn()) as post, \
             patch("AI.services.providers.openai_provider.OpenAICompatibleProvider.complete") as complete:
            result = chat_service.send_message(
                self.student, self.conversation, "Create a monkey eating a banana."
            )

        complete.assert_not_called()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["kind"], "image")

    def test_a_picture_asked_for_on_the_live_endpoint_arrives_whole(self):
        """Nothing to stream: no words, then the same picture payload as JSON."""
        import json

        self.client.force_login(self.student)
        with patch("AI.services.imaging.base.requests.post", return_value=drawn()):
            response = self.client.post(
                "/api/chat/send/",
                {"message": "Create a monkey eating a banana.", "model": GPT, "stream": "1",
                 "conversation_id": self.conversation.pk},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            body = b"".join(response.streaming_content).decode()
        events = [block for block in body.split("\n\n") if block.startswith("event: ")]
        self.assertTrue(events[0].startswith("event: start"))
        self.assertFalse([e for e in events if e.startswith("event: delta")])
        done = json.loads(events[-1].split("data: ", 1)[1])
        self.assertEqual(done["kind"], "image")
        self.assertEqual(done["assistant_message"]["attachments"][0]["kind"], "image")

    def test_the_picture_comes_back_in_the_conversation(self):
        with patch("AI.services.imaging.base.requests.post", return_value=drawn()):
            result = chat_service.send_message(
                self.student, self.conversation, "Create a monkey eating a banana."
            )

        attachments = result["assistant_message"]["attachments"]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["kind"], "image")
        self.assertTrue(attachments[0]["url"])
        reply = self.conversation.messages.filter(role=MessageRole.ASSISTANT).last()
        self.assertEqual(reply.attachments.count(), 1)

    def test_the_prompt_that_was_drawn_comes_back_for_the_buttons(self):
        """"Generate again" has to re-draw what was drawn, not what was typed."""
        with patch("AI.services.imaging.base.requests.post", return_value=drawn("a monkey, oil painting")):
            result = chat_service.send_message(
                self.student, self.conversation, "Create a monkey eating a banana."
            )

        self.assertEqual(result["image_prompt"], "Create a monkey eating a banana.")
        self.assertEqual(result["revised_prompt"], "a monkey, oil painting")
        message = self.conversation.messages.filter(role=MessageRole.ASSISTANT).last()
        self.assertEqual(message.image_prompt, "Create a monkey eating a banana.")

    def test_a_variation_asks_for_something_different(self):
        with patch("AI.services.imaging.base.requests.post", return_value=drawn()) as post:
            chat_service.send_message(
                self.student, self.conversation, "/image a red bicycle", variation=True
            )
        sent = post.call_args.kwargs["data"]
        self.assertIn("different interpretation", sent)

    def test_the_picture_is_charged_and_logged(self):
        with patch("AI.services.imaging.base.requests.post", return_value=drawn()):
            result = chat_service.send_message(
                self.student, self.conversation, "Create a monkey eating a banana."
            )

        cost = image_service.token_cost()
        self.assertEqual(result["tokens_charged"], cost)

        record = ImageRequest.objects.get()
        self.assertEqual(record.status, ImageRequest.Status.OK)
        self.assertEqual(record.tokens_charged, cost)
        self.assertEqual(record.user_id, self.student.pk)
        self.assertTrue(record.attachment_id)
        self.assertEqual(record.image_model, "gpt-image-1")

    def test_a_picture_spends_the_same_daily_allowance_as_a_message(self):
        with patch("AI.services.imaging.base.requests.post", return_value=drawn()):
            chat_service.send_message(self.student, self.conversation, "/image a red bicycle")

        allocation = PackageAllocation.objects.get(user=self.student)
        self.assertEqual(allocation.daily_used, image_service.token_cost())

    def test_a_picture_is_refused_once_the_day_is_spent(self):
        from AI.services import usage_limit_service

        usage_limit_service.consume(self.student, self.model, 9_000)
        with patch("AI.services.imaging.base.requests.post") as post:
            with self.assertRaises(ChatBlocked) as caught:
                chat_service.send_message(self.student, self.conversation, "/image a red bicycle")
            post.assert_not_called()
        self.assertEqual(caught.exception.code, "usage_limit")


class FailureTests(ImageFixture):

    def test_a_failed_drawing_costs_nothing_and_is_recorded(self):
        before = wallet_service.balance_of(self.student, self.model)
        with patch("AI.services.imaging.base.requests.post",
                   return_value=FakeResponse({"error": {"message": "upstream"}}, status_code=503)):
            with self.assertRaises(AIError):
                chat_service.send_message(self.student, self.conversation, "/image a red bicycle")

        self.assertEqual(wallet_service.balance_of(self.student, self.model), before)
        record = ImageRequest.objects.get()
        self.assertEqual(record.status, ImageRequest.Status.FAILED)
        self.assertEqual(record.tokens_charged, 0)

    def test_an_outage_is_explained_without_naming_the_vendor(self):
        with patch("AI.services.imaging.base.requests.post",
                   return_value=FakeResponse({"error": {"message": "quota exceeded for org-42"}},
                                             status_code=503)):
            with self.assertRaises(AIError) as caught:
                chat_service.send_message(self.student, self.conversation, "/image a red bicycle")

        message = caught.exception.friendly_message
        self.assertIn("temporarily unavailable", message)
        self.assertNotIn("org-42", message)
        self.assertNotIn("quota", message.lower())

    def test_a_rejected_credential_never_reads_as_the_students_fault(self):
        with patch("AI.services.imaging.base.requests.post",
                   return_value=FakeResponse({"error": {"message": "Invalid API key sk-abc"}},
                                             status_code=401)):
            with self.assertRaises(AIError) as caught:
                chat_service.send_message(self.student, self.conversation, "/image a red bicycle")

        message = caught.exception.friendly_message
        self.assertNotIn("sk-abc", message)
        self.assertNotIn("API key", message)
        self.assertIn("contact support", message.lower())

    def test_with_drawing_off_the_request_is_answered_as_text_instead(self):
        """Drawing off must not mean a dead end — the chat model still answers."""
        settings_service.set_value("image_enabled", False)
        self.assertFalse(image_service.is_available())

        with patch("AI.services.providers.openai_provider.OpenAICompatibleProvider.complete") as complete:
            from AI.services.providers.base import AIResponse

            complete.return_value = AIResponse(
                content="I can describe one.", input_tokens=10, output_tokens=10,
                total_tokens=20, model=GPT, request_id="r", latency_ms=10,
            )
            result = chat_service.send_message(
                self.student, self.conversation, "Create a monkey eating a banana."
            )
        self.assertNotEqual(result.get("kind"), "image")
        complete.assert_called_once()

    def test_an_explicit_command_with_no_credential_says_so_plainly(self):
        settings_service.set_value("image_api_key", "")
        with override_settings(OPENAI_API_KEY="", ANTHROPIC_API_KEY="x"):
            with self.assertRaises(ChatBlocked) as caught:
                chat_service.send_message(self.student, self.conversation, "/image a red bicycle")
        self.assertEqual(caught.exception.code, "image_disabled")
        self.assertIn("temporarily unavailable", caught.exception.friendly_message)


class ConfigurationTests(ImageFixture):

    def test_the_admin_settings_drive_the_request(self):
        settings_service.set_value("image_model", "gpt-image-1-mini")
        settings_service.set_value("image_size", "1536x1024")
        settings_service.set_value("image_quality", "high")

        with patch("AI.services.imaging.base.requests.post", return_value=drawn()) as post:
            chat_service.send_message(self.student, self.conversation, "/image a red bicycle")

        sent = post.call_args.kwargs["data"]
        self.assertIn("gpt-image-1-mini", sent)
        self.assertIn("1536x1024", sent)
        self.assertIn("high", sent)

    def test_the_vendor_can_be_switched_without_touching_code(self):
        settings_service.set_value("image_provider", "gemini")
        settings_service.set_value("image_model", "")

        with patch("AI.services.imaging.base.requests.post", return_value=gemini_drawn()) as post:
            result = chat_service.send_message(
                self.student, self.conversation, "/image a red bicycle"
            )

        self.assertTrue(result["assistant_message"]["attachments"])
        url = post.call_args.args[0] if post.call_args.args else post.call_args.kwargs.get("url", "")
        self.assertIn("generativelanguage.googleapis.com", url)
        self.assertEqual(ImageRequest.objects.get().provider, "gemini")

    def test_an_unknown_vendor_falls_back_rather_than_breaking(self):
        settings_service.set_value("image_provider", "not-a-vendor")
        self.assertEqual(image_service.config()["provider"], "openai")

    def test_the_price_of_a_picture_is_configurable(self):
        settings_service.set_value("image_token_cost", 2_500)
        self.assertEqual(image_service.token_cost(), 2_500)

    def test_turning_it_off_stops_it_for_everyone(self):
        settings_service.set_value("image_enabled", False)
        self.assertFalse(image_service.is_available())
        with self.assertRaises(ChatBlocked):
            chat_service.send_message(self.student, self.conversation, "/image a red bicycle")

    def test_the_status_summary_never_contains_the_key(self):
        status = image_service.status()
        self.assertNotIn("test-key", str(status))
        self.assertTrue(status["has_key"])
        self.assertEqual(status["state"], "Connected")

    def test_the_settings_screen_reports_a_secret_without_revealing_it(self):
        rows = settings_service.grouped()["images"]
        row = next(item for item in rows if item["key"] == "image_api_key")
        self.assertTrue(row["is_secret"])
        self.assertTrue(row["is_set"])
        self.assertEqual(row["value"], "")
        self.assertEqual(row["raw"], "")
        self.assertNotIn("test-key", str(rows))
