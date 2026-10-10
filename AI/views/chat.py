"""Chat page and its JSON endpoints.

Every endpoint re-checks ownership of the conversation server-side; changing an
id in the URL never exposes another student's data.  Token limits and model
availability are likewise re-checked here on every request — the chat UI mirrors
those decisions but never makes them.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from AI.models import (
    FOLDER_COLOURS, AIModel, AIUsageLog, ChatPreference, Conversation, Message, MessageBookmark, MessageRole, Plan,
)
from AI.services import (
    chat_service,
    embedding_service,
    image_service,
    library_service,
    model_router,
    organise_service,
    share_service,
    notification_service,
    settings_service,
    stream_service,
    study_service,
    wallet_service,
    trash_service,
    model_status_service,
)
from AI.services.ai_service import AIError, provider_status
from AI.services.chat_service import ChatBlocked
from AI.services.model_service import ModelUnavailable
from AI.utils import ratelimit
from AI.utils.decorators import login_required_student
from AI.utils.ratelimit import RateLimited
from AI.utils.request import json_error, json_ok
from AI.utils.attachments import accepted_extensions, prepare_many
from AI.utils.uploads import InvalidUpload

logger = logging.getLogger("AI.views.chat")

#: Blocking codes that mean "this is a billing problem, show the upgrade path".
#: Refusals that mean "buy more tokens" — answered with Payment Required.
BILLING_CODES = {"no_tokens", "low_tokens", "insufficient_tokens"}

#: Refusals that mean "come back later". A usage window is a rate limit, so it
#: answers 429 — which is both accurate and what lets the client tell the two
#: apart without reading the prose.
RATE_CODES = {"usage_limit"}


def _mark_model_switches(messages) -> None:
    """Mark where the conversation moved to a different model.

    The marker goes on the *question* whose answer came from the new model, so
    the divider is drawn above that exchange — "from here on, another model" —
    rather than between a question and its own answer.

    Derived from the transcript rather than stored on the message, which means
    it is right after a reload, right for a conversation that predates the
    feature, and right whether the model changed because a limit was reached or
    because the student chose another one — all of which are the same event to
    somebody reading back: the answers after this point came from elsewhere.

    The first reply in a conversation is never a switch; there is nothing to
    have switched from.
    """
    names: dict[str, str] | None = None
    previous = ""
    messages = list(messages)
    for message in messages:
        message.switched_to = ""
    for index, message in enumerate(messages):
        if message.role != MessageRole.ASSISTANT:
            continue
        current = (message.model_key or "").strip()
        if not current:
            continue
        if previous and current != previous:
            if names is None:
                # Fetched once, and only when a transcript actually contains a
                # switch — most do not.
                names = dict(AIModel.objects.values_list("key", "name"))
            # A retired model keeps its key as the label: the switch happened,
            # and saying "switching to gpt-4o-mini" is better than saying
            # nothing about a model that no longer exists.
            label = names.get(current, current)
            # Walk back to the question this reply answered; the divider
            # belongs above it. A reply with no question before it (an image
            # edit, say) keeps the marker itself.
            target = message
            for earlier in reversed(messages[:index]):
                if earlier.role == MessageRole.USER:
                    target = earlier
                    break
                if earlier.role == MessageRole.ASSISTANT:
                    break
            target.switched_to = label
        previous = current


def _conversation_payload(conversation) -> dict:
    return {
        "id": conversation.pk,
        "title": conversation.title,
        "updated_at": timezone.localtime(conversation.updated_at).strftime("%d %b, %H:%M"),
        "short_time": _short_time(conversation.updated_at),
        "message_count": conversation.message_count,
        "is_pinned": conversation.is_pinned,
        "folder_id": conversation.folder_id,
    }


def _short_time(value) -> str:
    """Sidebar timestamp: '2m', '3h', 'Yd', 'Mon', then a date."""
    if not value:
        return ""
    delta = timezone.now() - value
    seconds = delta.total_seconds()
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    if seconds < 172800:
        return "Yd"
    if seconds < 604800:
        return timezone.localtime(value).strftime("%a")
    return timezone.localtime(value).strftime("%d %b")


def _group_label(value) -> str:
    """Which heading a conversation sits under in the sidebar.

    Purely presentational — the ordering already comes from the queryset; this
    only names the runs so the list reads as "Today / Yesterday / …" rather
    than as one undifferentiated column of titles.
    """
    if not value:
        return "Earlier"
    today = timezone.localdate()
    day = timezone.localtime(value).date()
    delta = (today - day).days
    if delta <= 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if delta <= 7:
        return "Previous 7 days"
    if delta <= 30:
        return "Previous 30 days"
    return "Earlier"


def _greeting() -> str:
    """Time-of-day greeting for the empty state, in the server's timezone."""
    hour = timezone.localtime().hour
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


def _requested_model(request) -> str:
    return (request.POST.get("model") or request.GET.get("model") or "").strip()[:64]


def _balance_for(request, user) -> dict:
    """The token position for the model the student has selected.

    Without this, every refresh described the *default* model, so a student
    working in GPT-4o saw GPT-5 Mini's limits snap back into the meter after a
    stop, an error or a model switch. An unknown or disabled key falls back to
    the default, exactly as before.
    """
    key = _requested_model(request)
    model = AIModel.objects.filter(key=key, is_active=True).first() if key else None
    return chat_service.balance_payload(user, ai_model=model)


def _blocked_response(exc: ChatBlocked, user, request=None):
    if exc.code == "not_found":
        # Object-level authorisation: another student's conversation is simply
        # absent as far as this student is concerned.
        return json_error(exc.friendly_message, status=404, code=exc.code)
    if exc.code in BILLING_CODES:
        status = 402
    elif exc.code in RATE_CODES:
        status = 429
    elif exc.code in {"maintenance", "model_paused"}:
        status = 503          # the service, not the student, is the reason
    else:
        status = 400
    payload = dict(getattr(exc, "extra", {}) or {})
    payload.update(_balance_for(request, user) if request is not None else chat_service.balance_payload(user))
    return json_error(exc.friendly_message, status=status, code=exc.code, **payload)


TYPICAL_CACHE_KEY = "chat:typical-answer-tokens"


def typical_answer_tokens() -> dict[str, int]:
    """What one answer from each model usually costs, from the last 30 days.

    The median of what was actually charged, per model — so the picker can
    say "≈ 900 tokens per answer" from real use rather than a guess. Needs a
    handful of answers before it says anything; cached for half an hour.
    """
    cached = cache.get(TYPICAL_CACHE_KEY)
    if cached is not None:
        return cached
    since = timezone.now() - timedelta(days=30)
    result = {}
    for key in AIModel.objects.filter(is_active=True).values_list("key", flat=True):
        values = sorted(
            AIUsageLog.objects.filter(model_key=key, success=True, created_at__gte=since, tokens_charged__gt=0)
            .order_by("-id").values_list("tokens_charged", flat=True)[:400]
        )
        if len(values) >= 5:
            result[key] = values[len(values) // 2]
    cache.set(TYPICAL_CACHE_KEY, result, 1800)
    return result


def shell_context(request, *, selected_key: str = "") -> dict:
    """Everything the application shell needs, whatever is showing inside it.

    The chat page and every panel (Usage, Pricing, Help, …) render the *same*
    shell, so this has to be computed once, in one place: two copies of the
    sidebar would drift, and a panel rendered without the token meter would
    look like a different product.
    """
    user = request.user

    conversations = list(
        Conversation.objects.filter(user=user, is_archived=False)
        .only("id", "title", "updated_at", "message_count", "is_pinned", "folder_id")[:200]
    )

    # One row per model the platform can serve, each already carrying this
    # student's balance and — when they cannot use it — the reason why. The
    # picker renders these; the backend re-checks every one of them on send.
    model_rows = [row for row in wallet_service.overview(user) if row["model"].is_active]
    usable_rows = [row for row in model_rows if row["usable"]]
    # What the picker says about each model beyond its name: what one answer
    # usually costs, and anything an administrator has flagged about it.
    typical = typical_answer_tokens()
    for row in model_rows:
        row["typical"] = typical.get(row["model"].key)
        row["notice"] = model_status_service.model_notice(row["model"])
    chat_paused, chat_paused_note = model_status_service.chat_pause()

    # The model new chats start on, chosen in Settings and so the same on
    # every device. An explicit ?model= still wins; a default the student can
    # no longer use is simply skipped.
    preference = ChatPreference.objects.filter(user=user).only("default_model_key", "daily_goal").first()
    default_key = preference.default_model_key if preference else ""
    if default_key and not any(row["model"].key == default_key for row in usable_rows):
        default_key = ""
    selected_key = selected_key or _requested_model(request) or default_key
    active_model = next((row["model"] for row in usable_rows if row["model"].key == selected_key), None)
    if active_model is None:
        active_model = model_router.preferred_model(user)

    tokens = chat_service.balance_payload(user, ai_model=active_model)
    provider = provider_status()

    return {
        "conversations": [
            {
                "obj": conversation,
                "short_time": _short_time(conversation.updated_at),
                "group": _group_label(conversation.updated_at),
            }
            for conversation in conversations
        ],
        "greeting": _greeting(),
        # Just the first name for the greeting: "Good morning, Alex."
        # reads as a person, "Good morning, Alex Kim." reads as a database.
        "first_name": (user.display_name or user.username).split()[0],
        "tokens": tokens,
        # Serialised for the client so the meter can follow the picker
        # without a round trip. Balances only, never anything sensitive.
        "models_json": json.dumps(tokens["models"]),
        # The three ceilings, for the sidebar meter's first paint. Recomputed
        # on the server after every reply — the browser only ever renders it.
        "usage_json": json.dumps(tokens["usage"]),
        "balance": tokens["balance"],
        "provider": provider,
        "models": [row["model"] for row in model_rows],
        "model_rows": model_rows,
        "active_model": active_model,
        "unread_notifications": notification_service.unread_count(user),
        "sharing_enabled": share_service.sharing_enabled(),
        "archived_count": Conversation.objects.filter(user=user, is_archived=True).count(),
        "trash_count": Conversation.all_objects.trashed().filter(user=user).count(),
        "chat_paused": chat_paused,
        "chat_paused_notice": model_status_service.maintenance_notice(chat_paused_note) if chat_paused else None,
        "folders": organise_service.folders_for(user),
        "folder_colours": FOLDER_COLOURS,
        # The environment switch and the administrator's own setting both have
        # to allow it; the setting used to be saved and then never read.
        "vision_enabled": settings.AI_VISION_ENABLED
        and bool(settings_service.get("ai_vision_enabled", True))
        and bool(active_model and active_model.supports_vision),
        "default_model_key": default_key,
        "study_today": study_service.today(user, goal=preference.daily_goal if preference else 0),
        "has_tokens": tokens["balance"] > 0,
        # Every part of this is re-checked server-side on send; it only
        # decides whether the composer starts out enabled.
        "can_chat": bool(usable_rows) and active_model is not None and tokens["balance"] > 0,
        "no_model_reason": (
            "" if usable_rows else "You have no AI tokens left. Open Pricing to top up."
        ),
        "cheapest_plan": Plan.objects.filter(is_active=True).order_by("price").first(),
        "max_image_mb": 5,
        "max_chars": chat_service.MAX_MESSAGE_CHARS,
        "max_files": settings.UPLOAD_MAX_FILES_PER_MESSAGE,
        "max_file_mb": settings.UPLOAD_MAX_DOCUMENT_BYTES // (1024 * 1024),
        "max_total_mb": settings.UPLOAD_MAX_TOTAL_BYTES // (1024 * 1024),
        "accept_attr": ",".join(f".{ext}" for ext in accepted_extensions()),
        "voice_enabled": settings.AI_VOICE_ENABLED and provider["configured"],
        "voice_max_seconds": settings.AI_TRANSCRIBE_MAX_SECONDS,
        "speech_enabled": settings.AI_SPEECH_ENABLED and bool(settings.OPENAI_API_KEY),
        "image_enabled": image_service.is_available(),
        # The edit button only appears when the configured vendor can
        # actually do it — an action that will certainly fail is worse
        # than one that is not offered.
        "image_edit_enabled": image_service.can_edit(),
        # The detection rules, so the composer's waiting label can agree with
        # the decision the server will actually make. Cosmetic only — nothing
        # about routing or billing depends on what the browser concluded.
        "image_hints_json": json.dumps(image_service.client_hints()),
        # Answers written word by word. The browser still checks it can read
        # a stream, and the server re-checks the setting on every request.
        "chat_streaming": bool(settings_service.get("chat_streaming", True)),
        # Search by meaning in the palette: switched on and a key to embed with.
        "semantic_search": embedding_service.is_enabled(),
    }


@login_required_student
def chat(request, conversation_id: int | None = None):
    user = request.user

    active = None
    chat_messages = []
    if conversation_id is not None:
        active = Conversation.objects.filter(pk=conversation_id, user=user).first()
        if active is None:
            return redirect("student:chat")
        chat_messages = list(
            active.messages.all()
            # The image request carries the prompt that was actually drawn,
            # which is what "generate again" and "copy prompt" need — and is
            # often not quite what the student typed.
            .prefetch_related("attachments", "image_requests")
            .order_by("created_at", "id")
        )
        _mark_model_switches(chat_messages)
        # The student's own verdicts, so a thumb they pressed stays pressed.
        verdicts = library_service.ratings_for(user, [m.pk for m in chat_messages])
        saved = organise_service.bookmarked_ids(user, [m.pk for m in chat_messages])
        for message in chat_messages:
            message.my_rating = verdicts.get(message.pk, {}).get("rating", 0)
            message.is_saved = message.pk in saved
            message.show_suggestions = False
        # Suggestions only under the newest answer: older ones were overtaken
        # by the conversation, and offering them again would be noise.
        if chat_messages and chat_messages[-1].role == MessageRole.ASSISTANT:
            chat_messages[-1].show_suggestions = bool(chat_messages[-1].suggestions)

    context = shell_context(request)
    context.update(
        {
            "nav_active": "chat",
            "active_conversation": active,
            "chat_messages": chat_messages,
            # Orientation is shown to someone who has not chatted yet. The
            # dismissal itself is remembered in the browser.
            "is_new_user": not Conversation.objects.filter(user=user)
            .exclude(pk=getattr(active, "pk", None))
            .exists()
            and not chat_messages,
        }
    )
    return render(request, "chat/chat.html", context)


# ---------------------------------------------------------------------------
# Live answers
# ---------------------------------------------------------------------------
def _wants_stream(request) -> bool:
    """The browser asked for the answer word by word, and it is switched on."""
    return request.POST.get("stream") == "1" and bool(settings_service.get("chat_streaming", True))


def _failure(exc, request, user, *, conversation=None):
    """The JSON a failed answer request returns, for the streamed path.

    Mirrors the ``except`` chains of the plain endpoints, so a streamed
    request that fails tells the browser exactly what a plain one would.
    """
    if isinstance(exc, ModelUnavailable):
        return json_error(exc.friendly_message, status=409, code=exc.code)
    if isinstance(exc, ChatBlocked):
        return _blocked_response(exc, user, request)
    if isinstance(exc, AIError):
        extra = {}
        if conversation is not None:
            extra = {"conversation_id": conversation.pk, **_balance_for(request, user)}
        return json_error(exc.friendly_message, status=502, code=exc.code, retryable=exc.retryable, **extra)
    logger.exception("Unhandled streamed chat failure for user=%s", user.pk)
    return json_error("Something went wrong. Please try again.", status=500, code="server_error")


def _stream(request, job, *, conversation, with_conversation: bool = True):
    """Answer ``job`` as a live stream (see :mod:`AI.services.stream_service`)."""
    user = request.user

    def finish(result: dict) -> dict:
        if with_conversation:
            result["conversation"] = _conversation_payload(conversation)
        return {"ok": True, **result}

    def fail(exc: Exception) -> tuple[int, dict]:
        response = _failure(exc, request, user, conversation=conversation)
        return response.status_code, json.loads(response.content)

    return stream_service.respond(user, job, finish=finish, fail=fail, conversation_id=conversation.pk)


def _hold_uploads(attachments) -> None:
    """Read uploads into memory before a worker thread takes them.

    Django deletes a large upload's temporary file when the request ends, and
    with a live answer the request can end (the tab closed) while the worker
    is still saving it. The total is capped by UPLOAD_MAX_TOTAL_BYTES.
    """
    if not stream_service.threaded():
        return
    for item in attachments:
        item.file.seek(0)
        item.file = ContentFile(item.file.read(), name=getattr(item.file, "name", "") or item.name)


@login_required_student
@require_POST
def stream_stop(request, stream_id: str):
    """Stop a live answer. What was written so far is kept and charged."""
    if not stream_service.request_stop(request.user, stream_id):
        return json_error("That answer has already finished.", status=404, code="not_found")
    return json_ok()


@login_required_student
@require_POST
def stream_presence(request, stream_id: str):
    """The tab was hidden or closed while an answer was written, or came back.

    Sent with ``navigator.sendBeacon`` so it arrives even from a closing tab;
    an answer that finishes while the student is away is announced with a
    browser notification (if they turned those on).
    """
    stream_service.set_presence(request.user, stream_id, away=request.POST.get("state") != "back")
    return json_ok()


@login_required_student
@require_POST
def send(request):
    """Send one message and return the assistant reply as JSON."""
    user = request.user

    try:
        ratelimit.enforce("ai_request", str(user.pk))
    except RateLimited as exc:
        return json_error(exc.friendly_message, status=429, code="rate_limited", retry_after=exc.retry_after)

    text = (request.POST.get("message") or "").strip()
    conversation_id = request.POST.get("conversation_id") or ""

    # "files" is the new multi-upload field; "image" is kept so an older tab
    # that is still open keeps working.
    uploads = request.FILES.getlist("files") or request.FILES.getlist("image")
    try:
        attachments = prepare_many(uploads, limit=settings.UPLOAD_MAX_FILES_PER_MESSAGE)
    except InvalidUpload as exc:
        return json_error(str(exc), status=400, code="bad_upload")

    if not text and not attachments:
        return json_error("Type a message or attach a file to send.", code="empty")

    # The browser names each question. A retry after a dropped connection
    # carries the same name; if the first attempt got through, the answer is
    # already here (or on its way), and asking again would charge twice.
    client_id = (request.POST.get("client_id") or "").strip()[:64]
    earlier = chat_service.find_by_client_id(user, client_id)
    if earlier is not None:
        return json_error(
            "That message already reached us — its answer is in this chat.",
            status=409, code="already_sent", retryable=False,
            conversation_id=earlier.conversation_id, **_balance_for(request, user),
        )

    try:
        if conversation_id:
            conversation = chat_service.get_conversation(user, conversation_id)
        else:
            conversation = chat_service.create_conversation(user, chat_service.build_title(text))
            folder_id = (request.POST.get("folder_id") or "").strip()
            if folder_id.isdigit():
                try:
                    conversation = organise_service.move_conversation(user, conversation.pk, int(folder_id))
                except organise_service.OrganiseError:
                    pass            # a stale folder id just leaves it unfiled
    except ChatBlocked as exc:
        return json_error(exc.friendly_message, status=404, code=exc.code)

    # "Change this picture" arrives on the same endpoint as everything else,
    # carrying the id of the image it means. Keeping it here rather than on a
    # route of its own means one rate limit, one guard, one error shape.
    edit_source = (request.POST.get("edit_attachment") or "").strip()
    if edit_source.isdigit():
        try:
            result = chat_service.edit_image_reply(
                user, conversation, text, int(edit_source),
                model_key=_requested_model(request),
            )
        except ChatBlocked as exc:
            return _blocked_response(exc, user, request)
        except AIError as exc:
            return json_error(
                exc.friendly_message, status=502, code=exc.code, retryable=exc.retryable,
                conversation_id=conversation.pk, **_balance_for(request, user),
            )
        except Exception:  # pragma: no cover — never leak a traceback
            logger.exception("Unhandled image edit for user=%s", user.pk)
            return json_error("Something went wrong. Please try again.", status=500, code="server_error")
        result["conversation"] = _conversation_payload(conversation)
        return json_ok(**result)

    if _wants_stream(request):
        _hold_uploads(attachments)
        model_key = _requested_model(request)
        variation = request.POST.get("image_variation") == "1"

        def job(run):
            return chat_service.send_message(
                user, conversation, text, attachments=attachments, model_key=model_key,
                variation=variation, client_id=client_id,
                on_delta=run.delta, cancel=run.token, deferred=run.deferred,
            )

        return _stream(request, job, conversation=conversation)

    try:
        result = chat_service.send_message(
            user, conversation, text,
            attachments=attachments,
            model_key=_requested_model(request),
            # A hint, not an instruction: it only reaches the image endpoint,
            # and only when the message was going there anyway.
            variation=request.POST.get("image_variation") == "1",
            client_id=client_id,
        )
    except ModelUnavailable as exc:
        return json_error(exc.friendly_message, status=409, code=exc.code)
    except ChatBlocked as exc:
        return _blocked_response(exc, user, request)
    except AIError as exc:
        return json_error(
            exc.friendly_message,
            status=502,
            code=exc.code,
            retryable=exc.retryable,
            conversation_id=conversation.pk,
            **_balance_for(request, user),
        )
    except Exception:  # pragma: no cover — never leak a traceback to the UI
        logger.exception("Unhandled chat failure for user=%s", user.pk)
        return json_error("Something went wrong. Please try again.", status=500, code="server_error")

    result["conversation"] = _conversation_payload(conversation)
    return json_ok(**result)


@login_required_student
@require_POST
def regenerate(request, conversation_id: int):
    user = request.user
    try:
        ratelimit.enforce("ai_request", str(user.pk))
    except RateLimited as exc:
        return json_error(exc.friendly_message, status=429, code="rate_limited")

    if _wants_stream(request):
        try:
            conversation = chat_service.get_conversation(user, conversation_id)
        except ChatBlocked as exc:
            return _blocked_response(exc, user, request)
        model_key = _requested_model(request)
        return _stream(
            request,
            lambda run: chat_service.regenerate(
                user, conversation, model_key=model_key, on_delta=run.delta, cancel=run.token,
            ),
            conversation=conversation, with_conversation=False,
        )

    try:
        conversation = chat_service.get_conversation(user, conversation_id)
        result = chat_service.regenerate(user, conversation, model_key=_requested_model(request))
    except ModelUnavailable as exc:
        return json_error(exc.friendly_message, status=409, code=exc.code)
    except ChatBlocked as exc:
        return _blocked_response(exc, user, request)
    except AIError as exc:
        return json_error(exc.friendly_message, status=502, code=exc.code, retryable=exc.retryable)
    except Exception:  # pragma: no cover
        logger.exception("Regenerate failed for user=%s", user.pk)
        return json_error("Something went wrong. Please try again.", status=500, code="server_error")

    return json_ok(**result)


@login_required_student
@require_POST
def edit_message(request, conversation_id: int, message_id: int):
    """Rewrite one of the student's messages and answer again from there."""
    user = request.user
    try:
        ratelimit.enforce("ai_request", str(user.pk))
    except RateLimited as exc:
        return json_error(exc.friendly_message, status=429, code="rate_limited")

    text = (request.POST.get("message") or "").strip()
    if not text:
        try:
            text = (json.loads(request.body or "{}").get("message") or "").strip()
        except ValueError:
            text = ""

    if _wants_stream(request):
        try:
            conversation = chat_service.get_conversation(user, conversation_id)
        except ChatBlocked as exc:
            return _blocked_response(exc, user, request)
        model_key = _requested_model(request)
        return _stream(
            request,
            lambda run: chat_service.edit_and_resend(
                user, conversation, message_id, text, model_key=model_key,
                on_delta=run.delta, cancel=run.token,
            ),
            conversation=conversation,
        )

    try:
        conversation = chat_service.get_conversation(user, conversation_id)
        result = chat_service.edit_and_resend(
            user, conversation, message_id, text, model_key=_requested_model(request)
        )
    except ModelUnavailable as exc:
        return json_error(exc.friendly_message, status=409, code=exc.code)
    except ChatBlocked as exc:
        return _blocked_response(exc, user, request)
    except AIError as exc:
        return json_error(exc.friendly_message, status=502, code=exc.code, retryable=exc.retryable)
    except Exception:  # pragma: no cover
        logger.exception("Edit failed for user=%s", user.pk)
        return json_error("Something went wrong. Please try again.", status=500, code="server_error")

    result["conversation"] = _conversation_payload(conversation)
    return json_ok(**result)


@login_required_student
@require_POST
def transcribe(request):
    """Speech to text. The language is detected from the audio, not chosen."""
    user = request.user
    try:
        ratelimit.enforce("transcribe", str(user.pk), limit=30, window=300)
    except RateLimited as exc:
        return json_error(exc.friendly_message, status=429, code="rate_limited")

    upload = request.FILES.get("audio")
    if upload is None:
        return json_error("No recording was received.", code="empty_audio")

    audio = upload.read()
    mime = (getattr(upload, "content_type", "") or "audio/webm").split(";")[0]
    name = (getattr(upload, "name", "") or "speech.webm")[:80]

    try:
        # Whichever model they are working in is the balance that pays, so the
        # composer sends its current selection along with the recording.
        result = chat_service.transcribe_audio(
            user, audio, filename=name, mime=mime, model_key=_requested_model(request)
        )
    except ChatBlocked as exc:
        return _blocked_response(exc, user, request)
    except AIError as exc:
        return json_error(exc.friendly_message, status=502, code=exc.code, retryable=exc.retryable)
    except Exception:  # pragma: no cover
        logger.exception("Transcription failed for user=%s", user.pk)
        return json_error("That recording could not be transcribed.", status=500, code="server_error")

    return json_ok(**result)


@login_required_student
@require_POST
def speak(request):
    """Read one reply aloud. Returns audio, not JSON, so it can stream straight
    into an ``<audio>`` element."""
    user = request.user
    try:
        ratelimit.enforce("transcribe", f"tts:{user.pk}", limit=40, window=300)
    except RateLimited as exc:
        return json_error(exc.friendly_message, status=429, code="rate_limited")

    text = (request.POST.get("text") or "").strip()
    if not text:
        return json_error("There is nothing to read out.", code="empty")

    try:
        result = chat_service.speak_text(user, text, model_key=_requested_model(request))
    except ChatBlocked as exc:
        return _blocked_response(exc, user, request)
    except AIError as exc:
        return json_error(exc.friendly_message, status=502, code=exc.code, retryable=exc.retryable)
    except Exception:  # pragma: no cover
        logger.exception("Speech failed for user=%s", user.pk)
        return json_error("That reply could not be read out.", status=500, code="server_error")

    speech = result["speech"]
    response = HttpResponse(speech.data, content_type=speech.mime)
    response["Cache-Control"] = "no-store"
    response["X-Tokens-Charged"] = str(result["tokens_charged"])
    response["X-Token-Balance"] = str(result["balance"])
    return response


@login_required_student
def balance(request):
    """Current token position — used to resync a tab after an aborted request."""
    return json_ok(**_balance_for(request, request.user))


@login_required_student
@require_POST
def new_conversation(request):
    conversation = chat_service.create_conversation(request.user)
    return json_ok(conversation=_conversation_payload(conversation))


@login_required_student
@require_POST
def rename_conversation(request, conversation_id: int):
    conversation = Conversation.objects.filter(pk=conversation_id, user=request.user).first()
    if conversation is None:
        return json_error("Conversation not found.", status=404, code="not_found")

    try:
        payload = json.loads(request.body or "{}")
    except ValueError:
        payload = {}
    title = (payload.get("title") or request.POST.get("title") or "").strip()

    if not title:
        return json_error("Enter a name for this chat.", code="empty_title")

    conversation.title = title[:160]
    conversation.save(update_fields=["title", "updated_at"])
    return json_ok(conversation=_conversation_payload(conversation))


@login_required_student
@require_POST
def delete_conversation(request, conversation_id: int):
    """Move a chat to Recently deleted, from where it can be restored."""
    conversation = trash_service.trash(request.user, conversation_id)
    if conversation is None:
        return json_error("That chat no longer exists.", status=404, code="not_found")
    return json_ok(deleted=conversation_id, trashed=True, restore_days=trash_service.TRASH_DAYS)


@login_required_student
@require_POST
def restore_conversation(request, conversation_id: int):
    conversation = trash_service.restore(request.user, conversation_id)
    if conversation is None:
        return json_error("That chat is no longer in Recently deleted.", status=404, code="not_found")
    return json_ok(conversation=_conversation_payload(conversation))


@login_required_student
@require_POST
def purge_conversation(request, conversation_id: int):
    """Delete a chat for good — only one that is already in the trash."""
    if not trash_service.purge_for(request.user, conversation_id):
        return json_error("Only chats in Recently deleted can be deleted for good.", status=404, code="not_found")
    return json_ok(purged=conversation_id)


@login_required_student
@require_POST
def branch_conversation(request, conversation_id: int, message_id: int):
    """Continue in a new chat from one answer."""
    try:
        branch = chat_service.branch_from(request.user, conversation_id, message_id)
    except ChatBlocked as exc:
        return json_error(exc.friendly_message, status=404, code=exc.code)
    return json_ok(conversation=_conversation_payload(branch), url=reverse("student:chat_detail", args=[branch.pk]))


def _flag(request, name: str):
    """An explicit true/false from the request body, or ``None`` to toggle.

    Explicit values make these actions safe to repeat: a second click or a
    stale tab sets the same state again instead of flipping it back.
    """
    try:
        payload = json.loads(request.body or "{}")
    except ValueError:
        payload = {}
    value = payload.get(name, request.POST.get(name))
    if value is None or value == "":
        return None
    return value in (True, 1, "1", "true", "on")


@login_required_student
@require_POST
def pin_conversation(request, conversation_id: int):
    conversation = Conversation.objects.filter(pk=conversation_id, user=request.user).first()
    if conversation is None:
        return json_error("That chat no longer exists.", status=404, code="not_found")
    wanted = _flag(request, "pinned")
    conversation.is_pinned = (not conversation.is_pinned) if wanted is None else wanted
    conversation.save(update_fields=["is_pinned"])
    return json_ok(conversation=_conversation_payload(conversation), pinned=conversation.is_pinned)


@login_required_student
def conversation_messages(request, conversation_id: int):
    """Load a conversation without a full page reload."""
    conversation = Conversation.objects.filter(pk=conversation_id, user=request.user).first()
    if conversation is None:
        return json_error("Conversation not found.", status=404, code="not_found")

    # Everything a freshly delivered reply carries — pictures, rating, saved
    # state, suggestions — or a reply picked up by a resync comes back bare.
    rows = list(
        conversation.messages.all().order_by("created_at", "id")
        .select_related("feedback").prefetch_related("attachments")
    )
    saved = set(
        MessageBookmark.objects.filter(user=request.user, message__in=rows).values_list("message_id", flat=True)
    )
    payload = []
    for message in rows:
        feedback = getattr(message, "feedback", None)
        payload.append({
            "id": message.pk,
            "role": message.role,
            "content": message.content,
            "image_url": message.image.url if message.image else "",
            "attachments": chat_service.attachment_payload(message),
            "created_at": timezone.localtime(message.created_at).strftime("%H:%M"),
            "total_tokens": message.total_tokens,
            "model": message.model_key or message.model,
            "rating": feedback.rating if feedback else 0,
            "saved": message.pk in saved,
            "suggestions": list(message.suggestions or []),
        })
    return json_ok(
        conversation=_conversation_payload(conversation),
        messages=payload,
        **_balance_for(request, request.user),
    )


@login_required_student
def search_conversations(request):
    """Chats whose title or messages mention ``q``, each with where it matched.

    A match inside a message carries a short excerpt around the words and the
    message's id, so the command palette can show *why* a chat came up and
    jump straight to that message.
    """
    query = (request.GET.get("q") or "").strip()[:100]
    # Optional filters from the palette: how far back, and archived chats too.
    include_archived = request.GET.get("archived") == "1"
    days = request.GET.get("days") or ""
    since = timezone.now() - timedelta(days=int(days)) if days in {"7", "30", "90", "365"} else None

    conversations = Conversation.objects.filter(user=request.user)
    messages_scope = Message.objects.filter(conversation__user=request.user, conversation__deleted_at__isnull=True)
    if not include_archived:
        conversations = conversations.filter(is_archived=False)
        messages_scope = messages_scope.filter(conversation__is_archived=False)
    if since is not None:
        conversations = conversations.filter(updated_at__gte=since)
        messages_scope = messages_scope.filter(created_at__gte=since)

    hits: dict[int, Message] = {}
    saved = []
    if query:
        for message in (
            messages_scope.filter(content__icontains=query)
            .only("id", "conversation_id", "content", "role")
            .order_by("-id")[:400]
        ):
            hits.setdefault(message.conversation_id, message)
        conversations = conversations.filter(Q(title__icontains=query) | Q(pk__in=list(hits)[:100]))
        # Answers the student kept, found by their text or their note.
        for bookmark in organise_service.bookmarks_for(request.user, query=query)[:4]:
            saved.append({
                "message_id": bookmark.message_id,
                "conversation_id": bookmark.message.conversation_id,
                "title": bookmark.message.conversation.title,
                "snippet": _snippet(bookmark.note or bookmark.message.content, query),
            })

    results = []
    for conversation in conversations[:60]:
        payload = _conversation_payload(conversation)
        payload["is_archived"] = conversation.is_archived
        hit = hits.get(conversation.pk)
        if hit is not None:
            payload["snippet"] = _snippet(hit.content, query)
            payload["message_id"] = hit.pk
        results.append(payload)
    return json_ok(conversations=results, saved=saved)


def _snippet(text: str, query: str, width: int = 70) -> str:
    """About ``2 * width`` characters of ``text`` centred on ``query``."""
    flat = " ".join((text or "").split())
    at = flat.lower().find(query.lower())
    if at < 0:
        return flat[: width * 2]
    start = max(0, at - width)
    end = min(len(flat), at + len(query) + width)
    return ("\u2026" if start else "") + flat[start:end] + ("\u2026" if end < len(flat) else "")


@login_required_student
@require_POST
def archive_conversation(request, conversation_id: int):
    """Move a chat out of the sidebar without deleting it."""
    conversation = Conversation.objects.filter(pk=conversation_id, user=request.user).first()
    if conversation is None:
        return json_error("That chat no longer exists.", status=404, code="not_found")

    wanted = _flag(request, "archived")
    conversation.is_archived = (not conversation.is_archived) if wanted is None else wanted
    conversation.save(update_fields=["is_archived", "updated_at"])
    return json_ok(
        conversation=_conversation_payload(conversation),
        archived=conversation.is_archived,
    )


@login_required_student
def export_conversation(request, conversation_id: int):
    """Download a chat to keep or hand in.

    ``?format=`` picks the shape: ``md`` (the default — the assistant already
    answers in Markdown, so it opens cleanly anywhere), ``txt`` for somewhere
    that shows formatting as clutter, and ``print`` for a clean page the
    browser can print or save as a PDF.
    """
    conversation = (
        Conversation.objects.filter(pk=conversation_id, user=request.user)
        .prefetch_related("messages__attachments")
        .first()
    )
    if conversation is None:
        raise Http404("Conversation not found.")

    rows = list(conversation.messages.all().order_by("created_at", "id"))
    fmt = (request.GET.get("format") or "md").lower()
    exported = timezone.localtime(timezone.now()).strftime("%d %B %Y at %H:%M")
    started = timezone.localtime(conversation.created_at).strftime("%d %B %Y, %H:%M")

    if fmt == "print":
        transcript = [
            {
                "role": message.role,
                "content": message.content or "",
                "when": timezone.localtime(message.created_at).strftime("%d %b %Y, %H:%M"),
                "model": (message.model_key or message.model) if message.role == "assistant" else "",
                "files": [f.name for f in message.attachments.all()],
            }
            for message in rows
        ]
        return render(request, "student/chat_print.html", {
            "conversation": conversation, "transcript": transcript,
            "exported": exported, "started": started,
        })

    plain = fmt == "txt"
    lines = [
        conversation.title if plain else f"# {conversation.title}",
        "",
        (f"Exported from {settings.BRAND['full_name']} on {exported}" if plain
         else f"*Exported from {settings.BRAND['full_name']} on {exported}*"),
        "",
        f"- Started: {started}",
        f"- Messages: {conversation.message_count}",
        f"- Tokens used: {conversation.total_tokens:,}",
        "",
        "-" * 40 if plain else "---",
        "",
    ]

    for message in rows:
        stamp = timezone.localtime(message.created_at).strftime("%d %b %Y, %H:%M")
        who = "You" if message.role == "user" else "Assistant"
        lines.append(f"{who} · {stamp}" if plain else f"## {who} · {stamp}")
        lines.append("")

        files = list(message.attachments.all())
        if files:
            names = ", ".join(f.name for f in files) if plain else ", ".join(f"`{f.name}`" for f in files)
            lines.append(f"Attached: {names}" if plain else f"**Attached:** {names}")
            lines.append("")

        lines.append(message.content or ("(no text)" if plain else "*(no text)*"))
        lines.append("")
        if message.role == "assistant" and message.total_tokens:
            meta = f"{message.model_key or message.model} · {message.total_tokens:,} tokens"
            lines.append(f"[{meta}]" if plain else f"<sub>{meta}</sub>")
            lines.append("")
        lines.append("-" * 40 if plain else "---")
        lines.append("")

    # A filename built from the title, reduced to characters that are safe on
    # every filesystem and in a Content-Disposition header.
    slug = "".join(
        char if char.isalnum() or char in " -_" else "" for char in conversation.title
    ).strip().replace(" ", "-")[:60] or "conversation"

    kind = "text/plain" if plain else "text/markdown"
    response = HttpResponse("\n".join(lines), content_type=f"{kind}; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{slug}.{"txt" if plain else "md"}"'
    return response


# ---------------------------------------------------------------------------
# Answer feedback and the prompt library
# ---------------------------------------------------------------------------
@login_required_student
@require_POST
def feedback(request, message_id: int):
    """Thumbs up, thumbs down (with an optional reason), or clear the verdict."""
    try:
        verdict = library_service.rate_message(
            request.user, message_id, request.POST.get("rating"),
            reason=(request.POST.get("reason") or "").strip(),
            # Absent means "leave it": choosing a reason is a second request.
            comment=request.POST.get("comment"),
        )
    except library_service.LibraryError as exc:
        status = 404 if exc.code == "not_found" else 400
        return json_error(exc.friendly_message, status=status, code=exc.code)
    return json_ok(
        rating=verdict.rating if verdict else 0,
        reason=verdict.reason if verdict else "",
    )


@login_required_student
def prompts_api(request):
    """The composer's prompt menu: featured prompts, then the student's own.

    POST saves the composer's text as a new prompt of the student's own.
    """
    user = request.user
    if request.method == "POST":
        try:
            prompt = library_service.create_prompt(
                user,
                title=request.POST.get("title") or "",
                body=request.POST.get("body") or "",
                category=request.POST.get("category") or "",
            )
        except library_service.LibraryError as exc:
            return json_error(exc.friendly_message, status=400, code=exc.code)
        return json_ok(prompt=library_service.prompt_payload(prompt, user))

    library = library_service.prompts_for(user)
    return json_ok(
        featured=[library_service.prompt_payload(p, user) for p in library["featured"]],
        mine=[library_service.prompt_payload(p, user) for p in library["mine"]],
    )


@login_required_student
@require_POST
def prompt_use(request, prompt_id: int):
    try:
        prompt = library_service.record_use(request.user, prompt_id)
    except library_service.LibraryError as exc:
        return json_error(exc.friendly_message, status=404, code=exc.code)
    return json_ok(prompt=library_service.prompt_payload(prompt, request.user))



@login_required_student
def share_api(request, conversation_id: int):
    """GET: is this conversation shared? POST action=share|revoke: change it."""
    try:
        if request.method == "POST":
            if request.POST.get("action") == "revoke":
                share_service.revoke(request.user, conversation_id)
                return json_ok(shared=False, url="")
            link = share_service.share(request.user, conversation_id)
        else:
            link = share_service.status(request.user, conversation_id)
    except share_service.ShareError as exc:
        status = 404 if exc.code == "not_found" else 400
        return json_error(exc.friendly_message, status=status, code=exc.code)
    if link is None:
        return json_ok(shared=False, url="", enabled=share_service.sharing_enabled())
    url = request.build_absolute_uri(reverse("public:shared_chat", args=[link.token]))
    return json_ok(shared=True, url=url, views=link.view_count, enabled=True)



# ---------------------------------------------------------------------------
# Folders and saved answers
# ---------------------------------------------------------------------------
def _folder_error(exc):
    return json_error(exc.friendly_message, status=404 if exc.code == "not_found" else 400, code=exc.code)


@login_required_student
def folders_api(request):
    """GET: the student's folders. POST: create one."""
    user = request.user
    if request.method == "POST":
        try:
            folder = organise_service.create_folder(
                user, name=request.POST.get("name", ""), colour=request.POST.get("colour", ""),
                instructions=request.POST.get("instructions", ""),
            )
        except organise_service.OrganiseError as exc:
            return _folder_error(exc)
        folder.count = 0
        return json_ok(folder=organise_service.folder_payload(folder))
    return json_ok(folders=[organise_service.folder_payload(f) for f in organise_service.folders_for(user)])


@login_required_student
@require_POST
def folder_api(request, folder_id: int):
    """action=update (name, colour, instructions) or action=delete."""
    user = request.user
    try:
        if request.POST.get("action") == "delete":
            moved = organise_service.delete_folder(user, folder_id)
            return json_ok(deleted=True, unfiled=moved)
        folder = organise_service.update_folder(
            user, folder_id, name=request.POST.get("name", ""), colour=request.POST.get("colour", ""),
            instructions=request.POST.get("instructions", ""),
        )
    except organise_service.OrganiseError as exc:
        return _folder_error(exc)
    return json_ok(folder=organise_service.folder_payload(folder))


@login_required_student
@require_POST
def conversation_folder(request, conversation_id: int):
    raw = (request.POST.get("folder_id") or "").strip()
    try:
        conversation = organise_service.move_conversation(
            request.user, conversation_id, int(raw) if raw.isdigit() else None,
        )
    except organise_service.OrganiseError as exc:
        return _folder_error(exc)
    return json_ok(conversation=_conversation_payload(conversation))


@login_required_student
@require_POST
def bookmark(request, message_id: int):
    try:
        saved = organise_service.toggle_bookmark(request.user, message_id)
    except organise_service.OrganiseError as exc:
        return _folder_error(exc)
    return json_ok(saved=saved)
