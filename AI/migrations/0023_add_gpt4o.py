"""Add GPT-4o as a fourth model, with three million tokens held by the Admin.

Everything here is a row, not code: GPT-4o is the same kind of thing as the
three models already in the catalogue, so it needs a catalogue entry, a supply
pool, and three packages — and nothing in the chat runtime, the router, the
wallet ledger or the limit system changes to accommodate it.

Two things are specific to how this model was bought:

* **its own credential.** The row names ``OPENAI_GPT4O_API_KEY``; the key
  itself stays in the server environment, so this migration — and the database
  it writes to — never contains it.
* **admin-only supply.** The three million tokens go into the administrator's
  pool and nowhere else, and ``auto_grant`` is off so no signup can hand a
  slice of them out. Every token a student ever holds is one an administrator
  deliberately gave them.

The supply is recorded as 1,500,000 input + 1,500,000 output because that is
how the capacity was purchased. Students still spend one undifferentiated
token — an answer costs input *and* output, and nobody gets to choose the
ratio — so the split is reporting, not a second currency.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import migrations

KEY = "gpt-4o"

TOTAL_TOKENS = 3_000_000
INPUT_TOKENS = 1_500_000
OUTPUT_TOKENS = 1_500_000

#: Retail price of a million GPT-4o tokens, used only to derive the three
#: prices below. GPT-4o costs about $4.75 per million blended — between Claude
#: 4.5 and Claude 4.5 High — and the price sits between theirs accordingly, and
#: below Claude 4.5 High at every size.
SIZES = [
    {
        "tokens": 50_000,
        "tier": "Starter",
        "price": "3.49",
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
        "price": "4.99",
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
        "price": "7.49",
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

    model, _ = AIModel.objects.update_or_create(
        key=KEY,
        defaults={
            "name": "GPT-4o",
            "provider": "openai",
            "provider_ref": provider,
            "description": "Multimodal. Reads images as well as text, and answers quickly.",
            "is_active": True,
            "is_default": False,
            # GPT-4o genuinely sees images, so a student can photograph a
            # question and ask about it. This is the flag the composer reads
            # before offering to attach one.
            "supports_vision": True,
            "token_type": "gpt_4o",
            "thinking_mode": "off",
            "max_output_tokens": 4096,
            "temperature": Decimal("0.70"),
            "input_cost_per_1m": Decimal("2.50"),
            "output_cost_per_1m": Decimal("10.00"),
            # 0 means no platform-wide ceiling of its own: the supply pool is
            # what limits this model, and it is finite by construction.
            "token_allocation": 0,
            # The name of the setting, never the key.
            "api_key_setting": "OPENAI_GPT4O_API_KEY",
            # Admin-distributed only. No signup hands any of this out.
            "auto_grant": False,
            "sort_order": 5,
        },
    )

    pool, created = ModelTokenPool.objects.get_or_create(ai_model_id=model.id)
    # Idempotent: re-running must not mint a second three million.
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
                f"Opening GPT-4o allocation: {INPUT_TOKENS:,} input + "
                f"{OUTPUT_TOKENS:,} output"
            ),
            balance_after=pool.balance,
        )

    # The packages. Placed after the existing nine so the pricing switch keeps
    # the order the picker already shows.
    order = 100
    for size in SIZES:
        slug = f"{KEY}-{size['tokens'] // 1000}k"
        Plan.objects.update_or_create(
            slug=slug,
            defaults={
                "name": f"GPT-4o — {size['tier']}",
                "description": size["blurb"],
                "tokens": size["tokens"],
                "ai_model_id": model.id,
                "price": Decimal(size["price"]),
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
    """Remove the model, its packages and its unspent supply.

    Refuses if any of it has been handed out: deleting a model a student holds
    tokens for would silently destroy something they were given, and the ledger
    rows that explain it.
    """
    AIModel = apps.get_model("AI", "AIModel")
    Plan = apps.get_model("AI", "Plan")
    UserAIWallet = apps.get_model("AI", "UserAIWallet")

    model = AIModel.objects.filter(key=KEY).first()
    if model is None:
        return
    if UserAIWallet.objects.filter(ai_model_id=model.id).exclude(balance=0).exists():
        raise RuntimeError(
            "GPT-4o tokens are held by students; reclaim them before removing the model."
        )
    Plan.objects.filter(ai_model_id=model.id).delete()
    model.delete()


class Migration(migrations.Migration):

    dependencies = [("AI", "0022_model_auto_grant")]

    operations = [migrations.RunPython(forwards, backwards)]
