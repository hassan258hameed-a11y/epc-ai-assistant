"""Seed the multi-provider catalogue and carry existing balances into wallets.

Three things happen here, in order, and all of them are idempotent:

1. **Vendors and models.** OpenAI + GPT-5 Mini, Anthropic + Claude Sonnet 5 and
   Claude Haiku 4.5. Models already in the catalogue are linked to their vendor
   and left otherwise untouched — an administrator's own tuning is not reset.

2. **Admin pools.** Each model gets its own supply, seeded so the platform is
   usable immediately, and recorded as a real top-up so the ledger explains
   where the tokens came from.

3. **Existing student balances.** Every student's legacy balance moves into the
   wallet of whichever model was serving them before this migration. Nobody
   loses tokens; nobody silently gains a model they were never given. Students
   created *after* this migration start at zero on every model, which is the
   intended steady state.
"""
from django.db import migrations
from django.utils import timezone

#: The starter catalogue. ``key`` is the id sent to the vendor on the wire.
MODELS = [
    {
        "key": "gpt-5-mini",
        "name": "GPT-5 Mini",
        "provider": "openai",
        "description": "Reasons before answering, at everyday cost. A strong default.",
        "token_type": "gpt_5_mini",
        "max_output_tokens": 4096,
        "supports_vision": True,
        "thinking_mode": "off",
        "input_cost_per_1m": "0.2500",
        "output_cost_per_1m": "2.0000",
        "sort_order": 10,
        "is_default": True,
    },
    {
        "key": "claude-sonnet-5",
        "name": "Claude Sonnet 5",
        "provider": "anthropic",
        "description": "Balanced Claude model. Strong reasoning with a very large context window.",
        "token_type": "claude_sonnet_5",
        "max_output_tokens": 4096,
        "supports_vision": True,
        "thinking_mode": "off",
        "input_cost_per_1m": "2.0000",
        "output_cost_per_1m": "10.0000",
        "sort_order": 20,
        "is_default": False,
    },
    {
        "key": "claude-haiku-4-5",
        "name": "Claude Haiku 4.5",
        "provider": "anthropic",
        "description": "Fastest Claude model. Ideal for quick questions and everyday study help.",
        "token_type": "claude_haiku_4_5",
        "max_output_tokens": 3072,
        "supports_vision": True,
        "thinking_mode": "off",
        "input_cost_per_1m": "1.0000",
        "output_cost_per_1m": "5.0000",
        "sort_order": 30,
        "is_default": False,
    },
]

PROVIDERS = [
    {
        "slug": "openai",
        "name": "OpenAI",
        "description": "GPT models, called over the Chat Completions API.",
        "website": "https://platform.openai.com",
        "sort_order": 10,
    },
    {
        "slug": "anthropic",
        "name": "Anthropic",
        "description": "Claude models, called over the Messages API.",
        "website": "https://platform.claude.com",
        "sort_order": 20,
    },
]

#: Starting supply per model, so an administrator can distribute on day one
#: without first having to seed the pools by hand. This is *application*
#: accounting only — it does not buy anything from OpenAI or Anthropic.
STARTING_POOL = 1_000_000


def seed(apps, schema_editor):
    AIProvider = apps.get_model("AI", "AIProvider")
    AIModel = apps.get_model("AI", "AIModel")
    ModelTokenPool = apps.get_model("AI", "ModelTokenPool")
    ModelPoolTransaction = apps.get_model("AI", "ModelPoolTransaction")
    UserAIWallet = apps.get_model("AI", "UserAIWallet")
    AITokenTransaction = apps.get_model("AI", "AITokenTransaction")
    TokenAccount = apps.get_model("AI", "TokenAccount")

    # -- 1. vendors --------------------------------------------------------
    providers = {}
    for spec in PROVIDERS:
        provider, _ = AIProvider.objects.get_or_create(
            slug=spec["slug"],
            defaults={
                "name": spec["name"],
                "description": spec["description"],
                "website": spec["website"],
                "sort_order": spec["sort_order"],
                "is_active": True,
            },
        )
        providers[spec["slug"]] = provider

    # Link anything that already existed. Its own settings are left alone.
    for model in AIModel.objects.filter(provider_ref__isnull=True):
        provider = providers.get((model.provider or "openai").lower())
        if provider is not None:
            model.provider_ref = provider
            if not model.token_type:
                model.token_type = (model.key or "").replace(".", "_").replace("-", "_").lower()[:48]
            model.save(update_fields=["provider_ref", "token_type"])

    # -- 2. models ---------------------------------------------------------
    seeded = []
    for spec in MODELS:
        provider = providers[spec["provider"]]
        model = AIModel.objects.filter(key=spec["key"]).first()
        if model is None:
            model = AIModel.objects.create(
                key=spec["key"],
                name=spec["name"],
                provider=spec["provider"],
                provider_ref=provider,
                description=spec["description"],
                token_type=spec["token_type"],
                max_output_tokens=spec["max_output_tokens"],
                supports_vision=spec["supports_vision"],
                thinking_mode=spec["thinking_mode"],
                input_cost_per_1m=spec["input_cost_per_1m"],
                output_cost_per_1m=spec["output_cost_per_1m"],
                sort_order=spec["sort_order"],
                is_active=True,
                is_default=False,
            )
        else:
            # Re-point an existing row at its vendor. The name is normalised to
            # the vendor's own product name so the three models are unambiguous
            # in the admin console and the picker; the budget, pricing and
            # allocation an administrator tuned are all left alone.
            model.provider = spec["provider"]
            model.provider_ref = provider
            model.name = spec["name"]
            if not model.token_type:
                model.token_type = spec["token_type"]
            model.save(update_fields=["provider", "provider_ref", "name", "token_type"])
        seeded.append(model)

    # Exactly one default, and only if nothing sensible is set already.
    if not AIModel.objects.filter(is_default=True, is_active=True).exists():
        first = seeded[0]
        AIModel.objects.filter(is_default=True).update(is_default=False)
        AIModel.objects.filter(pk=first.pk).update(is_default=True)

    # -- 3. admin pools ----------------------------------------------------
    now = timezone.now()
    for model in AIModel.objects.all():
        pool, created = ModelTokenPool.objects.get_or_create(ai_model=model)
        if created and STARTING_POOL > 0:
            pool.balance = STARTING_POOL
            pool.total_added = STARTING_POOL
            pool.save(update_fields=["balance", "total_added", "updated_at"])
            ModelPoolTransaction.objects.create(
                ai_model=model,
                amount=STARTING_POOL,
                action="topup",
                description=f"Opening supply — {STARTING_POOL:,} {model.name} tokens",
                balance_after=STARTING_POOL,
                created_at=now,
            )

    # -- 4. existing student balances --------------------------------------
    # Whatever a student held before this migration was spent on the model that
    # was then the default, so that is the wallet it belongs in.
    landing = (
        AIModel.objects.filter(is_default=True, is_active=True).first()
        or AIModel.objects.order_by("sort_order", "id").first()
    )
    if landing is None:
        return

    for account in TokenAccount.objects.select_related("user").filter(balance__gt=0):
        wallet, created = UserAIWallet.objects.get_or_create(
            user=account.user, ai_model=landing
        )
        if not created and wallet.balance:
            continue  # already migrated
        wallet.balance = account.balance
        wallet.allocated_tokens = account.balance + account.total_consumed
        wallet.used_tokens = account.total_consumed
        wallet.lifetime_allocated = max(account.total_allocated, account.balance)
        wallet.lifetime_used = account.total_consumed
        wallet.first_allocated_at = account.cycle_started_at or now
        wallet.save()

        AITokenTransaction.objects.create(
            user=account.user,
            ai_model=landing,
            amount=account.balance,
            transaction_type="admin_allocation",
            balance_before=0,
            balance_after=account.balance,
            reason=f"Carried forward from the shared balance into {landing.name}",
            reference_id=f"migration:0013:{account.user_id}:{landing.pk}",
            created_at=now,
        )


def unseed(apps, schema_editor):
    """Reversal drops only what this migration created, never student history."""
    AIProvider = apps.get_model("AI", "AIProvider")
    AIModel = apps.get_model("AI", "AIModel")
    AITokenTransaction = apps.get_model("AI", "AITokenTransaction")

    AITokenTransaction.objects.filter(reference_id__startswith="migration:0013:").delete()
    # provider_ref is PROTECT, so the links have to go before the vendors do.
    AIModel.objects.update(provider_ref=None)
    AIProvider.objects.filter(slug__in=[spec["slug"] for spec in PROVIDERS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("AI", "0012_multi_provider_wallets"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
