"""Price the Fable 5.1 and Opus 5 packages, and put them on sale.

They shipped inactive in 0030 because nobody had said what they should cost,
and a package on sale at an invented price is not something you can take back
once somebody has bought one. Now there is a decision — these are the expensive
models, so they sit above everything else — and the packages can open.

The ladder, per 100,000 tokens:

    GPT-5 Mini       $2.49     the cheap everyday model
    Claude 4.5       $3.99
    GPT-4o           $4.99
    Claude 4.5 High  $6.49
    Fable 5.1        $7.49
    Opus 5           $8.99     the most capable, and the dearest to run

Two properties hold at every size, not just at 100,000, because a ladder that
crosses over somewhere in the middle is worse than no ladder: each model is
dearer than the one below it, and within a model the price per million falls as
the package grows, so the larger package is always the better value.

Opus above Fable because Opus is the premium tier. Both above Claude 4.5 High,
which was previously the dearest thing on the shelf.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import migrations

#: model key -> {tokens: price}. Every package is set explicitly rather than
#: scaled from a multiplier, so what a student is charged is a number somebody
#: chose and can be read straight off the page.
PRICES = {
    "claude-fable-5-1": {
        50_000: "4.99",
        100_000: "7.49",
        200_000: "11.49",
    },
    "claude-opus-5": {
        50_000: "5.99",
        100_000: "8.99",
        200_000: "13.99",
    },
}

#: The middle package is the one to highlight, as it is for every other model.
FEATURED_SIZE = 100_000


def forwards(apps, schema_editor):
    Plan = apps.get_model("AI", "Plan")

    for key, sizes in PRICES.items():
        for tokens, price in sizes.items():
            Plan.objects.filter(ai_model__key=key, tokens=tokens).update(
                price=Decimal(price),
                is_active=True,
                is_featured=(tokens == FEATURED_SIZE),
            )


def backwards(apps, schema_editor):
    """Take them off sale again, leaving the rows and their history in place.

    Deliberately not a delete: a package somebody has bought must not vanish,
    and the subscription that came from it points here.
    """
    Plan = apps.get_model("AI", "Plan")
    Plan.objects.filter(ai_model__key__in=PRICES).update(is_active=False, is_featured=False)


class Migration(migrations.Migration):

    dependencies = [("AI", "0031_system_prompt_mentions_images")]

    operations = [migrations.RunPython(forwards, backwards)]
