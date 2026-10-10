"""Image generation, spoken replies, and the context window that pays for them.

Everything here runs against fakes.  The point is not that the vendor works —
that is what ``test_live_providers`` is for — but that the routing, the
billing and the failure paths around it do.
"""
from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from AI.models import (
    AccountStatus,
    AttachmentKind,
    Conversation,
    Message,
    MessageRole,
    MessageStatus,
    Role,
    User,
)
from AI.services import chat_service, image_service, settings_service, wallet_service
from AI.services.providers.base import AIError, AIResponse
from AI.tests.helpers import GPT, SONNET, balance, fund, model_for
from AI.utils.attachments import prepare_many

PASSWORD = "Sup3rSecret!x"

#: The smallest thing that is unambiguously a PNG.
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def make_student(username="media") -> User:
    return User.objects.create_user(
        username=username,
        password=PASSWORD,
        email=f"{username}@test.local",
        full_name="Media Student",
        is_verified=True,
        status=AccountStatus.ACTIVE,
        role=Role.STUDENT,
    )


class ImageIntentTests(TestCase):
    """What counts as asking for a picture.

    Getting this wrong in the permissive direction spends a student's tokens on
    something they did not ask for, so the bar is deliberately high.
    """

    def test_a_request_to_draw_is_recognised(self):
        for text in (
            "Create an image of a futuristic city at night.",
            "generate a picture of a lighthouse",
            "Please can you make me an illustration of the water cycle",
            "draw a cat wearing a hat",
            "/image a red bicycle in the rain",
            "/draw a diagram of a plant cell",
            "Design a poster for a science fair",
        ):
            with self.subTest(text=text):
                self.assertTrue(image_service.looks_like_image_request(text))

    def test_a_question_about_images_is_left_to_the_chat_model(self):
        for text in (
            "How do I generate an image with Python?",
            "What is image compression?",
            "Explain how diffusion models create images",
            "Why does my drawing program crash?",
            "Can you help me write code that renders a picture?",
            "Describe the painting Guernica",
            "How are photos stored on disk?",
        ):
            with self.subTest(text=text):
                self.assertFalse(image_service.looks_like_image_request(text))

    def test_the_slash_command_is_stripped_from_the_prompt(self):
        self.assertEqual(image_service.build_prompt("/image a red bicycle"), "a red bicycle")
        self.assertEqual(image_service.build_prompt("/draw: a red bicycle"), "a red bicycle")

    def test_the_request_wrapper_is_stripped_from_the_prompt(self):
        self.assertEqual(
            image_service.build_prompt("Create an image of a futuristic city at night."),
            "a futuristic city at night.",
        )

    def test_an_empty_message_is_not_a_drawing_request(self):
        self.assertFalse(image_service.looks_like_image_request(""))
        self.assertFalse(image_service.looks_like_image_request("   "))


@override_settings(AI_IMAGE_ENABLED=True, OPENAI_API_KEY="test-key")
class ImageGenerationTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.student = make_student()
        fund(self.student, 10_000, GPT)
        self.conversation = chat_service.create_conversation(self.student, "Drawing")

    def _picture(self):
        return image_service.GeneratedImage(
            data=PNG, mime="image/png", ext="png",
            model="gpt-image-1", prompt="a lighthouse", revised_prompt="a lighthouse at dawn",
            latency_ms=900,
        )

    def test_a_drawing_request_produces_a_picture_in_the_conversation(self):
        with mock.patch.object(image_service, "generate", return_value=self._picture()) as drawn:
            result = chat_service.send_message(
                self.student, self.conversation, "Create an image of a lighthouse"
            )

        drawn.assert_called_once()
        self.assertEqual(result["kind"], "image")

        reply = Message.objects.get(pk=result["assistant_message"]["id"])
        self.assertEqual(reply.role, MessageRole.ASSISTANT)
        attachment = reply.attachments.get()
        self.assertEqual(attachment.kind, AttachmentKind.IMAGE)
        self.assertEqual(attachment.size, len(PNG))
        self.assertIn("lighthouse at dawn", reply.content)
        self.assertTrue(result["assistant_message"]["attachments"][0]["url"])

    def test_the_picture_is_charged_to_the_model_being_used(self):
        """One vendor draws for every model, so the balance in use is the one that pays.

        Making a student on Claude go and find GPT tokens before they can draw
        a diagram is a rule about our billing showing through as a limit on
        their work.
        """
        fund(self.student, 10_000, SONNET)
        before_gpt = balance(self.student, GPT)
        before_sonnet = balance(self.student, SONNET)

        with mock.patch.object(image_service, "generate", return_value=self._picture()):
            chat_service.send_message(
                self.student, self.conversation, "/image a lighthouse", model_key=SONNET
            )

        cost = image_service.token_cost()
        self.assertEqual(balance(self.student, SONNET), before_sonnet - cost)
        self.assertEqual(
            balance(self.student, GPT), before_gpt,
            "only the wallet in use may move",
        )

    def test_only_one_wallet_moves_per_picture(self):
        fund(self.student, 10_000, SONNET)
        before = {key: balance(self.student, key) for key in (GPT, SONNET)}

        with mock.patch.object(image_service, "generate", return_value=self._picture()):
            chat_service.send_message(
                self.student, self.conversation, "/image a lighthouse", model_key=GPT
            )

        moved = [key for key in before if balance(self.student, key) != before[key]]
        self.assertEqual(moved, [GPT])

    def test_an_empty_wallet_falls_back_rather_than_refusing(self):
        """A single spent balance must not switch drawing off entirely."""
        student = make_student("claudeonly")
        fund(student, 10_000, SONNET)          # no GPT tokens at all
        conversation = chat_service.create_conversation(student, "Drawing")
        before = balance(student, SONNET)

        with mock.patch.object(image_service, "generate", return_value=self._picture()):
            chat_service.send_message(student, conversation, "/image a lighthouse")

        self.assertEqual(balance(student, SONNET), before - image_service.token_cost())

    def test_the_charge_follows_the_quality_actually_requested(self):
        self.assertLess(image_service.token_cost("low"), image_service.token_cost("medium"))
        self.assertLess(image_service.token_cost("medium"), image_service.token_cost("high"))
        self.assertEqual(image_service.token_cost(""), image_service.token_cost("medium"))

    def test_a_failed_drawing_costs_nothing(self):
        before = balance(self.student, GPT)
        with mock.patch.object(
            image_service, "generate", side_effect=AIError("nope", code="provider_down")
        ):
            with self.assertRaises(AIError):
                chat_service.send_message(self.student, self.conversation, "/image a lighthouse")
        self.assertEqual(balance(self.student, GPT), before)

    def test_a_student_with_nothing_left_anywhere_is_told_why(self):
        broke = make_student("skint")          # funded on no model at all
        conversation = chat_service.create_conversation(broke, "Drawing")

        with self.assertRaises(chat_service.ChatBlocked) as caught:
            chat_service.send_message(broke, conversation, "/image a lighthouse")
        self.assertEqual(caught.exception.code, "no_tokens")
        self.assertIn("Pricing", caught.exception.friendly_message)

    def test_an_attachment_means_the_vision_model_not_the_image_endpoint(self):
        """"Turn this photo into a poster" is an edit, not a fresh drawing."""
        fake = AIResponse(content="Sure.", input_tokens=5, output_tokens=5, total_tokens=10, model=GPT)
        with mock.patch.object(image_service, "generate") as drawn, mock.patch(
            "AI.services.providers.openai_provider.OpenAICompatibleProvider.complete",
            return_value=fake,
        ):
            chat_service.send_message(
                self.student, self.conversation, "make an image poster from this",
                attachments=prepare_many(
                    [SimpleUploadedFile("photo.png", PNG, content_type="image/png")], limit=5
                ),
            )
        drawn.assert_not_called()

    @override_settings(AI_IMAGE_ENABLED=False)
    def test_the_explicit_command_says_so_when_drawing_is_switched_off(self):
        with self.assertRaises(chat_service.ChatBlocked) as caught:
            chat_service.send_message(self.student, self.conversation, "/image a lighthouse")
        self.assertEqual(caught.exception.code, "image_disabled")


class ContextWindowTests(TestCase):
    """History is sized in tokens, and what falls out is summarised, not lost."""

    def setUp(self):
        settings_service.bootstrap()
        self.student = make_student("windowed")
        fund(self.student, 100_000, GPT)
        self.conversation = chat_service.create_conversation(self.student, "Long one")

    def _add(self, role, content):
        return Message.objects.create(
            conversation=self.conversation, role=role, content=content, status=MessageStatus.OK
        )

    def test_a_long_history_is_trimmed_to_the_token_budget(self):
        for index in range(40):
            self._add(MessageRole.USER, f"question {index} " + "padding " * 60)
            self._add(MessageRole.ASSISTANT, f"answer {index} " + "padding " * 60)

        turns = chat_service.history_turns(self.conversation)
        spent = sum(chat_service.estimate_tokens(turn.content) for turn in turns)
        self.assertLessEqual(spent, chat_service.context_budget() * 1.2)
        self.assertLess(len(turns), 80, "the whole conversation must not be re-sent")

    def test_the_newest_turns_are_the_ones_kept(self):
        for index in range(30):
            self._add(MessageRole.USER, f"question {index} " + "padding " * 60)
        turns = chat_service.history_turns(self.conversation)
        self.assertIn("question 29", turns[-1].content)

    def test_a_single_oversized_message_is_still_sent(self):
        """Better to send one huge turn than to answer with no context at all."""
        self._add(MessageRole.USER, "x " * (chat_service.context_budget() * 2))
        turns = chat_service.history_turns(self.conversation)
        self.assertEqual(len(turns), 1)

    def test_a_stored_summary_is_carried_into_the_context(self):
        Conversation.objects.filter(pk=self.conversation.pk).update(
            summary="They are revising photosynthesis for a Friday exam.", summary_upto_id=1
        )
        self.conversation.refresh_from_db()
        for index in range(20):
            self._add(MessageRole.USER, f"question {index} " + "padding " * 60)

        turns = chat_service.history_turns(self.conversation)
        self.assertTrue(turns[0].content.startswith(chat_service.SUMMARY_PREFIX))
        self.assertIn("photosynthesis", turns[0].content)

    def test_a_short_conversation_is_sent_whole_and_gains_no_summary(self):
        self._add(MessageRole.USER, "hello")
        self._add(MessageRole.ASSISTANT, "hi")
        turns = chat_service.history_turns(self.conversation)
        self.assertEqual(len(turns), 2)
        self.assertFalse(any(t.content.startswith(chat_service.SUMMARY_PREFIX) for t in turns))

    def test_failed_exchanges_are_not_context(self):
        Message.objects.create(
            conversation=self.conversation, role=MessageRole.USER,
            content="this one errored", status=MessageStatus.FAILED,
        )
        self._add(MessageRole.USER, "this one worked")
        turns = chat_service.history_turns(self.conversation)
        self.assertEqual([t.content for t in turns], ["this one worked"])

    def test_the_summary_is_not_regenerated_for_a_short_conversation(self):
        self._add(MessageRole.USER, "hello")
        route = mock.Mock()
        self.assertFalse(chat_service.refresh_summary(self.student, self.conversation, route))
        route.provider.complete.assert_not_called()

    def _route(self, text="They are revising photosynthesis.", tokens=90):
        route = mock.Mock()
        route.model = model_for(GPT)
        route.provider.name = "openai"
        route.provider.complete.return_value = AIResponse(
            content=text, input_tokens=tokens - 20, output_tokens=20,
            total_tokens=tokens, model=GPT,
        )
        return route

    def test_a_long_conversation_is_summarised_once_and_then_left_alone(self):
        """The call is made when enough has fallen out, and not again until more does."""
        for index in range(60):
            self._add(MessageRole.USER, f"question {index}")
            self._add(MessageRole.ASSISTANT, f"answer {index}")

        route = self._route()
        self.assertTrue(chat_service.refresh_summary(self.student, self.conversation, route))
        route.provider.complete.assert_called_once()

        self.conversation.refresh_from_db()
        self.assertIn("photosynthesis", self.conversation.summary)
        self.assertGreater(self.conversation.summary_upto_id, 0)

        # Nothing new has fallen out, so nothing is re-summarised.
        route.provider.complete.reset_mock()
        self.assertFalse(chat_service.refresh_summary(self.student, self.conversation, route))
        route.provider.complete.assert_not_called()

    def test_summarising_is_charged_to_the_model_that_answered(self):
        for index in range(60):
            self._add(MessageRole.USER, f"question {index}")

        before = balance(self.student, GPT)
        route = self._route(tokens=90)
        chat_service.refresh_summary(self.student, self.conversation, route)
        self.assertEqual(balance(self.student, GPT), before - 90)

    def test_a_failed_summary_leaves_the_conversation_exactly_as_it_was(self):
        """Housekeeping must never damage the thing it was tidying."""
        for index in range(60):
            self._add(MessageRole.USER, f"question {index}")

        before = balance(self.student, GPT)
        route = self._route()
        route.provider.complete.side_effect = AIError("busy", code="rate_limited")

        self.assertFalse(chat_service.refresh_summary(self.student, self.conversation, route))
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.summary, "")
        self.assertEqual(balance(self.student, GPT), before)

    @override_settings(AI_IMAGE_ENABLED=True, OPENAI_API_KEY="test-key")
    def test_summarising_never_delays_or_breaks_the_reply(self):
        """A reply that already succeeded is not undone by a summary that fails."""
        fund(self.student, 50_000, GPT)
        for index in range(60):
            self._add(MessageRole.USER, f"question {index}")
            self._add(MessageRole.ASSISTANT, f"answer {index}")

        reply = AIResponse(content="Here you go.", input_tokens=30, output_tokens=10,
                           total_tokens=40, model=GPT)
        with mock.patch(
            "AI.services.providers.openai_provider.OpenAICompatibleProvider.complete"
        ) as complete:
            complete.side_effect = [reply, AIError("busy", code="rate_limited")]
            result = chat_service.send_message(self.student, self.conversation, "one more question")

        self.assertEqual(result["assistant_message"]["content"], "Here you go.")


@override_settings(OPENAI_API_KEY="test-key")
class TranscriptionRequestTests(TestCase):
    """The request has to fit the model it is sent to.

    Only the whisper family accepts ``verbose_json``; the newer models reject
    it outright. Getting this wrong turns every voice message into a 400, and
    the setting that chooses the model is the one an operator is most likely
    to change.
    """

    def _post(self, model):
        from AI.services import ai_service

        response = mock.Mock(status_code=200)
        response.json.return_value = {"text": "hello", "duration": 2.0}
        with override_settings(AI_TRANSCRIBE_MODEL=model):
            with mock.patch("AI.services.ai_service.requests.post", return_value=response) as post:
                ai_service.transcribe(b"audio-bytes", filename="a.webm", mime="audio/webm")
        return post.call_args.kwargs["data"]

    def test_whisper_is_asked_for_the_rich_format(self):
        self.assertEqual(self._post("whisper-1")["response_format"], "verbose_json")

    def test_the_newer_models_are_asked_for_plain_json(self):
        for model in ("gpt-4o-transcribe", "gpt-4o-mini-transcribe"):
            with self.subTest(model=model):
                self.assertEqual(self._post(model)["response_format"], "json")

    def test_a_missing_language_is_not_an_error(self):
        """The newer models report no language. The transcript is still right."""
        from AI.services import ai_service

        response = mock.Mock(status_code=200)
        response.json.return_value = {"text": "فوٹون"}
        with override_settings(AI_TRANSCRIBE_MODEL="gpt-4o-transcribe"):
            with mock.patch("AI.services.ai_service.requests.post", return_value=response):
                result = ai_service.transcribe(b"audio", filename="a.webm", mime="audio/webm")

        self.assertEqual(result.language, "")
        self.assertTrue(result.text)
        self.assertGreater(result.duration, 0, "a duration is always needed — it is what is charged")

    def test_silence_is_refused_rather_than_charged_for(self):
        from AI.services import ai_service

        response = mock.Mock(status_code=200)
        response.json.return_value = {"text": "   "}
        with mock.patch("AI.services.ai_service.requests.post", return_value=response):
            with self.assertRaises(AIError) as caught:
                ai_service.transcribe(b"audio", filename="a.webm", mime="audio/webm")
        self.assertEqual(caught.exception.code, "no_speech")


@override_settings(AI_SPEECH_ENABLED=True, OPENAI_API_KEY="test-key")
class SpokenReplyTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.student = make_student("listener")
        fund(self.student, 10_000, GPT)
        self.client.force_login(self.student)

    def _speech(self, characters=1000):
        from AI.services.ai_service import Speech

        return Speech(data=b"ID3fake-audio", mime="audio/mpeg", model="gpt-4o-mini-tts",
                      characters=characters, latency_ms=400)

    def test_a_reply_can_be_read_aloud_and_is_charged_by_length(self):
        before = balance(self.student, GPT)
        with mock.patch("AI.services.ai_service.synthesize", return_value=self._speech(1000)):
            response = self.client.post(
                reverse("student:chat_speak"), {"text": "x" * 1000},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "audio/mpeg")
        charged = int(response["X-Tokens-Charged"])
        self.assertEqual(charged, settings.AI_SPEECH_TOKENS_PER_1K_CHARS)
        self.assertEqual(balance(self.student, GPT), before - charged)

    def test_speech_is_charged_to_the_model_being_used(self):
        """A Claude user hears their answer read back, paid from their Claude balance."""
        fund(self.student, 5_000, SONNET)
        before_sonnet = balance(self.student, SONNET)
        before_gpt = balance(self.student, GPT)

        with mock.patch("AI.services.ai_service.synthesize", return_value=self._speech(1000)):
            response = self.client.post(
                reverse("student:chat_speak"),
                {"text": "x" * 1000, "model": SONNET},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 200)
        charged = int(response["X-Tokens-Charged"])
        self.assertEqual(balance(self.student, SONNET), before_sonnet - charged)
        self.assertEqual(balance(self.student, GPT), before_gpt, "only one wallet may move")

    def test_a_failed_synthesis_costs_nothing_and_explains_itself(self):
        before = balance(self.student, GPT)
        with mock.patch(
            "AI.services.ai_service.synthesize",
            side_effect=AIError("Spoken replies are busy right now.", code="rate_limited"),
        ):
            response = self.client.post(reverse("student:chat_speak"), {"text": "hello"},
                                        HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["code"], "rate_limited")
        self.assertEqual(balance(self.student, GPT), before)

    def test_an_empty_request_is_refused_before_any_call(self):
        with mock.patch("AI.services.ai_service.synthesize") as spoken:
            response = self.client.post(reverse("student:chat_speak"), {"text": "  "},
                                        HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 400)
        spoken.assert_not_called()

    def test_a_signed_out_visitor_cannot_spend_someone_elses_tokens(self):
        self.client.logout()
        response = self.client.post(reverse("student:chat_speak"), {"text": "hello"},
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 401)
