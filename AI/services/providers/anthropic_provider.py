"""Anthropic — the Messages API, through the official ``anthropic`` SDK.

Self-contained, exactly like :mod:`AI.services.providers.openai_provider`: no
OpenAI concept reaches this file and no Anthropic concept leaves it.  The two
vendors differ in ways that matter, and each difference is handled here:

* the system prompt is a **top-level parameter**, not a message with a role;
* ``max_tokens`` is **required** on every request;
* images arrive as ``{media_type, base64}`` pairs, not as data URLs;
* the conversation must **start with a user turn**;
* usage reports ``input_tokens`` / ``output_tokens`` with **no total**;
* **no sampling parameters exist** — ``temperature`` and ``top_p`` are not part
  of the Messages API surface at all — and the way thinking is configured
  changed between model generations.

Model ids come from the current Anthropic catalogue: ``claude-sonnet-5`` and
``claude-haiku-4-5``.  Neither takes a date suffix.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterable

from django.conf import settings

from AI.services.providers.base import (
    STOPPED,
    AIError,
    AIResponse,
    BaseProvider,
    CancelToken,
    ChatTurn,
    estimate_tokens,
    split_data_url,
)

logger = logging.getLogger("AI.provider.anthropic")

try:  # pragma: no cover — exercised by the absence path only
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None


class AnthropicProvider(BaseProvider):
    """Claude models: Sonnet 5, Haiku 4.5 and anything added after them."""

    name = "anthropic"

    #: Model families that take ``thinking: {"type": "adaptive"}``. Everything
    #: else is treated as a pre-adaptive model, which needs an explicit thinking
    #: budget instead. This is the only thing the distinction controls.
    ADAPTIVE_THINKING_PREFIXES = (
        "claude-opus-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-fable-",
        "claude-mythos-",
    )

    #: Anthropic's own minimum for an explicit thinking budget.
    MIN_THINKING_BUDGET = 1024

    #: Every Claude model currently in the catalogue reads images.
    VISION_CAPABLE_PREFIXES = ("claude-",)

    #: Passed explicitly on every client, never left to the SDK's own env
    #: lookup: a blank ``ANTHROPIC_BASE_URL`` in the environment would otherwise
    #: be read as "no host" and every request would fail with a bare connection
    #: error that says nothing about the cause.
    DEFAULT_BASE_URL = "https://api.anthropic.com"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        timeout: int | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        vision: bool | None = None,
        thinking_mode: str = "off",
        **_ignored: Any,
    ):
        self.api_key = api_key or getattr(settings, "ANTHROPIC_API_KEY", "")
        self.base_url = (base_url or getattr(settings, "ANTHROPIC_BASE_URL", "")).rstrip("/")
        self.model = model or getattr(settings, "ANTHROPIC_MODEL", "claude-sonnet-5")
        self.timeout = timeout or getattr(settings, "ANTHROPIC_TIMEOUT", 60)
        self.temperature = (
            getattr(settings, "AI_TEMPERATURE", 0.4) if temperature is None else float(temperature)
        )
        self.max_output_tokens = max_output_tokens or getattr(
            settings, "ANTHROPIC_MAX_OUTPUT_TOKENS", 4096
        )
        self._vision_override = vision
        self.thinking_mode = (thinking_mode or "off").lower()

    # -- capability ---------------------------------------------------------
    @property
    def is_configured(self) -> bool:
        return bool(self.api_key) and anthropic is not None

    @property
    def uses_adaptive_thinking(self) -> bool:
        model = (self.model or "").lower()
        return any(model.startswith(prefix) for prefix in self.ADAPTIVE_THINKING_PREFIXES)

    @property
    def supports_vision(self) -> bool:
        if not settings.AI_VISION_ENABLED:
            return False
        if self._vision_override is not None:
            return bool(self._vision_override)
        model = (self.model or "").lower()
        return any(model.startswith(prefix) for prefix in self.VISION_CAPABLE_PREFIXES)

    def _client(self):
        if anthropic is None:  # pragma: no cover — guarded by is_configured
            raise AIError(
                "The assistant is not configured yet. Please contact support.",
                code="not_configured",
                retryable=False,
            )
        return anthropic.Anthropic(
            api_key=self.api_key,
            base_url=self.base_url or self.DEFAULT_BASE_URL,
            timeout=float(self.timeout),
        )

    # -- payload ------------------------------------------------------------
    def _content_blocks(self, turn: ChatTurn) -> list[dict] | str:
        """One turn as Anthropic content: text, plus any images it carries."""
        images = turn.images() if turn.role == "user" else []
        if not images:
            return turn.content

        if not self.supports_vision:
            plural = "an image" if len(images) == 1 else f"{len(images)} images"
            return f"{turn.content}\n\n[The student attached {plural}, but this model cannot read images.]"

        blocks: list[dict] = []
        for url in images:
            parsed = split_data_url(url)
            if parsed is None:
                # A malformed image is dropped rather than sent: a 400 from the
                # vendor would cost the student a failed request for no reason.
                logger.warning("Dropping an unreadable image attachment for %s", self.model)
                continue
            media_type, payload = parsed
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": payload},
                }
            )
        if turn.content:
            blocks.append({"type": "text", "text": turn.content})
        return blocks or turn.content

    def _build_messages(self, turns: Iterable[ChatTurn]) -> list[dict]:
        """Turns as an Anthropic conversation.

        Anthropic requires the first message to be from the user, so a window
        that happens to open on an assistant reply has that reply trimmed off
        rather than being rejected mid-conversation.
        """
        messages: list[dict] = []
        for turn in turns:
            if turn.role not in {"user", "assistant"}:
                continue
            content = self._content_blocks(turn)
            if not content:
                continue
            if not messages and turn.role != "user":
                continue
            messages.append({"role": turn.role, "content": content})
        return messages

    def _thinking(self, budget: int) -> dict | None:
        """Translate the catalogue's thinking setting into this model's dialect."""
        if self.thinking_mode != "adaptive":
            return None
        if self.uses_adaptive_thinking:
            return {"type": "adaptive"}
        # Pre-adaptive models (Haiku 4.5 and older) need an explicit budget, and
        # it has to leave room inside max_tokens for an actual answer.
        allowance = max(self.MIN_THINKING_BUDGET, budget // 2)
        if allowance >= budget:
            return None
        return {"type": "enabled", "budget_tokens": allowance}

    def complete(
        self, turns, *, system_prompt: str = "", max_tokens: int | None = None, profile: str = "standard"
    ) -> AIResponse:
        # ``profile`` is accepted and not used: Claude takes no sampling
        # parameters, and thinking is the catalogue's decision (thinking_mode),
        # not a per-question one — enabling it here would spend the same
        # max_tokens the answer needs. Claude's care on a hard question comes
        # from the system prompt: visible, numbered working.
        payload, budget = self._payload(turns, system_prompt, max_tokens)

        started = time.monotonic()
        try:
            response = self._client().messages.create(**payload)
        except Exception as exc:
            raise self._translate_error(exc) from exc

        latency_ms = int((time.monotonic() - started) * 1000)

        content = "".join(
            block.text for block in (response.content or []) if getattr(block, "type", "") == "text"
        ).strip()

        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        stop_reason = getattr(response, "stop_reason", "") or ""

        if not content:
            raise self._empty_answer(stop_reason, budget)

        return AIResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            # Anthropic reports the two halves and no total, so the total is the
            # sum of what it actually billed — still never an estimate.
            total_tokens=input_tokens + output_tokens,
            model=getattr(response, "model", "") or self.model,
            request_id=str(getattr(response, "_request_id", "") or getattr(response, "id", "") or ""),
            latency_ms=latency_ms,
            finish_reason=stop_reason,
            raw={
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": int(
                        getattr(usage, "cache_read_input_tokens", 0) or 0
                    ),
                }
            },
        )

    def _payload(self, turns, system_prompt: str, max_tokens: int | None) -> tuple[dict, int]:
        """The request body and the output budget it carries."""
        if not self.is_configured:
            raise AIError(
                "The AI service is not configured yet. Please contact support.",
                code="not_configured",
                retryable=False,
            )

        messages = self._build_messages(turns)
        if not messages:
            raise AIError("There is nothing to send to the assistant.", code="empty_request", retryable=False)

        budget = int(max_tokens or self.max_output_tokens)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": budget,
            "messages": messages,
        }
        if system_prompt:
            payload["system"] = system_prompt

        # Sampling parameters are deliberately never sent. The current Messages
        # API has removed `temperature` and `top_p` from its surface entirely —
        # the newest models reject them with a 400 and the SDK does not accept
        # them as arguments at all. ``self.temperature`` is kept only so the
        # catalogue row can carry one value for every provider; OpenAI uses it.
        thinking = self._thinking(budget)
        if thinking is not None:
            payload["thinking"] = thinking
        return payload, budget

    def _empty_answer(self, stop_reason: str, budget: int) -> AIError:
        if stop_reason == "refusal":
            return AIError(
                "The assistant declined to answer that. Try rephrasing your question.",
                code="refused",
                retryable=False,
            )
        if stop_reason == "max_tokens":
            logger.warning(
                "%s used its whole %s-token budget without producing an answer.", self.model, budget
            )
            return AIError(
                "The assistant ran out of room before it finished answering. "
                "Please try again, or ask a shorter question.",
                code="output_budget_exhausted",
            )
        return AIError("The assistant returned an empty response. Please try again.", code="empty_response")

    # -- streaming ----------------------------------------------------------
    def stream(
        self, turns, *, system_prompt: str = "", max_tokens: int | None = None, profile: str = "standard",
        on_delta=None, cancel: CancelToken | None = None,
    ) -> AIResponse:
        payload, budget = self._payload(turns, system_prompt, max_tokens)
        emit = on_delta or (lambda _text: None)

        started = time.monotonic()
        try:
            events = self._client().messages.create(**payload, stream=True)
        except Exception as exc:
            raise self._translate_error(exc) from exc

        pieces: list[str] = []
        input_tokens = output_tokens = cache_read = 0
        stop_reason = model = request_id = ""
        stopped = False
        response = getattr(events, "response", None)
        if response is not None:
            request_id = str(response.headers.get("request-id") or "")
        if cancel is not None:
            cancel.on_cancel(events.close)
        try:
            for event in events:
                if cancel is not None and cancel.cancelled:
                    stopped = True
                    break
                kind = getattr(event, "type", "")
                if kind == "message_start":
                    message = event.message
                    model = getattr(message, "model", "") or model
                    request_id = request_id or str(getattr(message, "id", "") or "")
                    usage = getattr(message, "usage", None)
                    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
                    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
                elif kind == "content_block_delta":
                    delta = event.delta
                    # Thinking arrives as its own delta type and is never shown.
                    if getattr(delta, "type", "") == "text_delta" and delta.text:
                        pieces.append(delta.text)
                        emit(delta.text)
                elif kind == "message_delta":
                    stop_reason = getattr(event.delta, "stop_reason", "") or stop_reason
                    usage = getattr(event, "usage", None)
                    # Cumulative, and on newer API versions it restates input too.
                    output_tokens = int(getattr(usage, "output_tokens", 0) or 0) or output_tokens
                    input_tokens = int(getattr(usage, "input_tokens", 0) or 0) or input_tokens
        except Exception as exc:
            if cancel is not None and cancel.cancelled:
                stopped = True                      # our own close() ended the read
            else:
                raise self._translate_error(exc) from exc
        finally:
            try:
                events.close()
            except Exception:  # pragma: no cover — closing is best-effort
                pass

        written = "".join(pieces)
        content = written.strip()
        if not content and not stopped:
            raise self._empty_answer(stop_reason, budget)
        estimated = stopped
        if stopped:
            # message_delta, which carries the real output count, comes at the
            # end — so a stopped answer's output is estimated from its text.
            # Input is Anthropic's own number from message_start.
            output_tokens = max(output_tokens, estimate_tokens(written))

        return AIResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            model=model or self.model,
            request_id=request_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            finish_reason=STOPPED if stopped else stop_reason,
            raw={
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": cache_read,
                },
                "estimated": estimated,
                "streamed": True,
            },
        )

    # -- errors -------------------------------------------------------------
    def _translate_error(self, exc: Exception) -> AIError:
        """Turn an SDK exception into something a student can read.

        The vendor's own wording can name models, quotas and endpoints, so it is
        logged and never forwarded.
        """
        if anthropic is None:  # pragma: no cover
            return AIError("The assistant is unavailable.", code="not_configured", retryable=False)

        if isinstance(exc, AIError):  # pragma: no cover — defensive
            return exc

        if isinstance(exc, anthropic.APITimeoutError):
            return AIError(
                "The assistant took too long to respond. Please try again.",
                code="timeout",
            )
        if isinstance(exc, anthropic.APIConnectionError):
            return AIError(
                "We could not reach the assistant. Check your connection and try again.",
                code="network",
            )

        if isinstance(exc, anthropic.APIStatusError):
            status = exc.status_code
            logger.warning(
                "Anthropic error model=%s status=%s detail=%s",
                self.model, status, str(getattr(exc, "message", exc))[:300],
            )
            if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
                return AIError(
                    "The AI service rejected our credentials. Please contact support.",
                    code="auth", status=status, retryable=False,
                )
            if isinstance(exc, anthropic.NotFoundError):
                return AIError(
                    "That model is not available right now. Pick another model.",
                    code="model_unavailable", status=status, retryable=False,
                )
            if isinstance(exc, anthropic.RateLimitError):
                return AIError(
                    "The assistant is busy right now. Please wait a moment and try again.",
                    code="rate_limited", status=status,
                )
            if status == 402:
                return AIError(
                    "The assistant is temporarily unavailable. Please contact support.",
                    code="provider_credit", status=status, retryable=False,
                )
            if status == 413:
                return AIError(
                    "That request is too large. Try a shorter message or fewer attachments.",
                    code="too_large", status=status, retryable=False,
                )
            if status and status >= 500:
                return AIError(
                    "The assistant is temporarily unavailable. Please try again shortly.",
                    code="provider_down", status=status,
                )
            if status == 400:
                detail = str(getattr(exc, "message", "")).lower()
                if "image" in detail:
                    return AIError(
                        "That image could not be processed. Try a different image.",
                        code="bad_image", status=status, retryable=False,
                    )
                return AIError(
                    "The assistant could not process that request. Try rephrasing your message.",
                    code="bad_request", status=status, retryable=False,
                )
            return AIError(
                "Something went wrong with the assistant.", code="unknown", status=status
            )

        logger.exception("Unexpected Anthropic failure model=%s", self.model)
        return AIError("Something went wrong talking to the assistant.", code="transport")
