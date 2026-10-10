"""Shared fixtures for tests that need a student who can actually chat.

Tokens are per model now, so "give this user 10,000 tokens" is no longer a
complete instruction — it has to name the model. These helpers make that one
line at the call site, and keep every test funding a student the same way the
admin console does: out of the model's pool, through the ledger.
"""
from __future__ import annotations

from AI.models import AIModel
from AI.services import model_pool_service, wallet_service

#: The seeded catalogue, by the id sent to the provider.
GPT = "gpt-5-mini"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5"


def model_for(key: str = GPT) -> AIModel:
    return AIModel.objects.get(key=key)


def fund_pool(key: str = GPT, amount: int = 5_000_000) -> AIModel:
    """Make sure the admin pool for one model can cover what a test hands out."""
    model = model_for(key)
    shortfall = amount - model_pool_service.balance(model)
    if shortfall > 0:
        model_pool_service.top_up(model, shortfall, description="Test fixture")
    return model


def fund(user, amount: int, key: str = GPT) -> AIModel:
    """Give ``user`` ``amount`` tokens for one model, drawn from that model's pool."""
    model = fund_pool(key)
    model_pool_service.draw(model, amount, student=user)
    wallet_service.credit(user, model, amount, reason="Test allocation")
    return model


def balance(user, key: str = GPT) -> int:
    return wallet_service.balance_of(user, model_for(key))
