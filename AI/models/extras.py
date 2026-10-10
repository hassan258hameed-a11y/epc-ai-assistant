"""Promo codes, shared conversations and a student's standing instructions.

* :class:`PromoCode` / :class:`PromoRedemption` — a discount an administrator
  publishes, and each use of it. A redemption is *reserved* while its payment is
  pending, *used* once the payment settles, and *released* if the payment is
  cancelled, fails or is refunded — so a code with ten uses can never be spent
  eleven times by eleven people racing to the gateway.
* :class:`ConversationShare` — a read-only public link to a snapshot of one
  conversation, revocable at any time by its owner or an administrator.
* :class:`ChatPreference` — what a student wants every answer to know about
  them and how they like answers written.
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone

#: A pending payment holds its code for this long. After that an abandoned
#: checkout stops counting against the code's limits.
RESERVATION_HOURS = 24


class PromoCode(models.Model):
    class Kind(models.TextChoices):
        PERCENT = "percent", "Percentage off"
        FIXED = "fixed", "Fixed amount off"

    code = models.CharField(
        max_length=32, unique=True,
        validators=[RegexValidator(r"^[A-Z0-9][A-Z0-9_-]{2,31}$",
                                   "3–32 characters: letters, numbers, - and _.")],
        help_text="What students type at checkout. Stored in capitals.",
    )
    description = models.CharField(max_length=160, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PERCENT)
    value = models.DecimalField(
        max_digits=8, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Percent (e.g. 20) or dollars (e.g. 1.50).",
    )
    max_uses = models.PositiveIntegerField(default=0, help_text="Across everybody. 0 = unlimited.")
    per_user_limit = models.PositiveIntegerField(default=1, help_text="Per student. 0 = unlimited.")
    min_purchase = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0"))
    ai_model = models.ForeignKey(
        "AI.AIModel", null=True, blank=True, on_delete=models.SET_NULL, related_name="promo_codes",
        help_text="Only packages for this model. Blank = any model.",
    )
    eligible_plans = models.ManyToManyField(
        "AI.Plan", blank=True, related_name="promo_codes",
        help_text="Only these packages. Blank = any package.",
    )
    starts_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.code

    def save(self, *args, **kwargs):
        self.code = (self.code or "").strip().upper()
        super().save(*args, **kwargs)

    # -- counting ----------------------------------------------------------
    def counted_redemptions(self):
        """Uses that hold a place: paid ones, and pending ones still fresh."""
        fresh = timezone.now() - timedelta(hours=RESERVATION_HOURS)
        return self.redemptions.filter(
            models.Q(status=PromoRedemption.Status.USED)
            | models.Q(status=PromoRedemption.Status.RESERVED, created_at__gte=fresh)
        )

    @property
    def uses(self) -> int:
        return self.counted_redemptions().count()

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= timezone.now())

    @property
    def is_scheduled(self) -> bool:
        return bool(self.starts_at and self.starts_at > timezone.now())

    @property
    def status_label(self) -> tuple[str, str]:
        if not self.is_active:
            return "Paused", "neutral"
        if self.is_expired:
            return "Expired", "danger"
        if self.is_scheduled:
            return "Scheduled", "warning"
        if self.max_uses and self.uses >= self.max_uses:
            return "Used up", "danger"
        return "Live", "success"

    @property
    def value_label(self) -> str:
        if self.kind == self.Kind.PERCENT:
            return f"{self.value.normalize():f}% off"
        return f"${self.value:.2f} off"

    def discount_for(self, price: Decimal) -> Decimal:
        if self.kind == self.Kind.PERCENT:
            amount = price * self.value / Decimal("100")
        else:
            amount = self.value
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return min(max(amount, Decimal("0.00")), price)


class PromoRedemption(models.Model):
    class Status(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        USED = "used", "Used"
        RELEASED = "released", "Released"

    promo = models.ForeignKey(PromoCode, on_delete=models.CASCADE, related_name="redemptions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="promo_redemptions")
    payment = models.OneToOneField("AI.Payment", on_delete=models.CASCADE, related_name="promo_redemption")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RESERVED, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


def _share_token() -> str:
    return secrets.token_urlsafe(16)


class ConversationShare(models.Model):
    conversation = models.OneToOneField(
        "AI.Conversation", on_delete=models.CASCADE, related_name="share",
    )
    token = models.CharField(max_length=40, unique=True, default=_share_token, db_index=True)
    #: The link shows the conversation as it was when shared, up to and
    #: including this message. Updating the link moves it forward.
    last_message_id = models.BigIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    view_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"share:{self.conversation_id}"


class ChatPreference(models.Model):
    class Style(models.TextChoices):
        BALANCED = "balanced", "Balanced"
        CONCISE = "concise", "Concise"
        DETAILED = "detailed", "Detailed"
        STEP_BY_STEP = "steps", "Step by step"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chat_preference",
    )
    enabled = models.BooleanField(default=True)
    about_me = models.CharField(
        max_length=600, blank=True,
        help_text="What should the assistant know about you?",
    )
    instructions = models.CharField(
        max_length=900, blank=True,
        help_text="How would you like it to respond?",
    )
    style = models.CharField(max_length=12, choices=Style.choices, default=Style.BALANCED)
    language = models.CharField(max_length=40, blank=True, help_text="Answer in this language.")
    #: The model a new chat starts on, on every device. Blank: the platform default.
    default_model_key = models.CharField(max_length=64, blank=True)
    #: Questions a day the student wants to ask; 0 means no goal.
    daily_goal = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"preferences:{self.user_id}"

    @property
    def is_blank(self) -> bool:
        """Nothing that changes the prompt. The default model and the goal are
        settings, not instructions, so they do not count."""
        return not (self.about_me or self.instructions or self.language) and self.style == self.Style.BALANCED
