"""Add Fable 5.1 and Opus 5, with their supply held by the administrator.

These are the fifth and sixth models, and they arrive the same way GPT-4o did:
as rows. A catalogue entry, a supply pool and three packages each. Nothing in
the chat runtime, the router, the wallet ledger, the limit system, the referral
rules or the console changes to accommodate them — the model selector, the
per-model analytics and the low-token warnings all read the catalogue, so they
pick these up without being told.

Both are Anthropic models and both use the vendor's own credential, exactly as
Claude 4.5 and Claude 4.5 High already do: ``api_key_setting`` is blank, which
the provider reads as "use ``ANTHROPIC_API_KEY``". No new secret is introduced,
and none is stored here.

Two deliberate differences from GPT-4o, both reversible with one click:

* **the packages start inactive.** Their prices are placeholders — nobody has
  said what these models should sell for — and an inactive package cannot be
  bought at a price nobody chose. Everything else about them is complete, so
  turning them on is a toggle on the Plans page.
* **list prices are left at zero, meaning unpriced.** The cost-and-margin page
  renders an unset price as "unpriced" rather than as free, precisely so that a
  figure nobody has entered is never mistaken for one that has been. Filling
  them in is one field per model in the console.

The supply mirrors GPT-4o's: three million tokens each, recorded as 1,500,000
input plus 1,500,000 output because that is the shape the other admin-held
model uses. Adjust it to whatever capacity was actually bought — the pool
top-up field on the AI tokens page does that without a migration.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import migrations

TOTAL_TOKENS = 3_000_000
INPUT_TOKENS = 1_500_000
OUTPUT_TOKENS = 1_500_000

MODELS = [
    {
        "key": "claude-fable-5-1",
        "name": "Fable 5.1",
        "token_type": "claude_fable_5_1",
        "description": "Anthropic's Fable generation. Reads images and answers quickly.",
        "sort_order": 30,
        # Fable takes adaptive thinking, but like every other model in this
        # catalogue it ships with thinking off: hidden reasoning is billed to
        # the student as output tokens, and a metered product should not start
        # spending them on a setting nobody chose.
        "max_output_tokens": 2048,
    },
    {
        "key": "claude-opus-5",
        "name": "Opus 5",
        "token_type": "claude_opus_5",
        "description": "Anthropic's most capable model. Best for hard reasoning and long work.",
        "sort_order": 40,
        # A larger reply budget than the others. Opus is a reasoning model: on
        # a small budget it can spend the whole allowance thinking and return
        # an empty answer, which reads to a student as the model being broken.
        "max_output_tokens": 4096,
    },
]

#: The three sizes every model in the catalogue is sold in. Prices are
#: provisional and the packages ship inactive because of it.
SIZES = [
    {
        "tokens": 50_000,
        "tier": "Starter",
        "price": "3.99",
        "blurb": "A few weeks of everyday study.",
        "daily": 5_000,
        "weekly": 20_000,
        "monthly": 50_000,
        "featured": False,
        "features": [
            "50,000 tokens, and they never expire",
            "Reads images and photos of questions",
            "Full chat, voice and file support",
            "Image generation included",
        ],
    },
    {
        "tokens": 100_000,
        "tier": "Standard",
        "price": "6.99",
        "blurb": "Our most popular size.",
        "daily": 9_000,
        "weekly": 35_000,
        "monthly": 100_000,
        "featured": False,
        "features": [
            "100,000 tokens, and they never expire",
            "Reads images and photos of questions",
            "Full chat, voice and file support",
            "Image generation included",
            "Priority during busy periods",
        ],
    },
    {
        "tokens": 200_000,
        "tier": "Pro",
        "price": "11.99",
        "blurb": "For heavy research and coding work.",
        "daily": 16_000,
        "weekly": 65_000,
        "monthly": 200_000,
        "featured": False,
        "features": [
            "200,000 tokens, and they never expire",
            "Best value per token",
            "Reads images and photos of questions",
            "Full chat, voice and file support",
            "Image generation included",
            "Priority during busy periods",
        ],
    },
]


def forwards(apps, schema_editor):
    AIModel = apps.get_model("AI", "AIModel")
    AIProvider = apps.get_model("AI", "AIProvider")
    ModelTokenPool = apps.get_model("AI", "ModelTokenPool")
    ModelPoolTransaction = apps.get_model("AI", "ModelPoolTransaction")
    Plan = apps.get_model("AI", "Plan")

    provider = AIProvider.objects.filter(slug="anthropic").first()

    order = 200
    for spec in MODELS:
        model, _ = AIModel.objects.update_or_create(
            key=spec["key"],
            defaults={
                "name": spec["name"],
                "provider": "anthropic",
                "provider_ref": provider,
                "description": spec["description"],
                "is_active": True,
                "is_default": False,
                # Every Claude model in the catalogue reads images, and the
                # provider's own capability check agrees.
                "supports_vision": True,
                "token_type": spec["token_type"],
                "thinking_mode": "off",
                "max_output_tokens": spec["max_output_tokens"],
                "temperature": Decimal("0.70"),
                # Unset, which the console reports as "unpriced" rather than as
                # free. Entering the real figures is one field per model.
                "input_cost_per_1m": Decimal("0"),
                "output_cost_per_1m": Decimal("0"),
                # 0 means no ceiling of its own: the supply pool is what limits
                # this model, and it is finite by construction.
                "token_allocation": 0,
                # Blank: use the Anthropic vendor credential, as the other two
                # Claude models already do.
                "api_key_setting": "",
                # Admin-distributed only. No signup hands any of this out, so
                # every token a student holds is one an administrator gave them.
                "auto_grant": False,
                "sort_order": spec["sort_order"],
            },
        )

        pool, created = ModelTokenPool.objects.get_or_create(ai_model_id=model.id)
        # Idempotent: re-running must never mint a second three million.
        if created or pool.total_added == 0:
            pool.balance = TOTAL_TOKENS
            pool.total_added = TOTAL_TOKENS
            pool.total_distributed = 0
            pool.total_reclaimed = 0
            pool.input_allocation = INPUT_TOKENS
            pool.output_allocation = OUTPUT_TOKENS
            pool.save()

            ModelPoolTransaction.objects.create(
                ai_model_id=model.id,
                amount=TOTAL_TOKENS,
                action="topup",
                description=(
                    f"Opening {spec['name']} allocation: {INPUT_TOKENS:,} input + "
                    f"{OUTPUT_TOKENS:,} output"
                ),
                balance_after=pool.balance,
            )

        for size in SIZES:
            slug = f"{spec['key']}-{size['tokens'] // 1000}k"
            Plan.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": f"{spec['name']} — {size['tier']}",
                    "description": size["blurb"],
                    "tokens": size["tokens"],
                    "ai_model_id": model.id,
                    "price": Decimal(size["price"]),
                    "currency": "USD",
                    "duration_days": 365,
                    "features": "\n".join(size["features"]),
                    # Off until somebody decides what these should cost.
                    "is_active": False,
                    "is_featured": size["featured"],
                    "display_order": order,
                    "daily_limit": size["daily"],
                    "weekly_limit": size["weekly"],
                    "monthly_limit": size["monthly"],
                },
            )
            order += 1


def backwards(apps, schema_editor):
    """Remove both models, their packages and their unspent supply.

    Refuses if any of it has been handed out: deleting a model a student holds
    tokens for would silently destroy something they were given, along with the
    ledger rows that explain it.
    """
    AIModel = apps.get_model("AI", "AIModel")
    Plan = apps.get_model("AI", "Plan")
    UserAIWallet = apps.get_model("AI", "UserAIWallet")

    for spec in MODELS:
        model = AIModel.objects.filter(key=spec["key"]).first()
        if model is None:
            continue
        if UserAIWallet.objects.filter(ai_model_id=model.id).exclude(balance=0).exists():
            raise RuntimeError(
                f"{spec['name']} tokens are held by students; reclaim them "
                f"before removing the model."
            )
        Plan.objects.filter(ai_model_id=model.id).delete()
        model.delete()


class Migration(migrations.Migration):

    dependencies = [("AI", "0029_model_warning_thresholds")]

    operations = [migrations.RunPython(forwards, backwards)]
