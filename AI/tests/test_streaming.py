"""Live answers: streamed word by word, stopped on request, finished if the tab closes.

Covers the two vendors' streaming parsers (usage, estimates, a stop part-way,
an endpoint that ignores ``stream``), the filter that keeps the suggestion
block out of what the student watches, the three endpoints that stream (send,
regenerate, edit) — same payload as the plain JSON, same errors, charged
once — a stop before and after the first word, the Stop and presence
endpoints, the "answer is ready" notification, and the worker thread itself.
"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import patch

from django.core.cache import cache
from django.core.signals import request_finished
from django.db import close_old_connections
from django.test import SimpleTestCase, TestCase, override_settings
from requests.structures import CaseInsensitiveDict

from AI.models import AccountStatus, AIUsageLog, Conversation, Message, MessageRole, User
from AI.services import chat_service, followup_service, settings_service, stream_service
from AI.services.ai_service import AIError, AIResponse
from AI.services.providers.anthropic_provider import AnthropicProvider
from AI.services.providers.base import STOPPED, CancelToken
from AI.services.providers.openai_provider import OpenAICompatibleProvider
from AI.tests.helpers import GPT, balance, fund

PASSWORD = "Sup3rSecret!x"
CONFIGURED = "AI.services.ai_service.OpenAICompatibleProvider.is_configured"
STREAM = "AI.services.ai_service.OpenAICompatibleProvider.stream"
NOTIFY = "AI.services.stream_service._notify_ready"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeHTTPStream:
    """What ``requests.post(..., stream=True)`` returns, minus the network."""

    def __init__(self, lines=(), *, status=200, content_type="text/event-stream", body=None):
        self.status_code = status
        self.headers = CaseInsensitiveDict({"Content-Type": content_type, "x-request-id": "req-http"})
        self._lines = list(lines)
        self._body = body
        self.closed = False
        self.text = ""

    def iter_lines(self, chunk_size=None):
        for line in self._lines:
            if self.closed:
                raise ConnectionError("read on a closed connection")
            yield line

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    def close(self):
        self.closed = True


def openai_lines(*pieces, usage=None, finish="stop"):
    lines = []
    for piece in pieces:
        chunk = {"id": "chatcmpl-1", "model": "gpt-4o", "choices": [{"delta": {"content": piece}}]}
        lines += [b"data: " + json.dumps(chunk).encode(), b""]
    lines += [b"data: " + json.dumps({"id": "chatcmpl-1", "choices": [{"delta": {}, "finish_reason": finish}]}).encode(), b""]
    if usage is not None:
        lines += [b"data: " + json.dumps({"id": "chatcmpl-1", "choices": [], "usage": usage}).encode(), b""]
    return lines + [b"data: [DONE]", b""]


def openai() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(api_key="sk-test", base_url="https://vendor.test/v1", model="gpt-4o")


class FakeEvents:
    def __init__(self, events):
        self.events = events
        self.closed = False
        self.response = NS(headers={"request-id": "req-anthropic"})

    def __iter__(self):
        for event in self.events:
            if self.closed:
                raise RuntimeError("closed")
            yield event

    def close(self):
        self.closed = True


def anthropic_events(*pieces, output_tokens=12):
    events = [NS(type="message_start", message=NS(
        model="claude-sonnet-5", id="msg_1",
        usage=NS(input_tokens=50, output_tokens=1, cache_read_input_tokens=7),
    ))]
    events.append(NS(type="content_block_delta", delta=NS(type="thinking_delta", thinking="private")))
    events += [NS(type="content_block_delta", delta=NS(type="text_delta", text=p)) for p in pieces]
    events.append(NS(type="message_delta", delta=NS(stop_reason="end_turn"), usage=NS(output_tokens=output_tokens)))
    events.append(NS(type="message_stop"))
    return events


def fake_stream(pieces, *, usage=(100, 200), stop_after=None):
    """A vendor ``stream`` that writes ``pieces`` and honours Stop."""

    def stream(self, turns, *, system_prompt="", max_tokens=None, profile="standard", on_delta=None, cancel=None):
        written = []
        for index, piece in enumerate(pieces):
            if cancel is not None and cancel.cancelled:
                break
            written.append(piece)
            on_delta(piece)
            if stop_after is not None and index + 1 == stop_after:
                cancel.cancel()
        stopped = cancel is not None and cancel.cancelled
        return AIResponse(
            content="".join(written).strip(), input_tokens=usage[0], output_tokens=usage[1],
            total_tokens=sum(usage), model="gpt-5-mini", request_id="req-s", latency_ms=12,
            finish_reason=STOPPED if stopped else "stop",
        )

    return stream


def read_events(response) -> list[tuple[str, dict]]:
    body = b"".join(response.streaming_content).decode("utf-8")
    events = []
    for block in body.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        name, data = "message", None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        events.append((name, data))
    return events


def text_of(events) -> str:
    return "".join(data["t"] for name, data in events if name == "delta")


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------
class OpenAIStreamTests(SimpleTestCase):
    def test_words_arrive_in_order_and_usage_is_the_vendors(self):
        got = []
        lines = openai_lines("Hello", ", ", "world.", usage={"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49})
        with patch("AI.services.providers.openai_provider.requests.post", return_value=FakeHTTPStream(lines)) as post:
            result = openai().stream([], system_prompt="S", on_delta=got.append)
        self.assertEqual(got, ["Hello", ", ", "world."])
        self.assertEqual(result.content, "Hello, world.")
        self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (40, 9, 49))
        self.assertEqual(result.finish_reason, "stop")
        self.assertFalse(result.raw["estimated"])
        body = json.loads(post.call_args.kwargs["data"])
        self.assertTrue(body["stream"])
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertTrue(post.call_args.kwargs["stream"])

    def test_missing_usage_is_estimated_not_free(self):
        with patch("AI.services.providers.openai_provider.requests.post",
                   return_value=FakeHTTPStream(openai_lines("x" * 400))):
            result = openai().stream([], system_prompt="y" * 80)
        self.assertTrue(result.raw["estimated"])
        self.assertEqual(result.output_tokens, 100)
        self.assertGreaterEqual(result.input_tokens, 20)
        self.assertEqual(result.total_tokens, result.input_tokens + result.output_tokens)

    def test_an_endpoint_that_ignores_stream_still_answers(self):
        body = {"id": "c1", "model": "gpt-4o", "choices": [{"message": {"content": "Whole answer."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}
        got = []
        with patch("AI.services.providers.openai_provider.requests.post",
                   return_value=FakeHTTPStream(content_type="application/json", body=body)):
            result = openai().stream([], on_delta=got.append)
        self.assertEqual(got, ["Whole answer."])
        self.assertEqual(result.total_tokens, 8)

    def test_stop_keeps_what_was_written_and_closes_the_connection(self):
        token = CancelToken()
        got = []
        fake = FakeHTTPStream(openai_lines("First part. ", "Second part.", usage={"total_tokens": 99}))

        def on_delta(text):
            got.append(text)
            token.cancel()

        with patch("AI.services.providers.openai_provider.requests.post", return_value=fake):
            result = openai().stream([], on_delta=on_delta, cancel=token)
        self.assertEqual(got, ["First part. "])
        self.assertEqual(result.content, "First part.")
        self.assertEqual(result.finish_reason, STOPPED)
        self.assertTrue(result.raw["estimated"])
        self.assertTrue(fake.closed)

    def test_vendor_errors_are_translated(self):
        failing = FakeHTTPStream(status=429, content_type="application/json", body={"error": {"message": "slow down"}})
        with patch("AI.services.providers.openai_provider.requests.post", return_value=failing):
            with self.assertRaises(AIError) as caught:
                openai().stream([])
        self.assertEqual(caught.exception.code, "rate_limited")

    def test_a_connection_that_breaks_part_way_is_a_retryable_failure(self):
        class Broken(FakeHTTPStream):
            def iter_lines(self, chunk_size=None):
                yield openai_lines("Half")[0]
                raise ConnectionError("reset")

        with patch("AI.services.providers.openai_provider.requests.post", return_value=Broken()):
            with self.assertRaises(AIError) as caught:
                openai().stream([], on_delta=lambda _t: None)
        self.assertEqual(caught.exception.code, "network")
        self.assertTrue(caught.exception.retryable)

    def test_an_empty_stream_is_an_error_not_an_empty_answer(self):
        with patch("AI.services.providers.openai_provider.requests.post",
                   return_value=FakeHTTPStream(openai_lines(usage={"total_tokens": 5}))):
            with self.assertRaises(AIError) as caught:
                openai().stream([])
        self.assertEqual(caught.exception.code, "empty_response")


class AnthropicStreamTests(SimpleTestCase):
    def provider(self, events):
        provider = AnthropicProvider(api_key="sk-ant-test", model="claude-sonnet-5")
        stream = FakeEvents(events)
        client = NS(messages=NS(create=lambda **kwargs: stream))
        return provider, stream, patch.object(AnthropicProvider, "_client", return_value=client)

    def test_text_is_streamed_thinking_is_not_and_usage_is_anthropics(self):
        provider, _stream, client = self.provider(anthropic_events("Hello ", "there."))
        got = []
        with client:
            result = provider.stream([NS(role="user", content="hi", images=lambda: [])], on_delta=got.append)
        self.assertEqual(got, ["Hello ", "there."])
        self.assertEqual(result.content, "Hello there.")
        self.assertEqual((result.input_tokens, result.output_tokens), (50, 12))
        self.assertEqual(result.finish_reason, "end_turn")
        self.assertEqual(result.request_id, "req-anthropic")
        self.assertEqual(result.raw["usage"]["cache_read_input_tokens"], 7)

    def test_stop_keeps_the_part_written_with_an_estimate(self):
        provider, stream, client = self.provider(anthropic_events("x" * 80, "never shown"))
        token = CancelToken()
        got = []

        def on_delta(text):
            got.append(text)
            token.cancel()

        with client:
            result = provider.stream([NS(role="user", content="hi", images=lambda: [])], on_delta=on_delta, cancel=token)
        self.assertEqual(got, ["x" * 80])
        self.assertEqual(result.finish_reason, STOPPED)
        self.assertEqual(result.input_tokens, 50)
        self.assertEqual(result.output_tokens, 20)
        self.assertTrue(stream.closed)


# ---------------------------------------------------------------------------
# What the student watches
# ---------------------------------------------------------------------------
class FollowupFilterTests(SimpleTestCase):
    def watch(self, text, step=3):
        seen = []
        feed = followup_service.StreamFilter(seen.append).feed
        for i in range(0, len(text), step):
            feed(text[i:i + step])
        return "".join(seen)

    def test_the_block_never_shows_in_any_of_its_spellings(self):
        for marker in ("[[FOLLOWUPS]]", "**[[Follow-ups]]**", "### [[followups]]", "`[[FOLLOW UPS]]`"):
            with self.subTest(marker=marker):
                shown = self.watch(f"The answer.\n\n{marker}\n- Next question?\n- Another?")
                self.assertEqual(shown.strip(), "The answer.")

    def test_ordinary_text_and_links_pass_through_whole(self):
        text = "Line one.\n[A link](https://example.com) and more.\n- a list\n"
        self.assertEqual(self.watch(text, step=1), text)

    def test_a_half_written_marker_is_trimmed_from_a_stopped_answer(self):
        self.assertEqual(followup_service.trim_partial_marker("Answer.\n\n**[[FOLLO"), "Answer.")
        self.assertEqual(followup_service.trim_partial_marker("Answer with [x](y)."), "Answer with [x](y).")


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------
@patch(CONFIGURED, True)
class StreamedEndpointTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        cache.clear()
        self.user = User.objects.create_user(
            username="sokha", email="sokha@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        fund(self.user, 20_000, GPT)
        self.client.force_login(self.user)

    def post(self, url, data=None):
        return self.client.post(url, data or {}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def send(self, message="Explain how photosynthesis works", **extra):
        return self.post("/api/chat/send/", {"message": message, "model": GPT, "stream": "1", **extra})

    def test_send_streams_the_words_then_the_same_payload_as_json(self):
        pieces = ["Plants ", "use light ", "to make sugar.\n\n[[FOLLOWUPS]]\n", "- What is chlorophyll?\n- Why are leaves green?"]
        # The job runs when the body is read, so the reading is inside the patch.
        with patch(STREAM, fake_stream(pieces)):
            response = self.send()
            events = read_events(response)
        self.assertEqual(response["Content-Type"], "text/event-stream; charset=utf-8")
        self.assertEqual(response["X-Accel-Buffering"], "no")
        names = [name for name, _data in events]
        self.assertEqual(names[0], "start")
        self.assertEqual(names[-1], "done")
        start, done = events[0][1], events[-1][1]
        self.assertTrue(start["stream_id"])
        self.assertEqual(start["conversation_id"], done["conversation_id"])

        self.assertEqual(text_of(events).strip(), "Plants use light to make sugar.")
        self.assertTrue(done["ok"])
        answer = done["assistant_message"]
        self.assertEqual(answer["content"], "Plants use light to make sugar.")
        self.assertEqual(answer["suggestions"], ["What is chlorophyll?", "Why are leaves green?"])
        self.assertIn("conversation", done)
        self.assertEqual(done["tokens_charged"], 300)

        self.assertEqual(balance(self.user), 20_000 - 300)
        self.assertEqual(AIUsageLog.objects.filter(user=self.user, success=True).count(), 1)
        saved = Message.objects.get(pk=answer["id"])
        self.assertEqual(saved.content, answer["content"])

    def test_with_streaming_switched_off_the_answer_comes_as_json(self):
        settings_service.set_value("chat_streaming", False)
        with patch(STREAM, fake_stream(["Unused."])), \
                patch("AI.services.ai_service.OpenAICompatibleProvider.complete",
                      return_value=AIResponse(content="Plain.", input_tokens=1, output_tokens=1, total_tokens=2, model="gpt-5-mini")):
            response = self.send()
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["assistant_message"]["content"], "Plain.")

    def test_a_vendor_failure_arrives_as_the_same_error_and_costs_nothing(self):
        def failing(self, turns, **kwargs):
            raise AIError("The assistant is busy right now.", code="rate_limited")

        with patch(STREAM, failing):
            events = read_events(self.send())
        name, error = events[-1]
        self.assertEqual(name, "error")
        self.assertEqual(error["status"], 502)
        self.assertEqual(error["code"], "rate_limited")
        self.assertTrue(error["retryable"])
        self.assertIn("conversation_id", error)
        self.assertEqual(balance(self.user), 20_000)
        self.assertTrue(Message.objects.filter(conversation__user=self.user, role=MessageRole.USER).exists())

    def test_a_refusal_arrives_with_its_status(self):
        broke = User.objects.create_user(username="dara", email="d@test.local", password=PASSWORD,
                                         is_verified=True, status=AccountStatus.ACTIVE)
        self.client.force_login(broke)
        with patch(STREAM, fake_stream(["never"])):
            events = read_events(self.send())
        name, error = events[-1]
        self.assertEqual(name, "error")
        self.assertEqual(error["status"], 402)
        self.assertFalse(error["ok"])

    def test_a_retry_of_a_message_that_got_through_is_refused_before_streaming(self):
        chat = Conversation.objects.create(user=self.user, title="Earlier")
        Message.objects.create(conversation=chat, role=MessageRole.USER, content="q", client_id="same-id")
        response = self.send(client_id="same-id")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "already_sent")

    def test_stop_part_way_keeps_and_charges_only_what_was_written(self):
        with patch(STREAM, fake_stream(["Part one. ", "Part two."], usage=(100, 20), stop_after=1)):
            events = read_events(self.send())
        done = events[-1][1]
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(done["assistant_message"]["content"], "Part one.")
        self.assertEqual(done["assistant_message"]["suggestions"], [])
        self.assertEqual(balance(self.user), 20_000 - 120)
        log = AIUsageLog.objects.get(user=self.user)
        self.assertEqual(log.error_code, STOPPED)
        self.assertTrue(log.success)

    def test_stop_before_the_first_word_saves_nothing_and_charges_the_question(self):
        def stopped(self, turns, *, on_delta=None, cancel=None, **kwargs):
            cancel.cancel()
            return AIResponse(content="", input_tokens=80, output_tokens=0, total_tokens=80,
                              model="gpt-5-mini", finish_reason=STOPPED)

        with patch(STREAM, stopped):
            events = read_events(self.send())
        name, error = events[-1]
        self.assertEqual((name, error["code"]), ("error", "stopped"))
        self.assertFalse(Message.objects.filter(conversation__user=self.user, role=MessageRole.ASSISTANT).exists())
        self.assertEqual(balance(self.user), 20_000 - 80)
        self.assertEqual(AIUsageLog.objects.get(user=self.user).error_code, STOPPED)

    def test_summary_housekeeping_runs_after_the_answer(self):
        with patch(STREAM, fake_stream(["An answer."])), \
                patch.object(chat_service, "refresh_summary") as summary:
            events = read_events(self.send())
        self.assertEqual(events[-1][0], "done")
        summary.assert_called_once()

    def test_regenerate_streams_and_replaces_the_old_answer(self):
        chat = Conversation.objects.create(user=self.user, title="Gravity")
        Message.objects.create(conversation=chat, role=MessageRole.USER, content="Why do things fall?")
        old = Message.objects.create(conversation=chat, role=MessageRole.ASSISTANT, content="Old answer.", model_key=GPT)
        with patch(STREAM, fake_stream(["Mass ", "attracts mass."])):
            events = read_events(self.post(f"/api/chat/{chat.pk}/regenerate/", {"model": GPT, "stream": "1"}))
        self.assertEqual(text_of(events), "Mass attracts mass.")
        done = events[-1][1]
        self.assertEqual(done["assistant_message"]["content"], "Mass attracts mass.")
        self.assertFalse(Message.objects.filter(pk=old.pk).exists())

    def test_edit_streams_and_replaces_what_came_after(self):
        chat = Conversation.objects.create(user=self.user, title="Cells")
        question = Message.objects.create(conversation=chat, role=MessageRole.USER, content="What is a cell?")
        old = Message.objects.create(conversation=chat, role=MessageRole.ASSISTANT, content="Old.")
        with patch(STREAM, fake_stream(["The ", "nucleus."])):
            events = read_events(self.post(f"/api/chat/{chat.pk}/messages/{question.pk}/edit/",
                                           {"message": "What is a nucleus?", "model": GPT, "stream": "1"}))
        self.assertEqual(events[-1][1]["assistant_message"]["content"], "The nucleus.")
        self.assertFalse(Message.objects.filter(pk=old.pk).exists())
        question.refresh_from_db()
        self.assertEqual(question.content, "What is a nucleus?")

    def test_someone_elses_chat_cannot_be_streamed(self):
        other = User.objects.create_user(username="vannak", email="v@test.local", password=PASSWORD,
                                         is_verified=True, status=AccountStatus.ACTIVE)
        theirs = Conversation.objects.create(user=other, title="Private")
        response = self.post(f"/api/chat/{theirs.pk}/regenerate/", {"stream": "1"})
        self.assertEqual(response.status_code, 404)

    def test_the_page_asks_for_streams_only_when_switched_on(self):
        self.assertContains(self.client.get("/chat/"), "var STREAMING = true &&")
        settings_service.set_value("chat_streaming", False)
        self.assertContains(self.client.get("/chat/"), "var STREAMING = false &&")

    def test_the_palette_searches_by_meaning_only_when_it_can(self):
        with patch("AI.services.embedding_service.is_enabled", return_value=True):
            self.assertContains(self.client.get("/chat/"), "var MEANING = true;")
        with patch("AI.services.embedding_service.is_enabled", return_value=False):
            self.assertContains(self.client.get("/chat/"), "var MEANING = false;")

    # -- stop and presence ------------------------------------------------------
    def owned_run(self, user=None):
        run = stream_service.StreamRun(user or self.user)
        cache.set(stream_service._key(run.id), (user or self.user).pk, 60)
        with stream_service._runs_lock:
            stream_service._runs[run.id] = run
        self.addCleanup(stream_service._runs.pop, run.id, None)
        return run

    def test_stop_endpoint_stops_the_students_own_run(self):
        run = self.owned_run()
        self.assertEqual(self.post(f"/api/chat/stream/{run.id}/stop/").status_code, 200)
        self.assertTrue(run.token.cancelled)

    def test_stop_endpoint_ignores_unknown_and_other_peoples_runs(self):
        other = User.objects.create_user(username="kosal", email="k@test.local", password=PASSWORD,
                                         is_verified=True, status=AccountStatus.ACTIVE)
        theirs = self.owned_run(other)
        self.assertEqual(self.post(f"/api/chat/stream/{theirs.id}/stop/").status_code, 404)
        self.assertFalse(theirs.token.cancelled)
        self.assertEqual(self.post("/api/chat/stream/not-a-run/stop/").status_code, 404)

    def test_a_stop_sent_to_another_server_process_is_seen_through_the_cache(self):
        run = stream_service.StreamRun(self.user)                   # not registered here
        cache.set(stream_service._key(run.id), self.user.pk, 60)
        self.assertTrue(stream_service.request_stop(self.user, run.id))
        self.assertTrue(run.token.cancelled)

    def test_presence_marks_away_and_back(self):
        run = self.owned_run()
        self.post(f"/api/chat/stream/{run.id}/presence/", {"state": "away"})
        self.assertTrue(run.away())
        self.post(f"/api/chat/stream/{run.id}/presence/", {"state": "back"})
        self.assertFalse(run.away())

    def test_stop_and_presence_need_a_post_and_a_login(self):
        run = self.owned_run()
        self.assertEqual(self.client.get(f"/api/chat/stream/{run.id}/stop/").status_code, 405)
        self.client.logout()
        self.assertNotEqual(self.post(f"/api/chat/stream/{run.id}/stop/").status_code, 200)
        self.assertFalse(run.token.cancelled)

    # -- "your answer is ready" -------------------------------------------------
    def test_a_student_watching_gets_no_notification(self):
        with patch(STREAM, fake_stream(["Seen."])), patch(NOTIFY) as notify:
            read_events(self.send())
        notify.assert_not_called()

    def test_a_student_who_looked_away_is_notified_once(self):
        with patch(STREAM, fake_stream(["Ready."])), patch(NOTIFY) as notify, \
                patch.object(stream_service.StreamRun, "away", return_value=True):
            events = read_events(self.send())
        notify.assert_called_once_with(self.user, events[-1][1]["assistant_message"]["id"])

    def test_a_closed_tab_still_gets_the_answer_saved_and_a_notification(self):
        with patch(STREAM, fake_stream(["Saved ", "anyway."])), patch(NOTIFY) as notify:
            response = self.send()
            first = next(iter(response.streaming_content))
            self.assertIn(b"event: start", first)
            # The browser went away. The server closes the response, as the
            # test client does, without closing the test's own connection.
            request_finished.disconnect(close_old_connections)
            try:
                response.close()
            finally:
                request_finished.connect(close_old_connections)
        answer = Message.objects.get(conversation__user=self.user, role=MessageRole.ASSISTANT)
        self.assertEqual(answer.content, "Saved anyway.")
        notify.assert_called_once_with(self.user, answer.pk)
        self.assertEqual(balance(self.user), 20_000 - 300)

    def test_the_notification_names_the_chat_and_answer(self):
        chat = Conversation.objects.create(user=self.user, title="Tides")
        answer = Message.objects.create(conversation=chat, role=MessageRole.ASSISTANT, content="The moon.")
        with patch("AI.services.push_service.answer_ready") as ready:
            stream_service._notify_ready(self.user, answer.pk)
        ready.assert_called_once_with(self.user, chat, answer)

    def test_the_notification_never_reaches_someone_else(self):
        other = User.objects.create_user(username="sophea", email="s@test.local", password=PASSWORD,
                                         is_verified=True, status=AccountStatus.ACTIVE)
        chat = Conversation.objects.create(user=other, title="Theirs")
        answer = Message.objects.create(conversation=chat, role=MessageRole.ASSISTANT, content="x")
        with patch("AI.services.push_service.answer_ready") as ready:
            stream_service._notify_ready(self.user, answer.pk)
        ready.assert_not_called()


# ---------------------------------------------------------------------------
# The worker thread (no database: the job is a plain function)
# ---------------------------------------------------------------------------
@override_settings(CHAT_STREAM_THREADED=True)
class WorkerThreadTests(SimpleTestCase):
    user = NS(pk=4242)

    def respond(self, job):
        return stream_service.respond(
            self.user, job, finish=lambda result: {"ok": True, **result},
            fail=lambda exc: (502, {"ok": False, "code": "boom"}), conversation_id=9,
        )

    def test_words_written_on_the_worker_arrive_in_order(self):
        def job(run):
            for word in ("one ", "two ", "three"):
                time.sleep(0.01)
                run.delta(word)
            return {"assistant_message": {"id": 1}}

        events = read_events(self.respond(job))
        self.assertEqual(events[0], ("start", {"stream_id": events[0][1]["stream_id"], "conversation_id": 9}))
        self.assertEqual(text_of(events), "one two three")
        self.assertEqual(events[-1], ("done", {"ok": True, "assistant_message": {"id": 1}}))

    def test_a_failure_on_the_worker_becomes_an_error_event(self):
        def job(run):
            raise RuntimeError("vendor exploded")

        events = read_events(self.respond(job))
        self.assertEqual(events[-1], ("error", {"ok": False, "code": "boom", "status": 502}))

    def test_the_answer_finishes_after_the_browser_leaves(self):
        release = threading.Event()
        finished = threading.Event()

        def job(run):
            release.wait(2)
            run.delta("late words")
            finished.set()
            return {"assistant_message": {"id": 77}}

        with patch(NOTIFY) as notify:
            response = self.respond(job)
            next(iter(response.streaming_content))                  # "start"
            response.close()                                        # tab closed mid-answer
            release.set()
            self.assertTrue(finished.wait(2))
            for _ in range(100):
                if notify.called:
                    break
                time.sleep(0.02)
        notify.assert_called_once_with(self.user, 77)

    def test_stop_reaches_the_job_on_the_worker(self):
        seen = threading.Event()

        def job(run):
            run.delta("first")
            for _ in range(200):
                if run.token.cancelled:
                    seen.set()
                    return {"assistant_message": {"id": 3}, "stopped": True}
                time.sleep(0.01)
            return {"assistant_message": {"id": 3}}

        response = self.respond(job)
        stream = iter(response.streaming_content)
        start = next(stream)
        run_id = json.loads(start.decode().split("data: ", 1)[1])["stream_id"]
        self.assertTrue(stream_service.request_stop(self.user, run_id))
        rest = b"".join(stream).decode()
        self.assertTrue(seen.is_set())
        self.assertIn('"stopped":true', rest)
