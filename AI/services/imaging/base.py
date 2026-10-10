"""What every image vendor has to provide, and what they all share.

The chat runtime asks for one thing — *draw this, or change that* — and gets
bytes back.  Everything that differs between vendors (the URL, the body shape,
where the picture hides in the response, what "1024x1536" is called) is the
provider's problem and never leaks past this interface.

Two capabilities, declared rather than assumed:

* :meth:`ImageProvider.generate` — every vendor has this;
* :meth:`ImageProvider.edit` — only some do, so it is gated on
  :attr:`ImageProvider.supports_edit` and the UI hides the button when it is
  False.  Offering an action that will certainly fail is worse than not
  offering it.

Errors are translated here, once, into messages a student can act on. A vendor's
own text names models, quotas and account states that mean nothing to a student
and give away more than they should, so it goes to the log instead.
"""
from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass, field

import requests

from AI.services.providers.base import AIError

logger = logging.getLogger("AI.image")

#: The sizes the product offers. Each vendor maps these onto whatever it calls
#: them, so an administrator picks from one list whichever vendor is behind it.
VALID_SIZES = ("1024x1024", "1024x1536", "1536x1024", "auto")

VALID_QUALITIES = ("low", "medium", "high")


@dataclass
class GeneratedImage:
    """One picture, in memory, with everything needed to store and bill it."""

    data: bytes
    mime: str = "image/png"
    ext: str = "png"
    model: str = ""
    prompt: str = ""
    #: What the endpoint decided to draw, when it rewrites the prompt.
    revised_prompt: str = ""
    latency_ms: int = 0
    size: str = ""
    quality: str = ""
    provider: str = ""

    @property
    def byte_size(self) -> int:
        return len(self.data)


@dataclass
class ImageConfig:
    """Everything a provider needs to make one call. Built by the service."""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    size: str = "1024x1024"
    quality: str = "medium"
    timeout: int = 180
    extra: dict = field(default_factory=dict)


def sniff(raw: bytes) -> tuple[str, str]:
    """Read the format out of the bytes rather than trusting a header."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp", "webp"
    return "image/png", "png"


def decode_b64(encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise AIError("The generated image was corrupted. Please try again.", code="bad_image")


def translate_http(status_code: int, detail: str, *, vendor: str = "") -> AIError:
    """Turn a vendor status code into something a student can act on."""
    logger.warning("Image call failed vendor=%s status=%s detail=%s", vendor, status_code, detail[:300])
    lowered = (detail or "").lower()

    if status_code in {401, 403}:
        return AIError(
            "Image generation is not available right now. Please contact support.",
            code="auth", status=status_code, retryable=False,
        )
    if status_code == 429:
        return AIError(
            "The image service is busy right now. Please wait a moment and try again.",
            code="rate_limited", status=status_code,
        )
    # A refused prompt is the student's to fix, so it says so plainly rather
    # than reading as a platform fault they can only retry into.
    if status_code in {400, 422} and any(
        word in lowered for word in ("safety", "policy", "moderation", "content_filter", "blocked")
    ):
        return AIError(
            "That description was rejected by the image service's content rules. "
            "Try describing something else.",
            code="rejected_prompt", status=status_code, retryable=False,
        )
    if status_code in {400, 404, 422}:
        return AIError(
            "That description could not be drawn. Try rewording it.",
            code="bad_request", status=status_code, retryable=False,
        )
    if status_code >= 500:
        return AIError(
            "Image generation is temporarily unavailable. Please try again in a moment.",
            code="provider_down", status=status_code,
        )
    return AIError("Something went wrong drawing that image.", code="unknown", status=status_code)


class ImageProvider:
    """One vendor that can draw.

    Subclasses fill in the class attributes and implement :meth:`generate`;
    editing is optional and declared by :attr:`supports_edit`.
    """

    #: Routing key, stored in the admin setting.
    slug: str = ""
    #: What an administrator sees in the dropdown.
    name: str = ""
    #: Used when no model is configured.
    default_model: str = ""
    #: Models this vendor is known to serve, for the console's datalist.
    known_models: tuple[str, ...] = ()
    #: Django settings attribute the credential falls back to.
    env_key: str = ""
    #: Django settings attribute holding an override base URL, if any.
    env_base: str = ""
    #: Where the credential goes when there is no override.
    default_base: str = ""
    supports_edit: bool = False
    #: Whether the vendor accepts a quality hint at all.
    supports_quality: bool = False

    def __init__(self, config: ImageConfig):
        self.config = config

    # -- to implement ------------------------------------------------------
    def generate(self, prompt: str) -> GeneratedImage:  # pragma: no cover - interface
        raise NotImplementedError

    def edit(self, prompt: str, image: bytes, *, mime: str = "image/png") -> GeneratedImage:
        raise AIError(
            f"{self.name} cannot change an existing image. Try generating a new one.",
            code="edit_unsupported", retryable=False,
        )

    # -- shared ------------------------------------------------------------
    @property
    def model(self) -> str:
        return self.config.model or self.default_model

    @property
    def base_url(self) -> str:
        return (self.config.base_url or self.default_base).rstrip("/")

    def _post(self, url: str, **kwargs) -> requests.Response:
        """One HTTP call, with every transport failure already translated."""
        try:
            return requests.post(url, timeout=self.config.timeout, **kwargs)
        except requests.Timeout:
            raise AIError(
                "That image took too long to draw. Try again, or describe something simpler.",
                code="timeout",
            )
        except requests.ConnectionError:
            raise AIError(
                "We could not reach the image service. Check your connection and try again.",
                code="network",
            )
        except requests.RequestException as exc:  # pragma: no cover
            logger.exception("Image transport failure vendor=%s", self.slug)
            raise AIError("Something went wrong drawing that.", code="transport") from exc

    def _fail(self, response: requests.Response) -> AIError:
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                error = body.get("error")
                if isinstance(error, dict):
                    detail = error.get("message") or ""
                elif isinstance(error, str):
                    detail = error
                detail = detail or body.get("message") or ""
                # Stability returns {"errors": ["..."]}
                if not detail and isinstance(body.get("errors"), list):
                    detail = "; ".join(str(item) for item in body["errors"])
        except ValueError:
            detail = response.text[:200]
        return translate_http(response.status_code, detail, vendor=self.slug)

    def _wrap(self, raw: bytes, *, prompt: str, revised: str = "", latency_ms: int = 0,
              model: str = "") -> GeneratedImage:
        if not raw:
            raise AIError("The generated image was empty. Please try again.", code="bad_image")
        mime, ext = sniff(raw)
        return GeneratedImage(
            data=raw,
            mime=mime,
            ext=ext,
            model=model or self.model,
            prompt=prompt,
            revised_prompt=revised,
            latency_ms=latency_ms,
            size=self.config.size,
            quality=self.config.quality,
            provider=self.slug,
        )

    def _download(self, url: str) -> bytes:
        """Fetch a hosted result. Some vendors return a URL instead of bytes."""
        try:
            response = requests.get(url, timeout=self.config.timeout)
        except requests.RequestException:
            raise AIError(
                "The generated image could not be downloaded. Please try again.", code="network"
            )
        if response.status_code >= 400:
            raise AIError(
                "The generated image could not be downloaded. Please try again.", code="bad_image"
            )
        return response.content


__all__ = [
    "GeneratedImage",
    "ImageConfig",
    "ImageProvider",
    "VALID_QUALITIES",
    "VALID_SIZES",
    "decode_b64",
    "sniff",
    "translate_http",
]
