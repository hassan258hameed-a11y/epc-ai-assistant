"""Reduce the catalogue to the one model this deployment is configured for.

Migration 0004 seeded three suggested OpenAI models. The API key is only known
to be provisioned for the one in ``OPENAI_MODEL``, and offering students a
picker full of models that may fail is worse than offering none — so the
suggestions are removed and the configured model becomes the sole default.

Usage history is not touched. ``AIUsageLog.model_key`` is a plain string, so
past spending on a removed model stays in the activity log and is still
reported, as a retired model, by ``model_service.usage_by_model``.
"""
from django.conf import settings
from django.db import migrations

FRIENDLY = {
    "gpt-5": ("Atlas 5", "Deepest reasoning. Best for hard multi-step problems.", 6144),
    "gpt-5-mini": (
        "Atlas 5 Mini",
        "Reasons before answering, then explains its working. Built for study questions.",
        4096,
    ),
    "gpt-5-nano": ("Atlas 5 Nano", "Fastest and cheapest of the reasoning models.", 3072),
    "gpt-4o": ("Atlas 4", "Strong reasoning and well-structured long answers.", 3072),
    "gpt-4o-mini": ("Atlas 4 Mini", "Fast and economical for everyday study questions.", 2048),
    "gpt-4.1": ("Atlas 4.1", "Large context window with dependable instruction-following.", 3072),
    "gpt-4.1-mini": ("Atlas 4.1 Mini", "Balanced quality and cost.", 2560),
    "o3": ("Atlas O3", "Research-grade reasoning. Slow and expensive.", 6144),
    "o4-mini": ("Atlas O4 Mini", "Compact reasoning model.", 4096),
}


def forwards(apps, schema_editor):
    AIModel = apps.get_model("AI", "AIModel")

    configured = (getattr(settings, "OPENAI_MODEL", "") or "").strip()
    if not configured:
        return

    name, description, budget = FRIENDLY.get(
        configured, (configured, "Added automatically from OPENAI_MODEL.", 2048)
    )

    model, created = AIModel.objects.get_or_create(
        key=configured,
        defaults={
            "name": name,
            "description": description,
            "supports_vision": True,
            "max_output_tokens": budget,
            "sort_order": 1,
        },
    )
    if not created and configured in FRIENDLY:
        # Bring an auto-added row (named after its raw key) up to the real spec.
        fields = {"sort_order": 1}
        if model.name == configured:
            fields.update(name=name, description=description)
        # A row auto-created from OPENAI_MODEL inherits the global output cap,
        # which is too small for a reasoning model: it can spend the whole budget
        # thinking and return nothing. Raise it to this model's own figure.
        if model.max_output_tokens < budget:
            fields["max_output_tokens"] = budget
        AIModel.objects.filter(pk=model.pk).update(**fields)

    AIModel.objects.filter(pk=model.pk).update(is_default=True, is_active=True)
    AIModel.objects.exclude(pk=model.pk).delete()


def backwards(apps, schema_editor):
    """Nothing to restore — the removed rows were suggestions, not history."""


class Migration(migrations.Migration):
    dependencies = [("AI", "0006_default_model_follows_env")]
    operations = [migrations.RunPython(forwards, backwards)]
