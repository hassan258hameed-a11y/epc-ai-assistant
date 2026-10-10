"""The catalogue of AI models the platform can call.

Rows here are the single source of truth for *which* models exist, which of
them students may pick, how much output budget each gets and how many tokens
each has left of its own allocation.  Keeping this in the database rather than
in settings is what lets an administrator turn a model off, re-budget it or add
a new one without a deploy.

Running totals (``tokens_consumed`` and friends) are denormalised counters kept
in step with :class:`AI.models.chat.AIUsageLog` by
``AI.services.model_service.record_usage``.  They exist so the admin console can
render per-model figures without scanning the whole usage log.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class AIModel(models.Model):
    """One callable model, with its own budget and availability switch."""

    #: The identifier sent to the provider, e.g. ``claude-sonnet-5``. This is the
    #: ``api_model_id`` of the design: what actually goes on the wire.
    key = models.CharField(max_length=64, unique=True, db_index=True)
    #: What students see in the model picker.
    name = models.CharField(max_length=64)
    #: Routing slug, resolved by ``AI.services.providers.build``. Kept as a plain
    #: string as well as a foreign key so provider selection never depends on a
    #: join, and so rows written before the registry existed keep working.
    provider = models.CharField(max_length=32, default="openai")
    provider_ref = models.ForeignKey(
        "AI.AIProvider",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="models",
        help_text="The vendor this model belongs to. Disabling the vendor disables the model.",
    )
    description = models.CharField(max_length=255, blank=True)

    #: Name of the server setting holding *this model's* credential, for a
    #: deployment that bills different models to different vendor accounts.
    #: Blank means "use the vendor's own default key", which is what every
    #: model did before per-model billing existed.
    #:
    #: The *name* is stored, never the key: the secret stays in the server
    #: environment, so nothing in the database or in any page can leak it.
    api_key_setting = models.CharField(
        max_length=64,
        blank=True,
        help_text=(
            "Server environment variable holding this model's API key, e.g. "
            "OPENAI_GPT4O_API_KEY. Leave blank to use the provider's default key."
        ),
    )

    is_active = models.BooleanField(
        default=True, help_text="Uncheck to hide this model from students immediately."
    )
    is_default = models.BooleanField(
        default=False, help_text="Used when a student does not pick a model."
    )
    supports_vision = models.BooleanField(default=False)
    #: One line in the model picker telling a student when to choose this
    #: model, e.g. "Quick everyday questions" or "Hard maths and long essays".
    best_for = models.CharField(
        max_length=120, blank=True,
        help_text="Shown in the student model picker: when this model is the right choice.",
    )

    class ServiceStatus(models.TextChoices):
        OPERATIONAL = "operational", "Working normally"
        DEGRADED = "degraded", "Slow or unreliable"
        PAUSED = "paused", "Paused"

    #: Set by an administrator during an incident. Degraded warns students in
    #: the picker and above the composer; paused stops new messages to this
    #: model (with the note) while history, export and other models still work.
    service_status = models.CharField(
        max_length=12, choices=ServiceStatus.choices, default=ServiceStatus.OPERATIONAL,
    )
    status_note = models.CharField(
        max_length=200, blank=True, help_text="Shown to students while the status is not normal.",
    )
    status_changed_at = models.DateTimeField(null=True, blank=True)

    #: Whether a new account is given some of this model's tokens automatically.
    #: Unchecked means the supply is distributed by hand only — which is the
    #: right setting for a model bought as a fixed block of capacity, where
    #: every token given away is one the organisation has already paid for and
    #: an administrator wants to decide who gets it.
    auto_grant = models.BooleanField(
        default=True,
        verbose_name="Include in the welcome grant",
        help_text=(
            "Give new students a small amount of this model automatically. "
            "Uncheck for a model the administrator distributes by hand."
        ),
    )

    #: The currency name for this model's tokens, e.g. ``claude_sonnet_5``. Every
    #: balance, pool and ledger row is denominated in exactly one of these, which
    #: is what stops Sonnet spending from touching GPT.
    token_type = models.SlugField(
        max_length=48, blank=True,
        help_text="Label for this model's token currency. Derived from the key when blank.",
    )

    class Thinking(models.TextChoices):
        OFF = "off", "Off — answer directly"
        ADAPTIVE = "adaptive", "Adaptive — think first (costs more output tokens)"

    #: Reasoning models bill their hidden thinking as output tokens, which the
    #: student pays for. Defaulting to "off" keeps a metered product predictable;
    #: an administrator turns it on per model when the quality is worth it.
    thinking_mode = models.CharField(
        max_length=12, choices=Thinking.choices, default=Thinking.OFF,
        help_text="Anthropic models only. Thinking tokens are billed as output tokens.",
    )

    max_output_tokens = models.PositiveIntegerField(
        default=0, help_text="Per-reply output budget. 0 falls back to the global default."
    )
    temperature = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        default=Decimal("0.70"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("2"))],
        help_text="Ignored by reasoning models, which only accept their default.",
    )

    # Provider list price, in USD per one million tokens. Used for cost reporting
    # only — students are always billed in tokens, never in currency.
    input_cost_per_1m = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    output_cost_per_1m = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))

    #: Optional cap across all students. 0 means "no cap".
    token_allocation = models.BigIntegerField(
        default=0, help_text="Total tokens this model may consume. 0 = unlimited."
    )

    # -- running totals ----------------------------------------------------
    tokens_consumed = models.BigIntegerField(default=0)
    input_tokens_total = models.BigIntegerField(default=0)
    output_tokens_total = models.BigIntegerField(default=0)
    request_count = models.BigIntegerField(default=0)
    failure_count = models.BigIntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)

    #: Percentages at which to warn a student about *this* model, overriding
    #: the platform-wide points. Blank means "use the global setting", which is
    #: what every model does until an administrator decides one of them needs
    #: warning about earlier — an expensive model a student burns through in a
    #: week is worth a heads-up at 50% that a cheap one is not.
    warning_thresholds = models.CharField(
        max_length=64, blank=True,
        verbose_name="Low-token warning points (%)",
        help_text=(
            "Comma-separated percentages, e.g. 50,25,10. Leave blank to use the "
            "platform default set under Settings."
        ),
    )

    #: The daily, weekly and monthly ceilings for tokens of this model that were
    #: not bought as a package — an administrator's grant, a welcome bonus, a
    #: referral reward. A package carries its plan's own ceilings; everything
    #: else carries these, so every model is rate-limited the same way whatever
    #: route the tokens arrived by. 0 means no ceiling for that window.
    default_daily_limit = models.PositiveIntegerField(
        default=0, blank=True, verbose_name="Default daily limit",
        help_text="Tokens a student may spend per day from tokens not bought as a package. 0 = no limit.",
    )
    default_weekly_limit = models.PositiveIntegerField(
        default=0, blank=True, verbose_name="Default weekly limit",
        help_text="Tokens per week from tokens not bought as a package. 0 = no limit.",
    )
    default_monthly_limit = models.PositiveIntegerField(
        default=0, blank=True, verbose_name="Default monthly limit",
        help_text="Tokens per month from tokens not bought as a package. 0 = no limit.",
    )

    sort_order = models.PositiveSmallIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name"]
        indexes = [models.Index(fields=["is_active", "sort_order"])]

    @property
    def is_paused(self) -> bool:
        return self.service_status == self.ServiceStatus.PAUSED

    @property
    def is_degraded(self) -> bool:
        return self.service_status == self.ServiceStatus.DEGRADED

    def __str__(self) -> str:
        return f"{self.name} ({self.key})"

    def save(self, *args, **kwargs):
        # The routing slug and the provider row must agree, whichever one the
        # caller set. The string is authoritative because it is what the router
        # dispatches on; the row exists so a vendor can be disabled wholesale.
        if self.provider_ref_id and not self.provider:
            self.provider = self.provider_ref.slug
        if not self.token_type:
            self.token_type = (self.key or "").replace(".", "_").replace("-", "_").lower()[:48]
        super().save(*args, **kwargs)

    @property
    def api_key(self) -> str:
        """This model's credential, resolved from the server environment.

        Server-side only. Returns an empty string when the row names no setting
        or the setting is unset, which the provider reads as "fall back to the
        vendor default" rather than as an error.
        """
        from django.conf import settings as django_settings

        if not self.api_key_setting:
            return ""
        return str(getattr(django_settings, self.api_key_setting, "") or "")

    @property
    def has_own_credential(self) -> bool:
        """Whether this model bills to its own vendor account. Never the key."""
        return bool(self.api_key)

    @property
    def provider_name(self) -> str:
        """Vendor name for display, falling back to the routing slug."""
        if self.provider_ref_id:
            return self.provider_ref.name
        return (self.provider or "").title()

    @property
    def provider_is_active(self) -> bool:
        """A model is only as available as the vendor behind it."""
        return self.provider_ref.is_active if self.provider_ref_id else True

    @property
    def slug(self) -> str:
        """The product name of this model, as students see it in the picker.

        The raw provider key stays in the admin console, where knowing exactly
        which model was billed actually matters.
        """
        return "-".join((self.name or self.key).lower().split()) or self.key

    @property
    def warning_points(self) -> list[int] | None:
        """This model's own warning percentages, or None to use the default.

        Parsed rather than trusted: the field is free text so an administrator
        can type "50, 25, 10", and one bad character must not stop a warning
        being sent at all.
        """
        raw = (self.warning_thresholds or "").replace(";", ",")
        if not raw.strip():
            return None
        points = []
        for part in raw.split(","):
            try:
                value = int(part.strip().rstrip("%"))
            except (TypeError, ValueError):
                continue
            if 0 < value <= 100:
                points.append(value)
        return sorted(set(points), reverse=True) or None

    @property
    def default_limits(self) -> dict[str, int]:
        return {
            "daily": int(self.default_daily_limit or 0),
            "weekly": int(self.default_weekly_limit or 0),
            "monthly": int(self.default_monthly_limit or 0),
        }

    @property
    def has_default_limits(self) -> bool:
        return any(self.default_limits.values())

    # -- budget ------------------------------------------------------------
    @property
    def is_metered(self) -> bool:
        return self.token_allocation > 0

    @property
    def tokens_remaining(self) -> int:
        if not self.is_metered:
            return 0
        return max(0, self.token_allocation - self.tokens_consumed)

    @property
    def is_exhausted(self) -> bool:
        return self.is_metered and self.tokens_consumed >= self.token_allocation

    @property
    def usage_percent(self) -> int:
        if not self.is_metered:
            return 0
        return min(100, round(self.tokens_consumed / self.token_allocation * 100))

    @property
    def is_available(self) -> bool:
        """Vendor up, model on, and still inside its own allocation."""
        return self.is_active and self.provider_is_active and not self.is_exhausted

    # -- admin supply ------------------------------------------------------
    @property
    def pool_balance(self) -> int:
        """Tokens the administrator still has to hand out for this model."""
        pool = getattr(self, "pool", None)
        return pool.balance if pool else 0

    # -- reporting ---------------------------------------------------------
    @property
    def estimated_cost(self) -> Decimal:
        """USD spent at list price, to two decimal places of meaning."""
        million = Decimal("1000000")
        inbound = (Decimal(self.input_tokens_total) / million) * self.input_cost_per_1m
        outbound = (Decimal(self.output_tokens_total) / million) * self.output_cost_per_1m
        return (inbound + outbound).quantize(Decimal("0.0001"))

    @property
    def availability_badge(self) -> tuple[str, str]:
        if not self.provider_is_active:
            return "Provider off", "danger"
        if not self.is_active:
            return "Disabled", "neutral"
        if self.is_exhausted:
            return "Allocation spent", "danger"
        if self.is_metered and self.usage_percent >= 85:
            return "Nearly spent", "warning"
        return "Available", "success"
