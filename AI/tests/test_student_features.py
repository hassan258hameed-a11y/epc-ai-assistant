"""Student-side features and fixes: study sets, settings, privacy, exports,
notifications, support e-mail, and the regenerate/edit safety net."""
from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from AI.models import (
    AccountStatus,
    AIUsageLog,
    Announcement,
    AnnouncementAudience,
    ChatPreference,
    Conversation,
    EmailLog,
    EmailType,
    Message,
    MessageBookmark,
    MessageFeedback,
    MessageRole,
    Notification,
    NotificationPreference,
    SavedPrompt,
    StudySet,
    SupportTicket,
    TicketStatus,
    User,
)
from AI.services import chat_service, notification_service, settings_service, study_service, support_service
from AI.services.providers.base import AIError, AIResponse
from AI.tests.helpers import GPT, balance, fund

PASSWORD = "Sup3rSecret!x"
COMPLETE = "AI.services.providers.openai_provider.OpenAICompatibleProvider.complete"


def student(name, **extra):
    return User.objects.create_user(username=name, email=f"{name}@test.local", password=PASSWORD,
                                    is_verified=True, status=AccountStatus.ACTIVE, **extra)


def reply(content="Answer.", total=30):
    return AIResponse(content=content, input_tokens=10, output_tokens=total - 10, total_tokens=total, model=GPT)


class Base(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.ann = student("ann")
        fund(self.ann, 50_000)
        self.client.force_login(self.ann)
        self.chat = chat_service.create_conversation(self.ann, "Cells")

    def ask(self, text="What do mitochondria do?", content="They make ATP through respiration, which powers the cell."):
        with patch(COMPLETE, return_value=reply(content)):
            return chat_service.send_message(self.ann, self.chat, text, model_key=GPT)

    def ajax(self, method, url, data=None):
        return getattr(self.client, method)(url, data or {}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")


class RegenerateEditSafetyTests(Base):
    def test_a_failed_regenerate_keeps_the_answer_it_would_have_replaced(self):
        self.ask()
        answer = Message.objects.get(conversation=self.chat, role=MessageRole.ASSISTANT)
        with patch(COMPLETE, side_effect=AIError("down", code="provider_down")):
            with self.assertRaises(AIError):
                chat_service.regenerate(self.ann, self.chat)
        self.assertTrue(Message.objects.filter(pk=answer.pk).exists())

    def test_a_successful_regenerate_replaces_it(self):
        self.ask()
        old = Message.objects.get(conversation=self.chat, role=MessageRole.ASSISTANT)
        with patch(COMPLETE, return_value=reply("A fresh answer.")) as vendor:
            chat_service.regenerate(self.ann, self.chat)
        self.assertFalse(Message.objects.filter(pk=old.pk).exists())
        answers = Message.objects.filter(conversation=self.chat, role=MessageRole.ASSISTANT)
        self.assertEqual([m.content for m in answers], ["A fresh answer."])
        # The old reply is not in what the model is shown.
        self.assertNotIn("respiration", " ".join(t.content for t in vendor.call_args.args[0]))

    def test_a_failed_edit_leaves_the_conversation_as_it_was(self):
        self.ask()
        question = Message.objects.get(conversation=self.chat, role=MessageRole.USER)
        with patch(COMPLETE, side_effect=AIError("down", code="provider_down")):
            with self.assertRaises(AIError):
                chat_service.edit_and_resend(self.ann, self.chat, question.pk, "What do ribosomes do?")
        question.refresh_from_db()
        self.assertEqual(question.content, "What do mitochondria do?")
        self.assertEqual(Message.objects.filter(conversation=self.chat).count(), 2)

    def test_a_successful_edit_rewrites_and_answers_the_new_wording(self):
        self.ask()
        question = Message.objects.get(conversation=self.chat, role=MessageRole.USER)
        with patch(COMPLETE, return_value=reply("Ribosomes build proteins.")) as vendor:
            chat_service.edit_and_resend(self.ann, self.chat, question.pk, "What do ribosomes do?")
        sent = vendor.call_args.args[0]
        self.assertEqual(sent[-1].content, "What do ribosomes do?")
        contents = list(Message.objects.filter(conversation=self.chat).order_by("created_at", "id")
                        .values_list("content", flat=True))
        self.assertEqual(contents, ["What do ribosomes do?", "Ribosomes build proteins."])


class NotificationTests(Base):
    def setUp(self):
        super().setUp()
        self.note = notification_service.notify(self.ann, "Hello", "Body")

    def test_mark_as_read_is_a_post_that_works(self):
        url = reverse("student:notification_read", args=[self.note.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url, {"return": "/app/notifications/?filter=unread"}, HTTP_X_APP_PANEL="1")
        self.assertRedirects(response, "/app/notifications/?filter=unread", fetch_redirect_response=False)
        self.note.refresh_from_db()
        self.assertTrue(self.note.is_read)

    def test_the_return_address_stays_on_the_notifications_page(self):
        url = reverse("student:notification_read", args=[self.note.pk])
        response = self.client.post(url, {"return": "https://evil.example/"})
        self.assertEqual(response["Location"], reverse("student:notifications"))

    def test_following_a_button_marks_it_read_quietly(self):
        url = reverse("student:notification_read", args=[self.note.pk])
        data = self.client.post(url, HTTP_X_MARK_ONLY="1", HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        self.assertEqual(data["unread"], 0)

    def test_delete_removes_only_your_own(self):
        other = notification_service.notify(student("bob"), "Hi bob")
        self.client.post(reverse("student:notification_delete", args=[other.pk]))
        self.assertTrue(Notification.objects.filter(pk=other.pk).exists())
        self.client.post(reverse("student:notification_delete", args=[self.note.pk]))
        self.assertFalse(Notification.objects.filter(pk=self.note.pk).exists())

    def test_the_panel_uses_forms_not_links(self):
        body = self.client.get(reverse("student:notifications"), HTTP_X_APP_PANEL="1").content.decode()
        self.assertIn(f'action="{reverse("student:notification_read", args=[self.note.pk])}"', body)
        self.assertNotIn(f'<a href="{reverse("student:notification_read", args=[self.note.pk])}"', body)


class ResyncPayloadTests(Base):
    def test_messages_carry_rating_saved_and_attachments(self):
        self.ask()
        answer = Message.objects.get(conversation=self.chat, role=MessageRole.ASSISTANT)
        MessageFeedback.objects.create(message=answer, user=self.ann, rating=-1, model_key=GPT)
        MessageBookmark.objects.create(user=self.ann, message=answer)
        data = self.ajax("get", reverse("student:chat_messages", args=[self.chat.pk])).json()
        last = data["messages"][-1]
        self.assertEqual(last["rating"], -1)
        self.assertTrue(last["saved"])
        self.assertEqual(last["attachments"], [])


class ExportTests(Base):
    def test_markdown_text_and_print(self):
        self.ask()
        url = reverse("student:chat_export", args=[self.chat.pk])
        md = self.client.get(url)
        self.assertIn(".md", md["Content-Disposition"])
        self.assertIn("## You", md.content.decode())
        txt = self.client.get(url + "?format=txt")
        self.assertIn(".txt", txt["Content-Disposition"])
        self.assertNotIn("## ", txt.content.decode())
        page = self.client.get(url + "?format=print").content.decode()
        self.assertIn("Print or save as PDF", page)
        self.assertIn("mitochondria", page)

    def test_nobody_else_can_export_it(self):
        self.client.force_login(student("bob"))
        self.assertEqual(self.client.get(reverse("student:chat_export", args=[self.chat.pk])).status_code, 404)


QUIZ = json.dumps({"title": "Cell energy", "questions": [
    {"question": "What makes ATP?", "options": ["Nucleus", "Mitochondria", "Ribosome", "Golgi"], "answer": 1,
     "why": "Respiration happens there."},
    {"question": "Broken", "options": ["Only one"], "answer": 0},
    {"question": "Letter answer?", "options": ["A1", "B1", "C1", "D1"], "answer": "C", "why": ""},
    {"question": "Out of range", "options": ["a", "b"], "answer": 5},
]})
CARDS = "```json\n" + json.dumps({"title": "Cells", "cards": [
    {"front": "ATP", "back": "The cell's energy currency."},
    {"front": "ATP", "back": "Duplicate front, dropped."},
    {"front": "", "back": "No front, dropped."},
]}) + "\n```"


class StudyParseTests(TestCase):
    def test_quiz_items_are_checked(self):
        title, items = study_service.parse("quiz", QUIZ, 8)
        self.assertEqual(title, "Cell energy")
        self.assertEqual([q["question"] for q in items], ["What makes ATP?", "Letter answer?"])
        self.assertEqual(items[1]["answer"], 2)

    def test_flashcards_tolerate_a_code_fence_and_drop_bad_cards(self):
        title, items = study_service.parse("flashcards", CARDS, 8)
        self.assertEqual(items, [{"front": "ATP", "back": "The cell's energy currency."}])

    def test_nothing_usable_is_an_error(self):
        with self.assertRaises(ValueError):
            study_service.parse("quiz", "Sorry, I can't.", 8)


class StudyFlowTests(Base):
    def test_making_a_quiz_bills_it_and_keeps_it(self):
        self.ask()
        before = balance(self.ann)
        with patch(COMPLETE, return_value=reply(QUIZ, total=400)):
            data = self.ajax("post", reverse("student:study_generate"),
                             {"conversation_id": self.chat.pk, "kind": "quiz", "count": 8, "model": GPT}).json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["set"]["size"], 2)
        self.assertEqual(balance(self.ann), before - 400)
        self.assertEqual(StudySet.objects.get().tokens_used, 400)

    def test_an_unusable_answer_is_not_charged(self):
        self.ask()
        before = balance(self.ann)
        with patch(COMPLETE, return_value=reply("no json here", total=300)):
            data = self.ajax("post", reverse("student:study_generate"),
                             {"conversation_id": self.chat.pk, "kind": "flashcards"}).json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["code"], "unusable_output")
        self.assertEqual(balance(self.ann), before)
        self.assertFalse(StudySet.objects.exists())

    def test_an_empty_chat_is_refused_before_any_request(self):
        with patch(COMPLETE) as vendor:
            data = self.ajax("post", reverse("student:study_generate"),
                             {"conversation_id": self.chat.pk, "kind": "quiz"}).json()
        self.assertEqual(data["code"], "too_short")
        vendor.assert_not_called()

    def test_a_set_is_private_and_attempts_keep_the_best_score(self):
        study_set = StudySet.objects.create(user=self.ann, kind="quiz", title="Q", items=json.loads(QUIZ)["questions"][:1])
        url = reverse("student:study_attempt", args=[study_set.pk])
        self.ajax("post", url, {"score": 60})
        self.ajax("post", url, {"score": 40})
        study_set.refresh_from_db()
        self.assertEqual((study_set.best_score, study_set.attempts), (60, 2))
        self.client.force_login(student("bob"))
        self.assertEqual(self.client.get(reverse("student:study_detail", args=[study_set.pk])).status_code, 404)

    def test_the_panel_lists_and_deletes(self):
        study_set = StudySet.objects.create(user=self.ann, kind="flashcards", title="Deck", items=[{"front": "a", "back": "b"}])
        body = self.client.get(reverse("student:study"), HTTP_X_APP_PANEL="1").content.decode()
        self.assertIn("Deck", body)
        self.client.post(reverse("student:study"), {"action": "delete", "set_id": study_set.pk}, HTTP_X_APP_PANEL="1")
        self.assertFalse(StudySet.objects.exists())


class GoalAndStreakTests(Base):
    def test_streak_counts_consecutive_days_through_yesterday(self):
        now = timezone.now()
        for days_ago in (1, 2, 3, 5):
            log = AIUsageLog.objects.create(user=self.ann, success=True, model_key=GPT)
            AIUsageLog.objects.filter(pk=log.pk).update(created_at=now - timedelta(days=days_ago))
        self.assertEqual(study_service.streak(self.ann), 3)
        self.ask()
        self.assertEqual(study_service.streak(self.ann), 4)

    def test_today_against_a_goal(self):
        self.ask()
        today = study_service.today(self.ann, goal=3)
        self.assertEqual((today["asked"], today["remaining"], today["met"]), (1, 2, False))


class SettingsTests(Base):
    def post(self, **data):
        return self.client.post(reverse("student:settings"), data, HTTP_X_APP_PANEL="1")

    def test_default_model_and_goal_are_saved_and_used(self):
        self.post(action="defaults", default_model_key=GPT, daily_goal=10)
        preference = ChatPreference.objects.get(user=self.ann)
        self.assertEqual((preference.default_model_key, preference.daily_goal), (GPT, 10))
        self.assertTrue(preference.is_blank, "defaults are not prompt instructions")
        response = self.client.get(reverse("student:chat"))
        self.assertEqual(response.context["default_model_key"], GPT)
        self.assertEqual(response.context["active_model"].key, GPT)
        self.assertIn('id="railToday"', response.content.decode())

    def test_a_model_you_cannot_use_is_refused(self):
        self.post(action="defaults", default_model_key="claude-opus-5", daily_goal=0)
        self.assertEqual(ChatPreference.objects.filter(user=self.ann).exclude(default_model_key="").count(), 0)

    def test_the_panel_offers_colours_and_privacy(self):
        body = self.client.get(reverse("student:settings"), HTTP_X_APP_PANEL="1").content.decode()
        for needle in ('data-pal-set="forest"', "data-motion-set", 'name="daily_goal"',
                       reverse("student:account_export"), reverse("student:account_close")):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)


class PrivacyTests(Base):
    def test_the_download_holds_every_chat(self):
        self.ask()
        data = json.loads(self.client.get(reverse("student:account_export")).content)
        self.assertEqual(data["account"]["username"], "ann")
        self.assertEqual(data["conversations"][0]["messages"][0]["content"], "What do mitochondria do?")

    def test_closing_needs_the_password_and_the_word(self):
        self.client.post(reverse("student:account_close"), {"password": "wrong", "confirm": "DELETE"})
        self.client.post(reverse("student:account_close"), {"password": PASSWORD, "confirm": "nope"})
        self.assertEqual(User.objects.get(pk=self.ann.pk).username, "ann")

    def test_closing_anonymises_and_signs_out(self):
        self.ask()
        response = self.client.post(reverse("student:account_close"), {"password": PASSWORD, "confirm": "delete"})
        self.assertRedirects(response, reverse("public:home"), fetch_redirect_response=False)
        user = User.objects.get(pk=self.ann.pk)
        self.assertEqual((user.username, user.email, user.is_active), (f"closed-{user.pk}", "", False))
        self.assertFalse(Conversation.objects.filter(user=user).exists())
        self.assertFalse(user.has_usable_password())
        self.assertNotIn("_auth_user_id", self.client.session)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class SupportTests(Base):
    def setUp(self):
        super().setUp()
        self.ticket = support_service.create_ticket(self.ann, data={
            "category": "technical", "subject": "It broke", "description": "Something went wrong here today.",
            "email": "ann-contact@test.local",
        })
        self.staff = User.objects.create_superuser(username="root", email="root@test.local", password=PASSWORD)

    def test_a_staff_reply_is_emailed_to_the_ticket_address(self):
        support_service.add_reply(self.ticket, self.staff, "Fixed now.", from_staff=True)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ann-contact@test.local"])
        self.assertIn("Fixed now.", mail.outbox[0].body)
        self.assertEqual(EmailLog.objects.get().email_type, EmailType.SUPPORT)

    def test_internal_notes_and_opted_out_students_get_no_email(self):
        support_service.add_reply(self.ticket, self.staff, "Note to self.", from_staff=True, internal=True)
        NotificationPreference.objects.update_or_create(user=self.ann, defaults={"email_enabled": False})
        support_service.add_reply(self.ticket, self.staff, "Reply.", from_staff=True)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(EmailLog.objects.get().status, "skipped")

    def test_a_student_can_close_their_ticket(self):
        self.client.post(reverse("student:ticket_close", args=[self.ticket.reference]))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatus.CLOSED)

    def test_a_reply_can_be_just_a_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("notes.txt", b"The error said 500 on checkout.", content_type="text/plain")
        self.client.post(reverse("student:ticket_reply", args=[self.ticket.reference]), {"body": "", "attachment": upload})
        replies = list(self.ticket.replies.all())
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0].attachment)


class AnnouncementTargetingTests(Base):
    def test_students_who_opted_out_are_never_reached(self):
        bob = student("bob")
        NotificationPreference.objects.update_or_create(user=bob, defaults={"announcements": False})
        reached = set(notification_service.audience_students(AnnouncementAudience.ALL).values_list("username", flat=True))
        self.assertIn("ann", reached)
        self.assertNotIn("bob", reached)

    def test_model_audience_and_retraction(self):
        from AI.models import AIModel

        student("bob")
        announcement = Announcement.objects.create(
            title="GPT news", body="…", audience=AnnouncementAudience.MODEL,
            audience_model=AIModel.objects.get(key=GPT),
        )
        self.assertEqual(notification_service.publish_announcement(announcement), 1)
        self.assertEqual(Notification.objects.filter(announcement=announcement).get().user, self.ann)
        notification_service.retract_announcement(announcement)
        self.assertFalse(Notification.objects.filter(announcement=announcement).exists())

    def test_a_scheduled_announcement_goes_out_once_when_due(self):
        announcement = Announcement.objects.create(
            title="Later", body="…", scheduled_for=timezone.now() - timedelta(minutes=1),
        )
        future = Announcement.objects.create(title="Tomorrow", body="…", scheduled_for=timezone.now() + timedelta(days=1))
        self.assertEqual(notification_service.publish_due(), 1)
        self.assertEqual(notification_service.publish_due(), 0)
        announcement.refresh_from_db()
        future.refresh_from_db()
        self.assertTrue(announcement.is_published)
        self.assertFalse(future.is_published)


class ShellTests(Base):
    def test_notice_banner_help_badge_and_invite_link(self):
        settings_service.set_value("maintenance_note", "Maintenance on Sunday 2am.")
        SupportTicket.objects.create(user=self.ann, full_name="Ann", email="a@x.y", category="other",
                                     subject="Help me", description="x" * 30)
        body = self.client.get(reverse("student:chat")).content.decode()
        self.assertIn("Maintenance on Sunday 2am.", body)
        self.assertIn('title="Open support requests">1<', body)
        self.assertIn(reverse("student:referrals"), body)
        self.assertIn('<span class="kbd">⌘J</span>', body)

    def test_the_vision_setting_is_respected(self):
        settings_service.set_value("ai_vision_enabled", False)
        self.assertFalse(self.client.get(reverse("student:chat")).context["vision_enabled"])


class PromptAndSavedTests(Base):
    def test_a_student_can_edit_their_own_prompt_only(self):
        mine = SavedPrompt.objects.create(user=self.ann, title="Old", body="Old body")
        featured = SavedPrompt.objects.create(user=None, is_featured=True, title="Feat", body="F")
        url = reverse("student:prompts")
        self.client.post(url, {"action": "update", "prompt_id": mine.pk, "title": "New", "body": "New body"},
                         HTTP_X_APP_PANEL="1")
        self.client.post(url, {"action": "update", "prompt_id": featured.pk, "title": "Hacked", "body": "x"},
                         HTTP_X_APP_PANEL="1")
        mine.refresh_from_db()
        featured.refresh_from_db()
        self.assertEqual((mine.title, featured.title), ("New", "Feat"))

    def test_saved_answers_take_a_searchable_note(self):
        self.ask()
        answer = Message.objects.get(conversation=self.chat, role=MessageRole.ASSISTANT)
        MessageBookmark.objects.create(user=self.ann, message=answer)
        self.client.post(reverse("student:saved"), {"note_for": answer.pk, "note": "for the essay"}, HTTP_X_APP_PANEL="1")
        self.assertEqual(MessageBookmark.objects.get().note, "for the essay")
        body = self.client.get(reverse("student:saved") + "?q=essay", HTTP_X_APP_PANEL="1").content.decode()
        self.assertIn("for the essay", body)

    def test_a_reason_after_a_comment_keeps_the_comment(self):
        from AI.services import library_service

        self.ask()
        answer = Message.objects.get(conversation=self.chat, role=MessageRole.ASSISTANT)
        library_service.rate_message(self.ann, answer.pk, -1, reason="inaccurate", comment="Wrong formula")
        library_service.rate_message(self.ann, answer.pk, -1, reason="incomplete")
        self.assertEqual(MessageFeedback.objects.get().comment, "Wrong formula")


class UsageTests(Base):
    def test_filters_and_csv(self):
        self.ask()
        body = self.client.get(reverse("student:usage") + f"?days=7&model={GPT}", HTTP_X_APP_PANEL="1").content.decode()
        self.assertIn("Last 7 days", body)
        csv_body = self.client.get(reverse("student:usage_export") + "?days=7").content.decode()
        self.assertIn("Date,Model,Requests", csv_body)
        self.assertEqual(len(csv_body.strip().splitlines()), 2)
