"""Drawing works on every model, with every vendor, and can change a picture.

Three separate claims, tested separately because they fail for different reasons:

* **every model** — a student on Claude must be able to draw without first being
  sent to find GPT tokens. The picture is billed to the model they are *using*,
  whichever vendor actually drew it;
* **every vendor** — the three integrations speak three different wire formats,
  so each is exercised against a response shaped the way that vendor really
  shapes one;
* **editing** — the same picture, changed, charged and logged like any other.
"""
from __future__ import annotations

import base64
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from AI.models import (
    AccountStatus,
    AIModel,
    ImageRequest,
    MessageAttachment,
    PackageAllocation,
    Plan,
    User,
)
from AI.services import chat_service, image_service, imaging, settings_service, wallet_service
from AI.services.chat_service import ChatBlocked
from AI.services.imaging import ImageConfig
from AI.services.providers.base import AIError

PASSWORD = "Sup3rSecret!x"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
JPEG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 32


class Fake:
    """Enough of ``requests.Response`` for any of the three vendors."""

    def __init__(self, payload=None, *, status_code=200, content=b"", text=""):
        self._payload = payload
        self.status_code = status_code
        self.content = content
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def openai_ok():
    return Fake({"data": [{"b64_json": base64.b64encode(PNG).decode()}], "model": "gpt-image-1"})


def gemini_ok():
    return Fake(
        {
            "candidates": [
                {"content": {"parts": [
                    {"inlineData": {"mimeType": "image/png",
                                    "data": base64.b64encode(PNG).decode()}}
                ]}}
            ]
        }
    )


def stability_ok():
    return Fake(None, content=PNG)


POST = "AI.services.imaging.base.requests.post"


class VendorWireTests(TestCase):
    """Each integration against the response shape its vendor really sends."""

    def config(self, **overrides):
        base = dict(api_key="k", model="", size="1024x1024", quality="medium", timeout=10)
        base.update(overrides)
        return ImageConfig(**base)

    def test_openai_posts_json_and_reads_base64(self):
        provider = imaging.build("openai", self.config())
        with patch(POST, return_value=openai_ok()) as post:
            picture = provider.generate("a red bicycle")

        self.assertEqual(picture.data, PNG)
        self.assertEqual(picture.provider, "openai")
        self.assertIn("/images/generations", post.call_args.args[0])
        self.assertIn("a red bicycle", post.call_args.kwargs["data"])

    def test_openai_edits_over_multipart(self):
        provider = imaging.build("openai", self.config())
        with patch(POST, return_value=openai_ok()) as post:
            provider.edit("make the sky orange", PNG, mime="image/png")

        self.assertIn("/images/edits", post.call_args.args[0])
        # The source picture goes up as a file, not as JSON.
        self.assertIn("image", post.call_args.kwargs["files"])
        self.assertEqual(post.call_args.kwargs["data"]["prompt"], "make the sky orange")

    def test_gemini_asks_for_an_image_modality_and_walks_the_parts(self):
        provider = imaging.build("gemini", self.config())
        with patch(POST, return_value=gemini_ok()) as post:
            picture = provider.generate("a red bicycle")

        self.assertEqual(picture.data, PNG)
        self.assertEqual(picture.provider, "gemini")
        body = post.call_args.kwargs["data"]
        self.assertIn("responseModalities", body)
        # The key travels in a header, never in the URL.
        self.assertIn("x-goog-api-key", post.call_args.kwargs["headers"])
        self.assertNotIn("key=", post.call_args.args[0])

    def test_gemini_edits_by_sending_the_picture_back(self):
        provider = imaging.build("gemini", self.config())
        with patch(POST, return_value=gemini_ok()) as post:
            provider.edit("make the sky orange", PNG)

        body = post.call_args.kwargs["data"]
        self.assertIn("inline_data", body)
        self.assertIn("make the sky orange", body)

    def test_gemini_reports_a_refusal_that_arrives_as_a_200(self):
        blocked = Fake({"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})
        provider = imaging.build("gemini", self.config())
        with patch(POST, return_value=blocked):
            with self.assertRaises(AIError) as caught:
                provider.generate("something refused")
        self.assertEqual(caught.exception.code, "rejected_prompt")

    def test_stability_asks_for_bytes_and_gets_them(self):
        provider = imaging.build("stability", self.config())
        with patch(POST, return_value=stability_ok()) as post:
            picture = provider.generate("a red bicycle")

        self.assertEqual(picture.data, PNG)
        self.assertEqual(picture.provider, "stability")
        self.assertEqual(post.call_args.kwargs["headers"]["Accept"], "image/*")
        self.assertEqual(post.call_args.kwargs["data"]["aspect_ratio"], "1:1")

    def test_every_vendor_sniffs_the_format_from_the_bytes(self):
        """A vendor that returns a JPEG must not have it stored as .png."""
        provider = imaging.build("stability", self.config())
        with patch(POST, return_value=Fake(None, content=JPEG_HEADER)):
            picture = provider.generate("anything")
        self.assertEqual(picture.ext, "jpg")
        self.assertEqual(picture.mime, "image/jpeg")

    def test_a_vendor_outage_is_the_same_message_whichever_vendor(self):
        for slug in ("openai", "gemini", "stability"):
            with self.subTest(slug=slug):
                provider = imaging.build(slug, self.config())
                with patch(POST, return_value=Fake({"error": {"message": "boom"}}, status_code=503)):
                    with self.assertRaises(AIError) as caught:
                        provider.generate("a red bicycle")
                self.assertEqual(caught.exception.code, "provider_down")
                self.assertIn("temporarily unavailable", caught.exception.friendly_message)

    def test_the_registry_describes_what_each_vendor_can_do(self):
        catalogue = {row["slug"]: row for row in imaging.catalogue()}
        self.assertEqual(set(catalogue), {"openai", "gemini", "stability"})
        self.assertTrue(catalogue["openai"]["supports_edit"])
        self.assertTrue(catalogue["openai"]["default_model"])
        for row in catalogue.values():
            self.assertTrue(row["env_key"], row["slug"])


class EveryModelDrawsTests(TestCase):
    """A student can draw whichever model they are talking to."""

    def setUp(self):
        settings_service.bootstrap()
        settings_service.set_value("image_enabled", True)
        settings_service.set_value("image_api_key", "test-key")
        settings_service.set_value("image_provider", "openai")
        self.student = User.objects.create_user(
            username="s1", email="s1@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        # Every active model, whatever the catalogue currently holds. The point
        # of this class is that drawing works for *all* of them, so pinning a
        # count here would only mean the test stopped covering a model the day
        # one was added — which is the day it most needs covering.
        self.models = list(AIModel.objects.filter(is_active=True).order_by("sort_order"))
        self.assertGreaterEqual(
            len(self.models), 2, "the catalogue should hold several models to compare"
        )

        for model in self.models:
            plan = Plan.objects.create(
                name=f"{model.name} 100k", tokens=100_000, ai_model=model,
                price=Decimal("2.49"), daily_limit=9_000, weekly_limit=35_000,
                monthly_limit=100_000,
            )
            wallet_service.credit(
                self.student, model, plan.tokens,
                transaction_type="plan_purchase", reason=plan.name, plan=plan,
            )

    def test_each_model_draws_and_pays_from_its_own_balance(self):
        cost = image_service.token_cost()
        for model in self.models:
            with self.subTest(model=model.key):
                before = wallet_service.balance_of(self.student, model)
                conversation = chat_service.create_conversation(self.student)

                with patch(POST, return_value=openai_ok()), \
                     patch("AI.services.providers.openai_provider.OpenAICompatibleProvider.complete") as chat, \
                     patch("AI.services.providers.anthropic_provider.AnthropicProvider.complete") as claude:
                    result = chat_service.send_message(
                        self.student, conversation,
                        "Create a monkey eating a banana.", model_key=model.key,
                    )

                    # Whichever model was selected, no chat model was asked to draw.
                    chat.assert_not_called()
                    claude.assert_not_called()

                self.assertEqual(result["kind"], "image")
                self.assertTrue(result["assistant_message"]["attachments"])
                # The wallet that paid is the model the student was using.
                self.assertEqual(
                    wallet_service.balance_of(self.student, model), before - cost, model.key
                )
                self.assertEqual(result["assistant_message"]["model"], model.key)

    def test_the_other_two_balances_are_untouched(self):
        drawn_on = self.models[1]
        others = [m for m in self.models if m.pk != drawn_on.pk]
        before = {m.pk: wallet_service.balance_of(self.student, m) for m in others}

        conversation = chat_service.create_conversation(self.student)
        with patch(POST, return_value=openai_ok()):
            chat_service.send_message(
                self.student, conversation, "/image a red bicycle", model_key=drawn_on.key
            )

        for model in others:
            self.assertEqual(wallet_service.balance_of(self.student, model), before[model.pk])

    def test_a_drawing_spends_the_windows_of_the_model_it_was_billed_to(self):
        model = self.models[2]
        conversation = chat_service.create_conversation(self.student)
        with patch(POST, return_value=openai_ok()):
            chat_service.send_message(
                self.student, conversation, "/image a red bicycle", model_key=model.key
            )

        cost = image_service.token_cost()
        allocation = PackageAllocation.objects.get(user=self.student, ai_model=model)
        self.assertEqual(allocation.daily_used, cost)
        other = PackageAllocation.objects.get(user=self.student, ai_model=self.models[0])
        self.assertEqual(other.daily_used, 0)

    def test_the_ledger_names_both_the_service_and_the_wallet(self):
        model = self.models[1]
        conversation = chat_service.create_conversation(self.student)
        with patch(POST, return_value=openai_ok()):
            chat_service.send_message(
                self.student, conversation, "/image a red bicycle", model_key=model.key
            )

        record = ImageRequest.objects.get()
        self.assertEqual(record.ai_model_id, model.pk)      # the wallet that paid
        self.assertEqual(record.provider, "openai")          # the vendor that drew
        self.assertEqual(record.image_model, "gpt-image-1")


class EditingTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        settings_service.set_value("image_enabled", True)
        settings_service.set_value("image_api_key", "test-key")
        settings_service.set_value("image_provider", "openai")
        self.student = User.objects.create_user(
            username="s2", email="s2@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.other = User.objects.create_user(
            username="s3", email="s3@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.model = AIModel.objects.get(key="gpt-5-mini")
        plan = Plan.objects.create(
            name="Edits 100k", tokens=100_000, ai_model=self.model, price=Decimal("2.49"),
            daily_limit=9_000, weekly_limit=35_000, monthly_limit=100_000,
        )
        wallet_service.credit(
            self.student, self.model, plan.tokens,
            transaction_type="plan_purchase", reason=plan.name, plan=plan,
        )
        self.conversation = chat_service.create_conversation(self.student)
        with patch(POST, return_value=openai_ok()):
            self.first = chat_service.send_message(
                self.student, self.conversation, "/image a red bicycle"
            )
        self.attachment_id = self.first["assistant_message"]["attachments"][0]["id"]

    def test_an_edit_sends_the_original_picture_back_to_the_vendor(self):
        with patch(POST, return_value=openai_ok()) as post:
            result = chat_service.edit_image_reply(
                self.student, self.conversation, "make the sky orange", self.attachment_id
            )

        self.assertEqual(result["kind"], "image")
        self.assertIn("/images/edits", post.call_args.args[0])
        sent = post.call_args.kwargs["files"]["image"][1]
        self.assertEqual(sent, PNG)

    def test_the_edited_picture_is_a_new_attachment_charged_like_any_other(self):
        before = wallet_service.balance_of(self.student, self.model)
        with patch(POST, return_value=openai_ok()):
            result = chat_service.edit_image_reply(
                self.student, self.conversation, "make the sky orange", self.attachment_id
            )

        cost = image_service.token_cost()
        self.assertEqual(result["tokens_charged"], cost)
        self.assertEqual(wallet_service.balance_of(self.student, self.model), before - cost)
        self.assertEqual(MessageAttachment.objects.count(), 2)
        self.assertEqual(ImageRequest.objects.count(), 2)

    def test_somebody_elses_picture_is_simply_not_found(self):
        """Object-level authorisation, not a permission message that confirms it exists."""
        with self.assertRaises(ChatBlocked) as caught:
            chat_service.edit_image_reply(
                self.other, chat_service.create_conversation(self.other),
                "make the sky orange", self.attachment_id,
            )
        self.assertEqual(caught.exception.code, "not_found")

    def test_an_edit_with_no_instruction_is_refused_before_anything_is_charged(self):
        before = wallet_service.balance_of(self.student, self.model)
        with patch(POST) as post:
            with self.assertRaises(ChatBlocked) as caught:
                chat_service.edit_image_reply(
                    self.student, self.conversation, "   ", self.attachment_id
                )
            post.assert_not_called()
        self.assertEqual(caught.exception.code, "empty_prompt")
        self.assertEqual(wallet_service.balance_of(self.student, self.model), before)

    def test_a_vendor_that_cannot_edit_says_so_instead_of_failing(self):
        class DrawOnly(imaging.REGISTRY["openai"]):
            slug = "drawonly"
            supports_edit = False

        with patch.dict(imaging.REGISTRY, {"drawonly": DrawOnly}, clear=False):
            settings_service.set_value("image_provider", "drawonly")
            self.assertFalse(image_service.can_edit())
            with patch(POST) as post:
                with self.assertRaises(ChatBlocked) as caught:
                    chat_service.edit_image_reply(
                        self.student, self.conversation, "make it blue", self.attachment_id
                    )
                post.assert_not_called()
        self.assertEqual(caught.exception.code, "edit_unsupported")

    def test_a_failed_edit_costs_nothing_and_is_recorded(self):
        before = wallet_service.balance_of(self.student, self.model)
        with patch(POST, return_value=Fake({"error": {"message": "boom"}}, status_code=503)):
            with self.assertRaises(AIError):
                chat_service.edit_image_reply(
                    self.student, self.conversation, "make the sky orange", self.attachment_id
                )

        self.assertEqual(wallet_service.balance_of(self.student, self.model), before)
        failed = ImageRequest.objects.filter(status=ImageRequest.Status.FAILED).get()
        self.assertEqual(failed.tokens_charged, 0)
