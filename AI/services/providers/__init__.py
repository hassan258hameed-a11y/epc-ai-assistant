"""The provider registry — the one place that maps a vendor slug to code.

Adding a vendor is three steps and touches nothing else:

1. write ``AI/services/providers/<vendor>_provider.py`` implementing
   :class:`~AI.services.providers.base.BaseProvider`,
2. add it to :data:`REGISTRY` below,
3. create the ``AIProvider`` row (and its models) in the admin console.

The chat runtime, the router, the token ledger and the templates all stay as
they are, which is the point of keeping this file boring.
"""
from __future__ import annotations

import logging

from AI.services.providers.anthropic_provider import AnthropicProvider
from AI.services.providers.base import (
    AIError,
    AIResponse,
    BaseProvider,
    ChatTurn,
    split_data_url,
)
from AI.services.providers.openai_provider import OpenAICompatibleProvider

logger = logging.getLogger("AI.provider")

#: Vendor slug -> implementation. The slug is what ``AIModel.provider`` stores
#: and what ``AIProvider.slug`` must match.
REGISTRY: dict[str, type[BaseProvider]] = {
    "openai": OpenAICompatibleProvider,
    "anthropic": AnthropicProvider,
}

#: Presentation metadata for vendors we ship with, used when seeding and by the
#: admin console. Unknown vendors still work; they just render by slug.
PROVIDER_META: dict[str, dict] = {
    "openai": {
        "name": "OpenAI",
        "description": "GPT models, called over the Chat Completions API.",
        "website": "https://platform.openai.com",
        "sort_order": 10,
    },
    "anthropic": {
        "name": "Anthropic",
        "description": "Claude models, called over the Messages API.",
        "website": "https://platform.claude.com",
        "sort_order": 20,
    },
}


class UnknownProvider(Exception):
    """A model names a vendor with no implementation behind it."""

    def __init__(self, slug: str):
        self.slug = slug
        super().__init__(f"No provider is registered for '{slug}'.")


def available_slugs() -> list[str]:
    return sorted(REGISTRY)


def build(slug: str | None, **overrides) -> BaseProvider:
    """Instantiate the vendor client for ``slug``.

    Raises :class:`UnknownProvider` rather than silently falling back, because
    quietly answering a Claude request with GPT would be a billing error the
    student would never see.
    """
    key = (slug or "").strip().lower()
    provider_class = REGISTRY.get(key)
    if provider_class is None:
        raise UnknownProvider(key or "(unset)")
    return provider_class(**overrides)


def for_model(ai_model) -> BaseProvider:
    """Build the vendor client configured from one catalogue row.

    Everything the vendor needs — the api model id, the output budget, the
    vision flag, the thinking setting, and the credential — comes from the row,
    so an administrator can retune a model without a deploy.

    The credential is resolved from the server environment by name: the row
    stores which setting holds the key, never the key itself.
    """
    return build(
        ai_model.provider,
        model=ai_model.key,
        # Blank falls through to the vendor's default key inside the client, so
        # a model with no credential of its own behaves exactly as before.
        api_key=ai_model.api_key,
        temperature=float(ai_model.temperature),
        max_output_tokens=ai_model.max_output_tokens or None,
        vision=ai_model.supports_vision,
        thinking_mode=ai_model.thinking_mode,
    )


def is_configured(slug: str) -> bool:
    """Does this vendor have credentials? Never reveals what they are."""
    try:
        return build(slug).is_configured
    except UnknownProvider:
        return False
    except Exception:  # pragma: no cover — a status check must never raise
        logger.exception("Could not check configuration for provider=%s", slug)
        return False


__all__ = [
    "AIError",
    "AIResponse",
    "AnthropicProvider",
    "BaseProvider",
    "ChatTurn",
    "OpenAICompatibleProvider",
    "PROVIDER_META",
    "REGISTRY",
    "UnknownProvider",
    "available_slugs",
    "build",
    "for_model",
    "is_configured",
    "split_data_url",
]
