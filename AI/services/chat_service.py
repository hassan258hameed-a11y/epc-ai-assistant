"""Chat orchestration: conversations, provider calls and usage accounting.

Order of operations for a message is deliberate:

1. Guard (account active, rate limit, message shape).
2. Route: pick the model, check the student may use *that* model, check they
   hold enough of *that model's* tokens, then build its vendor client.
3. Persist the student's message.
4. Call the provider.
5. On failure: log it, charge nothing, keep the student's message.
6. On success: persist the reply and charge the *actual* usage reported, to the
   wallet belonging to the model that answered.

Step 5 is why a failed request never costs a student anything: the only debit
here happens after a response with real usage numbers.

Step 6 is why there is no cross-provider leakage: the wallet is selected from
``route.model``, which is the same object that produced the vendor client, so
the model that answered and the balance that paid are the same row by
construction rather than by convention.
"""
from __future__ import annotations

import logging
import math

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import F, Q
from django.db.models.functions import Greatest
from django.utils import timezone

from AI.models import (
    AIUsageLog,
    AttachmentKind,
    Conversation,
    ImageRequest,
    Message,
    MessageAttachment,
    MessageRole,
    MessageStatus,
)
from AI.services import (
    followup_service,
    image_service,
    model_router,
    model_service,
    preference_service,
    question_profile,
    settings_service,
    usage_limit_service,
    wallet_service,
)
from AI.services.ai_service import AIError, ChatTurn
from AI.services.model_service import ModelUnavailable
from AI.services.providers.base import STOPPED
from AI.services.token_service import InsufficientTokens
from AI.services.usage_limit_service import UsageLimitReached
from AI.services.wallet_service import InsufficientModelTokens, ModelAccessDenied
from AI.utils import extract

logger = logging.getLogger("AI.chat")

#: Room reserved for the reply when deciding whether a request can be afforded.
#: Below this a student would pay for a prompt and get a truncated answer.
#: Sized against the free grant rather than against a full plan: a new account
#: has to be able to actually send its first message.
REPLY_HEADROOM_TOKENS = 120

MAX_MESSAGE_CHARS = 12000


class ChatBlocked(Exception):
    """Request refused before any provider call was made."""

    def __init__(self, message: str, *, code: str = "blocked", **extra):
        self.friendly_message = message
        self.code = code
        self.extra = extra
        super().__init__(message)


def min_request_tokens() -> int:
    return max(1, int(getattr(settings, "AI_MIN_REQUEST_TOKENS", 500)))


def estimate_tokens(text: str) -> int:
    """Cheap upper-ish bound on prompt size, ~4 characters per token.

    Used only to decide whether a request is affordable *before* it is sent.
    Billing always uses the provider's reported numbers, never this.
    """
    return math.ceil(len(text or "") / 4)


def build_title(text: str) -> str:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return "New chat"
    return cleaned[:57] + "…" if len(cleaned) > 60 else cleaned


def get_conversation(user, conversation_id) -> Conversation:
    """Fetch a conversation, enforcing ownership.

    Object-level authorisation: a student can never read another student's chat
    by changing the id in the URL.
    """
    conversation = Conversation.objects.filter(pk=conversation_id, user=user).first()
    if conversation is None:
        raise ChatBlocked("Conversation not found.", code="not_found")
    return conversation


def create_conversation(user, title: str = "New chat") -> Conversation:
    return Conversation.objects.create(user=user, title=title[:160] or "New chat")


#: How the summary is introduced to the model. Kept to one line, because it is
#: re-sent on every request for the rest of the conversation.
SUMMARY_PREFIX = "Earlier in this conversation: "


def context_budget() -> int:
    return max(200, int(settings_service.get("ai_context_token_budget", settings.AI_CONTEXT_TOKEN_BUDGET)))


def history_turns(
    conversation: Conversation, limit: int | None = None, *, until: Message | None = None, exclude=(),
) -> list[ChatTurn]:
    """Recent context, oldest first, sized by tokens rather than by message count.

    Two things decide what gets sent:

    * a **token budget**, because a message count is a poor proxy for cost —
      eight one-line questions and eight pasted stack traces are the same
      number of rows and nowhere near the same price;
    * a **summary** of everything older, so dropping the tail of a long
      conversation loses its bulk without losing its thread.

    Turns are taken newest-first and reversed, so the messages nearest the
    question are the ones that survive — those are the ones it is actually
    about. Failed exchanges are excluded: a request that errored is not part
    of what was said.

    ``until`` ends the window at that message (inclusive) and ``exclude``
    leaves messages out — how a regenerate or an edit sees the conversation as
    it will be, without deleting anything before the new answer exists.
    """
    ceiling = int(limit or settings_service.get("ai_max_context_messages", settings.AI_MAX_CONTEXT_MESSAGES))
    budget = context_budget()

    rows = conversation.messages.filter(status=MessageStatus.OK)
    if until is not None:
        rows = rows.filter(
            Q(created_at__lt=until.created_at) | Q(created_at=until.created_at, id__lte=until.id)
        )
    if exclude:
        rows = rows.exclude(id__in=list(exclude))
    recent = list(
        rows
        .prefetch_related("attachments")
        .order_by("-created_at", "-id")[:ceiling]
    )

    kept: list[ChatTurn] = []
    spent = 0
    dropped_from_window = False
    for msg in recent:
        if msg.role not in {MessageRole.USER, MessageRole.ASSISTANT}:
            continue
        content = compose_turn_content(msg)
        if not content.strip():
            continue
        cost = estimate_tokens(content)
        # Always keep at least one turn: a single message longer than the whole
        # budget still has to be sent, or the model answers with no context at
        # all rather than with less of it.
        if kept and spent + cost > budget:
            dropped_from_window = True
            break
        kept.append(ChatTurn(role=msg.role, content=content))
        spent += cost

    kept.reverse()

    # Anything the ceiling itself cut off is also missing from the window.
    if len(recent) >= ceiling:
        dropped_from_window = True

    summary = (conversation.summary or "").strip()
    if summary and (dropped_from_window or conversation.summary_upto_id):
        # Carried as a user turn rather than a second system prompt: providers
        # differ on how many system messages they accept, and this is context,
        # not instruction.
        kept.insert(0, ChatTurn(role=MessageRole.USER, content=SUMMARY_PREFIX + summary))

    return kept


def _summary_source(conversation: Conversation, keep_recent: int) -> list[Message]:
    """The messages that have fallen out of the window and are not yet summarised."""
    newest_ids = list(
        conversation.messages.filter(status=MessageStatus.OK)
        .order_by("-created_at", "-id")
        .values_list("id", flat=True)[:keep_recent]
    )
    queryset = conversation.messages.filter(status=MessageStatus.OK).exclude(id__in=newest_ids)
    if conversation.summary_upto_id:
        queryset = queryset.filter(id__gt=conversation.summary_upto_id)
    return list(queryset.order_by("created_at", "id"))


def refresh_summary(user, conversation: Conversation, route) -> bool:
    """Fold the messages that no longer fit into the conversation's summary.

    Deliberately lazy and cached. It runs only once enough messages have fallen
    out of the window to be worth a call, it reuses the model that just
    answered (so no second vendor is involved and no second wallet is touched),
    and it caps its own output — a summary that grows without limit costs more
    than the history it replaced.

    Returns whether the summary changed. A failure here is never allowed to
    affect the reply that has already been delivered.
    """
    if not settings_service.get("ai_summarize_history", True):
        return False

    threshold = max(2, int(getattr(settings, "AI_SUMMARY_REFRESH_AFTER", 6)))
    # Keep roughly the window's worth of recent messages out of the summary.
    keep_recent = int(settings_service.get("ai_max_context_messages", settings.AI_MAX_CONTEXT_MESSAGES))
    pending = _summary_source(conversation, keep_recent)
    if len(pending) < threshold:
        return False

    transcript = "\n\n".join(
        f"{'Student' if m.role == MessageRole.USER else 'Assistant'}: "
        f"{(m.content or '').strip()[:1200]}"
        for m in pending
        if (m.content or "").strip()
    )
    if not transcript:
        return False

    previous = (conversation.summary or "").strip()
    instruction = (
        "Summarise this part of a tutoring conversation in at most 80 words. "
        "Keep the topic, any facts about the student, and anything still "
        "unresolved. Drop pleasantries and anything already answered and closed."
    )
    if previous:
        instruction += "\n\nExisting summary to fold in and replace:\n" + previous

    try:
        result = route.provider.complete(
            [ChatTurn(role=MessageRole.USER, content=f"{instruction}\n\n---\n{transcript}")],
            system_prompt="",
            max_tokens=int(getattr(settings, "AI_SUMMARY_MAX_TOKENS", 200)),
        )
    except AIError as exc:
        logger.info("Summary refresh skipped for conversation=%s: %s", conversation.pk, exc.code)
        return False
    except Exception:  # pragma: no cover — a summary must never break a reply
        logger.exception("Summary refresh failed for conversation=%s", conversation.pk)
        return False

    text = (result.content or "").strip()
    if not text:
        return False

    newest = pending[-1].pk
    Conversation.objects.filter(pk=conversation.pk).update(summary=text, summary_upto_id=newest)
    conversation.summary = text
    conversation.summary_upto_id = newest

    if result.total_tokens > 0:
        wallet_service.consume(
            user,
            route.model,
            result.total_tokens,
            reason=f"Context summary — {route.model.name}",
            conversation=conversation,
            clamp=True,
        )
        AIUsageLog.objects.create(
            user=user,
            conversation=conversation,
            model=result.model,
            model_key=route.model.key,
            provider=route.provider.name,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
            tokens_charged=result.total_tokens,
            success=True,
            latency_ms=result.latency_ms,
        )
    return True


def compose_turn_content(message: Message) -> str:
    """A message as the model should see it: what was typed, plus any file text.

    Document text lives on the attachment rather than in ``Message.content``, so
    the student's own bubble shows what they actually wrote — not the contents
    of a forty-page PDF.
    """
    parts = [(message.content or "").strip()]
    for attachment in message.attachments.all():
        if attachment.kind != AttachmentKind.DOCUMENT:
            continue
        result = extract.ExtractionResult(
            attachment.extracted_text, pages=attachment.pages, note=attachment.extraction_note
        )
        parts.append(extract.as_prompt_block(attachment.name, result))
    return "\n\n".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
def _guard_account(user) -> None:
    if not user.is_active_account:
        raise ChatBlocked("Your account is not active. Please contact support.", code="account_disabled")


def _guard_budget(user, ai_model, *, prompt_tokens: int = 0) -> int:
    """Refuse a request *this model's* wallet cannot cover.

    This is the authoritative token limit. It runs on every request, before the
    provider is contacted, and no client-side state can influence it. Every
    message names the model, because "you have run out" is useless advice when
    the student has plenty of tokens for a different one.
    """
    balance = wallet_service.balance_of(user, ai_model)
    label = ai_model.name

    if balance <= 0:
        raise ChatBlocked(
            f"You do not have enough {label} tokens to continue. "
            "Please contact the administrator.",
            code="no_tokens",
            balance=0,
            model=ai_model.key,
            model_name=label,
        )

    floor = min_request_tokens()
    if balance < floor:
        raise ChatBlocked(
            f"You have {balance:,} {label} tokens left — not enough for another reply. "
            "Please contact the administrator.",
            code="low_tokens",
            balance=balance,
            model=ai_model.key,
            model_name=label,
        )

    required = prompt_tokens + REPLY_HEADROOM_TOKENS
    if prompt_tokens and balance < required:
        raise ChatBlocked(
            f"That message needs about {required:,} {label} tokens and you have "
            f"{balance:,} left. Shorten it, start a new chat, or pick another model.",
            code="insufficient_tokens",
            balance=balance,
            model=ai_model.key,
            model_name=label,
        )

    # The window has to cover the prompt *and* an answer. Checking only the
    # prompt is what let a request through on a headroom too small to reply in.
    _guard_usage_window(
        user, ai_model,
        required=max(prompt_tokens + REPLY_HEADROOM_TOKENS, min_request_tokens()),
    )
    return balance


def _guard_usage_window(user, ai_model, *, required: int) -> None:
    """Refuse a request the student owns the tokens for but may not spend yet.

    Deliberately separate from the balance check above and deliberately after
    it, because the two failures mean opposite things to the person reading
    them: one says *buy more*, the other says *come back at half past four*.
    Answering "you have run out" to somebody with 180,000 tokens in the bank
    would be simply untrue.
    """
    try:
        usage_limit_service.check(user, ai_model, required=required)
    except UsageLimitReached as exc:
        raise ChatBlocked(
            exc.friendly_message,
            code="usage_limit",
            model=ai_model.key,
            model_name=ai_model.name,
            windows=exc.windows,
            resets={
                name: {
                    "reset_at": info.get("reset_at", ""),
                    "reset_display": info.get("reset_display", ""),
                    "reset_in_seconds": info.get("reset_in_seconds", 0),
                    "limit": info.get("limit", 0),
                    "used": info.get("used", 0),
                }
                for name, info in exc.resets.items()
            },
        )


#: Refusals that mean "this model has no room", as opposed to "you may not use
#: this model". Only these are worth trying another model for: a disabled model
#: or one the student has no access to would refuse the fallback too, and for
#: the same reason.
_OUT_OF_ROOM = frozenset({"no_tokens", "insufficient_tokens", "usage_limit", "model_paused"})


def _fallback_candidates(user, exclude_key: str):
    """Models this student could actually spend right now, best first.

    Ordered by the catalogue's own order so the choice is predictable — the
    same student in the same state always lands on the same model — rather
    than by whichever happens to have the largest balance, which would move
    around as they spend.
    """
    rows = wallet_service.overview(user)
    return [
        row["model"]
        for row in rows
        if row["usable"] and row["balance"] > 0 and row["model"].key != exclude_key
    ]


def _route_with_fallback(user, model_key: str, *, prompt_tokens: int = 0):
    """The requested model, or the next one that can take the message.

    Returns ``(route, switched_from)``. ``switched_from`` is the model that was
    asked for and could not answer, or None when nothing was switched — which
    is what the transcript uses to draw the divider.
    """
    try:
        return _route(user, model_key, prompt_tokens=prompt_tokens), None
    except ChatBlocked as blocked:
        if blocked.code not in _OUT_OF_ROOM:
            raise
        if not settings_service.get("auto_switch_model", True):
            raise

        original = None
        try:
            original = model_router.resolve_model(user, model_key)
        except Exception:      # pragma: no cover - the refusal below is enough
            raise blocked

        for candidate in _fallback_candidates(user, original.key):
            try:
                route = _route(user, candidate.key, prompt_tokens=prompt_tokens)
            except ChatBlocked:
                continue       # this one is out of room too; try the next
            logger.info(
                "auto-switch user=%s %s -> %s (%s)",
                user.pk, original.key, candidate.key, blocked.code,
            )
            return route, original

        # Nothing else can take it either, so the original refusal stands —
        # it explains the actual problem better than "no model is available".
        raise blocked


def _route(user, model_key: str, *, prompt_tokens: int = 0):
    """Resolve model + access + budget + vendor client, as one decision.

    Every way this can fail is turned into a :class:`ChatBlocked` carrying a
    message that already names the model, so the views need no per-provider
    branches and nothing vendor-specific reaches the browser.
    """
    try:
        route = model_router.route(user, model_key, minimum_tokens=1)
    except ModelAccessDenied as exc:
        raise ChatBlocked(
            exc.friendly_message,
            code="model_forbidden",
            model=exc.ai_model.key,
            model_name=exc.ai_model.name,
        )
    except InsufficientModelTokens as exc:
        raise ChatBlocked(
            exc.friendly_message,
            code="no_tokens",
            balance=exc.balance,
            model=exc.ai_model.key,
            model_name=exc.ai_model.name,
        )

    # An administrator can pause one model (or all chat) during an incident.
    # Checked after access, so a student is never told about a model they
    # could not use anyway.
    from AI.services import model_status_service

    notice = model_status_service.blocks_sending(route.model)
    if notice is not None:
        raise ChatBlocked(
            notice["text"],
            code="maintenance" if notice["level"] == "maintenance" else "model_paused",
            model=route.model.key,
            model_name=route.model.name,
        )

    # The router proves the student may use the model and holds *some* of its
    # tokens; this proves they hold enough for the message actually being sent,
    # and enforces the per-request floor.
    _guard_budget(user, route.model, prompt_tokens=max(prompt_tokens, 0))
    return route


def reply_budget(user, route, *, prompt_tokens: int, profile: str = question_profile.STANDARD) -> int:
    """How long the answer is allowed to be, in tokens.

    Permission is not enough on its own. A request that is affordable when it
    is sent can still cost far more than the student has left, because the
    length of the answer is the vendor's decision, not ours — so the budget
    that goes on the wire is the smaller of what the model is configured for
    and what this student can actually still spend.

    The tighter of the two constraints wins: the tokens they own, and the
    allowance their package lets them spend today. Both already account for the
    prompt, which is paid for as well.
    """
    balance = wallet_service.balance_of(user, route.model)
    snapshot = usage_limit_service.snapshot(user, route.model)
    headroom = balance if not snapshot.metered else min(balance, snapshot.spendable)

    affordable = headroom - max(prompt_tokens, 0)
    # The vendor client's own budget, not a global default: each one is built
    # from its catalogue row, so this is the number that would have been used
    # had nobody capped it.
    configured = (
        route.model.max_output_tokens
        or getattr(route.provider, "max_output_tokens", 0)
        or settings.OPENAI_MAX_OUTPUT_TOKENS
    )
    # A reasoning model's thinking is billed as output and comes out of this
    # same budget. On a maths or code question it gets room to think as well
    # as answer — still never more than the student can actually spend, and
    # only ever used if the thinking needs it.
    if profile == question_profile.DEEP and getattr(route.provider, "is_reasoning_model", False) is True:
        configured = max(configured, question_profile.DEEP_REASONING_BUDGET)
    return max(0, min(configured, affordable))


def _guard_reply_budget(user, route, *, prompt_tokens: int, profile: str = question_profile.STANDARD) -> int:
    """Refuse a request that cannot buy an answer worth reading.

    A vendor handed a budget of forty tokens returns a sentence that stops
    mid-word, and charges for it. Better to say plainly that there is not
    enough left, and say what would change that.

    Reasoning models are the reason this asks the provider rather than doing
    the arithmetic itself: they raise a budget that is too small to think in,
    so the number we request is not always the number we are billed for.
    """
    budget = reply_budget(user, route, prompt_tokens=prompt_tokens, profile=profile)
    if budget < REPLY_HEADROOM_TOKENS:
        _guard_usage_window(user, route.model, required=prompt_tokens + REPLY_HEADROOM_TOKENS)
        raise ChatBlocked(
            f"You do not have enough {route.model.name} tokens left for a reply to that. "
            "Shorten the message, or top up.",
            code="insufficient_tokens",
            balance=wallet_service.balance_of(user, route.model),
            model=route.model.key,
            model_name=route.model.name,
        )

    # What the vendor will really spend, which for a reasoning model can exceed
    # what was asked for. If that no longer fits, the request is refused rather
    # than quietly overshooting.
    #
    # Asked for rather than assumed: a client that does not implement this is
    # taken at its word and given the cap, which is the safe reading — better a
    # request that is capped but unverified than a 500 because one vendor is
    # missing a method.
    describe = getattr(route.provider, "effective_output_budget", None)
    effective = describe(budget) if callable(describe) else budget
    if effective > budget:
        _guard_usage_window(user, route.model, required=prompt_tokens + effective)
    return budget


MAX_ATTACHMENTS = 5


BRANCH_ATTACHMENT_NOTE = "\n\n_(A file attached in the original chat is not copied into this branch.)_"


@transaction.atomic
def branch_from(user, conversation_id, message_id) -> Conversation:
    """A new chat holding this one's messages up to and including one answer.

    For trying another approach from a good point, or carrying on without a
    long, costly history. Text only: attached files stay with the original
    (a note says so), so deleting either chat can never remove the other's
    files. Nothing is billed — copying is not asking.
    """
    source = get_conversation(user, conversation_id)
    anchor = source.messages.filter(pk=message_id).first()
    if anchor is None:
        raise ChatBlocked("That message could not be found.", code="not_found")
    rows = list(
        source.messages.filter(id__lte=anchor.id).order_by("created_at", "id").prefetch_related("attachments")
    )
    branch = Conversation.objects.create(
        user=user, title=f"{source.title} (branch)"[:160], folder=source.folder,
    )
    copies = []
    for message in rows:
        content = message.content
        if message.image or list(message.attachments.all()):
            content = (content or "") + BRANCH_ATTACHMENT_NOTE
        copies.append(Message(
            conversation=branch, role=message.role, content=content, status=message.status,
            model=message.model, model_key=message.model_key,
        ))
    Message.objects.bulk_create(copies)
    Conversation.objects.filter(pk=branch.pk).update(message_count=len(copies))
    branch.refresh_from_db()
    return branch


def find_by_client_id(user, client_id: str) -> Message | None:
    """The student's question already saved under this browser-made id."""
    if not client_id:
        return None
    return (
        Message.objects.filter(conversation__user=user, role=MessageRole.USER, client_id=client_id[:64])
        .select_related("conversation").first()
    )


@transaction.atomic
def _save_user_message(conversation, text, attachments=None, client_id: str = "") -> Message:
    """Persist the student's message and everything they attached to it.

    Document text is extracted once, here, and stored on the attachment — so a
    regenerate or a follow-up question never has to re-parse the file, and a
    file the student later deletes from disk does not silently change history.
    """
    message = Message(
        conversation=conversation,
        role=MessageRole.USER,
        content=text,
        status=MessageStatus.OK,
        client_id=(client_id or "")[:64],
    )

    # The first image also fills the legacy single-image fields, so older
    # templates and the existing usage log keep working unchanged.
    first_image = next((a for a in (attachments or []) if a.kind == AttachmentKind.IMAGE), None)
    if first_image is not None:
        message.image_format = first_image.ext
        message.image_bytes = first_image.size
        message.image = first_image.file
    message.save()

    for item in attachments or []:
        row = MessageAttachment(
            message=message,
            kind=item.kind,
            file=item.file,
            name=item.name[:160],
            ext=item.ext[:8],
            mime=item.mime[:80],
            size=item.size,
        )
        if item.kind == AttachmentKind.DOCUMENT:
            row.extracted_text = item.text or ""
            row.extraction_note = (item.note or "")[:255]
            row.pages = item.pages
        row.save()

    conversation.touch(messages=1)
    return message


# ---------------------------------------------------------------------------
# The exchange
# ---------------------------------------------------------------------------
def _call_provider(provider, turns, *, on_delta=None, cancel=None, **kwargs):
    """``complete`` — or ``stream`` when someone is listening for the words.

    A client without ``stream`` (a test double, say) answers in one piece,
    which a listener receives as a single delta.
    """
    if on_delta is None:
        return provider.complete(turns, **kwargs)
    stream = getattr(provider, "stream", None)
    if stream is None:
        result = provider.complete(turns, **kwargs)
        if result.content:
            on_delta(result.content)
        return result
    return stream(turns, on_delta=on_delta, cancel=cancel, **kwargs)


def _run_completion(
    user, conversation, route, turns, *, had_image=False, user_message=None, max_tokens=None,
    profile: str = question_profile.STANDARD, on_delta=None, cancel=None,
):
    """Call the vendor, then persist, meter and charge exactly one reply.

    ``route`` carries the model and the client together, so the row that is
    billed is always the row that answered — there is no second lookup that
    could pick a different one.

    With ``on_delta`` the reply is streamed: each piece is handed over as it
    is written, minus the suggestion block. ``cancel`` stops it part-way; the
    part that was written is kept and charged, and nothing more.
    """
    ai_model = route.model
    provider = route.provider
    system_prompt = preference_service.system_prompt_for(
        user, settings_service.get("ai_system_prompt", settings_service.DEFAULT_SYSTEM_PROMPT),
        conversation=conversation,
    )
    # Nothing to follow up after "thanks!" — and the instruction is input the
    # student would pay for on a reply that has no use for it.
    wants_followups = followup_service.enabled() and profile != question_profile.LIGHT
    if wants_followups:
        system_prompt = f"{system_prompt}\n\n{followup_service.INSTRUCTION}"

    try:
        # ``max_tokens`` is the student's remaining headroom, not a preference:
        # without it the answer's length is the vendor's decision and the
        # ceiling that permitted the request cannot hold.
        relay = on_delta
        if on_delta is not None and wants_followups:
            relay = followup_service.StreamFilter(on_delta).feed
        result = _call_provider(
            provider, turns, on_delta=relay, cancel=cancel,
            system_prompt=system_prompt, max_tokens=max_tokens, profile=profile,
        )
    except AIError as exc:
        AIUsageLog.objects.create(
            user=user,
            conversation=conversation,
            message=user_message,
            model=getattr(provider, "model", ""),
            model_key=ai_model.key,
            provider=provider.name,
            success=False,
            error_code=exc.code,
            error_message=exc.friendly_message[:255],
            had_image=had_image,
        )
        model_service.record_failure(ai_model)
        logger.warning("AI call failed user=%s model=%s code=%s", user.pk, ai_model.key, exc.code)
        # No usage was reported, so nothing is charged.
        raise

    # The suggestion block is cut off before anything is saved, so the answer
    # that is stored, shown, copied and exported never contains it. It is
    # still billed: the model wrote it, and the tokens were really spent.
    answer, suggestions = followup_service.split(result.content)
    stopped = result.finish_reason == STOPPED
    if stopped:
        # Cut mid-block, the suggestions are unfinished; cut mid-marker, the
        # half-written marker line is not part of the answer either.
        answer = followup_service.trim_partial_marker(answer)
    if not wants_followups or stopped:
        suggestions = []
    if stopped and not answer:
        _charge_stopped(user, conversation, route, result, had_image=had_image)
        raise ChatBlocked("Stopped before the answer began.", code="stopped")
    assistant_message = Message.objects.create(
        conversation=conversation,
        role=MessageRole.ASSISTANT,
        content=answer,
        suggestions=suggestions,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        total_tokens=result.total_tokens,
        model=result.model,
        model_key=ai_model.key,
        request_id=result.request_id,
        latency_ms=result.latency_ms,
        status=MessageStatus.OK,
    )

    charged = 0
    if result.total_tokens > 0:
        # The debit names the model explicitly, so only that model's wallet can
        # move. Nothing here can reach another provider's balance.
        ledger = wallet_service.consume(
            user,
            ai_model,
            result.total_tokens,
            reason=f"{ai_model.name} — {result.input_tokens} in / {result.output_tokens} out",
            conversation=conversation,
            message=assistant_message,
            clamp=True,
        )
        charged = abs(ledger.amount)

    AIUsageLog.objects.create(
        user=user,
        conversation=conversation,
        message=assistant_message,
        model=result.model,
        model_key=ai_model.key,
        provider=provider.name,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        total_tokens=result.total_tokens,
        tokens_charged=charged,
        reasoning_tokens=result.reasoning_tokens,
        request_id=result.request_id,
        success=True,
        error_code=STOPPED if stopped else "",
        latency_ms=result.latency_ms,
        had_image=had_image,
    )

    model_service.record_usage(
        ai_model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        total_tokens=result.total_tokens,
    )
    conversation.touch(tokens=result.total_tokens, messages=1)

    return assistant_message, result, charged


def _charge_stopped(user, conversation, route, result, *, had_image=False) -> int:
    """A reply stopped before it wrote anything still read the question.

    The vendor bills the prompt the moment it starts, so that part is charged
    — to the model that was asked, as always — and nothing is saved.
    """
    ai_model, provider = route.model, route.provider
    charged = 0
    if result.total_tokens > 0:
        ledger = wallet_service.consume(
            user, ai_model, result.total_tokens,
            reason=f"{ai_model.name} — stopped before answering ({result.input_tokens} in)",
            conversation=conversation, clamp=True,
        )
        charged = abs(ledger.amount)
    AIUsageLog.objects.create(
        user=user, conversation=conversation, model=result.model, model_key=ai_model.key,
        provider=provider.name, input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        total_tokens=result.total_tokens, tokens_charged=charged, reasoning_tokens=result.reasoning_tokens,
        request_id=result.request_id, success=True, error_code=STOPPED,
        error_message="Stopped before the answer began.", latency_ms=result.latency_ms, had_image=had_image,
    )
    model_service.record_usage(
        ai_model, input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        total_tokens=result.total_tokens,
    )
    return charged


def run_tool(
    user, conversation, *, prompt: str, system_prompt: str, model_key: str = "",
    max_output: int = 2000, label: str = "Tool", accept=None,
):
    """One request that is not a chat reply — a study set, say — held to every
    rule a reply is: the same account, balance and limit checks before it
    goes, the same wallet and usage log after.

    ``accept`` turns the vendor's text into the result, or raises ``ValueError``
    when the text is unusable. Nothing is charged for an unusable answer: the
    student asked for a quiz, not for the model's attempt at one.

    Returns ``(accepted, result, charged, ai_model)``.
    """
    _guard_account(user)
    turns = [ChatTurn(role=MessageRole.USER, content=prompt)]
    prompt_tokens = estimate_tokens(prompt) + estimate_tokens(system_prompt)
    route, _switched = _route_with_fallback(user, model_key, prompt_tokens=prompt_tokens)
    ai_model, provider = route.model, route.provider
    budget = _guard_reply_budget(user, route, prompt_tokens=prompt_tokens)

    def log_failure(code: str, message: str) -> None:
        AIUsageLog.objects.create(
            user=user, conversation=conversation, model=getattr(provider, "model", ""),
            model_key=ai_model.key, provider=provider.name, success=False,
            error_code=code, error_message=message[:255],
        )

    try:
        result = provider.complete(turns, system_prompt=system_prompt, max_tokens=min(budget, max_output))
    except AIError as exc:
        log_failure(exc.code, exc.friendly_message)
        model_service.record_failure(ai_model)
        raise

    try:
        accepted = accept(result.content) if accept else result.content
    except ValueError as exc:
        log_failure("unusable_output", str(exc))
        logger.warning("%s from %s was unusable: %s", label, ai_model.key, exc)
        raise AIError(
            "The assistant's answer could not be turned into that. Please try again.",
            code="unusable_output", retryable=True,
        ) from exc

    charged = 0
    if result.total_tokens > 0:
        ledger = wallet_service.consume(
            user, ai_model, result.total_tokens,
            reason=f"{label} · {ai_model.name} — {result.input_tokens} in / {result.output_tokens} out",
            conversation=conversation, clamp=True,
        )
        charged = abs(ledger.amount)

    AIUsageLog.objects.create(
        user=user, conversation=conversation, model=result.model, model_key=ai_model.key,
        provider=provider.name, input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        total_tokens=result.total_tokens, tokens_charged=charged, reasoning_tokens=result.reasoning_tokens,
        request_id=result.request_id, success=True, latency_ms=result.latency_ms,
    )
    model_service.record_usage(
        ai_model, input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        total_tokens=result.total_tokens,
    )
    return accepted, result, charged, ai_model


def attachment_payload(message: Message) -> list[dict]:
    """How the browser renders the files on a message."""
    return [
        {
            "id": item.pk,
            "kind": item.kind,
            "name": item.name,
            "ext": item.ext,
            "size": item.size,
            "url": item.file.url if item.file else "",
            "icon": item.icon,
            "readable": item.readable,
            "note": item.extraction_note,
        }
        for item in message.attachments.all()
    ]


def _assistant_payload(message, result, ai_model) -> dict:
    return {
        "id": message.pk,
        "role": "assistant",
        "content": message.content,
        "created_at": timezone.localtime(message.created_at).strftime("%H:%M"),
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "total_tokens": result.total_tokens,
        "model": ai_model.key,
        "model_name": ai_model.name,
        "latency_ms": result.latency_ms,
        "suggestions": list(message.suggestions or []),
    }


# ---------------------------------------------------------------------------
# Pictures
# ---------------------------------------------------------------------------
def companion_model(user, model_key: str = ""):
    """The wallet that pays for drawing, dictation and spoken replies.

    None of these is a chat completion, and none of the three chat models
    provides any of them — they are served by a single dedicated vendor
    whichever model the student happens to be talking to. So the wallet that
    pays is **the model they are using**, not the vendor that happens to serve
    the request. Someone working in Claude Sonnet can draw a diagram and hear
    the answer read back without first being told to go and find GPT tokens.

    The ledger records this plainly: the entry names the service and the model
    whose balance paid for it, so an administrator reconciling a Claude pool
    can see that some of it went on companion services rather than on Claude.

    Falls back to any usable wallet when the selected model has run dry, so a
    single empty balance does not switch the feature off.
    """
    rows = [row for row in wallet_service.overview(user) if row["usable"]]
    if not rows:
        return None
    key = (model_key or "").strip()
    if key:
        chosen = next((row["model"] for row in rows if row["model"].key == key), None)
        if chosen is not None:
            return chosen
    preferred = model_router.preferred_model(user)
    if preferred is not None and any(row["model"].pk == preferred.pk for row in rows):
        return preferred
    return rows[0]["model"]


def _store_generated_image(message: Message, picture) -> MessageAttachment:
    """Attach the picture to the reply, using the same storage as an upload."""
    name = f"generated-{timezone.now().strftime('%Y%m%d-%H%M%S')}.{picture.ext}"
    attachment = MessageAttachment(
        message=message,
        kind=AttachmentKind.IMAGE,
        name=name,
        ext=picture.ext,
        mime=picture.mime,
        size=picture.byte_size,
    )
    attachment.file.save(name, ContentFile(picture.data), save=False)
    attachment.save()
    return attachment


def generate_image_reply(
    user,
    conversation: Conversation,
    text: str,
    *,
    model_key: str = "",
    prompt_override: str = "",
    variation: bool = False,
) -> dict:
    """Draw what was asked for and put it in the conversation.

    Kept separate from :func:`send_message` rather than folded into it, because
    almost nothing is shared: there is no turn list, no context window, no usage
    block to read, and the charge is per picture rather than per token. The one
    thing that *is* shared — a failure costs the student nothing — holds here
    for the same reason it does there: the debit happens after the bytes arrive.

    ``prompt_override`` is how "generate again" and "make a variation" re-draw
    without the student retyping anything; ``variation`` asks the endpoint for
    something different rather than the same picture twice.
    """
    _guard_account(user)

    if not image_service.is_available():
        raise ChatBlocked(
            "Image generation is temporarily unavailable. Please try again in a moment.",
            code="image_disabled",
        )

    prompt = (prompt_override or "").strip() or image_service.build_prompt(text)
    if not prompt:
        raise ChatBlocked(
            "Tell me what to draw \u2014 for example, \u201ca lighthouse at dawn\u201d.",
            code="empty_prompt",
        )
    if variation:
        # Same subject, different picture. Said in the prompt rather than by
        # seeding the endpoint, because not every vendor exposes a seed.
        prompt = f"{prompt} \u2014 a different interpretation, new composition and angle"

    ai_model = companion_model(user, model_key)
    if ai_model is None:
        raise ChatBlocked(
            "You have no tokens left on any model. Open Pricing to top up.",
            code="no_tokens",
            balance=0,
        )

    cfg = image_service.config()
    cost = image_service.token_cost(cfg["quality"])
    # The same two gates every chat message passes: enough tokens owned, and
    # enough of today's, this week's and this month's allowance left to spend
    # them. A picture is not exempt from the ceilings just because it happens
    # to be priced per picture.
    _guard_budget(user, ai_model, prompt_tokens=cost)

    user_message = _save_user_message(conversation, text)

    try:
        picture = image_service.generate(prompt)
    except AIError as exc:
        AIUsageLog.objects.create(
            user=user,
            conversation=conversation,
            message=user_message,
            model=cfg["model"],
            model_key=ai_model.key,
            provider=cfg["provider"],
            success=False,
            error_code=exc.code,
            error_message=exc.friendly_message[:255],
            had_image=True,
        )
        ImageRequest.objects.create(
            user=user,
            conversation=conversation,
            message=user_message,
            ai_model=ai_model,
            prompt=prompt,
            provider=cfg["provider"],
            image_model=cfg["model"],
            size=cfg["size"],
            quality=cfg["quality"],
            status=ImageRequest.Status.FAILED,
            error_code=exc.code[:64],
            error_message=exc.friendly_message[:255],
        )
        logger.warning("Image generation failed user=%s code=%s", user.pk, exc.code)
        raise

    caption = picture.revised_prompt or prompt
    assistant_message = Message.objects.create(
        conversation=conversation,
        role=MessageRole.ASSISTANT,
        content=f"Here is the image you asked for.\n\n*{caption}*",
        input_tokens=0,
        output_tokens=cost,
        total_tokens=cost,
        model=picture.model,
        model_key=ai_model.key,
        latency_ms=picture.latency_ms,
        status=MessageStatus.OK,
    )
    attachment = _store_generated_image(assistant_message, picture)

    ledger = wallet_service.consume(
        user, ai_model, cost,
        reason=f"Image generation ({picture.model}) \u2014 paid from {ai_model.name}",
        conversation=conversation,
        message=assistant_message,
        clamp=True,
    )
    charged = abs(ledger.amount)

    AIUsageLog.objects.create(
        user=user,
        conversation=conversation,
        message=assistant_message,
        model=picture.model,
        model_key=ai_model.key,
        provider=picture.provider or cfg["provider"],
        input_tokens=0,
        output_tokens=cost,
        total_tokens=cost,
        tokens_charged=charged,
        success=True,
        latency_ms=picture.latency_ms,
        had_image=True,
    )
    ImageRequest.objects.create(
        user=user,
        conversation=conversation,
        message=assistant_message,
        ai_model=ai_model,
        attachment=attachment,
        prompt=prompt,
        revised_prompt=picture.revised_prompt,
        provider=picture.provider or cfg["provider"],
        image_model=picture.model,
        size=picture.size,
        quality=picture.quality,
        status=ImageRequest.Status.OK,
        tokens_charged=charged,
        latency_ms=picture.latency_ms,
    )
    conversation.touch(tokens=cost, messages=1)

    if conversation.title in {"New chat", ""}:
        title = build_title(prompt)
        Conversation.objects.filter(pk=conversation.pk).update(title=title)
        conversation.title = title

    balance = wallet_service.balance_of(user, ai_model)
    _maybe_low_balance_alert(user, balance)

    return {
        "conversation_id": conversation.pk,
        "conversation_title": conversation.title,
        "kind": "image",
        # Carried back so the picture's own actions — draw it again, make a
        # variation, copy the words — need no round trip to discover what was
        # actually drawn, which is often not quite what was typed.
        "image_prompt": prompt,
        "revised_prompt": picture.revised_prompt,
        "user_message": {
            "id": user_message.pk,
            "role": "user",
            "content": user_message.content,
            "image_url": "",
            "attachments": attachment_payload(user_message),
            "created_at": timezone.localtime(user_message.created_at).strftime("%H:%M"),
        },
        "assistant_message": {
            "id": assistant_message.pk,
            "role": "assistant",
            "content": assistant_message.content,
            "created_at": timezone.localtime(assistant_message.created_at).strftime("%H:%M"),
            "attachments": attachment_payload(assistant_message),
            "input_tokens": 0,
            "output_tokens": cost,
            "total_tokens": cost,
            "model": ai_model.key,
            "model_name": "Image",
            "latency_ms": picture.latency_ms,
            "image_prompt": prompt,
        },
        "tokens_used": cost,
        "tokens_charged": charged,
        "reasoning_tokens": 0,
        **balance_payload(user, ai_model=ai_model, balance=balance),
    }


def edit_image_reply(
    user, conversation: Conversation, text: str, attachment_id: int, *, model_key: str = ""
) -> dict:
    """Re-draw a picture the student already has, following an instruction.

    Shares the charge, the ceilings and the failure behaviour with
    :func:`generate_image_reply` — the only difference is that the vendor is
    given the previous picture as well as the words. Ownership of that picture
    is proved from the database rather than trusted from the request, so an
    attachment id belonging to somebody else is simply not found.
    """
    _guard_account(user)

    if not image_service.can_edit():
        cfg = image_service.config()
        if cfg["enabled"] and cfg["api_key"] and not cfg["supports_edit"]:
            raise ChatBlocked(
                f"{cfg['provider_name']} cannot change an existing image. "
                "Describe a new one instead.",
                code="edit_unsupported",
            )
        raise ChatBlocked(
            "Image generation is temporarily unavailable. Please try again in a moment.",
            code="image_disabled",
        )

    instruction = image_service.strip_command(text).strip()
    if not instruction:
        raise ChatBlocked(
            "Say what you would like changed — for example, “make the sky orange”.",
            code="empty_prompt",
        )

    # Object-level authorisation: the picture has to be on a message in a
    # conversation this student owns.
    source = MessageAttachment.objects.filter(
        pk=attachment_id,
        kind=AttachmentKind.IMAGE,
        message__conversation__user=user,
    ).select_related("message").first()
    if source is None:
        raise ChatBlocked("That image could not be found.", code="not_found")

    try:
        source.file.open("rb")
        original = source.file.read()
    except (OSError, ValueError):
        raise ChatBlocked("That image could not be opened. Try generating a new one.", code="no_source")
    finally:
        try:
            source.file.close()
        except Exception:  # pragma: no cover - closing a closed file
            pass

    ai_model = companion_model(user, model_key)
    if ai_model is None:
        raise ChatBlocked(
            "You have no tokens left on any model. Open Pricing to top up.",
            code="no_tokens", balance=0,
        )

    cfg = image_service.config()
    cost = image_service.token_cost(cfg["quality"])
    _guard_budget(user, ai_model, prompt_tokens=cost)

    user_message = _save_user_message(conversation, text)

    try:
        picture = image_service.edit(instruction, original, mime=source.mime or "image/png")
    except AIError as exc:
        AIUsageLog.objects.create(
            user=user, conversation=conversation, message=user_message,
            model=cfg["model"], model_key=ai_model.key, provider=cfg["provider"],
            success=False, error_code=exc.code,
            error_message=exc.friendly_message[:255], had_image=True,
        )
        ImageRequest.objects.create(
            user=user, conversation=conversation, message=user_message, ai_model=ai_model,
            prompt=instruction, provider=cfg["provider"], image_model=cfg["model"],
            size=cfg["size"], quality=cfg["quality"],
            status=ImageRequest.Status.FAILED,
            error_code=exc.code[:64], error_message=exc.friendly_message[:255],
        )
        logger.warning("Image edit failed user=%s code=%s", user.pk, exc.code)
        raise

    assistant_message = Message.objects.create(
        conversation=conversation,
        role=MessageRole.ASSISTANT,
        content=f"Here is the updated image.\n\n*{instruction}*",
        input_tokens=0, output_tokens=cost, total_tokens=cost,
        model=picture.model, model_key=ai_model.key,
        latency_ms=picture.latency_ms, status=MessageStatus.OK,
    )
    attachment = _store_generated_image(assistant_message, picture)

    ledger = wallet_service.consume(
        user, ai_model, cost,
        reason=f"Image edit ({picture.model}) — paid from {ai_model.name}",
        conversation=conversation, message=assistant_message, clamp=True,
    )
    charged = abs(ledger.amount)

    AIUsageLog.objects.create(
        user=user, conversation=conversation, message=assistant_message,
        model=picture.model, model_key=ai_model.key,
        provider=picture.provider or cfg["provider"],
        input_tokens=0, output_tokens=cost, total_tokens=cost,
        tokens_charged=charged, success=True,
        latency_ms=picture.latency_ms, had_image=True,
    )
    ImageRequest.objects.create(
        user=user, conversation=conversation, message=assistant_message, ai_model=ai_model,
        attachment=attachment, prompt=instruction, revised_prompt=picture.revised_prompt,
        provider=picture.provider or cfg["provider"], image_model=picture.model,
        size=picture.size, quality=picture.quality,
        status=ImageRequest.Status.OK, tokens_charged=charged, latency_ms=picture.latency_ms,
    )
    conversation.touch(tokens=cost, messages=1)

    balance = wallet_service.balance_of(user, ai_model)
    _maybe_low_balance_alert(user, balance)

    return {
        "conversation_id": conversation.pk,
        "conversation_title": conversation.title,
        "kind": "image",
        "image_prompt": instruction,
        "revised_prompt": picture.revised_prompt,
        "user_message": {
            "id": user_message.pk,
            "role": "user",
            "content": user_message.content,
            "image_url": "",
            "attachments": attachment_payload(user_message),
            "created_at": timezone.localtime(user_message.created_at).strftime("%H:%M"),
        },
        "assistant_message": {
            "id": assistant_message.pk,
            "role": "assistant",
            "content": assistant_message.content,
            "created_at": timezone.localtime(assistant_message.created_at).strftime("%H:%M"),
            "attachments": attachment_payload(assistant_message),
            "input_tokens": 0,
            "output_tokens": cost,
            "total_tokens": cost,
            "model": ai_model.key,
            "model_name": "Image",
            "latency_ms": picture.latency_ms,
            "image_prompt": instruction,
        },
        "tokens_used": cost,
        "tokens_charged": charged,
        "reasoning_tokens": 0,
        **balance_payload(user, ai_model=ai_model, balance=balance),
    }


def send_message(
    user,
    conversation: Conversation,
    text: str,
    *,
    attachments=None,
    model_key: str = "",
    variation: bool = False,
    client_id: str = "",
    on_delta=None,
    cancel=None,
    deferred: list | None = None,
) -> dict:
    """Run one full chat exchange and return a serialisable result.

    ``variation`` only means anything for a picture: it is how the "make a
    variation" button asks for the same subject drawn differently.

    ``on_delta`` / ``cancel`` stream the answer (see :func:`_run_completion`).
    ``deferred``, when given, collects housekeeping that would otherwise run
    before returning, so a streamed answer is finished for the student first.
    """
    text = (text or "").strip()
    attachments = list(attachments or [])

    if not text and not attachments:
        raise ChatBlocked("Type a message or attach a file to send.", code="empty")
    if len(text) > MAX_MESSAGE_CHARS:
        raise ChatBlocked(
            f"That message is too long. Please shorten it to {MAX_MESSAGE_CHARS:,} characters.",
            code="too_long",
        )
    if len(attachments) > MAX_ATTACHMENTS:
        raise ChatBlocked(
            f"You can attach up to {MAX_ATTACHMENTS} files to one message.", code="too_many_files"
        )

    _guard_account(user)

    # None of the chat models draws, so a request for a picture is answered by
    # the image endpoint instead of being refused. Only when nothing is
    # attached: "make this photo into a poster" is an edit, not a fresh
    # drawing, and belongs to the vision model that can actually see it.
    if not attachments and image_service.looks_like_image_request(text):
        if image_service.is_available():
            return generate_image_reply(
                user, conversation, text, model_key=model_key, variation=variation
            )
        if image_service.is_explicit_command(text):
            # An explicit /image has nowhere else to go, so it has to be
            # refused — but as a service that is down rather than as a server
            # misconfiguration, which is not the student's to understand or
            # fix. The operator sees the real reason in the admin console.
            raise ChatBlocked(
                "Image generation is temporarily unavailable. Please try again in a moment.",
                code="image_disabled",
            )

    images = [a for a in attachments if a.kind == AttachmentKind.IMAGE]
    documents = [a for a in attachments if a.kind == AttachmentKind.DOCUMENT]

    # Document text joins the prompt, so it must be paid for like any other
    # prompt content — count it before deciding the request is affordable.
    document_block = "\n\n".join(
        extract.as_prompt_block(
            doc.name, extract.ExtractionResult(doc.text or "", pages=doc.pages, note=doc.note or "")
        )
        for doc in documents
    )

    turns = history_turns(conversation)
    prompt_estimate = (
        estimate_tokens(text)
        + estimate_tokens(document_block)
        + sum(estimate_tokens(turn.content) for turn in turns)
    )

    route, switched_from = _route_with_fallback(
        user, model_key, prompt_tokens=prompt_estimate
    )
    ai_model, provider = route.model, route.provider

    user_message = _save_user_message(conversation, text, attachments=attachments, client_id=client_id)

    current = ChatTurn(role=MessageRole.USER, content=compose_turn_content(user_message))
    if images:
        if not (provider.supports_vision and settings_service.get("ai_vision_enabled", True)):
            Message.objects.filter(pk=user_message.pk).update(
                error_message="This model cannot read images"
            )
            current.content += (
                f"\n\n[The student attached {len(images)} image(s), "
                "but this model cannot read images.]"
            )
        else:
            # One data URL per image; the provider builds the multi-part payload.
            current.image_data_urls = [img.as_data_url() for img in images]
    turns.append(current)

    profile = question_profile.classify(text)
    assistant_message, result, charged = _run_completion(
        user, conversation, route, turns,
        had_image=bool(images), user_message=user_message,
        max_tokens=_guard_reply_budget(user, route, prompt_tokens=prompt_estimate, profile=profile),
        profile=profile, on_delta=on_delta, cancel=cancel,
    )

    if conversation.title in {"New chat", ""}:
        title = build_title(text)
        Conversation.objects.filter(pk=conversation.pk).update(title=title)
        conversation.title = title

    # After the reply, not before it: summarising is housekeeping, and it must
    # never add latency to the answer the student is waiting for or cost them
    # a reply they would otherwise have got.
    if deferred is not None:
        deferred.append(lambda: refresh_summary(user, conversation, route))
    else:
        refresh_summary(user, conversation, route)

    balance = wallet_service.balance_of(user, ai_model)
    _maybe_low_balance_alert(user, balance)

    return {
        "conversation_id": conversation.pk,
        "conversation_title": conversation.title,
        "user_message": {
            "id": user_message.pk,
            "role": "user",
            "content": user_message.content,
            "image_url": user_message.image.url if user_message.image else "",
            "attachments": attachment_payload(user_message),
            "created_at": timezone.localtime(user_message.created_at).strftime("%H:%M"),
        },
        "assistant_message": _assistant_payload(assistant_message, result, ai_model),
        # A switch is reported as both halves: which model ran out, and which
        # one answered instead. The transcript names the second — "Switching to
        # Opus 5" — because that is the model the next answers come from; the
        # first is kept for the audit trail and for anything that wants to
        # explain why. Empty on the ordinary path, which is most of them.
        "switched_from": switched_from.key if switched_from else "",
        "switched_from_name": switched_from.name if switched_from else "",
        "switched_to": ai_model.key if switched_from else "",
        "switched_to_name": ai_model.name if switched_from else "",
        "tokens_used": result.total_tokens,
        "tokens_charged": charged,
        "reasoning_tokens": result.reasoning_tokens,
        **balance_payload(user, ai_model=ai_model, balance=balance),
    }


def regenerate(
    user, conversation: Conversation, *, model_key: str = "", on_delta=None, cancel=None,
) -> dict:
    """Answer the last question again, replacing the reply it already has.

    The old reply is removed only once the new one exists. Checking the
    account, the balance and the vendor all happen first, and any of them can
    refuse — a regenerate that fails must leave the answer the student already
    had, with its rating and bookmark, exactly where it was.
    """
    last = conversation.messages.order_by("-created_at", "-id").first()
    if last is None:
        raise ChatBlocked("There is nothing to regenerate yet.", code="empty")

    prompt = conversation.messages.filter(role=MessageRole.USER).order_by("-created_at", "-id").first()
    if prompt is None:
        raise ChatBlocked("There is no question to answer.", code="empty")

    _guard_account(user)

    replaced = last if last.role == MessageRole.ASSISTANT else None
    turns = history_turns(conversation, exclude=[replaced.pk] if replaced else ())
    prompt_tokens = sum(estimate_tokens(turn.content) for turn in turns)
    # A regenerate defaults to the model that produced the reply being replaced,
    # so re-rolling an answer never quietly switches vendor — or wallet.
    route, switched_from = _route_with_fallback(
        user, model_key or last.model_key, prompt_tokens=prompt_tokens,
    )
    ai_model = route.model

    # Asked again, so it deserves the same care as the first time.
    profile = question_profile.classify(prompt.content)
    assistant_message, result, charged = _run_completion(
        user, conversation, route, turns,
        max_tokens=_guard_reply_budget(user, route, prompt_tokens=prompt_tokens, profile=profile),
        profile=profile, on_delta=on_delta, cancel=cancel,
    )

    if replaced is not None:
        replaced.delete()
        Conversation.objects.filter(pk=conversation.pk, message_count__gt=0).update(
            message_count=F("message_count") - 1
        )

    balance = wallet_service.balance_of(user, ai_model)
    _maybe_low_balance_alert(user, balance)

    return {
        "assistant_message": _assistant_payload(assistant_message, result, ai_model),
        # A switch is reported as both halves: which model ran out, and which
        # one answered instead. The transcript names the second — "Switching to
        # Opus 5" — because that is the model the next answers come from; the
        # first is kept for the audit trail and for anything that wants to
        # explain why. Empty on the ordinary path, which is most of them.
        "switched_from": switched_from.key if switched_from else "",
        "switched_from_name": switched_from.name if switched_from else "",
        "switched_to": ai_model.key if switched_from else "",
        "switched_to_name": ai_model.name if switched_from else "",
        "tokens_used": result.total_tokens,
        "tokens_charged": charged,
        **balance_payload(user, ai_model=ai_model, balance=balance),
    }


def edit_and_resend(
    user, conversation: Conversation, message_id: int, text: str, *, model_key: str = "",
    on_delta=None, cancel=None,
) -> dict:
    """Replace one of the student's messages and answer again from that point.

    Everything after the edited message is removed first, so the conversation
    reads as though the new wording had been sent originally.
    """
    text = (text or "").strip()
    if not text:
        raise ChatBlocked("The message cannot be empty.", code="empty")
    if len(text) > MAX_MESSAGE_CHARS:
        raise ChatBlocked(
            f"That message is too long. Please shorten it to {MAX_MESSAGE_CHARS:,} characters.",
            code="too_long",
        )

    target = conversation.messages.filter(pk=message_id, role=MessageRole.USER).first()
    if target is None:
        raise ChatBlocked("That message is no longer available.", code="not_found")

    _guard_account(user)

    # The conversation as it will read, built without touching it: everything
    # up to the edited message, with the new wording in its place. Nothing is
    # deleted or rewritten until the new answer exists — a refusal or a vendor
    # failure leaves the chat exactly as it was.
    later_ids = list(
        conversation.messages.filter(created_at__gte=target.created_at).exclude(pk=target.pk)
        .values_list("id", flat=True)
    )
    turns = history_turns(conversation, until=target, exclude=[target.pk])
    original = target.content
    target.content = text
    turns.append(ChatTurn(role=MessageRole.USER, content=compose_turn_content(target)))
    target.content = original
    prompt_tokens = sum(estimate_tokens(turn.content) for turn in turns)

    route, switched_from = _route_with_fallback(user, model_key, prompt_tokens=prompt_tokens)
    ai_model = route.model

    profile = question_profile.classify(text)
    assistant_message, result, charged = _run_completion(
        user, conversation, route, turns, user_message=target,
        max_tokens=_guard_reply_budget(user, route, prompt_tokens=prompt_tokens, profile=profile),
        profile=profile, on_delta=on_delta, cancel=cancel,
    )

    with transaction.atomic():
        conversation.messages.filter(id__in=later_ids).delete()
        target.content = text
        target.status = MessageStatus.OK
        target.error_message = ""
        target.save(update_fields=["content", "status", "error_message"])
        if later_ids:
            Conversation.objects.filter(pk=conversation.pk).update(
                message_count=Greatest(F("message_count") - len(later_ids), 0)
            )

    balance = wallet_service.balance_of(user, ai_model)
    _maybe_low_balance_alert(user, balance)

    return {
        "conversation_id": conversation.pk,
        "user_message": {
            "id": target.pk,
            "role": "user",
            "content": target.content,
            "image_url": target.image.url if target.image else "",
            "created_at": timezone.localtime(target.created_at).strftime("%H:%M"),
        },
        "assistant_message": _assistant_payload(assistant_message, result, ai_model),
        # A switch is reported as both halves: which model ran out, and which
        # one answered instead. The transcript names the second — "Switching to
        # Opus 5" — because that is the model the next answers come from; the
        # first is kept for the audit trail and for anything that wants to
        # explain why. Empty on the ordinary path, which is most of them.
        "switched_from": switched_from.key if switched_from else "",
        "switched_from_name": switched_from.name if switched_from else "",
        "switched_to": ai_model.key if switched_from else "",
        "switched_to_name": ai_model.name if switched_from else "",
        "tokens_used": result.total_tokens,
        "tokens_charged": charged,
        **balance_payload(user, ai_model=ai_model, balance=balance),
    }


# ---------------------------------------------------------------------------
# Balance reporting for the UI
# ---------------------------------------------------------------------------
def balance_payload(user, ai_model=None, balance: int | None = None) -> dict:
    """The numbers the chat header and token meter render.

    The headline figures describe **one model** — the one the student is about
    to send to — because a combined total would tell them nothing about whether
    the next message will go through. ``models`` carries every wallet alongside
    it, so the picker can show each balance and grey out what is unusable.

    Always sourced from the ledger, never from anything the client sent.
    """
    threshold = int(settings_service.get("low_token_threshold", 5000))
    critical = int(settings_service.get("low_token_threshold_critical", 1000))

    rows = wallet_service.overview(user)
    if ai_model is None:
        ai_model = model_router.preferred_model(user)

    active = next((row for row in rows if ai_model and row["model"].pk == ai_model.pk), None)
    if active is None:
        active = {"balance": 0, "allocated": 0, "used": 0, "percent_used": 0, "percent_left": 0}

    balance = active["balance"] if balance is None else balance
    allocated = active["allocated"]
    totals = wallet_service.totals_of(user)

    return {
        "balance": balance,
        "allocated": allocated,
        "used": active["used"],
        "percent_remaining": (round(balance / allocated * 100) if allocated > 0 else 0),
        "percent_used": active["percent_used"],
        "is_empty": balance <= 0,
        "is_low": 0 < balance <= threshold,
        "is_critical": 0 < balance <= critical,
        "low_threshold": threshold,
        "lifetime_allocated": totals["lifetime_allocated"],
        "lifetime_used": totals["lifetime_used"],
        # How fast what is left may be spent. Always computed server-side from
        # the student's own packages — the browser is told the answer, never
        # asked for it, so editing anything on the page changes nothing.
        "usage": usage_limit_service.snapshot(user, ai_model).as_payload(),
        # Which model these headline numbers describe.
        "model": ai_model.key if ai_model else "",
        "model_name": ai_model.name if ai_model else "",
        # Every wallet, so the picker never has to guess.
        "models": [
            {
                "key": row["model"].key,
                "name": row["model"].name,
                "provider": row["model"].provider_name,
                "balance": row["balance"],
                "allocated": row["allocated"],
                "used": row["used"],
                "percent_used": row["percent_used"],
                "usable": row["usable"],
                "reason": row["reason"],
            }
            for row in rows
            if row["model"].is_active
        ],
        "total_balance": totals["balance"],
    }


def _maybe_low_balance_alert(user, balance: int) -> None:
    """Fire a low-balance notification at most once per threshold crossing."""
    try:
        from AI.services import notification_service

        notification_service.check_low_balance(user, balance)
    except Exception:  # pragma: no cover — never break a chat over an alert
        logger.exception("Low balance alert failed for user=%s", user.pk)


__all__ = [
    "ChatBlocked",
    "InsufficientTokens",
    "ModelUnavailable",
    "balance_payload",
    "build_title",
    "create_conversation",
    "edit_and_resend",
    "estimate_tokens",
    "context_budget",
    "companion_model",
    "edit_image_reply",
    "generate_image_reply",
    "get_conversation",
    "history_turns",
    "refresh_summary",
    "regenerate",
    "send_message",
]


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------
def transcribe_audio(
    user, audio: bytes, *, filename: str, mime: str, model_key: str = ""
) -> dict:
    """Turn a recording into text, and charge for it.

    Audio is billed by the minute rather than by tokens, so the duration is
    converted into tokens at a configured rate. A failed transcription costs
    nothing — the debit happens after a successful response, exactly as it does
    for a chat message.
    """
    from AI.services import ai_service

    _guard_account(user)

    if not getattr(settings, "AI_VOICE_ENABLED", True):
        raise ChatBlocked("Voice input is switched off.", code="voice_disabled")

    ceiling = getattr(settings, "AI_TRANSCRIBE_MAX_BYTES", 10 * 1024 * 1024)
    if len(audio) > ceiling:
        raise ChatBlocked(
            f"That recording is too large. Keep it under "
            f"{getattr(settings, 'AI_TRANSCRIBE_MAX_SECONDS', 120)} seconds.",
            code="audio_too_large",
        )

    ai_model = companion_model(user, model_key)
    if ai_model is None:
        raise ChatBlocked(
            "You have no tokens left on any model. Open Pricing to top up.",
            code="no_tokens",
            balance=0,
        )

    rate = max(1, int(getattr(settings, "AI_TRANSCRIBE_TOKENS_PER_MINUTE", 1500)))
    floor_seconds = max(1, int(getattr(settings, "AI_TRANSCRIBE_MIN_SECONDS", 3)))
    # Pre-check against a short clip, which is what most questions are.
    _guard_budget(user, ai_model, prompt_tokens=math.ceil(floor_seconds / 60 * rate))

    result = ai_service.transcribe(audio, filename=filename, mime=mime)

    # Billed by the second the vendor bills by, with a small floor. Rounding a
    # six-second question up to a whole minute charged ten times what it cost
    # and made the quickest way to ask a question the most expensive one.
    seconds = max(result.duration, float(floor_seconds))
    charge = max(1, math.ceil(seconds / 60.0 * rate))

    ledger = wallet_service.consume(
        user,
        ai_model,
        charge,
        reason=(
            f"Voice transcription — {result.duration:.0f}s "
            f"({result.language or 'language not reported'}) — paid from {ai_model.name}"
        ),
        clamp=True,
    )
    charged = abs(ledger.amount)

    AIUsageLog.objects.create(
        user=user,
        model=result.model,
        # Billed to the catalogue model whose wallet paid, so per-model usage
        # reporting stays consistent with per-model spending. The provider
        # column still names who actually served it, which is how the two are
        # told apart when a Claude balance pays for a companion service.
        model_key=ai_model.key,
        provider="openai",
        input_tokens=0,
        output_tokens=charge,
        total_tokens=charge,
        tokens_charged=charged,
        success=True,
        latency_ms=int(result.duration * 1000),
    )

    balance = wallet_service.balance_of(user, ai_model)
    _maybe_low_balance_alert(user, balance)

    return {
        "text": result.text,
        "language": result.language,
        "duration": round(result.duration, 1),
        "tokens_charged": charged,
        **balance_payload(user, ai_model=ai_model, balance=balance),
    }

def speak_text(user, text: str, *, voice: str = "", model_key: str = "") -> dict:
    """Read a reply aloud and charge for it.

    Billed the same way transcription is: an OpenAI service, paid for out of an
    OpenAI wallet, converted from the unit the vendor bills in (characters) into
    the one currency students see. A failed call costs nothing.
    """
    from AI.services import ai_service

    _guard_account(user)

    if not getattr(settings, "AI_SPEECH_ENABLED", True):
        raise ChatBlocked("Spoken replies are switched off.", code="speech_disabled")

    text = (text or "").strip()
    if not text:
        raise ChatBlocked("There is nothing to read out.", code="empty")

    ai_model = companion_model(user, model_key)
    if ai_model is None:
        raise ChatBlocked(
            "You have no tokens left on any model. Open Pricing to top up.",
            code="no_tokens",
            balance=0,
        )

    rate = max(1, int(getattr(settings, "AI_SPEECH_TOKENS_PER_1K_CHARS", 400)))
    ceiling = max(1, int(getattr(settings, "AI_SPEECH_MAX_CHARS", 4000)))
    estimate = max(1, math.ceil(min(len(text), ceiling) / 1000 * rate))
    _guard_budget(user, ai_model, prompt_tokens=estimate)

    speech = ai_service.synthesize(text, voice=voice)
    charge = max(1, math.ceil(speech.characters / 1000 * rate))

    ledger = wallet_service.consume(
        user, ai_model, charge,
        reason=f"Spoken reply — {speech.characters} characters — paid from {ai_model.name}",
        clamp=True,
    )

    AIUsageLog.objects.create(
        user=user,
        model=speech.model,
        model_key=ai_model.key,
        provider="openai",
        input_tokens=0,
        output_tokens=charge,
        total_tokens=charge,
        tokens_charged=abs(ledger.amount),
        success=True,
        latency_ms=speech.latency_ms,
    )

    balance = wallet_service.balance_of(user, ai_model)
    _maybe_low_balance_alert(user, balance)
    return {"speech": speech, "tokens_charged": abs(ledger.amount), "balance": balance}
