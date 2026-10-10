"""Point the default model at OPENAI_MODEL, and give known keys friendly names.

Migration 0004 seeded a suggested catalogue and made ``gpt-4o-mini`` the
default. That is the wrong choice on an install configured for a different
model: the API key may not even be provisioned for the seeded ones. The model
named in the environment is the one that is known to work, so it takes the
default — unless an administrator has already used a different one, in which
case their working setup is left alone.
"""
from django.conf import settings
from django.db import migrations

FRIENDLY = {
    "gpt-5": ("Atlas 5", "Deepest reasoning. Best for hard multi-step problems."),
    "gpt-5-mini": ("Atlas 5 Mini", "Reasons before answering, at everyday cost. A strong default."),
    "gpt-5-nano": ("Atlas 5 Nano", "Fastest and cheapest of the reasoning models."),
    "gpt-4.1": ("Atlas 4.1", "Large context window with dependable instruction-following."),
    "o3": ("Atlas O3", "Research-grade reasoning. Slow and expensive."),
    "o4-mini": ("Atlas O4 Mini", "Compact reasoning model."),
}


def forwards(apps, schema_editor):
    AIModel = apps.get_model("AI", "AIModel")

    configured = (getattr(settings, "OPENAI_MODEL", "") or "").strip()
    if not configured:
        return

    name, description = FRIENDLY.get(configured, (configured, "Added automatically from OPENAI_MODEL."))
    model, created = AIModel.objects.get_or_create(
        key=configured,
        defaults={
            "name": name,
            "description": description,
            "supports_vision": True,
            "max_output_tokens": getattr(settings, "OPENAI_MAX_OUTPUT_TOKENS", 2048),
            "sort_order": 1,
        },
    )
    if not created and model.name == configured and configured in FRIENDLY:
        AIModel.objects.filter(pk=model.pk).update(name=name, description=description, sort_order=1)

    # Only claim the default if nothing else has actually been used. A catalogue
    # with real traffic reflects a decision that has already been made.
    if AIModel.objects.exclude(pk=model.pk).filter(request_count__gt=0).exists():
        return

    AIModel.objects.exclude(pk=model.pk).update(is_default=False)
    AIModel.objects.filter(pk=model.pk).update(is_default=True, is_active=True)


class Migration(migrations.Migration):
    dependencies = [("AI", "0005_token_allocation_cycle")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
