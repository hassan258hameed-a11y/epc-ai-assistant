"""Add GPT-6 Astra and GPT-5 Sol, with their supply held by the administrator.

These are the seventh and eighth models, and they arrive the way GPT-4o, Fable
5.1 and Opus 5 did: as rows. A catalogue entry, a supply pool and three
packages each. The chat runtime, the router, the wallet ledger, the limit
windows, the model selector, the per-model analytics and the low-token warnings
all read the catalogue, so they pick these up without being told.

Both are OpenAI models on the existing Chat Completions client. The ids —
``gpt-6-astra`` and ``gpt-5.6-sol`` — are exactly what OpenAI's model pages
list, and both begin with a prefix the provider already treats as a reasoning
model (``gpt-6`` / ``gpt-5``), so they are sent ``max_completion_tokens`` and
``reasoning_effort`` rather than ``max_tokens`` and ``temperature``, with no new
code. OpenAI markets the second as "GPT-5.6 Sol"; the picker calls it GPT-5 Sol,
which is the product name this catalogue sells it under.

``api_key_setting`` is blank, so both bill to ``OPENAI_API_KEY`` — the same key
GPT-5 Mini already uses. No new secret is introduced, and none is stored here.

Priced from what they cost to run, per 100,000 tokens:

    GPT-5 Mini       $2.49
    Claude 4.5       $3.99
    GPT-4o           $4.99
    Claude 4.5 High  $6.49
    GPT-5 Sol        $6.99     $4 in / $20 out per million at list
    Fable 5.1        $7.49
    Opus 5           $8.99
    GPT-6 Astra     $14.99     $10 in / $50 out: the dearest model by far

The same two properties 0032 holds hold here at every size: each model is
dearer than the one below it, and within a model the price per million falls as
the package grows. Astra's packages still clear their cost if every token were
output, which is the worst case a student can produce.

The supply mirrors the other admin-held models: three million tokens each,
recorded as 1,500,000 input plus 1,500,000 output. Adjust it to whatever
capacity was actually bought — the pool top-up on the AI tokens page does that
without a migration.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import migrations

TOTAL_TOKENS = 3_000_000
INPUT_TOKENS = 1_500_000
OUTPUT_TOKENS = 1_500_000

MODELS = [
    {
        "key": "gpt-5.6-sol",
        "name": "GPT-5 Sol",
        "token_type": "gpt_5_6_sol",
        "description": "Strong reasoning for coding and multi-step problems. Reads images.",
        # Between Claude 4.5 High and Fable 5.1, where its price sits.
        "sort_order": 25,
        "input_cost_per_1m": "4.00",
        "output_cost_per_1m": "20.00",
        "prices": {50_000: "4.79", 100_000: "6.99", 200_000: "10.99"},
    },
    {
        "key": "gpt-6-astra",
        "name": "GPT-6 Astra",
        "token_type": "gpt_6_astra",
        "description": "OpenAI's most capable model. For the hardest problems and long work.",
        # Last: it is the dearest thing on the shelf.
        "sort_order": 50,
        "input_cost_per_1m": "10.00",
        "output_cost_per_1m": "50.00",
        "prices": {50_000: "9.49", 100_000: "14.99", 200_000: "27.99"},
    },
]

#: The three sizes every model in the catalogue is sold in, with the same
#: ceilings and copy as the others. The middle size is the highlighted one.
SIZES = [
    {
        "tokens": 50_000,
        "tier": "Starter",
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
        "blurb": "Our most popular size.",
        "daily": 9_000,
        "weekly": 35_000,
        "monthly": 100_000,
        "featured": True,
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

    provider = AIProvider.objects.filter(slug="openai").first()

    order = 300
    for spec in MODELS:
        model, _ = AIModel.objects.update_or_create(
            key=spec["key"],
            defaults={
                "name": spec["name"],
                "provider": "openai",
                "provider_ref": provider,
                "description": spec["description"],
                "is_active": True,
                "is_default": False,
                # Both take image input over Chat Completions.
                "supports_vision": True,
                "token_type": spec["token_type"],
                # "off" sends reasoning_effort=low: hidden reasoning is billed to
                # the student as output tokens, so it is kept to what earns its
                # place until an administrator turns it up.
                "thinking_mode": "off",
                # Reasoning models: starved of output they spend it all thinking
                # and answer with nothing, so they get the larger budget Opus has.
                "max_output_tokens": 4096,
                "temperature": Decimal("0.70"),
                "input_cost_per_1m": Decimal(spec["input_cost_per_1m"]),
                "output_cost_per_1m": Decimal(spec["output_cost_per_1m"]),
                # 0 means no ceiling of its own: the supply pool is what limits
                # this model, and it is finite by construction.
                "token_allocation": 0,
                # Blank: use OPENAI_API_KEY, as GPT-5 Mini does.
                "api_key_setting": "",
                # Admin-distributed only. No signup hands any of this out.
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
            slug = f"{spec['key'].replace('.', '-')}-{size['tokens'] // 1000}k"
            Plan.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": f"{spec['name']} — {size['tier']}",
                    "description": size["blurb"],
                    "tokens": size["tokens"],
                    "ai_model_id": model.id,
                    "price": Decimal(spec["prices"][size["tokens"]]),
                    "currency": "USD",
                    "duration_days": 365,
                    "features": "\n".join(size["features"]),
                    "is_active": True,
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

    dependencies = [("AI", "0034_announcement_audience_pin")]

    operations = [migrations.RunPython(forwards, backwards)]
