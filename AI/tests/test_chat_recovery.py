"""Deleting and recovering chats, model status, and never paying twice.

Covers the trash (delete → restore / delete for good / purge after 30 days),
what a trashed chat disappears from, idempotent pin/archive/unsave, the
notification return path, the browser-made message id that stops a retry
from charging twice, administrator model status (paused, degraded, chat
paused for maintenance) and what the model picker tells a student.
"""
from __future__ import annotations

from datetime import timedelta
from io import BytesIO
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase
from django.utils import timezone

from AI.models import (
    AccountStatus, AIModel, AIUsageLog, Conversation, Message, MessageAttachment, MessageBookmark,
    MessageRole, Notification, User,
)
from AI.services import (
    chat_service, model_status_service, organise_service, privacy_service, settings_service,
    share_service, trash_service,
)
from AI.services.ai_service import AIResponse
from AI.services.chat_service import ChatBlocked
from AI.tests.helpers import GPT, balance, fund, model_for

PASSWORD = "Sup3rSecret!x"
COMPLETE = "AI.services.ai_service.OpenAICompatibleProvider.complete"
CONFIGURED = "AI.services.ai_service.OpenAICompatibleProvider.is_configured"
OTHER = "gpt-4o"


def reply(model="gpt-5-mini", content="An answer.") -> AIResponse:
    return AIResponse(content=content, input_tokens=100, output_tokens=200, total_tokens=300,
                      model=model, request_id="req-1", latency_ms=500)


class Base(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.user = User.objects.create_user(
            username="sokha", email="sokha@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        fund(self.user, 20_000, GPT)
        self.client.force_login(self.user)
        self.chat = Conversation.objects.create(user=self.user, title="Photosynthesis")
        self.question = Message.objects.create(conversation=self.chat, role=MessageRole.USER, content="What is it?")
        self.answer = Message.objects.create(conversation=self.chat, role=MessageRole.ASSISTANT,
                                             content="Plants turning light into food.")

    def post(self, url, data=None, **extra):
        return self.client.post(url, data or {}, HTTP_X_REQUESTED_WITH="XMLHttpRequest", **extra)

    def post_json(self, url, payload):
        import json
        return self.client.post(url, json.dumps(payload), content_type="application/json",
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")


# ---------------------------------------------------------------------------
# The trash
# ---------------------------------------------------------------------------
class TrashTests(Base):
    def test_delete_moves_to_trash_and_restore_brings_it_back_untouched(self):
        before = Conversation.objects.get(pk=self.chat.pk).updated_at
        self.assertTrue(self.post(f"/api/chat/{self.chat.pk}/delete/").json()["trashed"])
        self.assertFalse(Conversation.objects.filter(pk=self.chat.pk).exists())
        self.assertEqual(trash_service.count(self.user), 1)

        self.assertTrue(self.post(f"/api/chat/{self.chat.pk}/restore/").json()["ok"])
        restored = Conversation.objects.get(pk=self.chat.pk)
        self.assertIsNone(restored.deleted_at)
        self.assertEqual(restored.updated_at, before)          # its place in the list is kept
        self.assertEqual(restored.messages.count(), 2)

    def test_deleting_twice_or_restoring_twice_is_harmless(self):
        self.post(f"/api/chat/{self.chat.pk}/delete/")
        self.assertEqual(self.post(f"/api/chat/{self.chat.pk}/delete/").status_code, 200)
        self.post(f"/api/chat/{self.chat.pk}/restore/")
        self.assertEqual(self.post(f"/api/chat/{self.chat.pk}/restore/").status_code, 200)

    def test_purge_only_works_on_trashed_chats(self):
        response = self.post(f"/api/chat/{self.chat.pk}/purge/")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Conversation.objects.filter(pk=self.chat.pk).exists())

    def test_purge_deletes_rows_and_stored_files(self):
        name = default_storage.save("uploads/chat/test/trash-photo.png", ContentFile(b"png-bytes"))
        MessageAttachment.objects.create(message=self.question, kind="image", file=name,
                                         name="photo.png", ext="png", mime="image/png", size=9)
        self.post(f"/api/chat/{self.chat.pk}/delete/")
        self.assertTrue(default_storage.exists(name), "a trashed chat keeps its files")
        self.assertTrue(self.post(f"/api/chat/{self.chat.pk}/purge/").json()["ok"])
        self.assertFalse(Conversation.all_objects.filter(pk=self.chat.pk).exists())
        self.assertFalse(default_storage.exists(name))

    def test_someone_elses_chat_cannot_be_trashed_restored_or_purged(self):
        other = User.objects.create_user(username="dara", email="d@test.local", password=PASSWORD,
                                         is_verified=True, status=AccountStatus.ACTIVE)
        theirs = Conversation.objects.create(user=other, title="Mine")
        for action in ("delete", "restore", "purge"):
            with self.subTest(action=action):
                self.assertEqual(self.post(f"/api/chat/{theirs.pk}/{action}/").status_code, 404)
        self.assertTrue(Conversation.objects.filter(pk=theirs.pk).exists())

    def test_old_trash_is_purged_by_the_scheduled_job(self):
        trash_service.trash(self.user, self.chat.pk)
        fresh = Conversation.objects.create(user=self.user, title="Fresh")
        trash_service.trash(self.user, fresh.pk)
        Conversation.all_objects.filter(pk=self.chat.pk).update(deleted_at=timezone.now() - timedelta(days=31))
        self.assertEqual(trash_service.purge_expired(), 1)
        self.assertFalse(Conversation.all_objects.filter(pk=self.chat.pk).exists())
        self.assertTrue(Conversation.all_objects.filter(pk=fresh.pk).exists())

    def test_days_left_counts_down(self):
        trash_service.trash(self.user, self.chat.pk)
        Conversation.all_objects.filter(pk=self.chat.pk).update(deleted_at=timezone.now() - timedelta(days=28))
        self.assertEqual(trash_service.items(self.user)[0]["days_left"], 2)

    def test_trash_panel_restores_purges_and_empties(self):
        trash_service.trash(self.user, self.chat.pk)
        page = self.client.get("/app/trash/", HTTP_X_APP_PANEL="1")
        self.assertContains(page, "Recently deleted")
        self.assertContains(page, "Photosynthesis")
        self.assertContains(page, "data-confirm")                # permanent steps ask first

        restored = self.client.post("/app/trash/", {"action": "restore", "conversation_id": self.chat.pk},
                                    HTTP_X_APP_PANEL="1")
        self.assertContains(restored, "data-refresh-sidebar")
        self.assertTrue(Conversation.objects.filter(pk=self.chat.pk).exists())

        trash_service.trash(self.user, self.chat.pk)
        self.client.post("/app/trash/", {"action": "purge", "conversation_id": self.chat.pk}, HTTP_X_APP_PANEL="1")
        self.assertFalse(Conversation.all_objects.filter(pk=self.chat.pk).exists())

        for title in ("A", "B"):
            trash_service.trash(self.user, Conversation.objects.create(user=self.user, title=title).pk)
        self.client.post("/app/trash/", {"action": "empty"}, HTTP_X_APP_PANEL="1")
        self.assertEqual(trash_service.count(self.user), 0)

    def test_trash_panel_ignores_junk_ids(self):
        response = self.client.post("/app/trash/", {"action": "restore", "conversation_id": "abc"},
                                    HTTP_X_APP_PANEL="1")
        self.assertEqual(response.status_code, 200)

    def test_archived_panel_delete_goes_to_trash_not_oblivion(self):
        Conversation.objects.filter(pk=self.chat.pk).update(is_archived=True)
        self.client.post("/app/archived/", {"action": "delete", "conversation_id": self.chat.pk},
                         HTTP_X_APP_PANEL="1")
        self.assertTrue(Conversation.all_objects.filter(pk=self.chat.pk, deleted_at__isnull=False).exists())

    def test_sidebar_shows_the_recently_deleted_link_with_its_count(self):
        trash_service.trash(self.user, self.chat.pk)
        page = self.client.get("/chat/")
        self.assertContains(page, 'id="trashCount">1<')
        self.assertNotContains(page, f'data-conv="{self.chat.pk}"')


class TrashedChatsDisappearTests(Base):
    def setUp(self):
        super().setUp()
        trash_service.trash(self.user, self.chat.pk)

    def test_opening_a_trashed_chat_goes_to_a_new_chat(self):
        self.assertRedirects(self.client.get(f"/chat/{self.chat.pk}/"), "/chat/", fetch_redirect_response=False)

    def test_search_does_not_find_it(self):
        data = self.client.get("/api/chat/search/?q=light", HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        self.assertEqual(data["conversations"], [])

    def test_its_saved_answers_are_hidden_until_restored(self):
        MessageBookmark.objects.create(user=self.user, message=self.answer)
        self.assertEqual(organise_service.bookmarks_for(self.user).count(), 0)
        trash_service.restore(self.user, self.chat.pk)
        self.assertEqual(organise_service.bookmarks_for(self.user).count(), 1)

    def test_a_shared_link_stops_working(self):
        trash_service.restore(self.user, self.chat.pk)
        if not share_service.sharing_enabled():
            self.skipTest("sharing is switched off")
        link = share_service.share(self.user, self.chat.pk)
        self.assertIsNotNone(share_service.open_link(link.token)[0])
        trash_service.trash(self.user, self.chat.pk)
        shared, _ = share_service.open_link(link.token)
        self.assertIsNone(shared)

    def test_sending_to_it_says_it_is_gone(self):
        with self.assertRaises(ChatBlocked) as caught:
            chat_service.get_conversation(self.user, self.chat.pk)
        self.assertEqual(caught.exception.code, "not_found")

    def test_closing_the_account_deletes_the_trash_too(self):
        privacy_service.close_account(self.user, password=PASSWORD, confirm="DELETE")
        self.assertFalse(Conversation.all_objects.filter(pk=self.chat.pk).exists())


# ---------------------------------------------------------------------------
# Safe to repeat
# ---------------------------------------------------------------------------
class IdempotentActionTests(Base):
    def test_pin_and_archive_accept_an_explicit_state(self):
        for _ in range(2):
            self.post_json(f"/api/chat/{self.chat.pk}/pin/", {"pinned": True})
        self.assertTrue(Conversation.objects.get(pk=self.chat.pk).is_pinned)
        self.post_json(f"/api/chat/{self.chat.pk}/pin/", {"pinned": False})
        self.assertFalse(Conversation.objects.get(pk=self.chat.pk).is_pinned)

        for _ in range(2):
            self.post_json(f"/api/chat/{self.chat.pk}/archive/", {"archived": True})
        self.assertTrue(Conversation.objects.get(pk=self.chat.pk).is_archived)

    def test_pin_still_toggles_without_a_value(self):
        self.post(f"/api/chat/{self.chat.pk}/pin/")
        self.assertTrue(Conversation.objects.get(pk=self.chat.pk).is_pinned)

    def test_removing_a_saved_answer_twice_leaves_it_removed(self):
        MessageBookmark.objects.create(user=self.user, message=self.answer)
        for _ in range(2):
            self.client.post("/app/saved/", {"remove": self.answer.pk}, HTTP_X_APP_PANEL="1")
        self.assertFalse(MessageBookmark.objects.filter(user=self.user).exists())

    def test_deleting_a_notification_after_mark_all_read_stays_on_the_panel(self):
        note = Notification.objects.create(user=self.user, title="Hi", body="There")
        response = self.client.post(f"/notifications/{note.pk}/delete/",
                                    {"return": "/app/notifications/read-all/"})
        self.assertRedirects(response, "/app/notifications/", fetch_redirect_response=False)

    def test_notification_return_keeps_its_own_query(self):
        note = Notification.objects.create(user=self.user, title="Hi", body="There")
        response = self.client.post(f"/notifications/{note.pk}/read/",
                                    {"return": "/app/notifications/?tab=unread&page=2"})
        self.assertRedirects(response, "/app/notifications/?tab=unread&page=2", fetch_redirect_response=False)

    def test_notification_return_refuses_other_sites(self):
        note = Notification.objects.create(user=self.user, title="Hi", body="There")
        response = self.client.post(f"/notifications/{note.pk}/delete/", {"return": "https://evil.example/app/notifications/"})
        self.assertRedirects(response, "/app/notifications/", fetch_redirect_response=False)


# ---------------------------------------------------------------------------
# Never pay twice for one question
# ---------------------------------------------------------------------------
@patch(CONFIGURED, True)
class ClientIdTests(Base):
    def send(self, client_id, message="Explain gravity"):
        return self.post("/api/chat/send/", {"message": message, "client_id": client_id,
                                             "conversation_id": self.chat.pk})

    def test_a_retry_with_the_same_id_is_not_charged_again(self):
        with patch(COMPLETE, return_value=reply()) as vendor:
            first = self.send("abc-123")
            self.assertEqual(first.status_code, 200)
            after_first = balance(self.user, GPT)
            again = self.send("abc-123")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["code"], "already_sent")
        self.assertEqual(again.json()["conversation_id"], self.chat.pk)
        self.assertEqual(vendor.call_count, 1)
        self.assertEqual(balance(self.user, GPT), after_first)

    def test_a_new_id_is_a_new_question(self):
        with patch(COMPLETE, return_value=reply()) as vendor:
            self.send("one")
            self.send("two")
        self.assertEqual(vendor.call_count, 2)

    def test_ids_are_per_student(self):
        other = User.objects.create_user(username="vanna", email="v@test.local", password=PASSWORD,
                                         is_verified=True, status=AccountStatus.ACTIVE)
        chat = Conversation.objects.create(user=other, title="x")
        Message.objects.create(conversation=chat, role=MessageRole.USER, content="q", client_id="shared-id")
        self.assertIsNone(chat_service.find_by_client_id(self.user, "shared-id"))


# ---------------------------------------------------------------------------
# Model status
# ---------------------------------------------------------------------------
@patch(CONFIGURED, True)
class ModelStatusTests(Base):
    def test_a_paused_model_switches_to_one_that_works(self):
        fund(self.user, 20_000, OTHER)
        model_status_service.set_status(model_for(GPT), "paused", "Provider outage")
        with patch(COMPLETE, return_value=reply(OTHER)):
            data = chat_service.send_message(self.user, self.chat, "Hello", model_key=GPT)
        self.assertEqual(data["switched_from"], GPT)
        self.assertEqual(data["switched_to"], OTHER)
        self.assertFalse(AIUsageLog.objects.filter(user=self.user, model_key=GPT).exists())

    def test_a_paused_model_with_nowhere_to_go_explains_itself(self):
        settings_service.set_value("auto_switch_model", False)
        try:
            model_status_service.set_status(model_for(GPT), "paused", "Back at 9 pm")
            with patch(COMPLETE, return_value=reply()) as vendor:
                response = self.post("/api/chat/send/", {"message": "Hi", "model": GPT})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["code"], "model_paused")
            self.assertIn("Back at 9 pm", response.json()["error"])
            vendor.assert_not_called()
        finally:
            settings_service.set_value("auto_switch_model", True)

    def test_maintenance_stops_every_model(self):
        fund(self.user, 20_000, OTHER)
        settings_service.set_value("chat_paused", True)
        settings_service.set_value("chat_paused_note", "Upgrading, back soon")
        try:
            with patch(COMPLETE, return_value=reply()) as vendor:
                response = self.post("/api/chat/send/", {"message": "Hi", "model": GPT})
            self.assertEqual(response.json()["code"], "maintenance")
            self.assertIn("Upgrading", response.json()["error"])
            vendor.assert_not_called()
            page = self.client.get("/chat/")
            self.assertContains(page, "data-maintenance-title")
        finally:
            settings_service.set_value("chat_paused", False)

    def test_degraded_still_sends_but_warns_in_the_picker(self):
        model_status_service.set_status(model_for(GPT), "degraded", "Answers are slow")
        with patch(COMPLETE, return_value=reply()):
            chat_service.send_message(self.user, self.chat, "Hello", model_key=GPT)
        page = self.client.get("/chat/").content.decode()
        self.assertIn('data-notice-level="degraded"', page)
        self.assertIn("Answers are slow", page)
        self.assertIn("Slow now", page)


class ModelPickerTests(Base):
    def test_picker_shows_best_for_badges_and_typical_cost(self):
        AIModel.objects.filter(key=GPT).update(best_for="Everyday questions and homework help")
        for _ in range(6):
            AIUsageLog.objects.create(user=self.user, model=GPT, model_key=GPT, success=True,
                                      total_tokens=900, tokens_charged=900)
        from django.core.cache import cache
        from AI.views.chat import TYPICAL_CACHE_KEY
        cache.delete(TYPICAL_CACHE_KEY)
        page = self.client.get("/chat/").content.decode()
        self.assertIn("Everyday questions and homework help", page)
        self.assertIn("≈ 900 per answer", page)
        self.assertIn("Reads images", page)
        self.assertIn("Compare models", page)

    def test_typical_cost_needs_enough_answers(self):
        AIUsageLog.objects.create(user=self.user, model=GPT, model_key=GPT, success=True, total_tokens=5, tokens_charged=5)
        from django.core.cache import cache
        from AI.views.chat import TYPICAL_CACHE_KEY, typical_answer_tokens
        cache.delete(TYPICAL_CACHE_KEY)
        self.assertNotIn(GPT, typical_answer_tokens())

    def test_chat_header_offers_the_chat_menu_for_the_open_chat(self):
        page = self.client.get(f"/chat/{self.chat.pk}/").content.decode()
        self.assertGreaterEqual(page.count(f'data-conv-menu="{self.chat.pk}"'), 3)   # sidebar, header, phone bar


# ---------------------------------------------------------------------------
# Branching and finding things
# ---------------------------------------------------------------------------
class BranchTests(Base):
    def test_branch_copies_up_to_the_answer_and_leaves_the_original(self):
        later = Message.objects.create(conversation=self.chat, role=MessageRole.USER, content="Later question")
        response = self.post(f"/api/chat/{self.chat.pk}/messages/{self.answer.pk}/branch/")
        data = response.json()
        self.assertTrue(data["ok"])
        branch = Conversation.objects.get(pk=data["conversation"]["id"])
        self.assertEqual(list(branch.messages.values_list("content", flat=True)),
                         ["What is it?", "Plants turning light into food."])
        self.assertIn("(branch)", branch.title)
        self.assertEqual(branch.message_count, 2)
        self.assertTrue(Message.objects.filter(pk=later.pk, conversation=self.chat).exists())
        self.assertEqual(data["url"], f"/chat/{branch.pk}/")

    def test_branch_never_shares_files_with_the_original(self):
        name = default_storage.save("uploads/chat/test/branch.png", ContentFile(b"png"))
        MessageAttachment.objects.create(message=self.question, kind="image", file=name, name="p.png", ext="png", size=3)
        data = self.post(f"/api/chat/{self.chat.pk}/messages/{self.answer.pk}/branch/").json()
        branch = Conversation.objects.get(pk=data["conversation"]["id"])
        self.assertFalse(MessageAttachment.objects.filter(message__conversation=branch).exists())
        self.assertIn("not copied", branch.messages.first().content)
        default_storage.delete(name)

    def test_branch_of_someone_elses_chat_is_refused(self):
        other = User.objects.create_user(username="kosal", email="k@test.local", password=PASSWORD,
                                         is_verified=True, status=AccountStatus.ACTIVE)
        theirs = Conversation.objects.create(user=other, title="x")
        msg = Message.objects.create(conversation=theirs, role=MessageRole.ASSISTANT, content="secret")
        self.assertEqual(self.post(f"/api/chat/{theirs.pk}/messages/{msg.pk}/branch/").status_code, 404)


class SearchFilterTests(Base):
    def search(self, query, **params):
        from urllib.parse import urlencode
        url = "/api/chat/search/?" + urlencode({"q": query, **params})
        return self.client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()

    def test_archived_chats_only_with_the_filter(self):
        Conversation.objects.filter(pk=self.chat.pk).update(is_archived=True)
        self.assertEqual(self.search("light")["conversations"], [])
        found = self.search("light", archived="1")["conversations"]
        self.assertEqual([c["id"] for c in found], [self.chat.pk])
        self.assertTrue(found[0]["is_archived"])

    def test_time_filter(self):
        Conversation.objects.filter(pk=self.chat.pk).update(updated_at=timezone.now() - timedelta(days=20))
        Message.objects.filter(conversation=self.chat).update(created_at=timezone.now() - timedelta(days=20))
        self.assertEqual(self.search("light", days="7")["conversations"], [])
        self.assertEqual(len(self.search("light", days="30")["conversations"]), 1)
        self.assertEqual(len(self.search("light", days="nonsense")["conversations"]), 1)

    def test_saved_answers_are_searched_by_note(self):
        MessageBookmark.objects.create(user=self.user, message=self.answer, note="biology exam")
        saved = self.search("biology")["saved"]
        self.assertEqual([s["message_id"] for s in saved], [self.answer.pk])
