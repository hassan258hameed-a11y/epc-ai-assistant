"""Answer quality: what every model is told, and how much care each question gets.

Three things are pinned here:

* **the question profile.** A greeting is answered without the follow-up
  instruction; maths and code get more reasoning (OpenAI reasoning models) or
  near-deterministic sampling without repetition penalties (GPT-4o); everything
  else is sent exactly as before.
* **the system prompt.** It asks for checked, readable answers in what the chat
  can display, and a student's chosen style now outranks its default length.
* **the renderer.** LaTeX that slips through reads as maths, and a model's line
  breaks survive.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import patch

from django.apps import apps as django_apps
from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from AI.models import AccountStatus, AIModel, AppSetting, ChatPreference, User
from AI.services import chat_service, followup_service, providers, question_profile, settings_service
from AI.services.providers.base import AIError, AIResponse, ChatTurn
from AI.tests.helpers import GPT, fund

PASSWORD = "Sup3rSecret!x"
POST = "AI.services.providers.openai_provider.requests.post"
COMPLETE = "AI.services.providers.openai_provider.OpenAICompatibleProvider.complete"
TEMPLATES = Path(settings.BASE_DIR) / "templates"
PROMPT_MIGRATION = importlib.import_module("AI.migrations.0042_system_prompt_quality")


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def completion(content="Hello.", finish="stop", reasoning=25):
    return {
        "id": "chatcmpl-test", "model": "gpt-5-mini",
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 60, "total_tokens": 100,
                  "completion_tokens_details": {"reasoning_tokens": reasoning}},
    }


def reply():
    return AIResponse(content="Answer.", input_tokens=10, output_tokens=20, total_tokens=30, model=GPT)


class ClassifyTests(SimpleTestCase):

    def test_greetings_and_thanks_are_light(self):
        for text in ("hi", "Hello!", "hey there", "thanks a lot 🙏", "Thank you so much", "ok", "bye",
                     "good morning", "សួស្តី", "អរគុណច្រើន"):
            with self.subTest(text=text):
                self.assertEqual(question_profile.classify(text), question_profile.LIGHT)

    def test_a_greeting_with_a_question_is_not_light(self):
        self.assertEqual(question_profile.classify("hi, can you explain photosynthesis?"), question_profile.STANDARD)

    def test_maths_science_and_code_are_deep(self):
        for text in ("Solve 2x + 3 = 7", "what is 15% of 240", "x^2 - 5x + 6 = 0", "√49 × 3",
                     "a car goes from 0 to 20 m/s in 5 s, find the acceleration",
                     "write a code to print 10 table in java", "why is my python giving IndexError",
                     "def add(a, b):\n    return a+b", "differentiate sin(x) cos(x)"):
            with self.subTest(text=text):
                self.assertEqual(question_profile.classify(text), question_profile.DEEP)

    def test_everyday_questions_are_standard(self):
        for text in ("name capital of cambodia", "what is the meaning of humble", "give me 2-3 examples of verbs",
                     "Explain the causes of World War 1", "what is html", "what happened in 2019-2020",
                     "how many continents are there", "Translate your last answer into Khmer.", "", "kick"):
            with self.subTest(text=text):
                self.assertEqual(question_profile.classify(text), question_profile.STANDARD)


@override_settings(OPENAI_API_KEY="sk-test", OPENAI_BASE_URL="https://api.openai.test/v1")
class WireTests(SimpleTestCase):
    """What leaves the server for each profile."""

    def send(self, model, *, profile, max_tokens=None, thinking_mode="off", responses=None):
        provider = providers.build("openai", model=model, max_output_tokens=4096, thinking_mode=thinking_mode,
                                   temperature=0.7)
        side = responses or [FakeResponse(completion())]
        with patch(POST, side_effect=side) as post:
            result = provider.complete([ChatTurn(role="user", content="q")], system_prompt="S",
                                       max_tokens=max_tokens, profile=profile)
        return post, result

    @staticmethod
    def payload(post, call=-1):
        return json.loads(post.call_args_list[call].kwargs["data"])

    def test_a_deep_question_thinks_harder_when_there_is_room(self):
        post, _ = self.send("gpt-5-mini", profile=question_profile.DEEP, max_tokens=4096)
        self.assertEqual(self.payload(post)["reasoning_effort"], "medium")

    def test_without_room_to_think_it_stays_low(self):
        post, _ = self.send("gpt-5-mini", profile=question_profile.DEEP, max_tokens=3000)
        self.assertEqual(self.payload(post)["reasoning_effort"], "low")

    def test_everyday_questions_stay_low(self):
        for profile in (question_profile.STANDARD, question_profile.LIGHT):
            with self.subTest(profile=profile):
                post, _ = self.send("gpt-6-astra", profile=profile, max_tokens=4096)
                self.assertEqual(self.payload(post)["reasoning_effort"], "low")

    def test_adaptive_thinking_is_still_the_vendors_call(self):
        post, _ = self.send("gpt-5-mini", profile=question_profile.DEEP, max_tokens=4096, thinking_mode="adaptive")
        self.assertNotIn("reasoning_effort", self.payload(post))

    def test_a_medium_attempt_that_only_thought_is_retried_at_low(self):
        empty = FakeResponse(completion(content="", finish="length", reasoning=4096))
        post, result = self.send("gpt-5-mini", profile=question_profile.DEEP, max_tokens=4096,
                                 responses=[empty, FakeResponse(completion("x = 2"))])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(self.payload(post, 0)["reasoning_effort"], "medium")
        self.assertEqual(self.payload(post, 1)["reasoning_effort"], "low")
        self.assertEqual(result.content, "x = 2")
        self.assertEqual(result.total_tokens, 100, "only the answer that was delivered is billed")

    def test_a_low_attempt_that_only_thought_is_not_retried(self):
        empty = FakeResponse(completion(content="", finish="length", reasoning=2048))
        with self.assertRaises(AIError) as caught:
            self.send("gpt-5-mini", profile=question_profile.STANDARD, max_tokens=2048, responses=[empty])
        self.assertEqual(caught.exception.code, "reasoning_budget_exhausted")

    def test_a_sampling_model_is_steady_on_maths_and_code(self):
        post, _ = self.send("gpt-4o", profile=question_profile.DEEP, max_tokens=1000)
        payload = self.payload(post)
        self.assertEqual(payload["temperature"], question_profile.DEEP_TEMPERATURE)
        # A digit or a variable name has to repeat in working and in code.
        self.assertNotIn("frequency_penalty", payload)
        self.assertNotIn("presence_penalty", payload)

    def test_a_sampling_model_is_unchanged_otherwise(self):
        post, _ = self.send("gpt-4o", profile=question_profile.STANDARD, max_tokens=1000)
        payload = self.payload(post)
        self.assertEqual(payload["temperature"], 0.7)
        self.assertEqual(payload["frequency_penalty"], settings.AI_FREQUENCY_PENALTY)


class ChatFlowTests(TestCase):

    def setUp(self):
        settings_service.bootstrap()
        self.ann = User.objects.create_user(username="ann", email="ann@test.local", password=PASSWORD,
                                            is_verified=True, status=AccountStatus.ACTIVE)
        fund(self.ann, 30_000)
        self.chat = chat_service.create_conversation(self.ann)

    def send(self, text):
        with patch(COMPLETE, return_value=reply()) as vendor:
            chat_service.send_message(self.ann, self.chat, text, model_key=GPT)
        return vendor.call_args.kwargs

    def test_a_greeting_is_sent_without_the_follow_up_instruction(self):
        self.assertNotIn(followup_service.MARKER, self.send("hi")["system_prompt"])
        self.assertIn(followup_service.MARKER, self.send("Explain osmosis")["system_prompt"])

    def test_the_profile_reaches_the_vendor(self):
        self.assertEqual(self.send("hello")["profile"], question_profile.LIGHT)
        self.assertEqual(self.send("Explain osmosis")["profile"], question_profile.STANDARD)
        self.assertEqual(self.send("Solve 2x + 3 = 7")["profile"], question_profile.DEEP)

    def test_a_deep_question_on_a_reasoning_model_gets_room_to_think(self):
        configured = AIModel.objects.get(key=GPT).max_output_tokens
        self.assertEqual(self.send("Explain osmosis")["max_tokens"], configured)
        self.assertEqual(self.send("Solve 2x + 3 = 7")["max_tokens"],
                         max(configured, question_profile.DEEP_REASONING_BUDGET))

    def test_room_to_think_never_exceeds_what_the_student_can_spend(self):
        bob = User.objects.create_user(username="bob", email="bob@test.local", password=PASSWORD,
                                       is_verified=True, status=AccountStatus.ACTIVE)
        fund(bob, 3_000)
        chat = chat_service.create_conversation(bob)
        with patch(COMPLETE, return_value=reply()) as vendor:
            chat_service.send_message(bob, chat, "Solve 2x + 3 = 7", model_key=GPT)
        self.assertLess(vendor.call_args.kwargs["max_tokens"], 3_000)

    def test_regenerating_keeps_the_same_care(self):
        self.send("Solve 2x + 3 = 7")
        with patch(COMPLETE, return_value=reply()) as vendor:
            chat_service.regenerate(self.ann, self.chat)
        self.assertEqual(vendor.call_args.kwargs["profile"], question_profile.DEEP)

    def test_a_chosen_style_outranks_the_default_length(self):
        ChatPreference.objects.create(user=self.ann, style=ChatPreference.Style.DETAILED, enabled=True)
        prompt = self.send("Explain osmosis")["system_prompt"]
        self.assertIn("Give thorough, well-structured answers", prompt)
        self.assertIn("They override the default length and format above", prompt)
        self.assertIn("accuracy and honesty above always take priority", prompt)


class SystemPromptTests(TestCase):

    #: What the previous prompt and follow-up instruction cost together, by the
    #: same estimate: the new pair must not cost more on any request.
    PREVIOUS_COST = 300

    def setUp(self):
        settings_service.bootstrap()

    def test_the_live_prompt_is_the_new_default(self):
        self.assertEqual(settings_service.get("ai_system_prompt"), settings_service.DEFAULT_SYSTEM_PROMPT)

    def test_the_code_default_and_the_migration_agree(self):
        self.assertEqual(settings_service.DEFAULT_SYSTEM_PROMPT, PROMPT_MIGRATION.NEW_DEFAULT)

    def test_it_asks_for_checked_readable_answers(self):
        prompt = settings_service.DEFAULT_SYSTEM_PROMPT
        for phrase in ("Lead with the answer", "check every calculation", "state your assumption",
                       "never LaTeX", "final answer in bold", "never nested lists",
                       "Reply in the language the student wrote in", "create an image of a red bicycle"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)

    def test_it_costs_no_more_than_before(self):
        """Re-sent, and paid for, on every request."""
        prompt = chat_service.estimate_tokens(settings_service.DEFAULT_SYSTEM_PROMPT)
        followups = chat_service.estimate_tokens(followup_service.INSTRUCTION)
        self.assertLessEqual(prompt + followups, self.PREVIOUS_COST)
        # A greeting carries the prompt alone.
        self.assertLess(prompt, self.PREVIOUS_COST * 0.8)

    def test_an_administrators_own_prompt_is_never_rewritten(self):
        AppSetting.objects.filter(key="ai_system_prompt").update(value="Our own prompt.")
        PROMPT_MIGRATION.forwards(django_apps, None)
        self.assertEqual(AppSetting.objects.get(key="ai_system_prompt").value, "Our own prompt.")

    def test_the_previous_default_is_upgraded_and_can_be_rolled_back(self):
        AppSetting.objects.filter(key="ai_system_prompt").update(value=PROMPT_MIGRATION.PREVIOUS_DEFAULT)
        PROMPT_MIGRATION.forwards(django_apps, None)
        self.assertEqual(AppSetting.objects.get(key="ai_system_prompt").value, PROMPT_MIGRATION.NEW_DEFAULT)
        PROMPT_MIGRATION.backwards(django_apps, None)
        self.assertEqual(AppSetting.objects.get(key="ai_system_prompt").value, PROMPT_MIGRATION.PREVIOUS_DEFAULT)


class RendererTests(SimpleTestCase):

    def test_every_answer_renderer_tidies_maths(self):
        for name in ("chat/chat.html", "components/light_markdown_js.html"):
            with self.subTest(template=name):
                source = (TEMPLATES / name).read_text(encoding="utf-8")
                self.assertIn('{% include "components/tidy_math_js.html" %}', source)
                self.assertIn("return tidyMath(", source)
        for name in ("public/shared_chat.html", "student/chat_print.html"):
            with self.subTest(template=name):
                source = (TEMPLATES / name).read_text(encoding="utf-8")
                self.assertIn('{% include "components/light_markdown_js.html" %}', source)

    def test_the_chat_keeps_a_models_line_breaks(self):
        source = (TEMPLATES / "chat" / "chat.html").read_text(encoding="utf-8")
        self.assertIn("paragraph.map(inline).join('<br>')", source)
        self.assertNotIn("inline(paragraph.join(' '))", source)
