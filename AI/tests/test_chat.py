"""Chat flow: token cut-off, actual-usage billing and failure handling."""
from __future__ import annotations

from io import BytesIO
from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image

from AI.models import AccountStatus, AIModel, AIUsageLog, Conversation, Message, MessageRole, User
from AI.services import (
    chat_service,
    model_service,
    settings_service,
    token_service,
    wallet_service,
)
from AI.tests.helpers import GPT, SONNET, balance, fund, fund_pool, model_for
from AI.services.ai_service import AIError, AIResponse
from AI.services.chat_service import ChatBlocked
from AI.utils.uploads import InvalidUpload, validate_image

PASSWORD = "Sup3rSecret!x"


def fake_reply(content="Here is the explanation.", input_tokens=120, output_tokens=380) -> AIResponse:
    return AIResponse(
        content=content,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        model="gpt-4o-mini",
        request_id="req-test-1",
        latency_ms=850,
    )


def png_upload(name="photo.png", size=(80, 80)) -> SimpleUploadedFile:
    buffer = BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type="image/png")


class ChatFlowTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.user = User.objects.create_user(
            username="chatter", email="chatter@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        # Tokens belong to a model now, so the fixture funds one explicitly.
        fund(self.user, 10_000, GPT)
        self.client.force_login(self.user)

    def _send(self, message="Explain gravity", **extra):
        payload = {"message": message}
        payload.update(extra)
        return self.client.post("/api/chat/send/", payload, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    @patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", True)
    @patch("AI.services.ai_service.OpenAICompatibleProvider.complete")
    def test_successful_exchange_charges_the_reported_usage(self, complete):
        complete.return_value = fake_reply(input_tokens=120, output_tokens=380)

        response = self._send()
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["tokens_used"], 500)
        self.assertEqual(data["tokens_charged"], 500)
        self.assertEqual(data["balance"], 9_500)
        self.assertEqual(balance(self.user, GPT), 9_500)

        conversation = Conversation.objects.get(user=self.user)
        self.assertEqual(conversation.messages.count(), 2)

        reply = conversation.messages.get(role=MessageRole.ASSISTANT)
        self.assertEqual(reply.input_tokens, 120)
        self.assertEqual(reply.output_tokens, 380)
        self.assertEqual(reply.total_tokens, 500)
        self.assertEqual(reply.model, "gpt-4o-mini")

    @patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", True)
    @patch("AI.services.ai_service.OpenAICompatibleProvider.complete")
    def test_billing_follows_the_provider_not_the_text_length(self, complete):
        """A very short reply that reports large usage is charged in full."""
        complete.return_value = fake_reply(content="Yes.", input_tokens=1_500, output_tokens=900)

        self._send()
        self.assertEqual(balance(self.user, GPT), 10_000 - 2_400)

    @patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", True)
    @patch("AI.services.ai_service.OpenAICompatibleProvider.complete")
    def test_usage_log_is_written_for_every_call(self, complete):
        complete.return_value = fake_reply()
        self._send()

        log = AIUsageLog.objects.get(user=self.user)
        self.assertTrue(log.success)
        self.assertEqual(log.total_tokens, 500)
        self.assertEqual(log.tokens_charged, 500)
        self.assertEqual(log.request_id, "req-test-1")

    @patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", True)
    @patch("AI.services.ai_service.OpenAICompatibleProvider.complete")
    def test_conversation_title_is_derived_from_the_first_question(self, complete):
        complete.return_value = fake_reply()
        self._send("How do I balance a chemical equation?")

        conversation = Conversation.objects.get(user=self.user)
        self.assertEqual(conversation.title, "How do I balance a chemical equation?")

    @patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", True)
    @patch("AI.services.ai_service.OpenAICompatibleProvider.complete")
    def test_a_failed_call_charges_nothing(self, complete):
        complete.side_effect = AIError("The assistant is unavailable.", code="provider_down")

        response = self._send()
        self.assertEqual(response.status_code, 502)
        data = response.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["code"], "provider_down")

        self.assertEqual(balance(self.user, GPT), 10_000)

        log = AIUsageLog.objects.get(user=self.user)
        self.assertFalse(log.success)
        self.assertEqual(log.error_code, "provider_down")
        self.assertEqual(log.tokens_charged, 0)

    @patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", True)
    @patch("AI.services.ai_service.OpenAICompatibleProvider.complete")
    def test_the_question_survives_a_failed_call_so_it_can_be_retried(self, complete):
        complete.side_effect = AIError("Timed out.", code="timeout")
        self._send("My question")

        conversation = Conversation.objects.get(user=self.user)
        self.assertEqual(conversation.messages.filter(role=MessageRole.USER).count(), 1)
        self.assertEqual(conversation.messages.filter(role=MessageRole.ASSISTANT).count(), 0)

    @patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", True)
    @patch("AI.services.ai_service.OpenAICompatibleProvider.complete")
    def test_empty_balance_stops_requests_before_the_provider_is_called(self, complete):
        wallet_service.reclaim(self.user, model_for(GPT), 10_000, reason='Emptied')

        response = self._send()
        self.assertEqual(response.status_code, 402)
        data = response.json()
        self.assertEqual(data["code"], "no_tokens")
        complete.assert_not_called()

    @patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", True)
    @patch("AI.services.ai_service.OpenAICompatibleProvider.complete")
    def test_balance_below_the_floor_is_refused(self, complete):
        """Under the per-request floor, nothing is attempted at all.

        The floor is read from settings rather than written in: it is tuned
        against the size of the free grant, and a test that hard-codes it
        starts failing for the wrong reason every time that is adjusted.
        """
        floor = settings.AI_MIN_REQUEST_TOKENS
        wallet_service.reclaim(self.user, model_for(GPT), 10_000 - (floor - 1), reason='Nearly empty')
        response = self._send()

        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["code"], "low_tokens")
        complete.assert_not_called()

    @patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", True)
    @patch("AI.services.ai_service.OpenAICompatibleProvider.complete")
    def test_a_balance_too_small_for_this_message_is_refused_by_name(self, complete):
        """Above the floor but below what this message costs, the refusal says so.

        A different code, because it is a different problem: the student can
        still send *something*, just not this. The message says which model
        and how much, so shortening it is an option they can act on.
        """
        floor = settings.AI_MIN_REQUEST_TOKENS
        wallet_service.reclaim(self.user, model_for(GPT), 10_000 - (floor + 20), reason='Almost out')
        response = self._send()

        self.assertEqual(response.status_code, 402)
        data = response.json()
        self.assertEqual(data["code"], "insufficient_tokens")
        self.assertIn("GPT-5 Mini", data["error"])
        complete.assert_not_called()

    @patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", True)
    @patch("AI.services.ai_service.OpenAICompatibleProvider.complete")
    def test_overrun_is_clamped_and_never_goes_negative(self, complete):
        """Real usage may exceed the balance we pre-checked; charge what exists."""
        wallet_service.reclaim(self.user, model_for(GPT), 9_500, reason='Almost spent')  # 500 left
        complete.return_value = fake_reply(input_tokens=400, output_tokens=800)  # 1,200 used

        response = self._send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(balance(self.user, GPT), 0)
        self.assertEqual(response.json()["tokens_charged"], 500)

    def test_empty_message_is_rejected(self):
        response = self._send("   ")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "empty")

    def test_overlong_message_is_rejected(self):
        response = self._send("x" * 12_001)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "too_long")

    def test_unconfigured_provider_gives_a_friendly_error(self):
        """Missing credentials read as "this model is unavailable", nothing more.

        The router treats it exactly like a disabled model — a 409 the picker
        can act on — and the message never hints at which secret is missing or
        where it lives.
        """
        with patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", False):
            response = self._send()

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertEqual(payload["code"], "not_configured")
        self.assertNotIn("API", payload["error"])
        self.assertNotIn("key", payload["error"].lower())

    @patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", True)
    @patch("AI.services.ai_service.OpenAICompatibleProvider.complete")
    def test_requests_are_rate_limited(self, complete):
        complete.return_value = fake_reply(input_tokens=1, output_tokens=1)

        statuses = [self._send(f"Question {i}").status_code for i in range(32)]
        self.assertIn(429, statuses)


class ConversationManagementTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.user = User.objects.create_user(
            username="organiser", email="organiser@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        fund(self.user, 5_000, GPT)
        self.client.force_login(self.user)
        self.conversation = Conversation.objects.create(user=self.user, title="Original title")

    def test_rename(self):
        response = self.client.post(
            f"/api/chat/{self.conversation.pk}/rename/",
            data='{"title": "Physics revision"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.title, "Physics revision")

    def test_rename_rejects_an_empty_title(self):
        response = self.client.post(
            f"/api/chat/{self.conversation.pk}/rename/",
            data='{"title": "   "}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.title, "Original title")

    def test_delete_moves_the_conversation_to_the_trash(self):
        Message.objects.create(conversation=self.conversation, role=MessageRole.USER, content="hi")
        response = self.client.post(
            f"/api/chat/{self.conversation.pk}/delete/", HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(response.status_code, 200)
        # Gone from everywhere a chat normally shows…
        self.assertFalse(Conversation.objects.filter(pk=self.conversation.pk).exists())
        # …but restorable, with its messages, until it is deleted for good.
        self.assertTrue(Conversation.all_objects.filter(pk=self.conversation.pk, deleted_at__isnull=False).exists())
        self.assertTrue(Message.objects.filter(conversation_id=self.conversation.pk).exists())

        self.client.post(f"/api/chat/{self.conversation.pk}/purge/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertFalse(Conversation.all_objects.filter(pk=self.conversation.pk).exists())
        self.assertFalse(Message.objects.filter(conversation_id=self.conversation.pk).exists())

    def test_pin_toggles(self):
        self.client.post(f"/api/chat/{self.conversation.pk}/pin/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.conversation.refresh_from_db()
        self.assertTrue(self.conversation.is_pinned)

        self.client.post(f"/api/chat/{self.conversation.pk}/pin/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.conversation.refresh_from_db()
        self.assertFalse(self.conversation.is_pinned)

    def test_history_excludes_failed_messages(self):
        Message.objects.create(conversation=self.conversation, role=MessageRole.USER, content="ok message")
        Message.objects.create(
            conversation=self.conversation, role=MessageRole.ASSISTANT, content="broken", status="failed"
        )
        turns = chat_service.history_turns(self.conversation)
        self.assertEqual([t.content for t in turns], ["ok message"])

    def test_search_only_returns_the_users_own_conversations(self):
        other = User.objects.create_user(username="other", email="other@test.local", password=PASSWORD)
        Conversation.objects.create(user=other, title="Someone else's chat")

        response = self.client.get("/api/chat/search/?q=chat", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        titles = [c["title"] for c in response.json()["conversations"]]
        self.assertNotIn("Someone else's chat", titles)


class ImageUploadTests(TestCase):
    def test_valid_png_is_accepted_and_normalised(self):
        result = validate_image(png_upload())
        self.assertEqual(result.mime, "image/png")
        self.assertEqual(result.ext, "png")
        self.assertTrue(result.as_data_url().startswith("data:image/png;base64,"))

    def test_a_disguised_text_file_is_rejected(self):
        malicious = SimpleUploadedFile("evil.png", b"<?php system($_GET[0]); ?>", content_type="image/png")
        with self.assertRaises(InvalidUpload):
            validate_image(malicious)

    def test_an_svg_is_rejected(self):
        svg = SimpleUploadedFile(
            "x.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            content_type="image/svg+xml",
        )
        with self.assertRaises(InvalidUpload):
            validate_image(svg)

    def test_oversized_image_is_rejected(self):
        oversized = SimpleUploadedFile("big.png", b"x" * (6 * 1024 * 1024), content_type="image/png")
        with self.assertRaises(InvalidUpload) as ctx:
            validate_image(oversized)
        self.assertIn("too large", str(ctx.exception).lower())

    def test_large_dimensions_are_scaled_down(self):
        result = validate_image(png_upload(size=(3000, 2200)))
        self.assertLessEqual(max(result.width, result.height), 2000)

    def test_empty_file_is_rejected(self):
        with self.assertRaises(InvalidUpload):
            validate_image(SimpleUploadedFile("empty.png", b"", content_type="image/png"))


class ModelCatalogueTests(TestCase):
    """A student may only use models the administrator has left available."""

    def setUp(self):
        settings_service.bootstrap()
        # The catalogue ships with only the configured model, so a second one is
        # created here explicitly — which is also how an administrator adds one.
        model_service.ensure_seeded()
        self.default = model_service.default_model()
        # A deliberately fictional key: this test is about the picker, not
        # about any particular model, and a real catalogue key would collide.
        self.extra = AIModel.objects.create(
            key="atlas-test-4", name="Atlas 4", description="A second model",
            supports_vision=True, max_output_tokens=3072, sort_order=20,
        )
        # A model an admin has just added needs its provider link and its pool
        # before anyone can be funded on it.
        model_service.ensure_seeded()
        self.user = User.objects.create_user(
            username="picker", email="picker@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        fund(self.user, 50_000, GPT)
        fund(self.user, 50_000, "atlas-test-4")
        self.client.force_login(self.user)

    def _send(self, model="", message="Explain gravity"):
        payload = {"message": message}
        if model:
            payload["model"] = model
        return self.client.post("/api/chat/send/", payload, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    @patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", True)
    @patch("AI.services.ai_service.OpenAICompatibleProvider.complete")
    def test_usage_is_attributed_to_the_chosen_model(self, complete):
        complete.return_value = fake_reply(input_tokens=100, output_tokens=200)
        before = AIModel.objects.get(key="atlas-test-4").tokens_consumed

        response = self._send(model="atlas-test-4")
        self.assertEqual(response.status_code, 200)

        self.assertEqual(AIUsageLog.objects.latest("id").model_key, "atlas-test-4")
        self.assertEqual(AIModel.objects.get(key="atlas-test-4").tokens_consumed, before + 300)

    @patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", True)
    @patch("AI.services.ai_service.OpenAICompatibleProvider.complete")
    def test_a_disabled_model_is_refused_server_side(self, complete):
        AIModel.objects.filter(key="atlas-test-4").update(is_active=False)

        response = self._send(model="atlas-test-4")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "model_disabled")
        complete.assert_not_called()

    @patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", True)
    @patch("AI.services.ai_service.OpenAICompatibleProvider.complete")
    def test_a_model_that_has_spent_its_allocation_stops_answering(self, complete):
        AIModel.objects.filter(key="atlas-test-4").update(token_allocation=1_000, tokens_consumed=1_000)

        response = self._send(model="atlas-test-4")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "model_exhausted")
        complete.assert_not_called()

    @patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", True)
    @patch("AI.services.ai_service.OpenAICompatibleProvider.complete")
    def test_an_unknown_model_falls_back_to_one_the_student_can_use(self, complete):
        """A stale tab should keep working rather than erroring."""
        complete.return_value = fake_reply()

        response = self._send(model="not-a-real-model")
        self.assertEqual(response.status_code, 200)
        # The fallback must be a model this student actually holds tokens for,
        # not merely the catalogue default.
        billed = AIUsageLog.objects.latest("id").model_key
        self.assertIn(billed, {model.key for model in wallet_service.usable_models(self.user)})

    def test_only_available_models_are_offered_to_students(self):
        spent = AIModel.objects.create(
            key="gpt-4.1-mini", name="Atlas 4.1 Mini", token_allocation=10, tokens_consumed=10,
        )
        AIModel.objects.filter(key="atlas-test-4").update(is_active=False)

        keys = [model.key for model in model_service.selectable_models()]
        self.assertNotIn("atlas-test-4", keys, "a disabled model must not be offered")
        self.assertNotIn(spent.key, keys, "a model that has spent its allocation must not be offered")
        self.assertIn(self.default.key, keys)

    def test_seeding_links_every_model_to_a_vendor_and_a_pool(self):
        """Models come from migrations; seeding only makes them usable.

        Auto-creating a model would invent one nobody chose to pay for, so the
        guarantee here is narrower and more useful: whatever is in the catalogue
        has a provider behind it, a pool to be funded from, and one default.
        """
        from AI.models import ModelTokenPool

        AIModel.objects.update(provider_ref=None, is_default=False)
        ModelTokenPool.objects.all().delete()

        model_service.ensure_seeded()

        models = list(AIModel.objects.all())
        self.assertTrue(models)
        for model in models:
            self.assertIsNotNone(model.provider_ref_id, f"{model.key} has no provider")
            self.assertTrue(ModelTokenPool.objects.filter(ai_model=model).exists())
        self.assertEqual(AIModel.objects.filter(is_default=True, is_active=True).count(), 1)

    def test_removing_a_model_keeps_its_usage_in_the_reporting(self):
        """History outlives the catalogue row it was recorded against."""
        AIUsageLog.objects.create(
            user=self.user, model="atlas-test-4", model_key="atlas-test-4",
            input_tokens=100, output_tokens=200, total_tokens=300, success=True,
        )
        self.extra.delete()

        retired = model_service.retired_usage()
        self.assertEqual(len(retired), 1)
        self.assertEqual(retired[0]["key"], "atlas-test-4")
        self.assertEqual(retired[0]["tokens_consumed"], 300)


class AllocationCycleTests(TestCase):
    """What a student is shown as "x of y" tracks one model's allocation."""

    def setUp(self):
        settings_service.bootstrap()
        self.user = User.objects.create_user(
            username="allocated", email="allocated@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.model = fund_pool(GPT)

    def test_usage_reads_as_the_admin_intended(self):
        fund(self.user, 50_000, GPT)
        wallet_service.consume(self.user, self.model, 7_650, reason="chat")

        tokens = chat_service.balance_payload(self.user, ai_model=self.model)
        self.assertEqual(tokens["balance"], 42_350)
        self.assertEqual(tokens["allocated"], 50_000)
        self.assertEqual(tokens["used"], 7_650)
        self.assertEqual(tokens["model"], GPT)

    def test_a_top_up_extends_the_current_allocation(self):
        fund(self.user, 10_000, GPT)
        wallet_service.consume(self.user, self.model, 2_000, reason="chat")
        fund(self.user, 5_000, GPT)

        tokens = chat_service.balance_payload(self.user, ai_model=self.model)
        self.assertEqual(tokens["balance"], 13_000)
        self.assertEqual(tokens["allocated"], 15_000)
        self.assertEqual(tokens["used"], 2_000)

    def test_the_balance_invariant_holds_after_an_admin_reclaim(self):
        fund(self.user, 20_000, GPT)
        wallet_service.consume(self.user, self.model, 5_000, reason="chat")
        wallet_service.reclaim(self.user, self.model, 3_000, reason="Correction")

        wallet = wallet_service.get_wallet(self.user, self.model)
        self.assertEqual(wallet.balance, 12_000)
        self.assertEqual(wallet.balance, wallet.allocated_tokens - wallet.used_tokens)

    def test_the_legacy_account_mirrors_every_wallet(self):
        """Older screens read one number; it must be the sum of the wallets."""
        fund(self.user, 20_000, GPT)
        fund(self.user, 30_000, SONNET)

        account = token_service.get_account(self.user)
        self.assertEqual(account.balance, 50_000)
        self.assertEqual(account.balance, account.cycle_allocated - account.cycle_consumed)


class EditAndResendTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        model_service.ensure_seeded()
        self.user = User.objects.create_user(
            username="editor", email="editor@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        fund(self.user, 50_000, GPT)
        self.client.force_login(self.user)

    @patch("AI.services.ai_service.OpenAICompatibleProvider.is_configured", True)
    @patch("AI.services.ai_service.OpenAICompatibleProvider.complete")
    def test_editing_a_question_replaces_everything_after_it(self, complete):
        complete.return_value = fake_reply(content="First answer.")
        self.client.post("/api/chat/send/", {"message": "Wrong question"},
                         HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        conversation = Conversation.objects.get(user=self.user)
        prompt = conversation.messages.get(role=MessageRole.USER)

        complete.return_value = fake_reply(content="Better answer.")
        response = self.client.post(
            f"/api/chat/{conversation.pk}/messages/{prompt.pk}/edit/",
            {"message": "Right question"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        prompt.refresh_from_db()
        self.assertEqual(prompt.content, "Right question")

        contents = list(conversation.messages.order_by("created_at").values_list("content", flat=True))
        self.assertEqual(contents, ["Right question", "Better answer."])

    def test_editing_another_students_message_is_refused(self):
        stranger = User.objects.create_user(
            username="stranger", email="stranger@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        conversation = Conversation.objects.create(user=stranger, title="Private")
        message = Message.objects.create(conversation=conversation, role=MessageRole.USER, content="secret")

        response = self.client.post(
            f"/api/chat/{conversation.pk}/messages/{message.pk}/edit/",
            {"message": "hijacked"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 404)
        message.refresh_from_db()
        self.assertEqual(message.content, "secret")
