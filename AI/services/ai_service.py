"""AI provider layer — the seam between the application and every vendor.

The rest of the application only ever talks to :func:`provider_for`, the
:class:`AIResponse` dataclass and :class:`AIError`, so adding a vendor is a new
module under :mod:`AI.services.providers` plus a catalogue row — never a change
to the chat runtime.

The vendor implementations themselves live in :mod:`AI.services.providers`, one
file per vendor, with no shared payload code between them.  This module keeps
the names the rest of the codebase already imports, and owns speech-to-text,
which is not part of the chat provider contract.

Token accounting uses the *actual* usage reported by the provider — never an
estimate from character counts.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests
from django.conf import settings

from AI.services import providers
from AI.services.providers import (
    AIError,
    AIResponse,
    AnthropicProvider,
    BaseProvider,
    ChatTurn,
    OpenAICompatibleProvider,
    UnknownProvider,
)

logger = logging.getLogger("AI.provider")

#: Kept as a module-level name because older call sites read it directly.
PROVIDERS = providers.REGISTRY


def get_provider(name: str | None = None, **overrides) -> BaseProvider:
    """Resolve a vendor client by slug, defaulting to ``AI_PROVIDER``."""
    return providers.build(name or settings.AI_PROVIDER or "openai", **overrides)


def provider_for(ai_model) -> BaseProvider:
    """Build the vendor client for one catalogue row."""
    if ai_model is None:
        return get_provider()
    return providers.for_model(ai_model)


def provider_status() -> dict:
    """A safe summary of the AI layer for status pages and the chat header.

    Deliberately contains no credential material — only whether each vendor has
    one at all.
    """
    default_slug = (settings.AI_PROVIDER or "openai").lower()
    per_provider = {slug: providers.is_configured(slug) for slug in providers.available_slugs()}

    status = {
        "provider": default_slug,
        "model": settings.OPENAI_MODEL,
        "configured": any(per_provider.values()),
        "vision": settings.AI_VISION_ENABLED,
        "providers": per_provider,
        "base_url": settings.OPENAI_BASE_URL,
    }

    # Prefer the catalogue's default as the headline model, since that is what a
    # student's next message will actually use.
    try:
        from AI.services import model_service

        default = model_service.default_model()
        if default is not None:
            status["model"] = default.key
            status["model_name"] = default.name
            status["provider"] = default.provider
            status["vision"] = settings.AI_VISION_ENABLED and default.supports_vision
            status["configured"] = per_provider.get(default.provider, False)
    except Exception:  # pragma: no cover — status must never raise
        logger.exception("Could not read the model catalogue for provider status")
    status.setdefault("model_name", status["model"])
    return status


@dataclass
class Transcription:
    text: str
    language: str = ""
    duration: float = 0.0
    model: str = ""


def transcribe(audio: bytes, *, filename: str = "speech.webm", mime: str = "audio/webm") -> Transcription:
    """Speech to text, in whatever language was actually spoken.

    Unlike the browser's own speech API — which transcribes the *one* language
    it is told to expect — a server-side transcription endpoint identifies the
    language itself and returns text in that same script. That is what makes a
    single microphone button work for a student who switches between English,
    Urdu and Hindi without touching a setting, and what stops Urdu speech
    coming back romanised.

    Raises :class:`AIError` with a message safe to show a student.
    """
    if not settings.OPENAI_API_KEY:
        raise AIError(
            "Voice input is not configured on this server.", code="not_configured", retryable=False
        )
    if not audio:
        raise AIError("No audio was received.", code="empty_audio", retryable=False)

    base_url = settings.OPENAI_BASE_URL.rstrip("/")
    model = settings.AI_TRANSCRIBE_MODEL

    # ``verbose_json`` carries the detected language and the duration, but only
    # the whisper family accepts it — the newer, more accurate models reject
    # anything but ``json``. Ask each for the most it will give, and treat what
    # is missing as missing rather than assuming it.
    payload = {
        "model": model,
        "response_format": "verbose_json" if model.startswith("whisper") else "json",
    }

    started = time.monotonic()
    try:
        response = requests.post(
            f"{base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            files={"file": (filename, audio, mime)},
            data=payload,
            timeout=settings.OPENAI_TIMEOUT,
        )
    except requests.Timeout:
        raise AIError("Transcription took too long. Try a shorter recording.", code="timeout")
    except requests.ConnectionError:
        raise AIError("We could not reach the transcription service.", code="network")
    except requests.RequestException as exc:  # pragma: no cover
        logger.exception("Transcription transport failure")
        raise AIError("Something went wrong transcribing that.", code="transport") from exc

    elapsed = time.monotonic() - started

    if response.status_code >= 400:
        detail = ""
        try:
            detail = ((response.json().get("error") or {}).get("message")) or ""
        except ValueError:
            detail = response.text[:200]
        logger.warning("Transcription failed status=%s detail=%s", response.status_code, detail[:300])

        if response.status_code in {401, 403}:
            raise AIError(
                "The transcription service rejected our credentials.", code="auth", retryable=False
            )
        if response.status_code == 429:
            raise AIError("Voice input is busy right now. Try again in a moment.", code="rate_limited")
        if response.status_code == 413:
            raise AIError("That recording is too long. Keep it under two minutes.",
                          code="too_long", retryable=False)
        raise AIError("That recording could not be transcribed.", code="bad_audio", retryable=False)

    try:
        data = response.json()
    except ValueError:
        raise AIError("The transcription service returned an unreadable response.", code="bad_response")

    text = (data.get("text") or "").strip()
    if not text:
        raise AIError(
            "No speech was picked up. Try again, a little closer to the microphone.",
            code="no_speech", retryable=False,
        )

    return Transcription(
        text=text,
        # Absent on the models that only speak ``json``. The transcript is
        # already in the right language either way — this is only used to tell
        # the student what we heard, so it is allowed to be empty.
        language=(data.get("language") or "").strip(),
        # Fall back to wall-clock time if the provider omits a duration, and
        # never below a tenth of a second: callers treat this as "how long the
        # recording was", and a zero there would report a clip that does not
        # exist. What is actually charged applies its own floor on top.
        duration=max(0.1, float(data.get("duration") or elapsed)),
        model=model,
    )

@dataclass
class Speech:
    """One synthesised reply, in memory."""

    data: bytes
    mime: str = "audio/mpeg"
    model: str = ""
    characters: int = 0
    latency_ms: int = 0


def synthesize(text: str, *, voice: str = "", fmt: str = "mp3") -> Speech:
    """Read a reply aloud, in whatever language it is written in.

    The voice model is multilingual and infers the language from the text, so
    nothing here has to be told that a reply is in Urdu rather than English —
    which matters, because a student who asked in Urdu is answered in Urdu, and
    a synthesiser locked to one language would read it as gibberish.

    Raises :class:`AIError` with a message that is safe to show a student.
    """
    text = (text or "").strip()
    if not settings.AI_SPEECH_ENABLED:
        raise AIError("Spoken replies are switched off.", code="not_configured", retryable=False)
    if not settings.OPENAI_API_KEY:
        raise AIError(
            "Spoken replies are not configured on this server.", code="not_configured", retryable=False
        )
    if not text:
        raise AIError("There is nothing to read out.", code="empty", retryable=False)

    text = text[: max(1, int(settings.AI_SPEECH_MAX_CHARS))]

    started = time.monotonic()
    try:
        response = requests.post(
            f"{settings.OPENAI_BASE_URL.rstrip('/')}/audio/speech",
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.AI_SPEECH_MODEL,
                "voice": voice or settings.AI_SPEECH_VOICE,
                "input": text,
                "response_format": fmt,
            },
            timeout=settings.OPENAI_TIMEOUT,
        )
    except requests.Timeout:
        raise AIError("Reading that out took too long. Please try again.", code="timeout")
    except requests.ConnectionError:
        raise AIError("We could not reach the speech service.", code="network")
    except requests.RequestException as exc:  # pragma: no cover
        logger.exception("Speech transport failure")
        raise AIError("Something went wrong reading that out.", code="transport") from exc

    latency_ms = int((time.monotonic() - started) * 1000)

    if response.status_code >= 400:
        detail = ""
        try:
            detail = ((response.json().get("error") or {}).get("message")) or ""
        except ValueError:
            detail = response.text[:200]
        logger.warning("Speech failed status=%s detail=%s", response.status_code, detail[:300])
        if response.status_code in {401, 403}:
            raise AIError(
                "The speech service rejected our credentials.", code="auth", retryable=False
            )
        if response.status_code == 429:
            raise AIError("Spoken replies are busy right now. Try again in a moment.", code="rate_limited")
        raise AIError("That reply could not be read out.", code="bad_request", retryable=False)

    audio = response.content
    if not audio:
        raise AIError("The speech service returned nothing.", code="empty_response")

    return Speech(
        data=audio,
        mime="audio/mpeg" if fmt == "mp3" else f"audio/{fmt}",
        model=settings.AI_SPEECH_MODEL,
        characters=len(text),
        latency_ms=latency_ms,
    )
