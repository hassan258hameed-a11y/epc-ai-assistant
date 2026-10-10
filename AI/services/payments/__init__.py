"""Pluggable payment providers.

Register a new method by adding it to :data:`PROVIDERS` — checkout, verification
and webhook handling all resolve through this registry.
"""
from __future__ import annotations

from AI.services.payments.aba_payway import ABAPayWayProvider
from AI.services.payments.acleda_khqr import ACLEDAKHQRProvider
from AI.services.payments.acleda_xpay import ACLEDAXPayProvider
from AI.services.payments.base import (
    CheckoutSession,
    Handoff,
    PaymentError,
    PaymentProvider,
    VerificationResult,
)
from AI.services.payments.binance import BinancePayProvider

PROVIDERS: dict[str, type[PaymentProvider]] = {
    ABAPayWayProvider.key: ABAPayWayProvider,
    ACLEDAXPayProvider.key: ACLEDAXPayProvider,
    ACLEDAKHQRProvider.key: ACLEDAKHQRProvider,
    BinancePayProvider.key: BinancePayProvider,
}


def get_provider(key: str) -> PaymentProvider:
    provider_class = PROVIDERS.get(key)
    if provider_class is None:
        raise PaymentError("That payment method is not available.", code="unknown_method")
    return provider_class()


def available_providers() -> list[PaymentProvider]:
    return [provider_class() for provider_class in PROVIDERS.values()]


__all__ = [
    "CheckoutSession",
    "Handoff",
    "PROVIDERS",
    "PaymentError",
    "PaymentProvider",
    "VerificationResult",
    "available_providers",
    "get_provider",
]
