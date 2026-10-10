"""Admin-managed referral campaigns, layered over the personal codes.

The platform already had referrals: every account is issued a personal code,
and both sides receive a fixed number of tokens once the new account verifies.
That works, is in use, and is not replaced here — a referral with no campaign is
exactly that arrangement and behaves as it always has.

What it could not do is let an administrator *run* anything. There was one
reward, set globally, in one currency, paid at one moment, to everybody. No code
could expire, be limited to fifty uses, offer 20% off Claude, or pay out only
once the invited student actually bought something.

A campaign is that missing layer: a named code with its own reward, its own
audience and its own end date. `Referral` gains a nullable link to one, so the
two systems share a single history table and the console has one place to look
rather than two that disagree.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


def _trim(value: Decimal) -> str:
    """``20.00`` -> ``20``; ``12.50`` -> ``12.5``.

    ``Decimal.normalize`` would give ``2E+1`` for 20, which is correct and
    unreadable.
    """
    text = f"{value:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class RewardType(models.TextChoices):
    """What a referral gives. Tokens are credited; discounts reduce a price."""

    NONE = "none", "No reward"
    TOKENS = "tokens", "Free tokens"
    PERCENT = "percent", "Percentage discount"
    FIXED = "fixed", "Fixed discount"


class RewardTrigger(models.TextChoices):
    ON_SIGNUP = "signup", "As soon as the account is verified"
    ON_PURCHASE = "purchase", "Only after a successful payment"


class RewardWho(models.TextChoices):
    REFERRER = "referrer", "The person who shared the code"
    REFERRED = "referred", "The person who used it"
    BOTH = "both", "Both"


class ReferralCampaign(models.Model):
    """One shareable code, with its own rules.

    Deliberately not a copy of ``Plan``: a campaign does not sell anything, it
    changes what a referral is worth. Keeping the two apart is what allows a
    single code to apply across several plans, or to none of them.
    """

    code = models.CharField(
        max_length=32, unique=True, db_index=True,
        help_text="What people type or share. Case-insensitive; stored upper-case.",
    )
    name = models.CharField(max_length=120, help_text="Internal name, e.g. “Autumn intake”.")
    description = models.CharField(max_length=255, blank=True)

    is_active = models.BooleanField(default=True, db_index=True)
    starts_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(
        null=True, blank=True, help_text="Leave blank for a code that never expires.",
    )
    #: 0 means unlimited. Counted in successful registrations, not clicks — a
    #: limit that could be exhausted by people merely opening a link would be
    #: trivial to burn through.
    usage_limit = models.PositiveIntegerField(
        default=0, help_text="Maximum number of signups. 0 = unlimited.",
    )

    #: Which model's tokens the reward is denominated in, and which model's
    #: plans a discount applies to. Null means every model.
    ai_model = models.ForeignKey(
        "AI.AIModel", null=True, blank=True, on_delete=models.CASCADE,
        related_name="referral_campaigns",
        help_text="Restrict this code to one model. Leave blank for all models.",
    )
    #: Empty means every plan of the applicable model(s).
    eligible_plans = models.ManyToManyField(
        "AI.Plan", blank=True, related_name="referral_campaigns",
        help_text="Restrict to specific plans. Leave empty for all of them.",
    )
    minimum_purchase = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"),
        help_text="Smallest purchase that qualifies. 0 = any purchase.",
    )

    reward_who = models.CharField(max_length=12, choices=RewardWho.choices, default=RewardWho.BOTH)
    reward_trigger = models.CharField(
        max_length=12, choices=RewardTrigger.choices, default=RewardTrigger.ON_SIGNUP,
    )

    referrer_reward_type = models.CharField(
        max_length=12, choices=RewardType.choices, default=RewardType.TOKENS,
    )
    referrer_reward_value = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="Tokens, percent, or currency — whichever the type says.",
    )
    referred_reward_type = models.CharField(
        max_length=12, choices=RewardType.choices, default=RewardType.TOKENS,
    )
    referred_reward_value = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))

    # -- counters, kept in step by referral_service ------------------------
    clicks = models.PositiveIntegerField(default=0)
    registrations = models.PositiveIntegerField(default=0)
    conversions = models.PositiveIntegerField(default=0)
    revenue = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    rewards_given = models.BigIntegerField(default=0, help_text="Tokens handed out by this code.")
    discount_given = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="referral_campaigns_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["is_active", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.code} ({self.name})"

    def save(self, *args, **kwargs):
        self.code = (self.code or "").strip().upper()
        super().save(*args, **kwargs)

    # -- state -------------------------------------------------------------
    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= timezone.now())

    @property
    def is_exhausted(self) -> bool:
        return bool(self.usage_limit and self.registrations >= self.usage_limit)

    @property
    def has_started(self) -> bool:
        return self.starts_at <= timezone.now()

    @property
    def is_usable(self) -> bool:
        """Whether a new signup may still use this code."""
        return self.is_active and self.has_started and not self.is_expired and not self.is_exhausted

    @property
    def state(self) -> str:
        if not self.is_active:
            return "paused"
        if not self.has_started:
            return "scheduled"
        if self.is_expired:
            return "expired"
        if self.is_exhausted:
            return "exhausted"
        return "live"

    @property
    def state_badge(self) -> tuple[str, str]:
        label, tone = {
            "live": ("Live", "success"),
            "paused": ("Paused", "neutral"),
            "scheduled": ("Scheduled", "info"),
            "expired": ("Expired", "danger"),
            "exhausted": ("Fully used", "warning"),
        }[self.state]
        return label, tone

    @property
    def conversion_rate(self) -> int:
        if not self.registrations:
            return 0
        return int(round(self.conversions / self.registrations * 100))

    @property
    def remaining_uses(self) -> int | None:
        if not self.usage_limit:
            return None
        return max(0, self.usage_limit - self.registrations)

    @property
    def share_path(self) -> str:
        """The link to hand out. Resolved against SITE_URL for display."""
        return f"/r/{self.code}/"

    # Templates cannot pass an argument to a method, so each side gets its own
    # property. Both defer to one implementation so the two can never describe
    # the same reward differently.
    @property
    def referred_reward_label(self) -> str:
        return self.describe_reward("referred")

    @property
    def referrer_reward_label(self) -> str:
        return self.describe_reward("referrer")

    def describe_reward(self, side: str) -> str:
        """A human sentence for one side of the deal, for the console."""
        kind = self.referrer_reward_type if side == "referrer" else self.referred_reward_type
        value = self.referrer_reward_value if side == "referrer" else self.referred_reward_value
        if kind == RewardType.NONE or not value:
            return "—"
        if kind == RewardType.TOKENS:
            if self.ai_model:
                return f"{int(value):,} {self.ai_model.name} tokens"
            # No model means every model, and tokens are a separate currency
            # per model — so this is that many of each, not that many shared.
            return f"{int(value):,} tokens of every model"
        if kind == RewardType.PERCENT:
            # `Decimal.normalize()` turns 20 into 2E+1. Strip the trailing zeros
            # by hand so 20.00 reads as "20" and 12.50 as "12.5".
            return f"{_trim(value)}% off"
        return f"${_trim(value)} off"


class ReferralClick(models.Model):
    """A visit to a campaign link, before anyone has signed up.

    Kept as rows rather than only as a counter so the console can show when
    interest happened, not just how much of it there was — a code that got two
    hundred clicks in one hour and none since is a different problem from one
    getting two a day.
    """

    campaign = models.ForeignKey(
        ReferralCampaign, on_delete=models.CASCADE, related_name="click_log"
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["campaign", "-created_at"])]

    def __str__(self) -> str:
        return f"click on {self.campaign_id}"
