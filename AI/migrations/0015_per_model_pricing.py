"""Per-model token packages, and a free grant sized to match them.

Tokens are a separate currency per model, so a plan that does not say which
model it sells is ambiguous the moment a second model exists.  The three legacy
plans predate that and sell "tokens" with no model attached; they are retired
here (not deleted — payments point at them) and replaced by three packages for
each of the three models.

The token amounts are not the same across models on purpose.  A Sonnet token
costs the platform roughly nine times what a GPT-5 Mini token does, and about
three times a Haiku one, so the same $3 buys proportionally fewer of them.
Selling identical amounts at identical prices would either give Sonnet away or
make GPT-5 Mini a bad deal.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import migrations

#: (tier, price, order) — the same three price points for every model.
TIERS = (
    ("Starter", Decimal("3.00"), 0),
    ("Standard", Decimal("5.00"), 1),
    ("Pro", Decimal("10.00"), 2),
)

#: model key -> (Starter, Standard, Pro) token amounts, scaled to what a token
#: of that model actually costs to serve.
PACKAGES = {
    "gpt-5-mini": (600_000, 1_200_000, 3_000_000),
    "claude-haiku-4-5": (200_000, 400_000, 1_000_000),
    "claude-sonnet-5": (70_000, 140_000, 350_000),
}

#: What each tier promises, beyond the token count itself.
FEATURES = {
    "Starter": ["Enough for a few weeks of study", "Tokens never expire", "Full chat, voice and file support"],
    "Standard": ["Our most popular size", "Tokens never expire", "Full chat, voice and file support",
                 "Priority during busy periods"],
    "Pro": ["Best value per token", "Tokens never expire", "Full chat, voice and file support",
            "Priority during busy periods", "Image generation included"],
}

BLURB = {
    "gpt-5-mini": "Fast, capable and the best value — the everyday model.",
    "claude-haiku-4-5": "Quick and careful. Strong at reasoning for its size.",
    "claude-sonnet-5": "The most capable model here. For hard problems and long work.",
}


def seed(apps, schema_editor):
    Plan = apps.get_model("AI", "Plan")
    AIModel = apps.get_model("AI", "AIModel")

    # Retire the model-less plans rather than deleting them: payments and
    # subscriptions reference them, and their history has to stay readable.
    Plan.objects.filter(ai_model__isnull=True).update(is_active=False, is_featured=False)

    order = 0
    for model_key, amounts in PACKAGES.items():
        ai_model = AIModel.objects.filter(key=model_key).first()
        if ai_model is None:
            continue
        for (tier, price, tier_order), tokens in zip(TIERS, amounts):
            slug = f"{model_key}-{tier.lower()}"
            Plan.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": f"{ai_model.name} — {tier}",
                    "description": BLURB.get(model_key, ""),
                    "tokens": tokens,
                    "ai_model": ai_model,
                    "price": price,
                    "currency": "USD",
                    # Tokens are bought outright and never expire; the window is
                    # long only so the subscription row that records the
                    # purchase does not read as already finished.
                    "duration_days": 365,
                    "features": "\n".join(FEATURES[tier]),
                    "is_active": True,
                    "is_featured": tier == "Standard",
                    "display_order": order * 10 + tier_order,
                },
            )
        order += 1


def unseed(apps, schema_editor):
    Plan = apps.get_model("AI", "Plan")
    slugs = [f"{key}-{tier.lower()}" for key in PACKAGES for tier, _, _ in TIERS]
    Plan.objects.filter(slug__in=slugs, subscriptions__isnull=True, payments__isnull=True).delete()
    Plan.objects.filter(ai_model__isnull=True).update(is_active=True)


def retune_settings(apps, schema_editor):
    """Bring the stored thresholds in line with the new package sizes.

    A 5,000-token warning threshold made sense against a 20,000-token trial; it
    is meaningless against a 500-token one, where it would fire before the
    student had sent anything.
    """
    AppSetting = apps.get_model("AI", "AppSetting")
    for key, value in (
        ("free_trial_tokens", "500"),
        ("low_token_threshold", "50"),
        ("low_token_threshold_critical", "10"),
    ):
        AppSetting.objects.filter(key=key).update(value=value)


def restore_settings(apps, schema_editor):
    AppSetting = apps.get_model("AI", "AppSetting")
    for key, value in (
        ("free_trial_tokens", "20000"),
        ("low_token_threshold", "5000"),
        ("low_token_threshold_critical", "1000"),
    ):
        AppSetting.objects.filter(key=key).update(value=value)


class Migration(migrations.Migration):

    dependencies = [("AI", "0014_plan_token_model")]

    operations = [
        migrations.RunPython(seed, unseed),
        migrations.RunPython(retune_settings, restore_settings),
    ]
