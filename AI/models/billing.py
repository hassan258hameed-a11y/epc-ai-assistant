"""Plans, subscriptions and payments."""
from __future__ import annotations

import secrets
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


def generate_reference(prefix: str = "EPC") -> str:
    stamp = timezone.now().strftime("%y%m%d")
    return f"{prefix}-{stamp}-{secrets.token_hex(4).upper()}"


class Plan(models.Model):
    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=100, unique=True, blank=True)
    description = models.CharField(max_length=255, blank=True)
    tokens = models.BigIntegerField(help_text="Tokens credited when the plan is purchased.")
    #: Which model's tokens this plan sells. Tokens are per-model, so a plan has
    #: to say which currency it pays in; blank means the catalogue default, which
    #: is what plans created before multi-model support meant.
    ai_model = models.ForeignKey(
        "AI.AIModel",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="plans",
        help_text="The model these tokens can be spent on. Leave blank for the default model.",
    )
    price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=8, default="USD")
    duration_days = models.PositiveIntegerField(default=30)

    features = models.TextField(blank=True, help_text="One feature per line — rendered as a checklist.")

    # -- usage ceilings ----------------------------------------------------
    # How fast the tokens in this package may be spent, in each of the three
    # windows the product supports. These are numbers an administrator types,
    # not fractions derived from ``tokens``: a percentage rule hidden in code is
    # exactly what makes a limit impossible to change without a deploy.
    #
    # Zero means "no ceiling of this kind", so a package can be sold with, say,
    # a daily limit and no weekly one. They are copied onto each purchase at
    # activation (see AI.services.usage_limit_service.open_allocation), so
    # re-tuning these never rewrites the terms of a package already sold.
    daily_limit = models.BigIntegerField(
        default=0, help_text="Tokens spendable per rolling 24 hours. 0 = no daily ceiling."
    )
    weekly_limit = models.BigIntegerField(
        default=0, help_text="Tokens spendable per rolling 7 days. 0 = no weekly ceiling."
    )
    monthly_limit = models.BigIntegerField(
        default=0, help_text="Tokens spendable per monthly cycle. 0 = no monthly ceiling."
    )

    is_active = models.BooleanField(default=True, db_index=True)
    is_featured = models.BooleanField(default=False)
    display_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "price"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "plan"
            slug, counter = base, 2
            while Plan.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def token_model(self):
        """The model this plan's tokens belong to, resolved at read time."""
        if self.ai_model_id:
            return self.ai_model
        from AI.services import model_service

        return model_service.default_model()

    @property
    def grant_rows(self) -> list[dict]:
        """Every model this plan credits, primary first.

        One place to ask "what does this plan actually give?", so the pricing
        card, the console and the code that credits the wallets can never
        disagree about it.
        """
        rows = []
        primary = self.token_model
        if primary is not None:
            rows.append({"model": primary, "tokens": self.tokens, "is_primary": True})
        seen = {primary.pk} if primary is not None else set()
        # A page listing many plans prefetches ``model_grants__ai_model``; a
        # ``select_related`` here would build a fresh queryset and throw that
        # prefetch away, costing a query per plan. Without a prefetch, the join
        # is still what keeps it to one query rather than one per grant.
        prefetched = getattr(self, "_prefetched_objects_cache", {})
        grants = (
            self.model_grants.all()
            if "model_grants" in prefetched
            else self.model_grants.select_related("ai_model").all()
        )
        for grant in grants:
            if grant.ai_model_id in seen or grant.tokens <= 0:
                continue
            rows.append({"model": grant.ai_model, "tokens": grant.tokens, "is_primary": False})
            seen.add(grant.ai_model_id)
        return rows

    @property
    def is_bundle(self) -> bool:
        """Whether this plan includes more than one model."""
        return len(self.grant_rows) > 1

    @property
    def total_tokens(self) -> int:
        """Across every model. Not spendable as one figure — tokens are
        per-model currencies — but useful for comparing plans."""
        return sum(row["tokens"] for row in self.grant_rows)

    @property
    def feature_list(self) -> list[str]:
        return [line.strip() for line in self.features.splitlines() if line.strip()]

    @property
    def has_limits(self) -> bool:
        return bool(self.daily_limit or self.weekly_limit or self.monthly_limit)

    @property
    def limit_rows(self) -> list[dict]:
        """The three ceilings, ready for a pricing card. Never a percentage."""
        return [
            {"label": "Daily limit", "window": "daily", "value": self.daily_limit},
            {"label": "Weekly limit", "window": "weekly", "value": self.weekly_limit},
            {"label": "Monthly limit", "window": "monthly", "value": self.monthly_limit},
        ]

    @property
    def price_per_million(self) -> Decimal:
        if not self.tokens:
            return Decimal("0")
        return (self.price / Decimal(self.tokens) * Decimal(1_000_000)).quantize(Decimal("0.01"))


class PlanModelGrant(models.Model):
    """Extra models a plan hands out, beyond the one it is sold for.

    A plan has always granted one model's tokens: ``ai_model`` says which and
    ``tokens`` says how many. That is the right shape for a product whose
    prices are quoted per model, and it stays the primary relationship — it is
    what attributes a payment to a model, and therefore what keeps per-model
    revenue adding up to total revenue.

    A bundle needs more than that: "Pro" might include GPT-5 Mini *and* Claude.
    Each additional model gets a row here with its own token count, so an
    administrator can configure exactly what a plan includes without the plan
    losing the single model its price is booked against.

    Revenue is deliberately not split across the models in a bundle. Deciding
    that GPT-5 Mini earned $12 of a $20 sale and Claude $8 would be an
    invention — nobody bought them separately — so the money stays with the
    plan's primary model and the tokens go wherever the plan says.
    """

    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="model_grants")
    ai_model = models.ForeignKey(
        "AI.AIModel", on_delete=models.CASCADE, related_name="plan_grants"
    )
    tokens = models.BigIntegerField(
        default=0, help_text="Tokens of this model credited when the plan is bought."
    )

    class Meta:
        ordering = ["ai_model__sort_order", "ai_model__name"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "ai_model"], name="unique_plan_model_grant"),
            models.CheckConstraint(condition=models.Q(tokens__gte=0), name="plan_grant_non_negative"),
        ]

    def __str__(self) -> str:
        return f"{self.plan_id}:{self.ai_model_id}={self.tokens}"


class Subscription(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscriptions")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    payment = models.OneToOneField(
        "AI.Payment", null=True, blank=True, on_delete=models.SET_NULL, related_name="subscription"
    )

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    tokens_granted = models.BigIntegerField(default=0)
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status", "-ends_at"]),
            models.Index(fields=["status", "ends_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} — {self.plan.name}"

    @property
    def is_live(self) -> bool:
        return self.status == self.Status.ACTIVE and self.ends_at > timezone.now()

    @property
    def days_remaining(self) -> int:
        delta = self.ends_at - timezone.now()
        return max(0, delta.days)


class PaymentMethod(models.TextChoices):
    ABA_PAYWAY = "aba_payway", "ABA PayWay"
    ACLEDA_XPAY = "acleda_xpay", "ACLEDA Bank"
    ACLEDA_KHQR = "acleda_khqr", "ACLEDA KHQR"
    BINANCE = "binance", "Binance Pay"


class PaymentStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PAID = "paid", "Paid"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    REFUNDED = "refunded", "Refunded"
    EXPIRED = "expired", "Expired"


class Payment(models.Model):
    """A checkout attempt.

    ``tokens_credited`` is the idempotency guard: crediting only ever happens
    once, inside a locked transaction, regardless of how many times a webhook or
    an admin confirmation arrives.
    """

    reference = models.CharField(max_length=40, unique=True, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payments")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="payments")

    #: The price in the plan's own currency (USD). Every report adds these up,
    #: so it never holds riel or USDT — what was actually asked of the payer
    #: is ``charge_amount`` in ``charge_currency``.
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=8, default="USD")
    method = models.CharField(max_length=24, choices=PaymentMethod.choices, db_index=True)
    charge_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    charge_currency = models.CharField(max_length=8, blank=True)
    #: Riel per dollar used for a KHR charge, fixed when the order was made.
    exchange_rate = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    #: After this the order can no longer be paid; a pending one is expired.
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    #: The gateway's own id for the order (Binance prepayId, the KHQR md5…).
    provider_order_id = models.CharField(max_length=120, blank=True, db_index=True)
    status = models.CharField(max_length=16, choices=PaymentStatus.choices, default=PaymentStatus.PENDING, db_index=True)

    tokens_to_grant = models.BigIntegerField(default=0)
    tokens_credited = models.BooleanField(default=False)

    provider_txn_id = models.CharField(max_length=120, blank=True, db_index=True)
    provider_payload = models.JSONField(default=dict, blank=True)
    checkout_payload = models.JSONField(default=dict, blank=True)

    #: What was taken off the list price, and why. Stored on the payment rather
    #: than recomputed from the referral, because a campaign can be edited or
    #: deleted and the amount a student was actually charged must not move when
    #: it is. ``amount`` is always what they pay; this says what it would have
    #: been.
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    discount_reason = models.CharField(max_length=120, blank=True)

    payer_note = models.CharField(max_length=255, blank=True)
    proof = models.ImageField(upload_to="uploads/payment-proof/%Y/%m/", blank=True, null=True)

    admin_note = models.CharField(max_length=255, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="payments_verified"
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["method", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.reference} — {self.status}"

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = generate_reference()
        super().save(*args, **kwargs)

    @property
    def status_tone(self) -> str:
        return {
            PaymentStatus.PAID: "success",
            PaymentStatus.PENDING: "warning",
            PaymentStatus.FAILED: "danger",
            PaymentStatus.CANCELLED: "neutral",
            PaymentStatus.REFUNDED: "info",
            PaymentStatus.EXPIRED: "neutral",
        }.get(self.status, "neutral")

    @property
    def is_final(self) -> bool:
        return self.status in {
            PaymentStatus.PAID, PaymentStatus.REFUNDED, PaymentStatus.CANCELLED, PaymentStatus.EXPIRED,
        }

    # -- what the payer is actually asked for -----------------------------------
    @property
    def pay_amount(self) -> Decimal:
        return self.charge_amount if self.charge_amount is not None else self.amount

    @property
    def pay_currency(self) -> str:
        return self.charge_currency or self.currency

    @property
    def pay_display(self) -> str:
        """"៛102,500", "25.00 USDT", "$25.00" — how the charge reads on screen."""
        currency, amount = self.pay_currency.upper(), self.pay_amount
        if currency == "KHR":
            return f"៛{int(amount):,}"
        if currency == "USD":
            return f"${amount:,.2f}"
        return f"{amount:,.2f} {currency}"

    @property
    def is_converted(self) -> bool:
        """Charged in a different currency from the plan's price."""
        return bool(self.charge_currency) and self.charge_currency.upper() != self.currency.upper()

    @property
    def is_expired_now(self) -> bool:
        return bool(self.expires_at) and self.status == PaymentStatus.PENDING and self.expires_at <= timezone.now()


    @property
    def list_price(self):
        """What the plan costs before any referral discount."""
        return self.amount + (self.discount_amount or Decimal("0.00"))

    @property
    def was_discounted(self) -> bool:
        return bool(self.discount_amount and self.discount_amount > 0)


class PaymentAttempt(models.Model):
    """One trip to a hosted gateway for a payment.

    ABA PayWay refuses a transaction id it has seen before, and an ACLEDA XPAY
    session is single-use (posting it twice cancels it). So every press of
    "Pay" opens a fresh attempt with its own gateway reference, and the order
    keeps all of them: a student who finishes an older attempt — in another
    tab, or after coming back — is still recognised and credited once.
    """

    payment = models.ForeignKey("Payment", on_delete=models.CASCADE, related_name="attempts")
    method = models.CharField(max_length=24, choices=PaymentMethod.choices)
    #: What the gateway knows the attempt by: PayWay's tran_id, XPAY's txid.
    provider_ref = models.CharField(max_length=64, unique=True)
    #: XPAY's paymentTokenid — the key its status query takes.
    token = models.CharField(max_length=200, blank=True, db_index=True)
    session_id = models.CharField(max_length=200, blank=True)
    #: Non-secret extras worth keeping (a deeplink, the amount sent).
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["payment", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.payment_id}:{self.provider_ref}"


class PaymentEvent(models.Model):
    """Append-only trail of everything that happened to a payment."""

    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=48)
    message = models.CharField(max_length=255, blank=True)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.payment_id}:{self.event_type}"
