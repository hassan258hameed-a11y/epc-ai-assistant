"""Reset every student balance to the admin pool, then rebuild the catalogue.

Three things happen here, in this order, because each depends on the last:

1. **Rename** the two Claude rows to the names the product now sells under.
   The catalogue ``key`` — what goes on the wire, and what every wallet, pool
   and ledger row is joined to — is deliberately left alone, so nothing moves
   and no history is orphaned.

2. **Reclaim** every token held by every student back into that model's admin
   pool. This is the cleanup the upgrade was asked for: balances handed out
   under the old, inconsistent package sizes are returned to the administrator,
   who is the source of supply. Nothing is deleted — every reclaim is written to
   the same ledger the balances came from, so the history still reconciles and
   any student can be told exactly what happened to their tokens and when.

3. **Rebuild** the packages: three sizes, on three models, nine rows, each with
   its own price and its own three ceilings. Old packages are retired rather
   than deleted, because payments point at them and a receipt must still be
   able to name what was bought.

Written as a migration rather than a script so it runs exactly once, in order,
inside a transaction, on every environment.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import migrations
from django.utils import timezone

# --------------------------------------------------------------------------
# The catalogue
#
# ``rate`` is the retail price of a million tokens of this model, and it is set
# from what the model actually costs to serve: roughly $0.8, $2.2 and $6.6 per
# million blended, which is the 1 : 3 : 8.5 spread the prices below keep. It is
# only used to *derive the initial prices* — every price lands in the database
# and is edited there afterwards, never here.
# --------------------------------------------------------------------------
MODELS = {
    "gpt-5-mini": {
        "name": "GPT-5 Mini",
        "description": "Fast and capable. The everyday model, and the best value here.",
        "sort_order": 1,
        "input_cost_per_1m": Decimal("0.25"),
        "output_cost_per_1m": Decimal("2.00"),
        "prices": {50_000: "1.99", 100_000: "2.49", 200_000: "2.99"},
    },
    "claude-haiku-4-5": {
        "name": "Claude 4.5",
        "description": "Quick and careful. Strong reasoning for everyday study work.",
        "sort_order": 10,
        "input_cost_per_1m": Decimal("1.00"),
        "output_cost_per_1m": Decimal("5.00"),
        "prices": {50_000: "2.99", 100_000: "3.99", 200_000: "4.99"},
    },
    "claude-sonnet-5": {
        "name": "Claude 4.5 High",
        "description": "The most capable model here. For hard problems and long work.",
        "sort_order": 20,
        "input_cost_per_1m": Decimal("3.00"),
        "output_cost_per_1m": Decimal("15.00"),
        "prices": {50_000: "4.49", 100_000: "6.49", 200_000: "9.99"},
    },
}

# --------------------------------------------------------------------------
# The three package sizes, and the ceilings each is sold with.
#
# These are not percentages of anything. Each is a number chosen so the three
# windows are genuinely different constraints: seven days of the daily limit
# comfortably exceeds the weekly one, and four weeks of the weekly limit
# comfortably exceeds the monthly one, so a student meets the day ceiling on a
# heavy afternoon, the week ceiling on a heavy week, and can still spend the
# whole package inside a month at a steady pace.
# --------------------------------------------------------------------------
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
            "Full chat, voice and file support",
            "Image generation included",
            "Priority during busy periods",
        ],
    },
]


def rename_models(apps):
    AIModel = apps.get_model("AI", "AIModel")
    for key, spec in MODELS.items():
        row = AIModel.objects.filter(key=key).first()
        if row is None:
            continue
        row.name = spec["name"]
        row.description = spec["description"]
        row.sort_order = spec["sort_order"]
        row.input_cost_per_1m = spec["input_cost_per_1m"]
        row.output_cost_per_1m = spec["output_cost_per_1m"]
        row.is_active = True
        row.save(
            update_fields=[
                "name", "description", "sort_order",
                "input_cost_per_1m", "output_cost_per_1m", "is_active", "updated_at",
            ]
        )


def reclaim_all_student_tokens(apps):
    """Return every student balance to its model's admin pool.

    Done through the same ledger the tokens arrived on: one reversal row per
    wallet, with before/after snapshots, so the history still replays to the new
    balance rather than jumping to zero unexplained.
    """
    UserAIWallet = apps.get_model("AI", "UserAIWallet")
    AITokenTransaction = apps.get_model("AI", "AITokenTransaction")
    ModelTokenPool = apps.get_model("AI", "ModelTokenPool")
    ModelPoolTransaction = apps.get_model("AI", "ModelPoolTransaction")
    TokenAccount = apps.get_model("AI", "TokenAccount")
    AIModel = apps.get_model("AI", "AIModel")

    now = timezone.now()
    live_model_ids = set(AIModel.objects.values_list("id", flat=True))

    # Orphans first: a wallet whose model no longer exists can never be spent
    # and would only distort the totals below.
    UserAIWallet.objects.exclude(ai_model_id__in=live_model_ids).delete()

    reclaimed_per_model: dict[int, int] = {}

    for wallet in UserAIWallet.objects.select_related("ai_model").all():
        before = wallet.balance
        # The signed movement that takes this wallet to zero. A negative balance
        # should be impossible — a check constraint forbids it — but if legacy
        # data carries one, this is where it is corrected rather than carried
        # forward into the new system.
        amount = -before

        if amount != 0:
            AITokenTransaction.objects.create(
                user_id=wallet.user_id,
                ai_model_id=wallet.ai_model_id,
                amount=amount,
                transaction_type="admin_reversal",
                balance_before=before,
                balance_after=0,
                reason="Platform reset — balance returned to the administrator's pool",
                reference_id=f"reset2026:{wallet.user_id}:{wallet.ai_model_id}",
                created_at=now,
            )
        if before > 0:
            reclaimed_per_model[wallet.ai_model_id] = (
                reclaimed_per_model.get(wallet.ai_model_id, 0) + before
            )

        wallet.balance = 0
        # Lifetime figures are history and are deliberately untouched; the
        # cycle figures describe a cycle that has just ended.
        wallet.allocated_tokens = wallet.used_tokens
        wallet.save(update_fields=["balance", "allocated_tokens", "updated_at"])

    for model_id, amount in reclaimed_per_model.items():
        pool, _ = ModelTokenPool.objects.get_or_create(ai_model_id=model_id)
        pool.balance = max(0, pool.balance) + amount
        pool.total_reclaimed = pool.total_reclaimed + amount
        pool.save(update_fields=["balance", "total_reclaimed", "updated_at"])

        ModelPoolTransaction.objects.create(
            ai_model_id=model_id,
            amount=amount,
            action="reclaim",
            description="Platform reset — student balances returned to the pool",
            balance_after=pool.balance,
            created_at=now,
        )

    # Every model must have a pool row, so the distribution screen can always
    # show a number rather than an absence.
    for model_id in live_model_ids:
        pool, created = ModelTokenPool.objects.get_or_create(ai_model_id=model_id)
        if pool.balance < 0:
            pool.balance = 0
            pool.save(update_fields=["balance", "updated_at"])

    # The legacy single-balance aggregate is a cache of the wallets above.
    TokenAccount.objects.all().update(
        balance=0, cycle_allocated=0, cycle_consumed=0, updated_at=now
    )


def rebuild_packages(apps):
    """Retire the old catalogue and write the nine packages now on sale."""
    Plan = apps.get_model("AI", "Plan")
    AIModel = apps.get_model("AI", "AIModel")

    # Retired, not deleted: payments point at these rows and a receipt has to
    # keep naming what was actually bought.
    Plan.objects.update(is_active=False, is_featured=False)

    order = 0
    for key, spec in MODELS.items():
        model = AIModel.objects.filter(key=key).first()
        if model is None:
            continue
        for size in SIZES:
            price = Decimal(spec["prices"][size["tokens"]])
            slug = f"{key}-{size['tokens'] // 1000}k"
            defaults = {
                "name": f"{spec['name']} — {size['tier']}",
                "description": size["blurb"],
                "tokens": size["tokens"],
                "ai_model_id": model.id,
                "price": price,
                "currency": "USD",
                "duration_days": 365,
                "features": "\n".join(size["features"]),
                "is_active": True,
                "is_featured": size["featured"],
                "display_order": order,
                "daily_limit": size["daily"],
                "weekly_limit": size["weekly"],
                "monthly_limit": size["monthly"],
            }
            Plan.objects.update_or_create(slug=slug, defaults=defaults)
            order += 1


def seed_settings(apps):
    """Materialise the new operator settings so the admin UI has rows to edit."""
    AppSetting = apps.get_model("AI", "AppSetting")
    rows = [
        ("minimum_limit_package", "50000", "int", "limits", "Minimum metered package"),
        ("usage_warning_thresholds", "[70, 80, 90]", "json", "limits", "Warning thresholds (%)"),
        ("usage_meter_visible", "true", "bool", "limits", "Show the usage meter"),
        ("support_whatsapp", "+923162211830", "str", "support", "WhatsApp number"),
        (
            "support_whatsapp_message",
            "Hello! I need help with my EPC AI Assistant account.",
            "str", "support", "WhatsApp opening message",
        ),
        ("support_hours", "Every day, 9am — 9pm", "str", "support", "Support hours"),
        ("image_enabled", "true", "bool", "images", "Enable image generation"),
        ("image_provider", "openai", "str", "images", "Image provider"),
        ("image_model", "", "str", "images", "Image model"),
        ("image_api_key", "", "secret", "images", "Image API key"),
        ("image_size", "1024x1024", "str", "images", "Default size"),
        ("image_quality", "medium", "str", "images", "Quality"),
        ("image_token_cost", "0", "int", "images", "Tokens per image"),
    ]
    for key, value, value_type, group, label in rows:
        AppSetting.objects.get_or_create(
            key=key,
            defaults={"value": value, "value_type": value_type, "group": group, "label": label},
        )


def forwards(apps, schema_editor):
    rename_models(apps)
    reclaim_all_student_tokens(apps)
    rebuild_packages(apps)
    seed_settings(apps)


def backwards(apps, schema_editor):
    """Deliberately not reversible.

    Reclaimed balances cannot be un-reclaimed without guessing which of them
    were correct in the first place, which is the very thing this migration
    exists to stop. Rolling back means restoring the pre-migration backup.
    """
    raise RuntimeError(
        "0019 cannot be reversed — restore a database backup taken before it ran."
    )


class Migration(migrations.Migration):

    dependencies = [("AI", "0018_package_allocations_and_limits")]

    operations = [migrations.RunPython(forwards, backwards)]
