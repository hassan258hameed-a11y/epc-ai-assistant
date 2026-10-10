"""Search inside messages, archived chats, and the chat page's new controls."""
from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from AI.models import AccountStatus, Conversation, Message, MessageRole, User
from AI.services import chat_service, settings_service
from AI.services.providers.base import AIResponse
from AI.tests.helpers import fund

PASSWORD = "Sup3rSecret!x"
COMPLETE = "AI.services.providers.openai_provider.OpenAICompatibleProvider.complete"


def student(name):
    return User.objects.create_user(
        username=name, email=f"{name}@test.local", password=PASSWORD,
        is_verified=True, status=AccountStatus.ACTIVE,
    )


class Fixture(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.ann = student("ann")
        fund(self.ann, 20_000)
        self.chat = chat_service.create_conversation(self.ann, "Biology revision")
        answer = "Mitochondria are the powerhouse of the cell; they make ATP through respiration."
        with patch(COMPLETE, return_value=AIResponse(
            content=answer, input_tokens=10, output_tokens=20, total_tokens=30, model="gpt-5-mini",
        )):
            chat_service.send_message(self.ann, self.chat, "What do mitochondria do?")
        self.client.force_login(self.ann)

    def search(self, q):
        return self.client.get(f"/api/chat/search/?q={q}", HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()


class SearchTests(Fixture):

    def test_a_word_inside_an_answer_finds_the_chat_with_an_excerpt(self):
        data = self.search("ATP")
        self.assertEqual([c["id"] for c in data["conversations"]], [self.chat.pk])
        hit = data["conversations"][0]
        self.assertIn("ATP", hit["snippet"])
        answer = Message.objects.get(conversation=self.chat, role=MessageRole.ASSISTANT)
        self.assertEqual(hit["message_id"], answer.pk)

    def test_a_title_match_needs_no_excerpt(self):
        hit = self.search("revision")["conversations"][0]
        self.assertNotIn("snippet", hit)

    def test_a_long_message_is_excerpted_around_the_match(self):
        Message.objects.create(conversation=self.chat, role=MessageRole.USER,
                               content=("filler " * 80) + "zebrafish" + (" filler" * 80))
        snippet = self.search("zebrafish")["conversations"][0]["snippet"]
        self.assertIn("zebrafish", snippet)
        self.assertLess(len(snippet), 200)
        self.assertTrue(snippet.startswith("…") and snippet.endswith("…"))

    def test_nobody_else_sees_your_messages(self):
        self.client.force_login(student("bob"))
        self.assertEqual(self.search("ATP")["conversations"], [])

    def test_archived_chats_stay_out_of_search(self):
        Conversation.objects.filter(pk=self.chat.pk).update(is_archived=True)
        self.assertEqual(self.search("ATP")["conversations"], [])


class ArchivedTests(Fixture):

    def archive(self):
        self.client.post(f"/api/chat/{self.chat.pk}/archive/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_the_panel_lists_archived_chats_only(self):
        body = self.client.get("/app/archived/").content.decode()
        self.assertIn("Nothing archived", body)
        self.archive()
        body = self.client.get("/app/archived/").content.decode()
        self.assertIn("Biology revision", body)

    def test_restore_puts_it_back(self):
        self.archive()
        self.client.post("/app/archived/", {"action": "restore", "conversation_id": self.chat.pk})
        self.chat.refresh_from_db()
        self.assertFalse(self.chat.is_archived)

    def test_delete_removes_it(self):
        self.archive()
        self.client.post("/app/archived/", {"action": "delete", "conversation_id": self.chat.pk})
        self.assertFalse(Conversation.objects.filter(pk=self.chat.pk).exists())

    def test_a_live_chat_cannot_be_deleted_from_here(self):
        """Only archived chats are in scope: this page is not a back door."""
        self.client.post("/app/archived/", {"action": "delete", "conversation_id": self.chat.pk})
        self.assertTrue(Conversation.objects.filter(pk=self.chat.pk).exists())

    def test_another_students_chat_is_untouchable(self):
        self.archive()
        self.client.force_login(student("bob"))
        self.client.post("/app/archived/", {"action": "delete", "conversation_id": self.chat.pk})
        self.assertTrue(Conversation.objects.filter(pk=self.chat.pk).exists())
        self.assertNotIn("Biology revision", self.client.get("/app/archived/").content.decode())

    def test_search_within_archived(self):
        self.archive()
        self.assertIn("Biology revision", self.client.get("/app/archived/?q=biology").content.decode())
        self.assertIn("No archived chats match", self.client.get("/app/archived/?q=volcano").content.decode())

    def test_the_sidebar_link_shows_the_count(self):
        self.assertIn('id="archivedLink"\n         hidden', self.client.get("/chat/").content.decode())
        self.archive()
        body = self.client.get("/chat/").content.decode()
        self.assertIn('<span id="archivedCount">1</span>', body)


class ShellTests(Fixture):

    def test_the_new_controls_are_on_the_page(self):
        body = self.client.get(f"/chat/{self.chat.pk}/").content.decode()
        for needle in ('id="cmdk"', 'id="cmdkBtn"', 'id="cmdkBtnMobile"', 'id="rewriteMenu"', "data-rewrite ",
                       'id="netBanner"', 'id="draftNote"', "function openCmdk", "function restoreDraft",
                       "ArrowUp"):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)

    def test_rewrite_is_offered_on_the_newest_answer_only(self):
        with patch(COMPLETE, return_value=AIResponse(
            content="Second.", input_tokens=10, output_tokens=20, total_tokens=30, model="gpt-5-mini",
        )):
            chat_service.send_message(self.ann, self.chat, "And ribosomes?")
        body = self.client.get(f"/chat/{self.chat.pk}/").content.decode()
        # Rendered buttons end their tag at a line break; the copy inside the
        # page's own script (used for live replies) is followed by its icon.
        self.assertEqual(body.count('aria-label="Rewrite this answer">\n'), 1)
