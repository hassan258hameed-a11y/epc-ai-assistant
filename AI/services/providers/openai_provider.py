"""OpenAI — Chat Completions.

Deliberately self-contained: everything that is specific to OpenAI's wire
format, its reasoning-model quirks and its error bodies lives in this file and
nowhere else.  Adding a vendor never means editing this one.

Works with OpenAI directly and with any reseller exposing the same schema —
point ``OPENAI_BASE_URL`` at their endpoint.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable

import requests
from django.conf import settings

from AI.services.providers.base import (
    STOPPED,
    AIError,
    AIResponse,
    BaseProvider,
    CancelToken,
    ChatTurn,
    estimate_tokens,
)
from AI.services.question_profile import DEEP, DEEP_REASONING_BUDGET, DEEP_TEMPERATURE

logger = logging.getLogger("AI.provider.openai")


class OpenAICompatibleProvider(BaseProvider):
    """Chat Completions client.

    Works with OpenAI directly and with any reseller exposing the same schema —
    point ``OPENAI_BASE_URL`` at their endpoint.
    """

    name = "openai"

    #: Families that think before answering. They reject ``max_tokens`` and any
    #: ``temperature`` other than the default, and they spend part of the output
    #: budget on hidden reasoning.
    REASONING_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")

    #: A reasoning model given too small a budget spends all of it thinking and
    #: returns an empty reply — while still billing for the tokens. This floor
    #: keeps enough room for an actual answer.
    MIN_REASONING_BUDGET = 2048

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
        # The router configures every vendor from the same catalogue row, so a
        # setting that means nothing here is accepted and ignored rather than
        # raising. Adding a field for one vendor must not break the others.
        **_ignored: Any,
    ):
        self.thinking_mode = (thinking_mode or "off").lower()
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.base_url = (base_url or settings.OPENAI_BASE_URL).rstrip("/")
        self.model = model or settings.OPENAI_MODEL
        self.timeout = timeout or settings.OPENAI_TIMEOUT
        self.temperature = settings.AI_TEMPERATURE if temperature is None else float(temperature)
        self.max_output_tokens = max_output_tokens or settings.OPENAI_MAX_OUTPUT_TOKENS
        #: ``None`` means "decide from the model name"; a bool is the catalogue's
        #: answer, which is authoritative because an administrator set it.
        self._vision_override = vision

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def is_reasoning_model(self) -> bool:
        model = (self.model or "").lower()
        return any(model.startswith(prefix) for prefix in self.REASONING_PREFIXES)

    def _output_budget(self, requested: int | None) -> int:
        budget = requested or self.max_output_tokens
        if self.is_reasoning_model:
            # A reasoning model given less than this spends it all thinking and
            # returns nothing — while still billing for it. So the floor is
            # raised, which is exactly why a caller has to ask what a request
            # will really cost rather than assume it got the cap it asked for.
            return max(budget, self.MIN_REASONING_BUDGET)
        return budget

    def effective_output_budget(self, requested: int | None = None) -> int:
        return self._output_budget(requested)

    @property
    def supports_vision(self) -> bool:
        if not settings.AI_VISION_ENABLED:
            return False
        if self._vision_override is not None:
            return bool(self._vision_override)
        vision_models = ("gpt-4o", "gpt-4.1", "gpt-4-turbo", "o4", "gpt-5", "gpt-6")
        return any(tag in self.model for tag in vision_models)

    # -- payload ----------------------------------------------------------
    def _build_messages(self, turns: Iterable[ChatTurn], system_prompt: str) -> list[dict]:
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        for turn in turns:
            images = turn.images()
            if images and self.supports_vision and turn.role == "user":
                parts: list[dict] = []
                if turn.content:
                    parts.append({"type": "text", "text": turn.content})
                for url in images:
                    parts.append({"type": "image_url", "image_url": {"url": url, "detail": "auto"}})
                messages.append({"role": turn.role, "content": parts})
            else:
                content = turn.content
                if images and not self.supports_vision:
                    plural = "an image" if len(images) == 1 else f"{len(images)} images"
                    content = f"{content}\n\n[The student attached {plural}, but this model cannot read images.]"
                messages.append({"role": turn.role, "content": content})
        return messages

    def _reasoning_effort(self, profile: str, budget: int) -> str | None:
        """``reasoning_effort`` for this request, or ``None`` for the vendor's default.

        "low" keeps the reasoning that earns its place and drops the rest: a
        reasoning model bills its hidden thinking as output, and on an everyday
        question that thinking is most of the cost. A maths or code question is
        where the rest earns its place too, so it gets "medium" — but only with
        room to use it, or the model thinks through the whole budget and
        returns nothing.
        """
        if self.thinking_mode == "adaptive":
            return None                     # the catalogue asked for the vendor's own judgement
        if profile == DEEP and budget >= DEEP_REASONING_BUDGET:
            return "medium"
        return "low"

    def _post(self, payload: dict) -> tuple[Any, dict, int]:
        """Send one request: ``(response, body, latency_ms)``, or raise :class:`AIError`."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        started = time.monotonic()
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                data=json.dumps(payload),
                timeout=self.timeout,
            )
        except requests.Timeout:
            raise AIError(
                "The assistant took too long to respond. Please try again.", code="timeout", retryable=True
            )
        except requests.ConnectionError:
            raise AIError(
                "We could not reach the assistant. Check your connection and try again.",
                code="network",
                retryable=True,
            )
        except requests.RequestException as exc:  # pragma: no cover
            logger.exception("Unexpected provider transport failure")
            raise AIError("Something went wrong talking to the assistant.", code="transport") from exc

        latency_ms = int((time.monotonic() - started) * 1000)

        if response.status_code >= 400:
            raise self._translate_error(response)

        try:
            data = response.json()
        except ValueError:
            raise AIError("The assistant returned an unreadable response.", code="bad_response")
        return response, data, latency_ms

    @staticmethod
    def _read(data: dict) -> tuple[str, str, dict, int]:
        """``(content, finish_reason, usage, reasoning_tokens)`` from a response body."""
        try:
            choice = data["choices"][0]
            content = (choice["message"].get("content") or "").strip()
            finish_reason = choice.get("finish_reason", "")
        except (KeyError, IndexError, TypeError):
            raise AIError("The assistant returned an empty response.", code="empty_response")
        usage = data.get("usage") or {}
        reasoning_tokens = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        return content, finish_reason, usage, reasoning_tokens

    def _require_configured(self) -> None:
        if not self.is_configured:
            raise AIError(
                "The AI service is not configured yet. Please contact support.",
                code="not_configured",
                retryable=False,
            )

    def _payload(self, turns, system_prompt: str, max_tokens: int | None, profile: str) -> tuple[dict, int]:
        """The request body and the output budget it carries."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._build_messages(turns, system_prompt),
        }

        # Reasoning models (gpt-5 family, o-series) renamed the output limit and
        # accept only the default temperature; older chat models take both.
        budget = self._output_budget(max_tokens)
        if self.is_reasoning_model:
            payload["max_completion_tokens"] = budget
            effort = self._reasoning_effort(profile, budget)
            if effort:
                payload["reasoning_effort"] = effort
        else:
            payload["max_tokens"] = budget
            if profile == DEEP:
                # Maths and code have one right answer: sampling freedom only
                # adds variance, and the repetition penalties below would punish
                # a digit or a variable name for appearing twice — which, in a
                # worked solution or a program, it has to.
                payload["temperature"] = min(self.temperature, DEEP_TEMPERATURE)
            else:
                payload["temperature"] = self.temperature
                # Nudges the model away from restating the question and away from
                # padding a finished answer — fewer tokens for the same content.
                payload["presence_penalty"] = settings.AI_PRESENCE_PENALTY
                payload["frequency_penalty"] = settings.AI_FREQUENCY_PENALTY
        return payload, budget

    def _empty_answer(self, finish_reason: str, reasoning_tokens: int, budget: int) -> AIError:
        # A reasoning model that hits the limit while still thinking returns
        # nothing. That is our budget being too small, not the student's
        # fault, so it is reported as retryable and charged to nobody.
        if finish_reason == "length" and reasoning_tokens:
            logger.warning(
                "%s exhausted its %s-token budget on reasoning (%s reasoning tokens) "
                "and returned no answer. Raise OPENAI_MAX_OUTPUT_TOKENS.",
                self.model, budget, reasoning_tokens,
            )
            return AIError(
                "The assistant ran out of room before it finished answering. "
                "Please try again, or ask a shorter question.",
                code="reasoning_budget_exhausted",
            )
        return AIError("The assistant returned an empty response. Please try again.", code="empty_response")

    def complete(
        self, turns, *, system_prompt: str = "", max_tokens: int | None = None, profile: str = "standard"
    ) -> AIResponse:
        self._require_configured()
        payload, budget = self._payload(turns, system_prompt, max_tokens, profile)

        response, data, latency_ms = self._post(payload)
        content, finish_reason, usage, reasoning_tokens = self._read(data)

        if not content and finish_reason == "length" and payload.get("reasoning_effort") == "medium":
            # Thought through the whole budget and wrote nothing. Asked again at
            # the everyday effort rather than failing the student: "medium" was
            # our choice, not theirs, so the wasted attempt is not billed to them.
            logger.warning(
                "%s spent its %s-token budget reasoning at medium effort; retrying at low.",
                self.model, budget,
            )
            payload["reasoning_effort"] = "low"
            response, data, retry_ms = self._post(payload)
            latency_ms += retry_ms
            content, finish_reason, usage, reasoning_tokens = self._read(data)

        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))

        if not content:
            raise self._empty_answer(finish_reason, reasoning_tokens, budget)

        return AIResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            model=data.get("model") or self.model,
            request_id=str(data.get("id") or response.headers.get("x-request-id") or ""),
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            reasoning_tokens=reasoning_tokens,
            raw={"usage": usage},
        )

    # -- streaming ----------------------------------------------------------
    #: Bytes read at a time. Small, because an endpoint that does not use
    #: chunked transfer would otherwise hold text back until a read fills.
    STREAM_READ_BYTES = 64

    def stream(
        self, turns, *, system_prompt: str = "", max_tokens: int | None = None, profile: str = "standard",
        on_delta=None, cancel: CancelToken | None = None,
    ) -> AIResponse:
        self._require_configured()
        payload, budget = self._payload(turns, system_prompt, max_tokens, profile)
        emit = on_delta or (lambda _text: None)

        part = self._stream_once(payload, emit, cancel)
        if (
            not part["content"] and not part["stopped"] and part["finish_reason"] == "length"
            and payload.get("reasoning_effort") == "medium"
        ):
            # Same rescue as complete(): nothing was written, so nothing was
            # shown, and asking again at the everyday effort is invisible.
            logger.warning(
                "%s spent its %s-token budget reasoning at medium effort; retrying at low.", self.model, budget,
            )
            payload["reasoning_effort"] = "low"
            latency = part["latency_ms"]
            part = self._stream_once(payload, emit, cancel)
            part["latency_ms"] += latency

        content = part["content"].strip()
        usage = part["usage"]
        reasoning_tokens = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        if not content and not part["stopped"]:
            raise self._empty_answer(part["finish_reason"], reasoning_tokens, budget)

        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        estimated = not usage
        if estimated:
            # Usage arrives in the last chunk. A stopped stream never gets it,
            # and some compatible endpoints never send it at all; either way
            # the tokens were spent, so they are estimated rather than skipped.
            input_tokens = self._estimate_input(payload)
            output_tokens = estimate_tokens(part["content"])
            if not part["stopped"]:
                logger.warning("%s streamed without reporting usage; the charge is estimated.", self.model)
        total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))

        return AIResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            model=part["model"] or self.model,
            request_id=part["request_id"],
            latency_ms=part["latency_ms"],
            finish_reason=STOPPED if part["stopped"] else part["finish_reason"],
            reasoning_tokens=reasoning_tokens,
            raw={"usage": usage, "estimated": estimated, "streamed": True},
        )

    @staticmethod
    def _estimate_input(payload: dict) -> int:
        total = 0
        for message in payload.get("messages") or []:
            content = message.get("content")
            if isinstance(content, str):
                total += estimate_tokens(content)
                continue
            for part in content or []:
                if part.get("type") == "text":
                    total += estimate_tokens(part.get("text") or "")
                else:
                    total += 765          # one image tile set at "auto" detail
        return total

    def _stream_once(self, payload: dict, emit, cancel: CancelToken | None) -> dict:
        """One streamed request, read to the end or until ``cancel`` fires."""
        body = dict(payload, stream=True, stream_options={"include_usage": True})
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        started = time.monotonic()
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                data=json.dumps(body),
                # (connect, between bytes): a reasoning model is silent while
                # it thinks, so the read allowance is the whole-answer one.
                timeout=(10, self.timeout),
                stream=True,
            )
        except requests.Timeout:
            raise AIError("The assistant took too long to respond. Please try again.", code="timeout")
        except requests.ConnectionError:
            raise AIError(
                "We could not reach the assistant. Check your connection and try again.", code="network",
            )
        except requests.RequestException as exc:  # pragma: no cover
            logger.exception("Unexpected provider transport failure")
            raise AIError("Something went wrong talking to the assistant.", code="transport") from exc

        part = {
            "content": "", "finish_reason": "", "usage": {}, "model": "", "stopped": False,
            "request_id": str(response.headers.get("x-request-id") or ""), "latency_ms": 0,
        }
        try:
            if response.status_code >= 400:
                raise self._translate_error(response)

            if "text/event-stream" not in (response.headers.get("Content-Type") or ""):
                # The endpoint ignored ``stream`` and answered in one piece.
                try:
                    data = response.json()
                except ValueError:
                    raise AIError("The assistant returned an unreadable response.", code="bad_response")
                content, finish_reason, usage, _reasoning = self._read(data)
                if content:
                    emit(content)
                part.update(content=content, finish_reason=finish_reason, usage=usage,
                            model=data.get("model") or "", request_id=str(data.get("id") or part["request_id"]))
                return part

            if cancel is not None:
                cancel.on_cancel(response.close)
            pieces: list[str] = []
            try:
                for line in response.iter_lines(chunk_size=self.STREAM_READ_BYTES):
                    if cancel is not None and cancel.cancelled:
                        part["stopped"] = True
                        break
                    if not line or not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        detail = (chunk["error"] or {}).get("message", "") if isinstance(chunk["error"], dict) else ""
                        logger.warning("AI provider stream error detail=%s", str(detail)[:300])
                        raise AIError(
                            "The assistant stopped part-way through. Please try again.", code="stream_error",
                        )
                    part["request_id"] = part["request_id"] or str(chunk.get("id") or "")
                    part["model"] = chunk.get("model") or part["model"]
                    if chunk.get("usage"):
                        part["usage"] = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            pieces.append(text)
                            emit(text)
                        if choice.get("finish_reason"):
                            part["finish_reason"] = choice["finish_reason"]
            except AIError:
                raise
            except Exception as exc:
                if cancel is not None and cancel.cancelled:
                    part["stopped"] = True          # our own close() ended the read
                else:
                    logger.warning("AI provider stream broke off: %s", type(exc).__name__)
                    raise AIError(
                        "The connection to the assistant dropped part-way through. Please try again.",
                        code="network",
                    ) from exc
            part["content"] = "".join(pieces)
            return part
        finally:
            part["latency_ms"] = int((time.monotonic() - started) * 1000)
            response.close()

    def _translate_error(self, response) -> AIError:
        status = response.status_code
        detail = ""
        try:
            body = response.json()
            detail = ((body.get("error") or {}).get("message")) or body.get("message") or ""
        except ValueError:
            detail = response.text[:200]

        logger.warning("AI provider error status=%s detail=%s", status, detail[:300])

        if status == 401 or status == 403:
            return AIError(
                "The AI service rejected our credentials. Please contact support.",
                code="auth", status=status, retryable=False,
            )
        if status == 429:
            return AIError(
                "The assistant is busy right now. Please wait a moment and try again.",
                code="rate_limited", status=status,
            )
        if status == 400 and "image" in detail.lower():
            return AIError(
                "That image could not be processed. Try a different image.",
                code="bad_image", status=status, retryable=False,
            )
        if status == 400:
            return AIError(
                "The assistant could not process that request. Try rephrasing your message.",
                code="bad_request", status=status, retryable=False,
            )
        if status >= 500:
            return AIError(
                "The assistant is temporarily unavailable. Please try again shortly.",
                code="provider_down", status=status,
            )
        return AIError("Something went wrong with the assistant.", code="unknown", status=status)
