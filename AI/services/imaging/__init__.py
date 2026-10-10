"""The image vendors the platform can call, and how one is chosen.

A registry rather than an if-chain, so the admin console can offer exactly what
is implemented — and so adding a fourth vendor is a class plus one line here,
with nothing in the chat runtime to change.
"""
from __future__ import annotations

from AI.services.imaging.base import (
    GeneratedImage,
    ImageConfig,
    ImageProvider,
    VALID_QUALITIES,
    VALID_SIZES,
)
from AI.services.imaging.gemini_images import GeminiImages
from AI.services.imaging.openai_images import OpenAIImages
from AI.services.imaging.stability_images import StabilityImages


class UnknownImageProvider(Exception):
    """The configured slug names a vendor nobody implemented."""


#: In the order an administrator sees them.
REGISTRY: dict[str, type[ImageProvider]] = {
    OpenAIImages.slug: OpenAIImages,
    GeminiImages.slug: GeminiImages,
    StabilityImages.slug: StabilityImages,
}

DEFAULT_SLUG = OpenAIImages.slug


def get_class(slug: str) -> type[ImageProvider]:
    provider = REGISTRY.get((slug or "").strip().lower())
    if provider is None:
        raise UnknownImageProvider(slug)
    return provider


def build(slug: str, config: ImageConfig) -> ImageProvider:
    return get_class(slug)(config)


def catalogue() -> list[dict]:
    """Every vendor, for the settings dropdown and its help text."""
    return [
        {
            "slug": provider.slug,
            "name": provider.name,
            "default_model": provider.default_model,
            "models": list(provider.known_models),
            "supports_edit": provider.supports_edit,
            "supports_quality": provider.supports_quality,
            "env_key": provider.env_key,
        }
        for provider in REGISTRY.values()
    ]


__all__ = [
    "DEFAULT_SLUG",
    "GeneratedImage",
    "ImageConfig",
    "ImageProvider",
    "REGISTRY",
    "UnknownImageProvider",
    "VALID_QUALITIES",
    "VALID_SIZES",
    "build",
    "catalogue",
    "get_class",
]
