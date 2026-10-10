"""The model router — the single place a request is turned into a vendor call.

One function, :func:`route`, answers the only question the chat runtime needs
answered: *given this student and this requested model, which vendor client do I
call, and may they call it?*  Everything that could differ between models is
resolved here, once:

    student + requested key
        -> catalogue row      (which model, and is it switched on?)
        -> wallet check       (do they have access and tokens for it?)
        -> provider client    (which vendor, configured how?)

Because routing lives in one module, adding a vendor never means auditing views
for a missing branch, and no view can accidentally answer a Claude request with
GPT.  Views catch :class:`~AI.services.model_service.ModelUnavailable`,
:class:`~AI.services.wallet_service.ModelAccessDenied` and
:class:`~AI.services.wallet_service.InsufficientModelTokens` and render the
message each already carries.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from AI.models import AIModel, UserAIWallet
from AI.services import model_service, providers, wallet_service
from AI.services.model_service import ModelUnavailable
from AI.services.providers import BaseProvider, UnknownProvider
from AI.services.wallet_service import InsufficientModelTokens, ModelAccessDenied

logger = logging.getLogger("AI.router")


@dataclass
class Route:
    """A resolved, authorised destination for one request."""

    model: AIModel
    provider: BaseProvider
    wallet: UserAIWallet

    @property
    def provider_slug(self) -> str:
        return self.model.provider

    @property
    def api_model_id(self) -> str:
        """What actually goes on the wire, e.g. ``claude-sonnet-5``."""
        return self.model.key


def resolve_model(user, requested_key: str | None = None) -> AIModel:
    """Pick the catalogue row for a request, without touching balances.

    An unknown or blank key falls back to something the student can actually
    use, so a stale browser tab keeps working. A key that names a model the
    administrator has *explicitly* disabled is refused rather than substituted —
    silently answering with a different model would bill a different wallet.
    """
    model_service.ensure_seeded()
    key = (requested_key or "").strip()

    if key:
        model = AIModel.objects.select_related("provider_ref").filter(key=key).first()
        if model is not None:
            if not model.provider_is_active:
                raise ModelUnavailable(
                    f"{model.provider_name} is temporarily unavailable. Please pick another model.",
                    code="provider_disabled",
                )
            if not model.is_active:
                raise ModelUnavailable(
                    f"{model.name} is not available right now. Pick another model.",
                    code="model_disabled",
                )
            if model.is_exhausted:
                raise ModelUnavailable(
                    f"{model.name} has used its whole token allocation. "
                    "Pick another model, or ask your administrator to top it up.",
                    code="model_exhausted",
                )
            return model
        logger.info("Unknown model key %r requested by user=%s; falling back", key[:64], user.pk)

    model = preferred_model(user)
    if model is None:
        raise ModelUnavailable(
            "No AI model is available to you yet. Please contact the administrator.",
            code="no_models",
        )
    return model


def preferred_model(user) -> AIModel | None:
    """The model a student should land on when they have not chosen one.

    A student holding only Haiku tokens should not open the chat pointed at a
    model they cannot send to, so a funded model always beats the catalogue
    default. The default is the fallback, not the rule.
    """
    usable = wallet_service.usable_models(user)
    if usable:
        default = next((model for model in usable if model.is_default), None)
        return default or usable[0]
    return model_service.default_model()


def route(user, requested_key: str | None = None, *, minimum_tokens: int = 1) -> Route:
    """Resolve, authorise and build — the whole routing decision, in order.

    Authorisation happens *before* the vendor client is built and long before it
    is called, so a student with no balance never causes a billable request.
    """
    model = resolve_model(user, requested_key)
    wallet = wallet_service.authorize(user, model, minimum=minimum_tokens)

    try:
        provider = providers.for_model(model)
    except UnknownProvider:
        logger.error("Model %s names unregistered provider %r", model.key, model.provider)
        raise ModelUnavailable(
            f"{model.name} is not available right now. Pick another model.",
            code="provider_missing",
        )

    if not provider.is_configured:
        # Missing credentials are an operator problem, not a student one, and the
        # message must never hint at which credential or where it lives.
        logger.error("Provider %s has no credentials configured", model.provider)
        raise ModelUnavailable(
            f"{model.name} is not available right now. Please contact the administrator.",
            code="not_configured",
        )

    return Route(model=model, provider=provider, wallet=wallet)


__all__ = [
    "InsufficientModelTokens",
    "ModelAccessDenied",
    "ModelUnavailable",
    "Route",
    "preferred_model",
    "resolve_model",
    "route",
]
