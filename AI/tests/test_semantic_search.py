"""Search by meaning: indexing answers as vectors and finding them again.

The embeddings endpoint is replaced by a deterministic fake. It maps words
that mean the same thing ("sunlight", "chlorophyll", "photosynthesis") to one
shared bucket and every other word to its own hashed bucket, so a query can
find an answer that shares none of its words — which is the point of the
feature, and exactly what the keyword search cannot do.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import numpy as np
import requests
from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from AI.models import (
    AccountStatus, Conversation, Message, MessageEmbedding, MessageRole, MessageStatus, User,
)
from AI.services import embedding_service, health_service, settings_service, trash_service
from AI.services.embedding_service import EmbeddingError

PASSWORD = "Sup3rSecret!x"
KEY = "sk-test-embeddings-0123456789abcdef"
POST = "AI.services.embedding_service.requests.post"
URL = "/api/chat/search/meaning/"
DIMS = 256

# ---------------------------------------------------------------------------
# The fake embedder
# ---------------------------------------------------------------------------
CONCEPTS = {"PHOTO": 0, "PLANT": 1, "WAR": 2, "HISTORY": 3, "MATH": 4}
SYNONYMS = {
    **dict.fromkeys(
        ["photosynthesis", "sunlight", "light", "chlorophyll", "sugar", "glucose", "food", "energy"], "PHOTO"
    ),
    **dict.fromkeys(["plant", "plants", "leaf", "leaves", "tree", "trees", "green"], "PLANT"),
    **dict.fromkeys(["war", "battle", "conflict", "soldiers", "archduke", "assassination", "ww1"], "WAR"),
    **dict.fromkeys(["history", "historical", "century", "1914", "empire"], "HISTORY"),
    **dict.fromkeys(["equation", "quadratic", "algebra", "formula", "roots", "maths"], "MATH"),
}
STOP = {
    "the", "a", "an", "in", "of", "to", "and", "or", "is", "are", "how", "do", "does", "what", "why",
    "into", "it", "they", "their", "them", "use", "uses", "make", "makes", "q", "chat", "turn", "by",
    "for", "on", "with", "that", "this", "about", "can", "you", "me", "i", "was", "were", "be", "from",
    "at", "as", "its", "which", "when", "who", "set", "off",
}
TOKEN = re.compile(r"[a-z0-9]+")


def fake_vector(text: str, dims: int) -> list[float]:
    vector = np.zeros(dims, dtype=np.float32)
    for word in TOKEN.findall(text.lower()):
        if word in STOP:
            continue
        concept = SYNONYMS.get(word)
        if concept:
            vector[CONCEPTS[concept]] += 1.0
        else:
            vector[16 + int(hashlib.md5(word.encode()).hexdigest(), 16) % (dims - 16)] += 1.0
    # Deliberately not unit length: normalising is the service's job.
    return (vector * 3.0).tolist()


class FakeResponse:
    def __init__(self, status_code: int = 200, body=None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


class FakeEmbedder:
    """Stands in for ``requests.post`` against an OpenAI-compatible /embeddings."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, url, headers=None, data=None, timeout=None, **kwargs):
        payload = json.loads(data)
        self.calls.append({"url": url, "headers": headers or {}, "payload": payload, "timeout": timeout})
        rows = [
            {"object": "embedding", "index": index, "embedding": fake_vector(text, payload["dimensions"])}
            for index, text in enumerate(payload["input"])
        ]
        rows.reverse()  # the service must put them back in order by "index"
        return FakeResponse(200, {"object": "list", "data": rows, "model": payload["model"]})

    @property
    def inputs(self) -> list[str]:
        return [text for call in self.calls for text in call["payload"]["input"]]


LEAVES = ("How do leaves turn sunlight into food?",
          "Leaves capture light with chlorophyll and make sugar from water and carbon dioxide.")
LEAVES_MORE = ("Why are leaves green?", "Chlorophyll absorbs red and blue light and reflects green.")
DESERT = ("Which plants grow in deserts?", "Cacti and other plants store water in their thick green stems.")
WAR = ("What started World War One?",
       "The assassination of Archduke Franz Ferdinand in 1914 set off the war.")
QUERY = "photosynthesis in plants"


@override_settings(
    EMBEDDING_API_KEY=KEY,
    EMBEDDING_BASE_URL="https://embeddings.test/v1",
    EMBEDDING_MODEL="text-embedding-3-small",
    EMBEDDING_DIMENSIONS=DIMS,
    EMBEDDING_TIMEOUT=20,
)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        settings_service.bootstrap()
        self.user = self.make_user("sokha")
        self.client.force_login(self.user)
        self.fake = FakeEmbedder()
        patcher = patch(POST, side_effect=self.fake)
        self.post_mock = patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def make_user(username: str) -> User:
        return User.objects.create_user(
            username=username, email=f"{username}@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )

    @staticmethod
    def chat(user, title, *exchanges, archived=False):
        conversation = Conversation.objects.create(user=user, title=title, is_archived=archived)
        answers = []
        for question, answer in exchanges:
            Message.objects.create(conversation=conversation, role=MessageRole.USER, content=question)
            answers.append(Message.objects.create(
                conversation=conversation, role=MessageRole.ASSISTANT, content=answer,
            ))
        return conversation, answers

    def search(self, q=QUERY, **params):
        response = self.client.get(URL, {"q": q, **params}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        return response, response.json()


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------
class IndexingTests(Base):
    def test_only_answers_are_indexed_each_with_its_question_and_the_chat_title(self):
        chat, answers = self.chat(self.user, "Leaves", LEAVES, LEAVES_MORE)

        self.assertEqual(embedding_service.index_pending(self.user), 2)

        rows = MessageEmbedding.objects.filter(conversation=chat)
        self.assertEqual({row.message_id for row in rows}, {a.pk for a in answers})
        self.assertTrue(all(row.message.role == MessageRole.ASSISTANT for row in rows))
        first = rows.get(message=answers[0])
        self.assertEqual((first.user_id, first.model_name, first.dimensions),
                         (self.user.pk, "text-embedding-3-small", DIMS))

        text = next(t for t in self.fake.inputs if "sugar" in t)
        self.assertEqual(text, "Chat: Leaves\nQ: " + LEAVES[0] + "\nA: " + LEAVES[1])
        self.assertEqual(first.text_hash, hashlib.sha256(text.encode()).hexdigest())
        # The follow-up answer carries its own question, not the first one.
        self.assertTrue(any(t.startswith("Chat: Leaves\nQ: Why are leaves green?") for t in self.fake.inputs))

        vector = np.frombuffer(bytes(first.vector), dtype="<f4")
        self.assertEqual(vector.shape, (DIMS,))
        self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=5)

    def test_the_request_is_openai_shaped_and_authenticated(self):
        self.chat(self.user, "Leaves", LEAVES)
        embedding_service.index_pending(self.user)

        call = self.fake.calls[0]
        self.assertEqual(call["url"], "https://embeddings.test/v1/embeddings")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertEqual(call["payload"]["model"], "text-embedding-3-small")
        self.assertEqual(call["payload"]["dimensions"], DIMS)
        self.assertEqual(call["timeout"], 20)

    def test_long_answers_are_trimmed_and_requests_are_batched_by_64(self):
        conversation = Conversation.objects.create(user=self.user, title="Essays")
        for n in range(70):
            Message.objects.create(conversation=conversation, role=MessageRole.USER, content=f"Question {n}")
            Message.objects.create(conversation=conversation, role=MessageRole.ASSISTANT, content="word " * 3000)

        self.assertEqual(embedding_service.index_pending(self.user, limit=100), 70)
        self.assertEqual([len(c["payload"]["input"]) for c in self.fake.calls], [64, 6])
        self.assertTrue(all(len(text) <= 2000 for text in self.fake.inputs))

    def test_trashed_failed_and_empty_answers_are_skipped(self):
        trashed, _ = self.chat(self.user, "Old", WAR)
        trash_service.trash(self.user, trashed.pk)
        live, _ = self.chat(self.user, "Leaves", LEAVES)
        Message.objects.create(conversation=live, role=MessageRole.ASSISTANT, content="half an ans",
                               status=MessageStatus.FAILED)
        Message.objects.create(conversation=live, role=MessageRole.ASSISTANT, content="   ")

        self.assertEqual(embedding_service.index_pending(self.user), 1)
        self.assertFalse(MessageEmbedding.objects.filter(conversation=trashed).exists())
        self.assertEqual(embedding_service.pending_count(self.user), 0)

    def test_a_second_run_does_nothing(self):
        self.chat(self.user, "Leaves", LEAVES)
        embedding_service.index_pending(self.user)
        calls = len(self.fake.calls)
        self.assertEqual(embedding_service.index_pending(self.user), 0)
        self.assertEqual(len(self.fake.calls), calls)

    def test_an_edited_answer_is_embedded_again(self):
        chat, (answer,) = self.chat(self.user, "Leaves", LEAVES)
        embedding_service.index_pending(self.user)
        before = MessageEmbedding.objects.get(message=answer)

        Message.objects.filter(pk=answer.pk).update(content="Rivers carry dissolved minerals into the sea.")
        chat.touch()  # as every edit and regenerate does

        self.assertEqual(embedding_service.index_pending(self.user), 1)
        after = MessageEmbedding.objects.get(message=answer)
        self.assertNotEqual(after.text_hash, before.text_hash)
        self.assertNotEqual(bytes(after.vector), bytes(before.vector))
        self.assertIn("Rivers carry dissolved minerals", self.fake.inputs[-1])
        self.assertEqual(embedding_service.index_pending(self.user), 0)

    def test_renaming_a_chat_re_embeds_its_answers(self):
        chat, _ = self.chat(self.user, "Leaves", LEAVES, LEAVES_MORE)
        embedding_service.index_pending(self.user)
        self.fake.calls.clear()

        response = self.client.post(
            f"/api/chat/{chat.pk}/rename/", json.dumps({"title": "Biology homework"}),
            content_type="application/json", HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)

        self.assertEqual(embedding_service.index_pending(self.user), 2)
        self.assertTrue(all(text.startswith("Chat: Biology homework\n") for text in self.fake.inputs))

    def test_a_different_model_or_size_means_re_embedding(self):
        self.chat(self.user, "Leaves", LEAVES, LEAVES_MORE)
        embedding_service.index_pending(self.user)

        with self.settings(EMBEDDING_MODEL="text-embedding-3-large", EMBEDDING_DIMENSIONS=512):
            self.assertEqual(embedding_service.pending_count(self.user), 2)
            self.assertEqual(embedding_service.index_pending(self.user), 2)
            self.assertEqual(self.fake.calls[-1]["payload"]["dimensions"], 512)
            self.assertEqual(
                set(MessageEmbedding.objects.values_list("model_name", "dimensions")),
                {("text-embedding-3-large", 512)},
            )
            row = MessageEmbedding.objects.first()
            self.assertEqual(len(bytes(row.vector)), 512 * 4)

    def test_indexing_never_raises_on_a_provider_failure(self):
        self.chat(self.user, "Leaves", LEAVES)
        self.post_mock.side_effect = requests.ConnectionError("no route to host")
        with self.assertLogs("AI", level="WARNING"):
            self.assertEqual(embedding_service.index_pending(self.user), 0)
        self.assertFalse(MessageEmbedding.objects.exists())

    def test_indexing_one_student_leaves_the_others_alone(self):
        other = self.make_user("dara")
        self.chat(other, "Leaves", LEAVES)
        self.chat(self.user, "War", WAR)
        embedding_service.index_pending(self.user)
        self.assertEqual(set(MessageEmbedding.objects.values_list("user_id", flat=True)), {self.user.pk})

    def test_switched_off_means_no_background_indexing(self):
        self.chat(self.user, "Leaves", LEAVES)
        settings_service.set_value("semantic_search_enabled", False)
        self.assertEqual(embedding_service.index_pending(self.user), 0)
        self.assertEqual(self.fake.calls, [])


class EmbedTextsTests(Base):
    def test_vectors_come_back_in_input_order_and_unit_length(self):
        vectors = embedding_service.embed_texts(["photosynthesis", "world war"])
        self.assertEqual(len(vectors), 2)
        self.assertGreater(vectors[0][CONCEPTS["PHOTO"]], 0.9)
        self.assertGreater(vectors[1][CONCEPTS["WAR"]], 0.5)
        for vector in vectors:
            self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=5)

    def _fails_with(self, response_or_exc) -> EmbeddingError:
        if isinstance(response_or_exc, Exception):
            self.post_mock.side_effect = response_or_exc
        else:
            self.post_mock.side_effect = None
            self.post_mock.return_value = response_or_exc
        with self.assertRaises(EmbeddingError) as ctx:
            embedding_service.embed_texts(["anything"])
        return ctx.exception

    def test_failures_become_clear_errors(self):
        cases = [
            (requests.Timeout("slow"), "timeout"),
            (requests.ConnectionError("down"), "network"),
            (FakeResponse(401, {"error": {"message": f"Incorrect API key provided: {KEY}"}}), "auth"),
            (FakeResponse(429, {"error": {"message": "Rate limit reached"}}), "rate_limited"),
            (FakeResponse(503, None, "upstream"), "provider_down"),
            (FakeResponse(400, {"error": {"message": "bad input"}}), "bad_request"),
            (FakeResponse(200, None, "<html>"), "bad_response"),
            (FakeResponse(200, {"data": []}), "bad_response"),
            (FakeResponse(200, {"data": [{"index": 0, "embedding": "nope"}]}), "bad_response"),
            (FakeResponse(200, {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}), "bad_response"),
        ]
        for response, code in cases:
            with self.subTest(code=code):
                error = self._fails_with(response)
                self.assertEqual(error.code, code)
                self.assertNotIn(KEY, str(error))
                self.assertNotIn(KEY, error.friendly_message)

    def test_a_longer_vector_is_truncated_to_the_configured_size(self):
        self.post_mock.side_effect = None
        self.post_mock.return_value = FakeResponse(200, {"data": [{"index": 0, "embedding": [1.0] * 1536}]})
        (vector,) = embedding_service.embed_texts(["anything"])
        self.assertEqual(vector.shape, (DIMS,))
        self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=5)

    @override_settings(EMBEDDING_API_KEY="")
    def test_no_key_no_request(self):
        with self.assertRaises(EmbeddingError) as ctx:
            embedding_service.embed_texts(["anything"])
        self.assertEqual(ctx.exception.code, "not_configured")
        self.assertEqual(self.fake.calls, [])

    def test_excerpt_is_plain_text_centred_on_a_query_word(self):
        answer = "## Overview\n" + "Background detail. " * 20 + "**Chlorophyll** is the pigment. " + "More. " * 30
        snippet = embedding_service.excerpt(answer, "chlorophyll pigments")
        self.assertIn("Chlorophyll is the pigment", snippet)
        self.assertTrue(snippet.startswith("…") and snippet.endswith("…"))
        self.assertNotIn("**", snippet)
        self.assertLessEqual(len(snippet), 2 * embedding_service.SNIPPET_WIDTH + 20)
        # No shared word: the opening, as plain text.
        self.assertTrue(embedding_service.excerpt(answer, "unrelated").startswith("Overview Background"))


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------
class SearchTests(Base):
    def test_finds_an_answer_by_meaning_that_shares_no_words_with_the_query(self):
        chat, (answer,) = self.chat(self.user, "Leaves", LEAVES)
        self.chat(self.user, "WW1", WAR)

        # The keyword search has nothing: no message says "photosynthesis".
        keyword = self.client.get("/api/chat/search/", {"q": "photosynthesis"},
                                  HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        self.assertEqual(keyword["conversations"], [])

        response, data = self.search()
        self.assertEqual(response.status_code, 200)
        self.assertEqual((data["ok"], data["enabled"], data["reason"]), (True, True, ""))
        self.assertEqual(len(data["results"]), 1)
        hit = data["results"][0]
        self.assertEqual(
            set(hit), {"conversation_id", "message_id", "title", "snippet", "score", "is_archived", "created_at"},
        )
        self.assertEqual((hit["conversation_id"], hit["message_id"], hit["title"]), (chat.pk, answer.pk, "Leaves"))
        self.assertIn("chlorophyll", hit["snippet"])
        self.assertGreater(hit["score"], 0.8)
        self.assertFalse(hit["is_archived"])
        self.assertTrue(hit["created_at"])

    def test_best_chat_first_one_result_per_chat_and_noise_dropped(self):
        leaves, (first, second) = self.chat(self.user, "Leaves", LEAVES, LEAVES_MORE)
        desert, _ = self.chat(self.user, "Deserts", DESERT)
        self.chat(self.user, "WW1", WAR)

        results = embedding_service.search(self.user, QUERY)

        self.assertEqual([r["conversation_id"] for r in results], [leaves.pk, desert.pk])
        self.assertIn(results[0]["message_id"], {first.pk, second.pk})
        scores = [r["score"] for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(score >= embedding_service.MIN_SCORE for score in scores))

    def test_a_brand_new_chat_is_findable_on_the_first_search(self):
        chat, _ = self.chat(self.user, "Leaves", LEAVES)
        self.assertFalse(MessageEmbedding.objects.exists())
        _, data = self.search()
        self.assertEqual([r["conversation_id"] for r in data["results"]], [chat.pk])

    def test_a_repeated_search_costs_no_request(self):
        self.chat(self.user, "Leaves", LEAVES)
        self.search()
        self.fake.calls.clear()
        _, data = self.search()
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(self.fake.calls, [])

    def test_archived_chats_only_when_asked(self):
        archived, _ = self.chat(self.user, "Leaves", LEAVES, archived=True)

        _, data = self.search()
        self.assertEqual(data["results"], [])

        _, data = self.search(archived="1")
        self.assertEqual([r["conversation_id"] for r in data["results"]], [archived.pk])
        self.assertTrue(data["results"][0]["is_archived"])

    def test_the_age_filter(self):
        chat, (answer,) = self.chat(self.user, "Leaves", LEAVES)
        Message.objects.filter(conversation=chat).update(created_at=timezone.now() - timedelta(days=60))

        self.assertEqual(self.search(days="30")[1]["results"], [])
        self.assertEqual(len(self.search(days="90")[1]["results"]), 1)
        self.assertEqual(len(self.search(days="12345")[1]["results"]), 1)   # not a choice: ignored

    def test_another_students_chats_are_never_returned(self):
        other = self.make_user("dara")
        theirs, _ = self.chat(other, "Leaves", LEAVES)
        embedding_service.index_pending(other)
        self.assertTrue(MessageEmbedding.objects.filter(user=other).exists())

        self.assertEqual(self.search()[1]["results"], [])

        mine, _ = self.chat(self.user, "Plants", LEAVES)
        ids = {r["conversation_id"] for r in self.search("how plants make food")[1]["results"]}
        self.assertEqual(ids, {mine.pk})
        self.assertNotIn(theirs.pk, {r["conversation_id"] for r in embedding_service.search(self.user, QUERY)})

    def test_trashed_chats_are_never_returned(self):
        chat, _ = self.chat(self.user, "Leaves", LEAVES)
        self.assertEqual(len(self.search()[1]["results"]), 1)

        trash_service.trash(self.user, chat.pk)
        self.assertEqual(self.search()[1]["results"], [])

        trash_service.restore(self.user, chat.pk)
        self.assertEqual(len(self.search()[1]["results"]), 1)

    def test_a_short_query_returns_nothing_and_costs_nothing(self):
        self.chat(self.user, "Leaves", LEAVES)
        response, data = self.search("ab")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((data["enabled"], data["results"], data["code"]), (True, [], "too_short"))
        self.assertEqual(self.fake.calls, [])

    def test_switched_off(self):
        self.chat(self.user, "Leaves", LEAVES)
        settings_service.set_value("semantic_search_enabled", False)
        response, data = self.search()
        self.assertEqual(response.status_code, 200)
        self.assertEqual((data["ok"], data["enabled"], data["results"], data["code"]), (True, False, [], "disabled"))
        self.assertTrue(data["reason"])
        self.assertEqual(self.fake.calls, [])

    @override_settings(EMBEDDING_API_KEY="")
    def test_not_configured(self):
        response, data = self.search()
        self.assertEqual(response.status_code, 200)
        self.assertEqual((data["enabled"], data["code"], data["results"]), (False, "not_configured", []))
        self.assertIn("set up", data["reason"])

    def test_a_provider_outage_is_an_empty_answer_with_a_reason(self):
        self.chat(self.user, "Leaves", LEAVES)
        self.post_mock.side_effect = requests.ConnectionError("down")
        with self.assertLogs("AI", level="WARNING"):
            response, data = self.search()
        self.assertEqual(response.status_code, 200)
        self.assertEqual((data["ok"], data["enabled"], data["results"], data["code"]), (True, True, [], "network"))
        self.assertTrue(data["reason"])

    def test_a_rejected_key_never_reaches_the_student_or_the_log(self):
        self.chat(self.user, "Leaves", LEAVES)
        self.post_mock.side_effect = None
        self.post_mock.return_value = FakeResponse(
            401, {"error": {"message": f"Incorrect API key provided: {KEY}. Find yours at the dashboard."}}
        )
        with self.assertLogs("AI", level="WARNING") as logs:
            response, data = self.search()
        self.assertEqual((response.status_code, data["code"], data["results"]), (200, "auth", []))
        self.assertNotIn(KEY, json.dumps(data))
        self.assertNotIn("API key", data["reason"])
        joined = "\n".join(logs.output)
        self.assertNotIn(KEY, joined)
        self.assertIn("[redacted]", joined)

    def test_rate_limited(self):
        self.chat(self.user, "Leaves", LEAVES)
        limits = dict(settings.RATE_LIMITS)
        limits["semantic_search"] = (2, 60)
        with self.settings(RATE_LIMITS=limits):
            self.assertEqual(self.search()[0].status_code, 200)
            self.assertEqual(self.search()[0].status_code, 200)
            response, data = self.search()
        self.assertEqual(response.status_code, 429)
        self.assertEqual((data["ok"], data["code"]), (False, "rate_limited"))

    def test_students_only_and_get_only(self):
        self.assertEqual(
            self.client.post(URL, {"q": QUERY}, HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code, 405,
        )
        self.client.logout()
        response = self.client.get(URL, {"q": QUERY}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 401)


# ---------------------------------------------------------------------------
# Lifecycle: vectors go with their messages
# ---------------------------------------------------------------------------
class LifecycleTests(Base):
    def test_purging_a_chat_deletes_its_vectors(self):
        chat, _ = self.chat(self.user, "Leaves", LEAVES, LEAVES_MORE)
        keep, _ = self.chat(self.user, "WW1", WAR)
        embedding_service.index_pending(self.user)
        self.assertEqual(MessageEmbedding.objects.count(), 3)

        trash_service.trash(self.user, chat.pk)
        trash_service.purge(Conversation.all_objects.get(pk=chat.pk))

        self.assertEqual(list(MessageEmbedding.objects.values_list("conversation_id", flat=True)), [keep.pk])

    def test_a_replaced_answer_takes_its_vector_with_it(self):
        _, (answer,) = self.chat(self.user, "Leaves", LEAVES)
        embedding_service.index_pending(self.user)
        answer.delete()
        self.assertFalse(MessageEmbedding.objects.exists())


# ---------------------------------------------------------------------------
# The management command, the hourly run and the health page
# ---------------------------------------------------------------------------
class CommandTests(Base):
    def run_command(self, *args) -> str:
        out = StringIO()
        call_command("index_embeddings", *args, stdout=out)
        return out.getvalue()

    def test_indexes_everything_and_reports_progress(self):
        self.chat(self.user, "Leaves", LEAVES, LEAVES_MORE)
        self.chat(self.make_user("dara"), "WW1", WAR)
        output = self.run_command()
        self.assertEqual(MessageEmbedding.objects.count(), 3)
        self.assertIn("indexed    3 of 3", output)
        self.assertIn("0 still pending", output)

    def test_one_student_and_a_limit(self):
        self.chat(self.user, "Leaves", LEAVES, LEAVES_MORE)
        self.chat(self.make_user("dara"), "WW1", WAR)
        self.run_command("--user", "sokha", "--limit", "1")
        self.assertEqual(list(MessageEmbedding.objects.values_list("user_id", flat=True)), [self.user.pk])

    def test_rebuild_replaces_vectors_from_another_model(self):
        chat, (answer, _) = self.chat(self.user, "Leaves", LEAVES, LEAVES_MORE)
        MessageEmbedding.objects.create(
            message=answer, user=self.user, conversation=chat, model_name="text-embedding-ada-002",
            dimensions=3, vector=b"\x00" * 12, text_hash="old",
        )
        output = self.run_command("--rebuild")
        self.assertIn("removed    1 vector(s)", output)
        self.assertEqual(
            set(MessageEmbedding.objects.values_list("model_name", "dimensions")),
            {("text-embedding-3-small", DIMS)},
        )
        self.assertEqual(MessageEmbedding.objects.count(), 2)

    def test_it_catches_edits_anywhere_not_only_in_recent_chats(self):
        chat, (answer,) = self.chat(self.user, "Leaves", LEAVES)
        embedding_service.index_pending(self.user)
        # Changed without the chat being touched: the per-search pass would not look.
        Message.objects.filter(pk=answer.pk).update(content="Rivers carry dissolved minerals into the sea.")
        Conversation.objects.filter(pk=chat.pk).update(updated_at=timezone.now() - timedelta(days=5))

        output = self.run_command()
        self.assertIn("1 edited", output)
        self.assertIn("Rivers carry", self.fake.inputs[-1])

    @override_settings(EMBEDDING_API_KEY="")
    def test_not_configured_is_an_error(self):
        with self.assertRaisesMessage(CommandError, "not configured"):
            self.run_command()

    def test_a_provider_failure_stops_with_a_clear_message(self):
        self.chat(self.user, "Leaves", LEAVES)
        self.post_mock.side_effect = requests.ConnectionError("down")
        with self.assertRaisesMessage(CommandError, "network"):
            self.run_command()

    def test_unknown_user(self):
        with self.assertRaisesMessage(CommandError, "No user"):
            self.run_command("--user", "nobody")


class ReminderRunTests(TestCase):
    """Not on :class:`Base`: the hourly run must see the real ``requests.post``."""

    def setUp(self):
        settings_service.bootstrap()

    def test_the_hourly_run_catches_up_the_index(self):
        out = StringIO()
        with patch("AI.services.embedding_service.index_pending", return_value=3) as index:
            call_command("send_reminders", "--skip-cleanup", stdout=out)
        index.assert_called_once_with(limit=200)
        self.assertIn("3 answer(s) indexed", out.getvalue())

    def test_an_indexing_crash_never_stops_the_run(self):
        out = StringIO()
        with patch("AI.services.embedding_service.index_pending", side_effect=RuntimeError("boom")):
            call_command("send_reminders", "--skip-cleanup", stdout=out)
        self.assertIn("Reminder run complete", out.getvalue())


class HealthTests(Base):
    def check(self):
        report = health_service.report()
        group = next(g for g in report["groups"] if g.key == "integrations")
        return next(c for c in group.checks if c.key == "semantic_search")

    def test_counts_indexed_and_pending(self):
        self.chat(self.user, "Leaves", LEAVES, LEAVES_MORE)
        self.chat(self.user, "WW1", WAR)
        embedding_service.index_pending(self.user, limit=2)
        calls = len(self.fake.calls)
        check = self.check()
        self.assertEqual((check.status, check.value), (health_service.OK, "2 indexed, 1 pending"))
        self.assertEqual(len(self.fake.calls), calls)   # the health page never calls out

    @override_settings(EMBEDDING_API_KEY="")
    def test_not_configured(self):
        check = self.check()
        self.assertEqual((check.status, check.value), (health_service.WARN, "Not configured"))

    def test_switched_off(self):
        settings_service.set_value("semantic_search_enabled", False)
        self.assertEqual(self.check().value, "Switched off")


@override_settings(EMBEDDING_API_KEY=KEY)
class OfflineGuardTests(TestCase):
    """With a real key in .env, the suite must still never call the real API."""

    def test_the_suite_never_reaches_the_real_endpoint(self):
        settings_service.bootstrap()
        user = Base.make_user("sokha")
        Base.chat(user, "Leaves", LEAVES)
        with patch("requests.sessions.Session.request") as transport:
            self.assertEqual(embedding_service.index_pending(user), 0)
            with self.assertRaises(EmbeddingError) as ctx:
                embedding_service.embed_texts(["hello"])
        self.assertEqual(ctx.exception.code, "offline")
        transport.assert_not_called()
