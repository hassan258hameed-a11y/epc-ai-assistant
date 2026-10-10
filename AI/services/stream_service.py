"""Live answers: one chat request run on a worker, relayed as Server-Sent Events.

The model call does not run on the request that shows it. A worker thread
owns it and puts what it produces on a queue; the response only reads that
queue. So when the tab closes, the connection drops, or the phone locks, the
answer still finishes, is saved and is billed exactly once — and, if the
student had left, they get a browser notification that it is ready.

Stopping is the one thing a closed connection must *not* mean, so it is its
own request (:func:`request_stop`): it trips the run's
:class:`~AI.services.providers.base.CancelToken`, the provider closes its
connection to the vendor, and the part already written is kept and charged.

Events, in order::

    start  {"stream_id", "conversation_id"}
    delta  {"t": "next words"}                 (any number, possibly none)
    done   {...exactly the JSON the non-streaming endpoint returns...}
    error  {...exactly its JSON error, plus "status"...}

A comment line is sent every few seconds while nothing else is, so proxies
and browsers do not decide the connection is idle.

Under the test runner (``CHAT_STREAM_THREADED`` off) the job runs inline, on
the request's own thread, because a second thread cannot see a test's open
transaction. The events are the same; they simply arrive all at once.
"""
from __future__ import annotations

import json
import logging
import queue
import secrets
import threading
import time
from typing import Any, Callable

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections, connection
from django.http import StreamingHttpResponse

from AI.services.providers.base import CancelToken

logger = logging.getLogger("AI.stream")

#: How long a run's id is honoured by Stop and presence requests.
RUN_TTL = 15 * 60
#: Seconds of silence before a keep-alive comment is sent.
KEEPALIVE_SECONDS = 10
#: Minimum gap between cache look-ups for a Stop sent to another process.
REMOTE_CHECK_SECONDS = 0.5

_runs: dict[str, "StreamRun"] = {}
_runs_lock = threading.Lock()


def _key(run_id: str, part: str = "") -> str:
    return f"chat:stream:{run_id}{':' + part if part else ''}"


def threaded() -> bool:
    return bool(getattr(settings, "CHAT_STREAM_THREADED", False))


class StreamRun:
    """One streamed answer: its queue, its stop switch, and who is watching."""

    def __init__(self, user):
        self.id = secrets.token_urlsafe(12)
        self.user = user
        self.events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._last_remote_check = 0.0
        self.token = CancelToken(check=self._stopped_elsewhere)
        #: Housekeeping the job asked to run once the answer is delivered.
        self.deferred: list[Callable[[], Any]] = []
        #: The response stopped reading before the answer was delivered.
        self.gone = False
        self.result: dict | None = None
        self._notified = False
        self._lock = threading.Lock()

    # -- the job's side ---------------------------------------------------
    def delta(self, text: str) -> None:
        if text:
            self.events.put(("delta", text))

    def _stopped_elsewhere(self) -> bool:
        """A Stop that reached another server process, through the cache."""
        now = time.monotonic()
        if now - self._last_remote_check < REMOTE_CHECK_SECONDS:
            return False
        self._last_remote_check = now
        try:
            return bool(cache.get(_key(self.id, "stop")))
        except Exception:  # pragma: no cover — a cache outage must not stop answers
            return False

    # -- who is watching ----------------------------------------------------
    def away(self) -> bool:
        try:
            return bool(cache.get(_key(self.id, "away")))
        except Exception:  # pragma: no cover
            return False

    def maybe_notify(self) -> None:
        """Tell the student the answer is ready — once, and only if they left.

        Called both when the answer is saved and when the response notices
        the tab is gone, because either can happen second.
        """
        with self._lock:
            if self._notified or self.result is None or not (self.gone or self.away()):
                return
            message = (self.result.get("assistant_message") or {}) if self.result.get("ok") else {}
            if not message.get("id"):
                return
            self._notified = True
        _in_background(_notify_ready, self.user, message["id"])


def _notify_ready(user, message_id: int) -> None:
    try:
        from AI.models import Message
        from AI.services import push_service
    except ImportError:  # pragma: no cover — notifications not installed
        return
    message = Message.objects.select_related("conversation").filter(
        pk=message_id, conversation__user=user
    ).first()
    if message is None:
        return
    try:
        push_service.answer_ready(user, message.conversation, message)
    except Exception:  # pragma: no cover — a notification is never worth an error
        logger.exception("Could not send the answer-ready notification for message=%s", message_id)


def _in_background(fn, *args) -> None:
    if not threaded():
        fn(*args)
        return

    def run():
        try:
            fn(*args)
        finally:
            connection.close()

    threading.Thread(target=run, daemon=True, name="stream-notify").start()


# ---------------------------------------------------------------------------
# Requests about a run (Stop, "I left", "I'm back")
# ---------------------------------------------------------------------------
def _owned(user, run_id: str) -> bool:
    if not run_id or len(run_id) > 40:
        return False
    try:
        return cache.get(_key(run_id)) == user.pk
    except Exception:  # pragma: no cover
        return False


def request_stop(user, run_id: str) -> bool:
    """Stop a run the student owns. False when there is no such run (anymore)."""
    if not _owned(user, run_id):
        return False
    cache.set(_key(run_id, "stop"), 1, RUN_TTL)
    with _runs_lock:
        run = _runs.get(run_id)
    if run is not None:
        run.token.cancel()
    return True


def set_presence(user, run_id: str, *, away: bool) -> bool:
    """The tab was hidden or closed (``away``), or came back."""
    if not _owned(user, run_id):
        return False
    if away:
        cache.set(_key(run_id, "away"), 1, RUN_TTL)
    else:
        cache.delete(_key(run_id, "away"))
    return True


# ---------------------------------------------------------------------------
# Running one
# ---------------------------------------------------------------------------
def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode("utf-8")


def respond(
    user,
    job: Callable[[StreamRun], dict],
    *,
    finish: Callable[[dict], dict],
    fail: Callable[[Exception], tuple[int, dict]],
    conversation_id: int | None = None,
) -> StreamingHttpResponse:
    """Stream ``job`` to the browser.

    ``job(run)`` does the work, passing ``run.delta`` / ``run.token`` /
    ``run.deferred`` on to the chat service, and returns its result;
    ``finish(result)`` turns that into the ``done`` payload. ``fail(exc)``
    turns any exception into ``(status, payload)`` — the same error the
    non-streaming endpoint would have sent.
    """
    run = StreamRun(user)
    cache.set(_key(run.id), user.pk, RUN_TTL)
    response = StreamingHttpResponse(
        _relay(run, job, finish, fail, conversation_id),
        content_type="text/event-stream; charset=utf-8",
    )
    response["Cache-Control"] = "no-cache, no-transform"
    # nginx buffers proxied responses by default, which would turn a live
    # answer back into one that appears all at once.
    response["X-Accel-Buffering"] = "no"
    return response


def _work(run: StreamRun, job, finish, fail) -> None:
    try:
        payload = finish(job(run))
    except Exception as exc:
        try:
            status, payload = fail(exc)
        except Exception:  # pragma: no cover — the translator itself failed
            logger.exception("Could not describe a streamed chat failure")
            status, payload = 500, {"ok": False, "error": "Something went wrong. Please try again.",
                                    "code": "server_error"}
        payload = dict(payload, status=status)
        run.events.put(("error", payload))
    else:
        run.result = payload
        run.events.put(("done", payload))
        for task in run.deferred:
            try:
                task()
            except Exception:  # pragma: no cover — housekeeping never fails an answer
                logger.exception("Deferred housekeeping failed after a streamed answer")
        run.maybe_notify()


def _start(run: StreamRun, job, finish, fail) -> None:
    with _runs_lock:
        _runs[run.id] = run

    def body():
        try:
            _work(run, job, finish, fail)
        finally:
            with _runs_lock:
                _runs.pop(run.id, None)

    if not threaded():
        body()
        return

    def threaded_body():
        close_old_connections()
        try:
            body()
        finally:
            connection.close()

    threading.Thread(target=threaded_body, daemon=True, name=f"stream-{run.id[:6]}").start()


def _relay(run: StreamRun, job, finish, fail, conversation_id):
    """The response body: whatever the worker produces, as it produces it."""
    delivered = False
    try:
        # Started here rather than in the view, so a response that is never
        # read never costs anything.
        _start(run, job, finish, fail)
        yield _sse("start", {"stream_id": run.id, "conversation_id": conversation_id})
        while True:
            try:
                kind, data = run.events.get(timeout=KEEPALIVE_SECONDS)
            except queue.Empty:
                yield b": keep-alive\n\n"
                continue
            if kind == "delta":
                # Whatever else is already waiting goes out in the same event.
                pieces, pending = [data], None
                while pending is None:
                    try:
                        more = run.events.get_nowait()
                    except queue.Empty:
                        break
                    if more[0] == "delta":
                        pieces.append(more[1])
                    else:
                        pending = more
                yield _sse("delta", {"t": "".join(pieces)})
                if pending is None:
                    continue
                kind, data = pending
            yield _sse(kind, data)
            if kind in ("done", "error"):
                delivered = True
                return
    finally:
        if not delivered:
            # The browser went away (tab closed, network lost). The worker
            # carries on; whoever finishes second sends the notification.
            run.gone = True
            run.maybe_notify()
