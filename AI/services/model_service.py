"""The AI model catalogue: resolution, availability and metering.

Two rules live here and are enforced for every request:

* a student may only use a model that is **active** in the catalogue, and
* a model that has spent its own **allocation** stops answering, for everyone.

Both are checked server-side.  The model picker in the chat UI is a convenience;
it is never the control.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from AI.models import AIModel, AIProvider

logger = logging.getLogger("AI.models")


class ModelUnavailable(Exception):
    """The requested model cannot serve this request."""

    def __init__(self, message: str, *, code: str = "model_unavailable"):
        self.friendly_message = message
        self.code = code
        super().__init__(message)


#: Friendly names for models the platform knows about, so an auto-added row is
#: not just a raw provider id in the student's model picker.
KNOWN_NAMES = {
    "gpt-5": ("Atlas 5", "Deepest reasoning. Best for hard multi-step problems."),
    "gpt-5-mini": ("Atlas 5 Mini", "Reasons before answering, at everyday cost. A strong default."),
    "gpt-5-nano": ("Atlas 5 Nano", "Fastest and cheapest of the reasoning models."),
    "gpt-4o": ("Atlas 4", "Strong reasoning and well-structured long answers."),
    "gpt-4o-mini": ("Atlas 4 Mini", "Fast and economical for everyday study questions."),
    "gpt-4.1": ("Atlas 4.1", "Large context window with dependable instruction-following."),
    "gpt-4.1-mini": ("Atlas 4.1 Mini", "Balanced quality and cost, with a large context window."),
    "o3": ("Atlas O3", "Research-grade reasoning. Slow and expensive."),
    "o4-mini": ("Atlas O4 Mini", "Compact reasoning model."),
    "claude-sonnet-5": (
        "Claude Sonnet 5",
        "Anthropic's balanced model. Strong reasoning with a very large context window.",
    ),
    "claude-haiku-4-5": (
        "Claude Haiku 4.5",
        "Anthropic's fastest model. Ideal for quick questions and everyday study help.",
    ),
}

#: Per-model defaults for keys the platform knows about, so an auto-created row
#: arrives sensibly configured. Reasoning models get a larger output budget
#: because part of it is spent thinking before the answer starts.
KNOWN_DEFAULTS = {
    "gpt-5": {"max_output_tokens": 6144, "supports_vision": True},
    "gpt-5-mini": {"max_output_tokens": 4096, "supports_vision": True},
    "gpt-5-nano": {"max_output_tokens": 3072, "supports_vision": True},
    "o3": {"max_output_tokens": 6144, "supports_vision": True},
    "o4-mini": {"max_output_tokens": 4096, "supports_vision": True},
    "gpt-4o": {"max_output_tokens": 3072, "supports_vision": True},
    "gpt-4o-mini": {"max_output_tokens": 2048, "supports_vision": True},
    "gpt-4.1": {"max_output_tokens": 3072, "supports_vision": True},
    "gpt-4.1-mini": {"max_output_tokens": 2560, "supports_vision": True},
    # Anthropic. Thinking stays off by default: on a metered product the
    # student pays for hidden reasoning as output tokens.
    "claude-sonnet-5": {
        "max_output_tokens": 4096, "supports_vision": True, "provider": "anthropic",
    },
    "claude-haiku-4-5": {
        "max_output_tokens": 3072, "supports_vision": True, "provider": "anthropic",
    },
}


def ensure_providers() -> dict[str, AIProvider]:
    """Guarantee a vendor row for every registered provider. Idempotent.

    The registry in :mod:`AI.services.providers` is what code can actually call;
    these rows are what an administrator can switch off. Keeping them in step
    means a newly shipped vendor appears in the console without a data migration.
    """
    from AI.services.providers import PROVIDER_META, available_slugs

    rows = {row.slug: row for row in AIProvider.objects.all()}
    for slug in available_slugs():
        if slug in rows:
            continue
        meta = PROVIDER_META.get(slug, {})
        rows[slug] = AIProvider.objects.create(
            slug=slug,
            name=meta.get("name", slug.title()),
            description=meta.get("description", ""),
            website=meta.get("website", ""),
            sort_order=meta.get("sort_order", 100),
        )
        logger.info("Registered AI provider %s", slug)
    return rows


def ensure_seeded() -> None:
    """Bring the catalogue into a usable state. Safe to call on any request path.

    Three things are guaranteed afterwards:

    * every registered vendor has an :class:`AIProvider` row,
    * every model is linked to its vendor and has an admin token pool, and
    * exactly one active model is the default.

    The models themselves are seeded by migration, not here — an auto-created
    model would be one nobody chose to pay for.
    """
    providers = ensure_providers()

    # Link any model that predates the provider registry, and any row an admin
    # created with a bare slug. Cheap: it only touches unlinked rows.
    unlinked = list(AIModel.objects.filter(provider_ref__isnull=True))
    for model in unlinked:
        provider = providers.get((model.provider or "openai").lower())
        if provider is not None:
            AIModel.objects.filter(pk=model.pk).update(provider_ref=provider)

    from AI.services import model_pool_service

    model_pool_service.ensure_pools()

    if not AIModel.objects.filter(is_default=True, is_active=True).exists():
        first = AIModel.objects.filter(is_active=True).order_by("sort_order", "id").first()
        if first is not None:
            with transaction.atomic():
                AIModel.objects.filter(is_default=True).update(is_default=False)
                AIModel.objects.filter(pk=first.pk).update(is_default=True)


def catalogue(*, active_only: bool = False):
    ensure_seeded()
    queryset = AIModel.objects.select_related("provider_ref", "pool")
    if active_only:
        queryset = queryset.filter(is_active=True, provider_ref__is_active=True)
    return queryset.order_by("sort_order", "name")


def selectable_models() -> list[AIModel]:
    """Models the platform can serve right now, before any per-student check.

    Whether a *particular* student may use one of these is a wallet question,
    answered by :func:`AI.services.wallet_service.overview`.
    """
    return [model for model in catalogue(active_only=True) if not model.is_exhausted]


def by_provider(*, active_only: bool = False) -> list[tuple[AIProvider, list[AIModel]]]:
    """The catalogue grouped by vendor, for screens that separate them visually."""
    grouped: dict[int, list[AIModel]] = {}
    for model in catalogue(active_only=active_only):
        grouped.setdefault(model.provider_ref_id, []).append(model)
    providers = AIProvider.objects.filter(pk__in=grouped).order_by("sort_order", "name")
    return [(provider, grouped[provider.pk]) for provider in providers]


def default_model() -> AIModel | None:
    ensure_seeded()
    queryset = AIModel.objects.select_related("provider_ref")
    return (
        queryset.filter(is_default=True, is_active=True, provider_ref__is_active=True).first()
        or queryset.filter(is_active=True, provider_ref__is_active=True)
        .order_by("sort_order", "id")
        .first()
    )


def resolve(requested_key: str | None = None) -> AIModel:
    """Pick the model for a request, refusing anything a student may not use.

    A blank or unknown key falls back to the default rather than erroring — a
    stale tab should keep working. An explicitly *disabled* or *exhausted* model
    is refused, because honouring it would bypass an administrator's decision.
    """
    ensure_seeded()
    key = (requested_key or "").strip()

    if key:
        model = AIModel.objects.filter(key=key).first()
        if model is None:
            model = default_model()
        elif not model.is_active:
            raise ModelUnavailable(
                f"{model.name} is not available right now. Pick another model.",
                code="model_disabled",
            )
        elif model.is_exhausted:
            raise ModelUnavailable(
                f"{model.name} has used its whole token allocation. "
                "Pick another model, or ask your administrator to top it up.",
                code="model_exhausted",
            )
    else:
        model = default_model()

    if model is None:
        raise ModelUnavailable("No AI model is available. Please contact support.", code="no_models")
    if model.is_exhausted:
        raise ModelUnavailable(
            f"{model.name} has used its whole token allocation. Please contact support.",
            code="model_exhausted",
        )
    return model


# ---------------------------------------------------------------------------
# Metering
# ---------------------------------------------------------------------------
def record_usage(model: AIModel, *, input_tokens: int, output_tokens: int, total_tokens: int) -> None:
    """Add one successful call to a model's running totals.

    Uses F expressions so concurrent requests each land, without a lock and
    without a read-modify-write race.
    """
    AIModel.objects.filter(pk=model.pk).update(
        tokens_consumed=F("tokens_consumed") + max(0, int(total_tokens)),
        input_tokens_total=F("input_tokens_total") + max(0, int(input_tokens)),
        output_tokens_total=F("output_tokens_total") + max(0, int(output_tokens)),
        request_count=F("request_count") + 1,
        last_used_at=timezone.now(),
    )


def record_failure(model: AIModel | None) -> None:
    if model is None:
        return
    AIModel.objects.filter(pk=model.pk).update(
        failure_count=F("failure_count") + 1, last_used_at=timezone.now()
    )


def set_allocation(model: AIModel, allocation: int, *, reset_usage: bool = False) -> AIModel:
    fields = {"token_allocation": max(0, int(allocation))}
    if reset_usage:
        fields.update(
            tokens_consumed=0,
            input_tokens_total=0,
            output_tokens_total=0,
            request_count=0,
            failure_count=0,
        )
    AIModel.objects.filter(pk=model.pk).update(**fields)
    model.refresh_from_db()
    return model


def make_default(model: AIModel) -> None:
    with transaction.atomic():
        AIModel.objects.filter(is_default=True).exclude(pk=model.pk).update(is_default=False)
        AIModel.objects.filter(pk=model.pk).update(is_default=True, is_active=True)


def recalculate_totals() -> int:
    """Rebuild every model's counters from the usage log. A repair tool."""
    from AI.models import AIUsageLog

    rows = (
        AIUsageLog.objects.exclude(model_key="")
        .values("model_key")
        .annotate(
            total=Sum("total_tokens", filter=Q(success=True)),
            inbound=Sum("input_tokens", filter=Q(success=True)),
            outbound=Sum("output_tokens", filter=Q(success=True)),
            requests=Count("id", filter=Q(success=True)),
            failures=Count("id", filter=Q(success=False)),
        )
    )
    updated = 0
    for row in rows:
        updated += AIModel.objects.filter(key=row["model_key"]).update(
            tokens_consumed=row["total"] or 0,
            input_tokens_total=row["inbound"] or 0,
            output_tokens_total=row["outbound"] or 0,
            request_count=row["requests"] or 0,
            failure_count=row["failures"] or 0,
        )
    return updated


def usage_by_model(days: int = 30) -> list[dict]:
    """Per-model usage for the admin console, joined to the catalogue."""
    from datetime import timedelta

    from AI.models import AIUsageLog

    ensure_seeded()
    start = timezone.now() - timedelta(days=days)
    rows = {
        row["model_key"]: row
        for row in AIUsageLog.objects.filter(created_at__gte=start)
        .exclude(model_key="")
        .values("model_key")
        .annotate(
            total=Sum("total_tokens", filter=Q(success=True)),
            inbound=Sum("input_tokens", filter=Q(success=True)),
            outbound=Sum("output_tokens", filter=Q(success=True)),
            requests=Count("id", filter=Q(success=True)),
            failures=Count("id", filter=Q(success=False)),
            students=Count("user_id", distinct=True, filter=Q(success=True)),
        )
    }

    def row_for(model, window):
        return {
            "model": model,
            "period_tokens": int(window.get("total") or 0),
            "period_input": int(window.get("inbound") or 0),
            "period_output": int(window.get("outbound") or 0),
            "period_requests": int(window.get("requests") or 0),
            "period_failures": int(window.get("failures") or 0),
            "students": int(window.get("students") or 0),
            "retired": False,
        }

    live = list(catalogue())
    out = [row_for(model, rows.get(model.key, {})) for model in live]
    out.sort(key=lambda item: item["model"].tokens_consumed, reverse=True)

    # Keys that appear in the usage log but no longer exist in the catalogue —
    # a model an administrator has since removed. Their history is still real
    # spending, so it stays visible rather than silently vanishing.
    known = {model.key for model in live}
    retired = retired_usage(exclude=known)
    for entry in retired:
        entry["period_tokens"] = int(rows.get(entry["key"], {}).get("total") or 0)
        entry["period_requests"] = int(rows.get(entry["key"], {}).get("requests") or 0)
    out.extend(retired)
    return out


def retired_usage(*, exclude: set[str] | None = None) -> list[dict]:
    """Lifetime totals for model keys that are no longer in the catalogue."""
    from AI.models import AIUsageLog

    exclude = exclude or {model.key for model in AIModel.objects.all()}
    rows = (
        AIUsageLog.objects.exclude(model_key="")
        .exclude(model_key__in=exclude)
        .values("model_key")
        .annotate(
            total=Sum("total_tokens", filter=Q(success=True)),
            inbound=Sum("input_tokens", filter=Q(success=True)),
            outbound=Sum("output_tokens", filter=Q(success=True)),
            requests=Count("id", filter=Q(success=True)),
            students=Count("user_id", distinct=True, filter=Q(success=True)),
        )
        .order_by("-total")
    )
    return [
        {
            "model": None,
            "key": row["model_key"],
            "retired": True,
            "tokens_consumed": int(row["total"] or 0),
            "input_tokens_total": int(row["inbound"] or 0),
            "output_tokens_total": int(row["outbound"] or 0),
            "request_count": int(row["requests"] or 0),
            "students": int(row["students"] or 0),
            "period_tokens": 0,
            "period_requests": 0,
        }
        for row in rows
    ]
