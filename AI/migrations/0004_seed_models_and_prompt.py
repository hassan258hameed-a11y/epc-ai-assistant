"""Seed the AI model catalogue, backfill model keys, and refresh the system prompt.

The prompt is only rewritten when it is still byte-for-byte the old default — an
administrator who has already tuned it keeps their wording.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import migrations

OLD_PROMPT = (
    "You are EPC AI Assistant, a patient and encouraging study partner for students. "
    "Explain concepts clearly and step by step, show your reasoning for maths and science, "
    "and use well-formatted Markdown with fenced code blocks for programming answers. "
    "If a question is ambiguous, ask a short clarifying question. Never fabricate sources."
)

SEED = [
    {
        "key": "gpt-4o-mini",
        "name": "Atlas 4 Mini",
        "description": "Fast and economical. The right default for everyday study questions.",
        "supports_vision": True,
        "input_cost_per_1m": Decimal("0.15"),
        "output_cost_per_1m": Decimal("0.60"),
        "max_output_tokens": 2048,
        "sort_order": 10,
        "is_default": True,
    },
    {
        "key": "gpt-4o",
        "name": "Atlas 4",
        "description": "Stronger reasoning and better long-answer structure. Costs more per reply.",
        "supports_vision": True,
        "input_cost_per_1m": Decimal("2.50"),
        "output_cost_per_1m": Decimal("10.00"),
        "max_output_tokens": 3072,
        "sort_order": 20,
    },
    {
        "key": "gpt-4.1-mini",
        "name": "Atlas 4.1 Mini",
        "description": "Balanced quality and cost, with a large context window.",
        "supports_vision": True,
        "input_cost_per_1m": Decimal("0.40"),
        "output_cost_per_1m": Decimal("1.60"),
        "max_output_tokens": 2560,
        "sort_order": 30,
    },
]


def forwards(apps, schema_editor):
    AIModel = apps.get_model("AI", "AIModel")
    AIUsageLog = apps.get_model("AI", "AIUsageLog")
    Message = apps.get_model("AI", "Message")
    AppSetting = apps.get_model("AI", "AppSetting")

    for spec in SEED:
        AIModel.objects.get_or_create(key=spec["key"], defaults=spec)

    # Historic rows recorded whatever the provider echoed back, which may be a
    # dated snapshot such as "gpt-4o-mini-2024-07-18". Map each onto the longest
    # catalogue key it starts with so per-model reporting includes the past.
    keys = sorted(AIModel.objects.values_list("key", flat=True), key=len, reverse=True)
    for key in keys:
        AIUsageLog.objects.filter(model__startswith=key, model_key="").update(model_key=key)
        Message.objects.filter(model__startswith=key, model_key="").update(model_key=key)

    # Rebuild per-model counters from the backfilled log.
    from django.db.models import Count, Q, Sum

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
    for row in rows:
        AIModel.objects.filter(key=row["model_key"]).update(
            tokens_consumed=row["total"] or 0,
            input_tokens_total=row["inbound"] or 0,
            output_tokens_total=row["outbound"] or 0,
            request_count=row["requests"] or 0,
            failure_count=row["failures"] or 0,
        )

    # Only replace an untouched prompt.
    from AI.services.settings_service import DEFAULT_SYSTEM_PROMPT

    AppSetting.objects.filter(key="ai_system_prompt", value=OLD_PROMPT).update(value=DEFAULT_SYSTEM_PROMPT)


def backwards(apps, schema_editor):
    """Leave the catalogue in place — removing it would discard usage history."""


class Migration(migrations.Migration):
    dependencies = [("AI", "0003_ai_model_catalogue")]
    operations = [migrations.RunPython(forwards, backwards)]
