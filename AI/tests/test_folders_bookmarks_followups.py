"""Chat folders, saved answers and suggested follow-up questions.

What a student would notice if these broke: a folder that files somebody
else's chat, a deleted folder that takes its chats with it, folder
instructions that never reach the model; a bookmark on another student's
answer; follow-up text leaking into the answer they copy or share, or
suggestions that cost a second request.
"""
from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from AI.models import (
    AccountStatus,
    ChatFolder,
    Conversation,
    Message,
    MessageBookmark,
    MessageRole,
    User,
)
from AI.services import chat_service, followup_service, organise_service, settings_service, wallet_service
from AI.services.providers.base import AIResponse
from AI.tests.helpers import fund, model_for

PASSWORD = "Sup3rSecret!x"
GPT = "gpt-5-mini"
COMPLETE = "AI.services.providers.openai_provider.OpenAICompatibleProvider.complete"
WITH_BLOCK = (
    "Photosynthesis turns light into chemical energy.\n\n"
    "[[FOLLOWUPS]]\n- What is chlorophyll?\n- Where does it happen?\n- Why do leaves change colour?"
)


def reply(content="Answer.", inp=100, out=200):
    return AIResponse(content=content, input_tokens=inp, output_tokens=out, total_tokens=inp + out,
                      model=GPT, request_id="r", latency_ms=10)


def student(name):
    return User.objects.create_user(
        username=name, email=f"{name}@test.local", password=PASSWORD,
        is_verified=True, status=AccountStatus.ACTIVE,
    )


# ---------------------------------------------------------------------------
# Follow-ups: the parser
# ---------------------------------------------------------------------------
class SplitTests(SimpleTestCase):

    def test_a_clean_block(self):
        answer, suggestions = followup_service.split(WITH_BLOCK)
        self.assertEqual(answer, "Photosynthesis turns light into chemical energy.")
        self.assertEqual(len(suggestions), 3)
        self.assertEqual(suggestions[0], "What is chlorophyll?")

    def test_the_ways_models_actually_write_it(self):
        variants = [
            "A.\n\n**[[FOLLOWUPS]]**\n1. One?\n2. Two?\n3. Three?",
            "A.\n\n---\n### [[Follow-ups]]:\n* One?\n* Two?\n* Three?",
            "A.\n`[[followups]]`\n• One?\n• Two?\n• Three?",
        ]
        for text in variants:
            with self.subTest(text=text):
                answer, suggestions = followup_service.split(text)
                self.assertEqual(answer, "A.")
                self.assertEqual(suggestions, ["One?", "Two?", "Three?"])

    def test_no_block_keeps_the_whole_answer(self):
        self.assertEqual(followup_service.split("Just an answer."), ("Just an answer.", []))

    def test_a_block_with_no_answer_is_not_mistaken_for_one(self):
        answer, suggestions = followup_service.split("[[FOLLOWUPS]]\n- Only this")
        self.assertIn("Only this", answer)
        self.assertEqual(suggestions, [])

    def test_at_most_three_distinct_and_short(self):
        text = "A.\n[[FOLLOWUPS]]\n- Same?\n- same?\n- " + "x" * 400 + "\n- B?\n- C?"
        _, suggestions = followup_service.split(text)
        self.assertEqual(len(suggestions), 3)
        self.assertEqual(suggestions[0], "Same?")
        self.assertLessEqual(len(suggestions[1]), followup_service.MAX_LENGTH)

    def test_the_marker_text_in_the_middle_of_a_sentence_is_ignored(self):
        text = "Write [[FOLLOWUPS]] in your notes to mark questions."
        self.assertEqual(followup_service.split(text), (text, []))


# ---------------------------------------------------------------------------
# Follow-ups: in a real exchange
# ---------------------------------------------------------------------------
class Fixture(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.ann = student("ann")
        fund(self.ann, 50_000)
        self.conversation = chat_service.create_conversation(self.ann)
        self.client.force_login(self.ann)

    def send(self, text="Explain photosynthesis", content=WITH_BLOCK, user=None, conversation=None):
        with patch(COMPLETE, return_value=reply(content)) as vendor:
            data = chat_service.send_message(user or self.ann, conversation or self.conversation, text)
        return data, vendor


class FollowupExchangeTests(Fixture):

    def test_the_block_never_reaches_the_stored_answer(self):
        data, _ = self.send()
        message = Message.objects.filter(role=MessageRole.ASSISTANT).latest("id")
        self.assertNotIn("FOLLOWUPS", message.content)
        self.assertNotIn("chlorophyll", message.content)
        self.assertEqual(len(message.suggestions), 3)
        self.assertEqual(data["assistant_message"]["suggestions"], message.suggestions)
        self.assertNotIn("FOLLOWUPS", data["assistant_message"]["content"])

    def test_the_model_is_asked_for_them_in_the_same_request(self):
        _, vendor = self.send()
        self.assertEqual(vendor.call_count, 1, "no second request for suggestions")
        self.assertIn(followup_service.MARKER, vendor.call_args.kwargs["system_prompt"])

    def test_the_tokens_are_billed_as_the_vendor_reported(self):
        before = wallet_service.balance_of(self.ann, model_for(GPT))
        self.send()
        self.assertEqual(before - wallet_service.balance_of(self.ann, model_for(GPT)), 300)

    def test_switched_off_by_an_administrator(self):
        settings_service.set_value("ai_followups_enabled", False)
        data, vendor = self.send()
        self.assertNotIn(followup_service.MARKER, vendor.call_args.kwargs["system_prompt"])
        self.assertEqual(data["assistant_message"]["suggestions"], [])
        stored = Message.objects.filter(role=MessageRole.ASSISTANT).latest("id")
        self.assertNotIn("FOLLOWUPS", stored.content, "a stray block is still removed")

    def test_only_the_newest_answer_shows_them(self):
        self.send(text="First")
        self.send(text="Second", content="Second answer.\n\n[[FOLLOWUPS]]\n- Newest one?")
        body = self.client.get(f"/chat/{self.conversation.pk}/").content.decode()
        self.assertIn("Newest one?", body)
        self.assertNotIn("What is chlorophyll?", body)
        self.assertEqual(body.count("data-followup>"), 1)

    def test_regenerate_gets_fresh_suggestions(self):
        self.send()
        data = None
        with patch(COMPLETE, return_value=reply("Again.\n[[FOLLOWUPS]]\n- Fresh?")):
            data = chat_service.regenerate(self.ann, self.conversation)
        self.assertEqual(data["assistant_message"]["suggestions"], ["Fresh?"])


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------
class FolderTests(Fixture):

    def post(self, url, **data):
        return self.client.post(url, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def make(self, name="Biology", **extra):
        return self.post("/api/folders/", name=name, colour="emerald", **extra).json()["folder"]

    def test_create_list_update(self):
        folder = self.make(instructions="Use simple words.")
        self.assertEqual((folder["name"], folder["colour"]), ("Biology", "emerald"))
        listed = self.client.get("/api/folders/", HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["folders"]
        self.assertEqual([f["name"] for f in listed], ["Biology"])
        updated = self.post(f"/api/folders/{folder['id']}/", action="update", name="Bio 101", colour="rose").json()
        self.assertEqual(updated["folder"]["name"], "Bio 101")
        self.assertEqual(updated["folder"]["colour"], "rose")

    def test_names_are_required_and_unique_per_student(self):
        self.assertEqual(self.post("/api/folders/", name="  ").status_code, 400)
        self.make("Maths")
        self.assertEqual(self.post("/api/folders/", name="maths").status_code, 400)
        bob = student("bob")
        organise_service.create_folder(bob, name="Maths")      # another student may reuse it

    def test_an_unknown_colour_falls_back(self):
        folder = self.post("/api/folders/", name="X", colour="<script>").json()["folder"]
        self.assertEqual(folder["colour"], "indigo")

    def test_moving_a_chat_in_and_out(self):
        folder = self.make()
        response = self.post(f"/api/chat/{self.conversation.pk}/folder/", folder_id=folder["id"])
        self.assertEqual(response.json()["conversation"]["folder_id"], folder["id"])
        self.post(f"/api/chat/{self.conversation.pk}/folder/", folder_id="")
        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.folder_id)

    def test_moving_does_not_bump_the_chat_to_today(self):
        folder = self.make()
        stamp = Conversation.objects.get(pk=self.conversation.pk).updated_at
        self.post(f"/api/chat/{self.conversation.pk}/folder/", folder_id=folder["id"])
        self.assertEqual(Conversation.objects.get(pk=self.conversation.pk).updated_at, stamp)

    def test_deleting_a_folder_keeps_its_chats(self):
        folder = self.make()
        self.post(f"/api/chat/{self.conversation.pk}/folder/", folder_id=folder["id"])
        data = self.post(f"/api/folders/{folder['id']}/", action="delete").json()
        self.assertEqual(data["unfiled"], 1)
        self.assertTrue(Conversation.objects.filter(pk=self.conversation.pk, folder__isnull=True).exists())

    def test_nobody_can_touch_another_students_folders_or_chats(self):
        mine = self.make()
        bob = student("bob")
        bobs_chat = chat_service.create_conversation(bob)
        bobs_folder = organise_service.create_folder(bob, name="Bob's")
        self.assertEqual(self.post(f"/api/chat/{bobs_chat.pk}/folder/", folder_id=mine["id"]).status_code, 404)
        self.assertEqual(self.post(f"/api/chat/{self.conversation.pk}/folder/", folder_id=bobs_folder.pk).status_code, 404)
        self.assertEqual(self.post(f"/api/folders/{bobs_folder.pk}/", action="delete").status_code, 404)
        self.assertTrue(ChatFolder.objects.filter(pk=bobs_folder.pk).exists())

    def test_a_chat_started_in_a_folder_is_filed_there(self):
        folder = self.make()
        with patch(COMPLETE, return_value=reply()):
            data = self.client.post("/api/chat/send/", {"message": "Hi", "folder_id": folder["id"]},
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        self.assertEqual(Conversation.objects.get(pk=data["conversation_id"]).folder_id, folder["id"])

    def test_someone_elses_folder_id_leaves_the_new_chat_unfiled(self):
        bobs_folder = organise_service.create_folder(student("bob"), name="Bob's")
        with patch(COMPLETE, return_value=reply()):
            data = self.client.post("/api/chat/send/", {"message": "Hi", "folder_id": bobs_folder.pk},
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        self.assertIsNone(Conversation.objects.get(pk=data["conversation_id"]).folder_id)

    def test_folder_instructions_reach_the_model(self):
        folder = organise_service.create_folder(self.ann, name="Bio", instructions="Answer like a biology tutor.")
        organise_service.move_conversation(self.ann, self.conversation.pk, folder.pk)
        _, vendor = self.send(content="ok")
        prompt = vendor.call_args.kwargs["system_prompt"]
        self.assertIn("Answer like a biology tutor.", prompt)
        base = settings_service.get("ai_system_prompt", settings_service.DEFAULT_SYSTEM_PROMPT)
        self.assertTrue(prompt.startswith(base))

    def test_the_sidebar_shows_folders_and_tags_chats(self):
        folder = organise_service.create_folder(self.ann, name="Chemistry")
        organise_service.move_conversation(self.ann, self.conversation.pk, folder.pk)
        self.send(content="ok")
        body = self.client.get("/chat/").content.decode()
        self.assertIn("Chemistry", body)
        self.assertIn(f'data-folder-row="{folder.pk}"', body)
        self.assertIn(f'data-folder="{folder.pk}"', body)


# ---------------------------------------------------------------------------
# Saved answers
# ---------------------------------------------------------------------------
class BookmarkTests(Fixture):

    def setUp(self):
        super().setUp()
        self.send()
        self.answer = Message.objects.filter(role=MessageRole.ASSISTANT).latest("id")
        self.question = Message.objects.filter(role=MessageRole.USER).latest("id")

    def toggle(self, message_id):
        return self.client.post(f"/api/chat/messages/{message_id}/bookmark/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_save_and_unsave(self):
        self.assertTrue(self.toggle(self.answer.pk).json()["saved"])
        self.assertTrue(MessageBookmark.objects.filter(user=self.ann, message=self.answer).exists())
        self.assertFalse(self.toggle(self.answer.pk).json()["saved"])
        self.assertFalse(MessageBookmark.objects.exists())

    def test_only_your_own_answers(self):
        self.assertEqual(self.toggle(self.question.pk).status_code, 404)
        self.client.force_login(student("bob"))
        self.assertEqual(self.toggle(self.answer.pk).status_code, 404)

    def test_the_saved_panel_lists_searches_and_removes(self):
        self.toggle(self.answer.pk)
        body = self.client.get("/app/saved/").content.decode()
        self.assertIn("Photosynthesis", body)
        self.assertIn(f"#message-{self.answer.pk}", body)
        self.assertIn("Photosynthesis", self.client.get("/app/saved/?q=photo").content.decode())
        self.assertIn("No saved answers mention", self.client.get("/app/saved/?q=volcano").content.decode())
        self.client.post("/app/saved/", {"remove": self.answer.pk})
        self.assertFalse(MessageBookmark.objects.exists())

    def test_a_saved_answer_shows_as_saved_in_the_chat(self):
        self.toggle(self.answer.pk)
        body = self.client.get(f"/chat/{self.conversation.pk}/").content.decode()
        self.assertIn(f'data-save="{self.answer.pk}"\n                            aria-pressed="true"', body)

    def test_deleting_the_chat_removes_the_bookmark(self):
        self.toggle(self.answer.pk)
        self.conversation.delete()
        self.assertFalse(MessageBookmark.objects.exists())


class ShellTests(Fixture):

    def test_the_conversation_menu_is_wired(self):
        """Its buttons said data-act while the handler read data-action."""
        body = self.client.get("/chat/").content.decode()
        self.assertNotIn('data-act="', body)
        for action in ("rename", "pin", "folder", "archive", "delete"):
            with self.subTest(action=action):
                self.assertIn(f'data-action="{action}"', body)

    def test_the_new_pieces_are_on_the_page(self):
        body = self.client.get("/chat/").content.decode()
        for needle in ('id="folderAdd"', 'id="folderDialog"', 'id="moveDialog"', 'data-panel="saved"',
                       "function showFollowups", "function applyListFilter", "function revealLinkedMessage"):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)
