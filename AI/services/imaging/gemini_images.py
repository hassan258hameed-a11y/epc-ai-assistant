"""Google Gemini images — generation and editing through generateContent.

Gemini has no separate image endpoint: a picture is a *response modality* of an
ordinary content call.  Editing is the same call with the source picture sent as
an extra inline part, which is why both methods here differ by one list entry.

The picture comes back as base64 inside a part, alongside any text the model
felt like adding, so the parts are walked rather than indexed.
"""
from __future__ import annotations

import base64
import json
import time

from AI.services.imaging.base import GeneratedImage, ImageProvider, decode_b64
from AI.services.providers.base import AIError


class GeminiImages(ImageProvider):
    slug = "gemini"
    name = "Google Gemini"
    default_model = "gemini-2.5-flash-image"
    known_models = ("gemini-2.5-flash-image", "imagen-4.0-generate-001")
    env_key = "GEMINI_API_KEY"
    env_base = "GEMINI_BASE_URL"
    default_base = "https://generativelanguage.googleapis.com/v1beta"
    supports_edit = True
    #: No quality dial on this API; the model decides.
    supports_quality = False

    #: Gemini takes an aspect ratio rather than a pixel size.
    _RATIOS = {
        "1024x1024": "1:1",
        "1024x1536": "2:3",
        "1536x1024": "3:2",
        "auto": "1:1",
    }

    def _url(self) -> str:
        return f"{self.base_url}/models/{self.model}:generateContent"

    def _headers(self) -> dict:
        # The key goes in a header rather than the query string, so it never
        # lands in a proxy log or an error message that quotes the URL.
        return {"x-goog-api-key": self.config.api_key, "Content-Type": "application/json"}

    def _body(self, parts: list[dict]) -> str:
        return json.dumps(
            {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "responseModalities": ["IMAGE"],
                    "imageConfig": {"aspectRatio": self._RATIOS.get(self.config.size, "1:1")},
                },
            }
        )

    def generate(self, prompt: str) -> GeneratedImage:
        started = time.monotonic()
        response = self._post(
            self._url(), headers=self._headers(), data=self._body([{"text": prompt[:4000]}])
        )
        return self._read(response, prompt, started)

    def edit(self, prompt: str, image: bytes, *, mime: str = "image/png") -> GeneratedImage:
        parts = [
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode()}},
            {"text": prompt[:4000]},
        ]
        started = time.monotonic()
        response = self._post(self._url(), headers=self._headers(), data=self._body(parts))
        return self._read(response, prompt, started)

    def _read(self, response, prompt: str, started: float) -> GeneratedImage:
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            raise self._fail(response)

        try:
            body = response.json()
        except ValueError:
            raise AIError("The image service returned an unreadable response.", code="bad_response")

        candidates = body.get("candidates") or []
        if not candidates:
            # A prompt refused before generation comes back as an empty
            # candidate list plus a reason, not as an HTTP error.
            feedback = (body.get("promptFeedback") or {}).get("blockReason") or ""
            if feedback:
                raise AIError(
                    "That description was rejected by the image service's content rules. "
                    "Try describing something else.",
                    code="rejected_prompt", retryable=False,
                )
            raise AIError("The image service returned nothing. Please try again.", code="empty_response")

        parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            data = inline.get("data")
            if data:
                return self._wrap(
                    decode_b64(data), prompt=prompt, latency_ms=latency_ms, model=self.model
                )

        raise AIError(
            "The image service returned nothing usable. Please try again.", code="empty_response"
        )
