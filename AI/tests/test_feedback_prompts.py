"""Answer feedback, the prompt library and the compare-models page.

What a student and an administrator would notice if these broke: a thumb that
does not stay pressed, a rating that lands on somebody else's reply, a prompt
that cannot be used or can be used by the wrong person, a console page that
disagrees with what students actually said.
"""
from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings

from AI.models import (
    AccountStatus,
    AIModel,
    AuditLog,
    MessageFeedback,
    MessageRole,
    SavedPrompt,
    User,
)
from AI.services import chat_service, library_service, settings_service
from AI.services.providers.base import AIResponse
from AI.tests.helpers import fund

PASSWORD = "Sup3rSecret!x"
GPT = "gpt-5-mini"
COMPLETE = "AI.services.providers.openai_provider.OpenAICompatibleProvider.complete"


def reply(model=GPT):
    return AIResponse(content="Answer.", input_tokens=100, output_tokens=200, total_tokens=300,
                      model=model, request_id="r", latency_ms=10)


def student(name="ann"):
    return User.objects.create_user(
        username=name, email=f"{name}@test.local", password=PASSWORD,
        is_verified=True, status=AccountStatus.ACTIVE,
    )


class Fixture(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.ann = student("ann")
        fund(self.ann, 50_000)
        self.conversation = chat_service.create_conversation(self.ann)
        with patch(COMPLETE, return_value=reply()):
            chat_service.send_message(self.ann, self.conversation, "Hello")
        self.answer = self.conversation.messages.get(role=MessageRole.ASSISTANT)
        self.question = self.conversation.messages.get(role=MessageRole.USER)
        self.client.force_login(self.ann)

    def rate(self, message_id, rating, **extra):
        return self.client.post(
            f"/api/chat/messages/{message_id}/feedback/", {"rating": rating, **extra},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )


class FeedbackTests(Fixture):

    def test_a_thumbs_up_is_recorded_against_the_model(self):
        response = self.rate(self.answer.pk, 1)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rating"], 1)
        verdict = MessageFeedback.objects.get(message=self.answer)
        self.assertEqual((verdict.rating, verdict.model_key), (1, GPT))
        self.assertEqual(verdict.ai_model.key, GPT)

    def test_changing_your_mind_edits_rather_than_adds(self):
        self.rate(self.answer.pk, 1)
        self.rate(self.answer.pk, -1, reason="inaccurate")
        self.assertEqual(MessageFeedback.objects.count(), 1)
        verdict = MessageFeedback.objects.get()
        self.assertEqual((verdict.rating, verdict.reason), (-1, "inaccurate"))

    def test_a_reason_only_explains_a_thumbs_down(self):
        self.rate(self.answer.pk, 1, reason="inaccurate")
        self.assertEqual(MessageFeedback.objects.get().reason, "")

    def test_an_unknown_reason_is_dropped(self):
        self.rate(self.answer.pk, -1, reason="<script>")
        self.assertEqual(MessageFeedback.objects.get().reason, "")

    def test_zero_clears_the_verdict(self):
        self.rate(self.answer.pk, -1)
        response = self.rate(self.answer.pk, 0)
        self.assertEqual(response.json()["rating"], 0)
        self.assertFalse(MessageFeedback.objects.exists())

    def test_another_students_reply_cannot_be_rated(self):
        self.client.force_login(student("bob"))
        response = self.rate(self.answer.pk, -1)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(MessageFeedback.objects.exists())

    def test_only_replies_can_be_rated(self):
        self.assertEqual(self.rate(self.question.pk, 1).status_code, 404)

    def test_nonsense_ratings_are_refused(self):
        self.assertEqual(self.rate(self.answer.pk, 7).status_code, 400)
        self.assertEqual(self.rate(self.answer.pk, "up").status_code, 400)

    def test_a_pressed_thumb_stays_pressed_on_reload(self):
        self.rate(self.answer.pk, 1)
        body = self.client.get(f"/chat/{self.conversation.pk}/").content.decode()
        self.assertIn(f'data-rate-for="{self.answer.pk}"', body)
        self.assertIn('data-rate-value="1"\n                              aria-pressed="true"', body)

    def test_deleting_the_conversation_removes_its_ratings(self):
        self.rate(self.answer.pk, 1)
        self.conversation.delete()
        self.assertFalse(MessageFeedback.objects.exists())


class QualityReportTests(Fixture):

    def test_the_report_counts_per_model(self):
        self.rate(self.answer.pk, -1, reason="too_long")
        report = library_service.quality_report(30)
        row = next(r for r in report["rows"] if r["key"] == GPT)
        self.assertEqual((row["up"], row["down"], row["score"]), (0, 1, 0))
        self.assertEqual(report["reasons"][0]["key"], "too_long")
        self.assertEqual(report["raters"], 1)

    def test_an_unrated_model_has_no_score_rather_than_zero(self):
        report = library_service.quality_report(30)
        row = next(r for r in report["rows"] if r["key"] == "gpt-6-astra")
        self.assertIsNone(row["score"])

    def test_every_model_is_listed(self):
        keys = {r["key"] for r in library_service.quality_report(30)["rows"]}
        self.assertTrue(set(AIModel.objects.values_list("key", flat=True)) <= keys)


class PromptLibraryTests(Fixture):

    def api(self):
        return self.client.get("/api/prompts/", HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()

    def test_the_seeded_featured_prompts_are_offered(self):
        data = self.api()
        self.assertEqual(len(data["featured"]), 6)
        self.assertTrue(all(p["featured"] for p in data["featured"]))
        self.assertEqual(data["mine"], [])

    def test_saving_from_the_composer(self):
        response = self.client.post(
            "/api/prompts/", {"title": "Flashcards", "body": "Make flashcards from:"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        mine = self.api()["mine"]
        self.assertEqual([p["title"] for p in mine], ["Flashcards"])
        self.assertTrue(mine[0]["mine"])

    def test_a_prompt_needs_a_title_and_a_body(self):
        for data in ({"title": "", "body": "x"}, {"title": "x", "body": "  "}):
            with self.subTest(data=data):
                response = self.client.post("/api/prompts/", data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
                self.assertEqual(response.status_code, 400)

    def test_there_is_a_ceiling_on_how_many(self):
        for n in range(library_service.MAX_PROMPTS_PER_STUDENT):
            SavedPrompt.objects.create(user=self.ann, title=f"p{n}", body="b")
        with self.assertRaises(library_service.LibraryError):
            library_service.create_prompt(self.ann, title="one more", body="b")

    def test_using_a_prompt_counts_it_and_returns_the_text(self):
        prompt = SavedPrompt.objects.filter(user=None).first()
        response = self.client.post(f"/api/prompts/{prompt.pk}/use/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.json()["prompt"]["body"], prompt.body)
        prompt.refresh_from_db()
        self.assertEqual(prompt.use_count, 1)

    def test_another_students_prompt_is_invisible(self):
        bob = student("bob")
        private = SavedPrompt.objects.create(user=bob, title="Bob's", body="secret")
        response = self.client.post(f"/api/prompts/{private.pk}/use/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Bob's", [p["title"] for p in self.api()["mine"]])
        with self.assertRaises(library_service.LibraryError):
            library_service.delete_prompt(self.ann, private.pk)
        self.assertTrue(SavedPrompt.objects.filter(pk=private.pk).exists())

    def test_a_hidden_featured_prompt_is_withdrawn(self):
        prompt = SavedPrompt.objects.filter(user=None).first()
        prompt.is_active = False
        prompt.save()
        self.assertNotIn(prompt.pk, [p["id"] for p in self.api()["featured"]])
        response = self.client.post(f"/api/prompts/{prompt.pk}/use/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 404)

    def test_the_panel_creates_and_deletes(self):
        self.client.post("/app/prompts/", {"action": "create", "title": "Outline", "body": "Outline this:"})
        prompt = SavedPrompt.objects.get(user=self.ann)
        body = self.client.get("/app/prompts/").content.decode()
        self.assertIn("Outline", body)
        self.assertIn(f'data-use-prompt="{prompt.pk}"', body)
        self.client.post("/app/prompts/", {"action": "delete", "prompt_id": prompt.pk})
        self.assertFalse(SavedPrompt.objects.filter(user=self.ann).exists())

    def test_the_panel_renders_as_a_fragment_too(self):
        response = self.client.get("/app/prompts/", HTTP_X_APP_PANEL="1")
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-panel-name="prompts"', response.content.decode())
        self.assertNotIn("<html", response.content.decode())


class CompareModelsTests(Fixture):

    def test_every_active_model_is_compared(self):
        body = self.client.get("/app/models/").content.decode()
        for model in AIModel.objects.filter(is_active=True):
            with self.subTest(model=model.key):
                self.assertIn(f'data-cmp-model="{model.key}"', body)

    def test_prices_come_from_the_live_packages(self):
        body = self.client.get("/app/models/").content.decode()
        self.assertIn("$2.49", body)       # GPT-5 Mini, 100k
        self.assertIn("$14.99", body)      # GPT-6 Astra, 100k

    def test_only_a_usable_model_offers_chat(self):
        body = self.client.get("/app/models/").content.decode()
        self.assertIn(f'data-use-model="{GPT}"', body)
        self.assertNotIn('data-use-model="gpt-6-astra"', body)

    def test_reasoning_is_the_providers_answer(self):
        from AI.views.panels import _thinks_first

        self.assertTrue(_thinks_first(AIModel.objects.get(key="gpt-6-astra")))
        self.assertFalse(_thinks_first(AIModel.objects.get(key="gpt-4o")))


class ShellTests(Fixture):

    def test_the_sidebar_and_composer_carry_the_new_features(self):
        body = self.client.get("/chat/").content.decode()
        for needle in ('data-panel="models"', 'data-panel="prompts"', 'id="promptBtn"',
                       'id="promptMenu"', "function rateHtml", "i-thumb-up"):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)


class ConsoleTests(Fixture):

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(
            username="root", email="root@test.local", password=PASSWORD,
        )

    def test_quality_page_shows_what_students_said(self):
        self.rate(self.answer.pk, -1, reason="inaccurate", comment="Wrong date")
        self.client.force_login(self.admin)
        response = self.client.get("/admin/quality/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Answer quality", body)
        self.assertIn("Inaccurate", body)
        self.assertIn("Wrong date", body)
        self.assertIn(f"/admin/conversations/{self.conversation.pk}/", body)

    def test_quality_filters_and_ranges(self):
        self.client.force_login(self.admin)
        for query in ("?range=7", "?range=90", f"?model={GPT}", "?reason=too_long", "?range=bogus"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get("/admin/quality/" + query).status_code, 200)

    def test_students_cannot_open_the_console_pages(self):
        for url in ("/admin/quality/", "/admin/prompts/"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertNotIn("/admin/", response["Location"])

    def test_an_admin_publishes_edits_hides_and_deletes(self):
        self.client.force_login(self.admin)
        self.client.post("/admin/prompts/new/", {"title": "Exam plan", "body": "Plan my revision for:", "category": "Study"})
        prompt = SavedPrompt.objects.get(title="Exam plan")
        self.assertTrue(prompt.is_featured)
        self.assertIsNone(prompt.user)
        self.assertEqual(self.client.get("/admin/prompts/").status_code, 200)
        self.assertEqual(self.client.get(f"/admin/prompts/?edit={prompt.pk}").status_code, 200)

        self.client.post(f"/admin/prompts/{prompt.pk}/save/", {"title": "Exam planner", "body": "Plan:", "sort_order": 5})
        prompt.refresh_from_db()
        self.assertEqual((prompt.title, prompt.sort_order), ("Exam planner", 5))

        self.client.post(f"/admin/prompts/{prompt.pk}/toggle/")
        prompt.refresh_from_db()
        self.assertFalse(prompt.is_active)

        self.client.post(f"/admin/prompts/{prompt.pk}/delete/")
        self.assertFalse(SavedPrompt.objects.filter(pk=prompt.pk).exists())
        self.assertGreaterEqual(AuditLog.objects.filter(description__icontains="prompt").count(), 4)

    def test_an_admin_cannot_touch_a_students_own_prompt(self):
        private = SavedPrompt.objects.create(user=self.ann, title="Mine", body="x")
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(f"/admin/prompts/{private.pk}/delete/").status_code, 404)
        self.assertTrue(SavedPrompt.objects.filter(pk=private.pk).exists())

    def test_the_console_nav_links_both_pages(self):
        self.client.force_login(self.admin)
        body = self.client.get("/admin/").content.decode()
        self.assertIn("/admin/quality/", body)
        self.assertIn("/admin/prompts/", body)
