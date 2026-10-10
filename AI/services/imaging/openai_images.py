"""OpenAI images — generation and editing over the Images API.

Generation is JSON; editing is multipart, because the source picture goes up as
a file.  Both come back as base64 in ``data[0]``, so only the request differs.
"""
from __future__ import annotations

import json
import time

from AI.services.imaging.base import GeneratedImage, ImageProvider, decode_b64
from AI.services.providers.base import AIError


class OpenAIImages(ImageProvider):
    slug = "openai"
    name = "OpenAI"
    default_model = "gpt-image-1"
    known_models = ("gpt-image-1", "gpt-image-1-mini", "dall-e-3")
    env_key = "OPENAI_API_KEY"
    env_base = "OPENAI_BASE_URL"
    default_base = "https://api.openai.com/v1"
    supports_edit = True
    supports_quality = True

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.config.api_key}"}

    def _size(self) -> str:
        # "auto" is only understood by gpt-image-1; anything older wants a real
        # size, so it is resolved here rather than being rejected on the wire.
        size = self.config.size or "1024x1024"
        if size == "auto" and not self.model.startswith("gpt-image"):
            return "1024x1024"
        return size

    def generate(self, prompt: str) -> GeneratedImage:
        payload = {
            "model": self.model,
            "prompt": prompt[:4000],
            "n": 1,
            "size": self._size(),
        }
        quality = (self.config.quality or "").strip().lower()
        if quality and self.model.startswith("gpt-image"):
            payload["quality"] = quality

        started = time.monotonic()
        response = self._post(
            f"{self.base_url}/images/generations",
            headers={**self._headers(), "Content-Type": "application/json"},
            data=json.dumps(payload),
        )
        return self._read(response, prompt, started)

    def edit(self, prompt: str, image: bytes, *, mime: str = "image/png") -> GeneratedImage:
        """Change an existing picture.

        No mask is sent: with a mask the endpoint repaints only the masked
        region, and there is nowhere in a chat composer to draw one. Without it
        the model re-renders the whole image following the instruction, which is
        what "make the sky orange" actually means to somebody typing it.
        """
        extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
        files = {"image": (f"source.{extension}", image, mime)}
        data = {"model": self.model, "prompt": prompt[:4000], "n": "1", "size": self._size()}

        started = time.monotonic()
        response = self._post(
            f"{self.base_url}/images/edits",
            headers=self._headers(),
            files=files,
            data=data,
        )
        return self._read(response, prompt, started)

    def _read(self, response, prompt: str, started: float) -> GeneratedImage:
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            raise self._fail(response)

        try:
            body = response.json()
        except ValueError:
            raise AIError("The image service returned an unreadable response.", code="bad_response")

        items = body.get("data") or []
        if not items:
            raise AIError("The image service returned nothing. Please try again.", code="empty_response")

        first = items[0] or {}
        encoded = first.get("b64_json") or ""
        url = first.get("url") or ""
        if encoded:
            raw = decode_b64(encoded)
        elif url:
            raw = self._download(url)
        else:
            raise AIError(
                "The image service returned nothing usable. Please try again.",
                code="empty_response",
            )

        return self._wrap(
            raw,
            prompt=prompt,
            revised=(first.get("revised_prompt") or "").strip(),
            latency_ms=latency_ms,
            model=body.get("model") or self.model,
        )
