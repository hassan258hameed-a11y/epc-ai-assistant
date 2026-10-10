"""Stability AI — generation and image-to-image editing over Stable Image.

Unlike the other two, this one hands back the picture itself rather than JSON
wrapping it, so a successful response is simply bytes.  Both calls are multipart
because the API asks for one even when nothing is being uploaded, which is why
each sends a throwaway field alongside the real ones.
"""
from __future__ import annotations

import time

from AI.services.imaging.base import GeneratedImage, ImageProvider
from AI.services.providers.base import AIError


class StabilityImages(ImageProvider):
    slug = "stability"
    name = "Stability AI"
    default_model = "core"
    known_models = ("core", "ultra", "sd3.5-large", "sd3.5-medium")
    env_key = "STABILITY_API_KEY"
    env_base = "STABILITY_BASE_URL"
    default_base = "https://api.stability.ai/v2beta/stable-image"
    supports_edit = True
    supports_quality = False

    #: Stability takes an aspect ratio, like Gemini.
    _RATIOS = {
        "1024x1024": "1:1",
        "1024x1536": "2:3",
        "1536x1024": "3:2",
        "auto": "1:1",
    }

    def _headers(self) -> dict:
        # ``Accept: image/*`` is what asks for raw bytes; without it the API
        # answers with base64 wrapped in JSON.
        return {"Authorization": f"Bearer {self.config.api_key}", "Accept": "image/*"}

    def _endpoint(self) -> str:
        model = (self.model or "core").strip()
        if model in {"core", "ultra"}:
            return f"{self.base_url}/generate/{model}"
        return f"{self.base_url}/generate/sd3"

    def generate(self, prompt: str) -> GeneratedImage:
        data = {
            "prompt": prompt[:4000],
            "output_format": "png",
            "aspect_ratio": self._RATIOS.get(self.config.size, "1:1"),
        }
        if self.model not in {"core", "ultra"}:
            data["model"] = self.model

        started = time.monotonic()
        response = self._post(
            self._endpoint(),
            headers=self._headers(),
            # A multipart body is required even with no file to send.
            files={"none": (None, "")},
            data=data,
        )
        return self._read(response, prompt, started)

    def edit(self, prompt: str, image: bytes, *, mime: str = "image/png") -> GeneratedImage:
        """Re-draw the picture following an instruction.

        Uses image-to-image with a high strength rather than inpainting: there
        is no mask to send from a chat composer, and "change the sky" means the
        whole picture is fair game.
        """
        extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
        started = time.monotonic()
        response = self._post(
            f"{self.base_url}/control/structure",
            headers=self._headers(),
            files={"image": (f"source.{extension}", image, mime)},
            data={"prompt": prompt[:4000], "output_format": "png", "control_strength": "0.6"},
        )
        return self._read(response, prompt, started)

    def _read(self, response, prompt: str, started: float) -> GeneratedImage:
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            raise self._fail(response)

        raw = response.content
        if not raw:
            raise AIError("The image service returned nothing. Please try again.", code="empty_response")
        return self._wrap(raw, prompt=prompt, latency_ms=latency_ms, model=self.model)
