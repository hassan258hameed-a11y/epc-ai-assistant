"""Report on the environment configuration.

    python manage.py check_setup

Shows what is wired up and what is still missing, without ever printing a
secret value.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from AI.models import Plan, Role, User
from AI.services import settings_service
from AI.services.ai_service import provider_status
from AI.services.payments import available_providers


class Command(BaseCommand):
    help = "Show which integrations are configured and flag anything missing."

    def handle(self, *args, **options):
        ok, warn, bad = self.style.SUCCESS("  ok  "), self.style.WARNING(" warn "), self.style.ERROR(" miss ")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Core"))
        self.stdout.write(f"{ok if not settings.DEBUG else warn} DEBUG = {settings.DEBUG}"
                          f"{'  (must be False in production)' if settings.DEBUG else ''}")

        weak_key = settings.SECRET_KEY.startswith("django-insecure")
        self.stdout.write(f"{bad if weak_key else ok} SECRET_KEY {'is the insecure default' if weak_key else 'is set'}")
        self.stdout.write(f"{ok} ALLOWED_HOSTS = {', '.join(settings.ALLOWED_HOSTS) or '(empty)'}")
        engine = settings.DATABASES['default']['ENGINE'].rsplit('.', 1)[-1]
        self.stdout.write(f"{warn if engine == 'sqlite3' else ok} Database: {engine}"
                          f"{'  (fine for development, use PostgreSQL/MySQL in production)' if engine == 'sqlite3' else ''}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("AI providers"))
        # Every vendor is reported, because one of them having no key is not
        # visible in a summary that only describes the default.
        try:
            from AI.models import AIProvider
            from AI.services import providers as provider_registry

            rows = {row.slug: row for row in AIProvider.objects.all()}
            for slug in provider_registry.available_slugs():
                configured = provider_registry.is_configured(slug)
                row = rows.get(slug)
                enabled = row.is_active if row else True
                state = ok if (configured and enabled) else bad
                label = row.name if row else slug
                self.stdout.write(
                    f"{state} {label} — "
                    f"{'credentials set' if configured else 'NO CREDENTIALS'}"
                    f"{'' if enabled else ', switched off in the console'}"
                )
                if not configured:
                    env_var = "OPENAI_API_KEY" if slug == "openai" else f"{slug.upper()}_API_KEY"
                    self.stdout.write(
                        f"       set {env_var} in .env — its models cannot answer without it"
                    )
        except Exception as exc:  # pragma: no cover — diagnostics must not crash
            self.stdout.write(f"{bad} Could not read the provider registry: {exc}")

        provider = provider_status()
        self.stdout.write(f"       default model: {provider['model']} ({provider['provider']})")
        self.stdout.write(f"       image input: {'enabled' if provider['vision'] else 'off'}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Admin token pools"))
        # One line per model. Tokens are a separate currency for each, so a
        # combined figure could read as healthy while the model a student needs
        # has nothing left in it.
        try:
            from AI.services import model_pool_service

            pools = model_pool_service.summaries()
            if not pools:
                self.stdout.write(f"{warn} No AI models in the catalogue yet")
            for entry in pools:
                state = bad if entry["is_empty"] else ok
                self.stdout.write(
                    f"{state} {entry['model'].name:<20} {entry['balance']:>12,} to distribute"
                )
                self.stdout.write(
                    f"       {entry['total_added']:,} added, "
                    f"{entry['outstanding']:,} held by students, "
                    f"{entry['total_reclaimed']:,} returned"
                )
                if entry["is_empty"]:
                    self.stdout.write(
                        f"       allocations of {entry['model'].name} are refused until you "
                        f"top it up at /admin/ai-tokens/"
                    )
        except Exception as exc:  # pragma: no cover — diagnostics must not crash
            self.stdout.write(f"{bad} Could not read the pools: {exc}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Model catalogue"))
        try:
            from AI.services import model_service

            model_service.ensure_seeded()
            models = list(model_service.catalogue())
            available = [m for m in models if m.is_available]
            self.stdout.write(
                f"{ok if available else bad} {len(available)} of {len(models)} models available to students"
            )
            for model in models:
                label, _tone = model.availability_badge
                budget = f"{model.token_allocation:,} allocated" if model.is_metered else "unlimited"
                self.stdout.write(
                    f"       {'*' if model.is_default else ' '} {model.key:<20} {label:<18} "
                    f"{budget}, {model.tokens_consumed:,} spent"
                )
            if not available:
                self.stdout.write("       enable at least one model at /admin/tokens/?tab=models")

            default = model_service.default_model()
            configured = (settings.OPENAI_MODEL or "").strip()
            if default and configured and default.key != configured:
                self.stdout.write(
                    f"{warn} The default model ({default.key}) is not the one in OPENAI_MODEL "
                    f"({configured})."
                )
                self.stdout.write(
                    "       Students' messages will go to the default. If that is not intended, "
                    "open /admin/tokens/?tab=models and choose 'Make default'."
                )
        except Exception as exc:  # pragma: no cover — diagnostics must not crash
            self.stdout.write(f"{bad} Could not read the catalogue: {exc}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("E-mail (OTP delivery)"))
        if settings.EMAIL_HOST_USER:
            self.stdout.write(f"{ok} SMTP {settings.EMAIL_HOST}:{settings.EMAIL_PORT} as {settings.EMAIL_HOST_USER}")
        else:
            self.stdout.write(f"{warn} No SMTP configured — codes are printed to the console")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Telegram"))
        if settings.TELEGRAM_BOT_TOKEN:
            self.stdout.write(f"{ok} Bot token set"
                              f"{'  @' + settings.TELEGRAM_BOT_USERNAME if settings.TELEGRAM_BOT_USERNAME else ''}")
            if not settings.TELEGRAM_BOT_USERNAME:
                self.stdout.write(f"{warn} TELEGRAM_BOT_USERNAME missing — deep links cannot be built")
            if not settings.TELEGRAM_WEBHOOK_SECRET:
                self.stdout.write(f"{warn} TELEGRAM_WEBHOOK_SECRET missing — callbacks cannot be authenticated")
        else:
            self.stdout.write(f"{warn} Not configured — e-mail is the only OTP channel")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Payments"))
        for gateway in available_providers():
            state = ok if gateway.is_configured else warn
            mode = "live verification" if gateway.is_configured else "manual confirmation by an admin"
            self.stdout.write(f"{state} {gateway.label}: {mode}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Data"))
        admins = User.objects.filter(role=Role.ADMIN).count()
        plans = Plan.objects.filter(is_active=True).count()
        self.stdout.write(f"{ok if admins else bad} {admins} administrator account(s)")
        self.stdout.write(f"{ok if plans else warn} {plans} active plan(s)")
        self.stdout.write(f"{ok} {User.objects.filter(role=Role.STUDENT).count()} student account(s)")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Business settings"))
        values = settings_service.all_settings()
        for key in ("free_trial_tokens", "referral_bonus_referrer", "low_token_threshold", "expiry_reminder_days"):
            self.stdout.write(f"       {key}: {values.get(key)}")

        self.stdout.write("")
        if not admins:
            self.stdout.write(self.style.ERROR("Create an administrator: python manage.py createsuperuser"))
        elif not plans:
            self.stdout.write(self.style.WARNING("Add a plan in the console before students can buy tokens."))
        else:
            self.stdout.write(self.style.SUCCESS("Setup looks good."))
        self.stdout.write("")
        self.stdout.write("The same checks, plus live failure rates and each pool's runway, are at")
        self.stdout.write("/admin/health/ once you are signed in as an administrator.")
        self.stdout.write("")
