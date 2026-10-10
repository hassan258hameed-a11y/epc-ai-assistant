"""Give the plans that predate per-model pricing the model they already use.

Three plans — Starter, Student and Scholar — were written before tokens became
a per-model currency, so their ``ai_model`` is null. That null is not neutral:
``Plan.token_model`` resolves it to the default model, so buying one of these
plans has always credited the default model's wallet. The tokens were
attributed; only the row saying so was missing.

The cost of that gap is that the two halves of a single purchase disagree. The
tokens land on GPT-5 Mini while the payment, which reaches a model only through
``plan.ai_model``, reaches none — so the console's per-model revenue summed to
zero while the all-models figure showed the real total, and no amount of
drilling down could explain the difference.

This writes down what the code already does. No balance, payment, subscription
or token movement is touched: the only change is that a plan now records the
model it has been granting all along. A plan created after per-model pricing
already names its model and is left alone.
"""
from django.db import migrations


def attribute_legacy_plans(apps, schema_editor):
    Plan = apps.get_model("AI", "Plan")
    AIModel = apps.get_model("AI", "AIModel")

    orphans = Plan.objects.filter(ai_model__isnull=True)
    if not orphans.exists():
        return

    # The same model ``Plan.token_model`` falls back to, resolved the same way:
    # the default if one is marked, otherwise the first active model in the
    # catalogue. Using anything else here would move where these plans' tokens
    # are reported without moving where they are actually credited.
    default = (
        AIModel.objects.filter(is_default=True).order_by("sort_order", "pk").first()
        or AIModel.objects.filter(is_active=True).order_by("sort_order", "pk").first()
        or AIModel.objects.order_by("sort_order", "pk").first()
    )
    if default is None:
        # A database with no models at all: nothing to attribute them to, and
        # nothing that could be reading per-model figures either.
        return

    orphans.update(ai_model=default)


def unattribute(apps, schema_editor):
    """Deliberately not reversible in data terms.

    Rolling this back cannot know which plans were null before it ran, and
    guessing would strip the model from plans that legitimately name one. The
    migration is safe to leave applied, so the reverse is a no-op rather than a
    destructive undo.
    """
    return


class Migration(migrations.Migration):

    dependencies = [("AI", "0023_add_gpt4o")]

    operations = [
        migrations.RunPython(attribute_legacy_plans, unattribute),
    ]
