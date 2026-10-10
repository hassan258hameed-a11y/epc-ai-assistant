"""OTP codes, Telegram links, notifications and referral records."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


def hash_code(code: str) -> str:
    """One-way hash of an OTP so a database leak does not reveal live codes."""
    key = settings.SECRET_KEY.encode()
    return hmac.new(key, code.strip().encode(), hashlib.sha256).hexdigest()


class OTPPurpose(models.TextChoices):
    SIGNUP = "signup", "Verify account"
    LOGIN = "login", "Login verification"
    RESET = "reset", "Password reset"
    TELEGRAM = "telegram", "Telegram verification"


class OTPChannel(models.TextChoices):
    EMAIL = "email", "E-mail"
    TELEGRAM = "telegram", "Telegram"
    #: The same code to the private Telegram chat and the e-mail address.
    BOTH = "both", "Telegram and e-mail"


class OTPCode(models.Model):
    """Single-use, short-lived verification code.

    Codes are stored hashed, expire quickly, are consumed on first success and
    are hard-capped on attempts.
    """

    MAX_ATTEMPTS = 5

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="otp_codes")
    purpose = models.CharField(max_length=16, choices=OTPPurpose.choices, db_index=True)
    channel = models.CharField(max_length=16, choices=OTPChannel.choices)
    code_hash = models.CharField(max_length=64)
    destination = models.CharField(max_length=190, blank=True, help_text="Masked e-mail or Telegram handle.")

    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "purpose", "-created_at"])]

    def __str__(self) -> str:
        return f"OTP {self.purpose} for {self.user_id}"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_usable(self) -> bool:
        return self.consumed_at is None and not self.is_expired and self.attempts < self.MAX_ATTEMPTS

    def matches(self, raw_code: str) -> bool:
        return hmac.compare_digest(self.code_hash, hash_code(raw_code))


class TelegramAccount(models.Model):
    """Link between a platform user and their Telegram chat.

    Telegram bots cannot open a conversation, so the user must press Start
    first.  We hand out a one-time ``link_token`` embedded in the deep link
    (``t.me/<bot>?start=<token>``); the bot webhook resolves it back to the user.
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="telegram")
    telegram_id = models.CharField(max_length=32, unique=True, null=True, blank=True, db_index=True)
    username = models.CharField(max_length=64, blank=True)
    first_name = models.CharField(max_length=64, blank=True)

    link_token = models.CharField(max_length=48, unique=True, db_index=True)
    #: A link token is single-use and short-lived: after this it no longer
    #: links anything, and a new one has to be generated.
    link_token_expires_at = models.DateTimeField(null=True, blank=True)
    is_linked = models.BooleanField(default=False)
    linked_at = models.DateTimeField(null=True, blank=True)
    #: Telegram's chat type at link time. Only "private" is ever linked: OTPs
    #: and account details must never land in a group or a channel.
    chat_type = models.CharField(max_length=16, blank=True)
    #: Set when Telegram reports the student blocked the bot; delivery stops
    #: until they press Start again.
    blocked_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    last_delivery_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.user_id} -> {self.telegram_id or 'unlinked'}"

    def save(self, *args, **kwargs):
        if not self.link_token:
            self.link_token = secrets.token_urlsafe(18)
        super().save(*args, **kwargs)

    @property
    def deep_link(self) -> str:
        bot = getattr(settings, "TELEGRAM_BOT_USERNAME", "") or ""
        return f"https://t.me/{bot.lstrip('@')}?start={self.link_token}" if bot else ""

    def rotate_token(self) -> str:
        self.link_token = secrets.token_urlsafe(18)
        self.save(update_fields=["link_token"])
        return self.link_token


class NotificationLevel(models.TextChoices):
    INFO = "info", "Info"
    SUCCESS = "success", "Success"
    WARNING = "warning", "Warning"
    DANGER = "danger", "Alert"


class NotificationCategory(models.TextChoices):
    SYSTEM = "system", "System"
    TOKENS = "tokens", "Tokens"
    PAYMENT = "payment", "Payment"
    EXPIRY = "expiry", "Expiry"
    REFERRAL = "referral", "Referral"
    ANNOUNCEMENT = "announcement", "Announcement"


class AnnouncementAudience(models.TextChoices):
    """Who a broadcast is for. Resolved to accounts at publish time."""

    ALL = "all", "All active students"
    LOW = "low", "Running low on tokens"
    EMPTY = "empty", "No tokens at all"
    PLAN = "plan", "Students on a plan"
    MODEL = "model", "Students with a model"


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    #: Set when this row is one student's copy of a broadcast, so the console
    #: can count how many recipients have opened it.
    announcement = models.ForeignKey(
        "Announcement", null=True, blank=True, on_delete=models.SET_NULL, related_name="deliveries"
    )
    #: Stays at the top of the student's notification centre until read.
    is_pinned = models.BooleanField(default=False)
    title = models.CharField(max_length=140)
    body = models.TextField(blank=True)
    level = models.CharField(max_length=12, choices=NotificationLevel.choices, default=NotificationLevel.INFO)
    category = models.CharField(
        max_length=16, choices=NotificationCategory.choices, default=NotificationCategory.SYSTEM
    )
    action_url = models.CharField(max_length=255, blank=True)
    action_label = models.CharField(max_length=48, blank=True)
    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "is_read", "-created_at"])]

    def __str__(self) -> str:
        return self.title


class NotificationPreference(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preference"
    )
    email_enabled = models.BooleanField(default=True)
    telegram_enabled = models.BooleanField(default=True)
    low_token_alerts = models.BooleanField(default=True)
    expiry_reminders = models.BooleanField(default=True)
    payment_alerts = models.BooleanField(default=True)
    announcements = models.BooleanField(default=True)
    #: Browser notifications on devices where the student turned them on.
    push_enabled = models.BooleanField(default=True)
    #: "Your answer is ready" when a long answer finishes after the tab closed.
    push_answers = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"prefs:{self.user_id}"


class TelegramDeliveryStatus(models.TextChoices):
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    BLOCKED = "blocked", "Blocked by the user"
    SKIPPED = "skipped", "Skipped"


class TelegramDelivery(models.Model):
    """Every message the bot tried to send, and what happened to it.

    The text itself is not stored — it may be an OTP. What is kept is enough
    to answer "did it go, and if not, why": the kind, the target and the error.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="telegram_deliveries",
    )
    chat_id = models.CharField(max_length=64, blank=True)
    #: "private" (a student's bot chat) or "channel" (the public channel).
    target = models.CharField(max_length=12, default="private")
    kind = models.CharField(max_length=16, default="alert")      # otp, payment, alert, announcement, test…
    status = models.CharField(max_length=10, choices=TelegramDeliveryStatus.choices)
    error = models.CharField(max_length=255, blank=True)
    telegram_message_id = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"]), models.Index(fields=["kind", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.kind} → {self.target}:{self.chat_id} ({self.status})"


class TelegramMessageTarget(models.TextChoices):
    CHANNEL = "channel", "Channel"
    GROUP = "group", "Community group"
    STUDENT = "student", "One student"
    STUDENTS = "students", "All connected students"


class TelegramMessageStatus(models.TextChoices):
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    PARTLY = "partly", "Partly sent"
    FAILED = "failed", "Failed"
    DELETED = "deleted", "Deleted"


class TelegramMessage(models.Model):
    """A message an administrator wrote on Console → Telegram → Send.

    Unlike :class:`TelegramDelivery` this keeps the text: it is something the
    team chose to say, never a code. One row per send, however many chats it
    reached; ``sent``/``failed``/``skipped`` count them.
    """

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="telegram_messages_sent",
    )
    target = models.CharField(max_length=10, choices=TelegramMessageTarget.choices)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="telegram_messages_received",
    )
    title = models.CharField(max_length=140, blank=True)
    body = models.TextField()
    button_label = models.CharField(max_length=40, blank=True)
    button_url = models.CharField(max_length=300, blank=True)
    #: One student only: also put it in their in-app notifications.
    in_app = models.BooleanField(default=False)

    status = models.CharField(max_length=10, choices=TelegramMessageStatus.choices,
                              default=TelegramMessageStatus.SENDING)
    recipients = models.PositiveIntegerField(default=0)
    sent = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=255, blank=True)
    #: The channel or group post, so it can be deleted again.
    telegram_message_id = models.CharField(max_length=32, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.get_target_display()}: {(self.title or self.body)[:40]}"


class Announcement(models.Model):
    title = models.CharField(max_length=140)
    body = models.TextField()
    level = models.CharField(max_length=12, choices=NotificationLevel.choices, default=NotificationLevel.INFO)
    audience = models.CharField(
        max_length=12, choices=AnnouncementAudience.choices, default=AnnouncementAudience.ALL
    )
    is_pinned = models.BooleanField(default=False)
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    recipients = models.PositiveIntegerField(default=0)
    #: Narrow the audience to one plan or one model (audience "plan"/"model").
    audience_plan = models.ForeignKey(
        "AI.Plan", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    audience_model = models.ForeignKey(
        "AI.AIModel", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    #: Optional button on the notification.
    action_label = models.CharField(max_length=40, blank=True)
    action_url = models.CharField(max_length=300, blank=True)
    #: Sent by the scheduler at this time instead of straight away.
    scheduled_for = models.DateTimeField(null=True, blank=True, db_index=True)
    #: Withdrawn: its notifications were removed from every inbox.
    retracted_at = models.DateTimeField(null=True, blank=True)
    #: Telegram: also post to the public channel, and/or to linked students'
    #: private bot chats (only those whose preferences allow announcements).
    telegram_channel = models.BooleanField(default=True)
    telegram_private = models.BooleanField(default=False)
    channel_status = models.CharField(max_length=12, blank=True)   # "", sent, failed, skipped
    channel_message_id = models.CharField(max_length=32, blank=True)
    channel_error = models.CharField(max_length=255, blank=True)
    #: And/or to the community group.
    telegram_group = models.BooleanField(default=False)
    group_status = models.CharField(max_length=12, blank=True)     # "", sent, failed, skipped, deleted
    group_message_id = models.CharField(max_length=32, blank=True)
    group_error = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="announcements"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title


class AlertLog(models.Model):
    """Deduplication guard so the same reminder is never sent twice.

    ``key`` encodes the specific occurrence, e.g. ``low:5000`` or
    ``expiry:2026-09-01:3``.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="alert_logs")
    alert_type = models.CharField(max_length=32)
    key = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "alert_type", "key"], name="unique_alert_per_key"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.alert_type}:{self.key}"


class EmailType(models.TextChoices):
    OTP = "otp", "Verification code"
    LOW_TOKENS = "low_tokens", "Low token warning"
    EXPIRY = "expiry", "Plan expiry reminder"
    PAYMENT = "payment", "Payment update"
    REFERRAL = "referral", "Referral reward"
    SUPPORT = "support", "Support reply"
    CREDENTIALS = "credentials", "Account credentials"
    OTHER = "other", "Other"


class EmailStatus(models.TextChoices):
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


class EmailLog(models.Model):
    """Every message the platform tried to send, and what happened to it.

    An administrator asked to explain why a student says they were never warned
    needs three facts: was it sent, where to, and what the balance was at the
    time. Without a log the honest answer is "I don't know" — the SMTP
    conversation is long gone and the balance has moved on since.

    The address is stored as sent rather than read back from the account, so a
    student who later changes their e-mail does not rewrite the history of
    where their mail actually went.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="email_logs"
    )
    recipient = models.EmailField(help_text="The address used, as it was at the time of sending.")
    email_type = models.CharField(
        max_length=24, choices=EmailType.choices, default=EmailType.OTHER, db_index=True
    )
    subject = models.CharField(max_length=190, blank=True)

    #: Which model's tokens the message was about. Null for mail that is not
    #: about a model at all — a verification code belongs to no currency.
    ai_model = models.ForeignKey(
        "AI.AIModel", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="email_logs",
    )
    #: What caused it, e.g. ``balance<=25%``. Free text so a new trigger does
    #: not need a migration.
    trigger = models.CharField(max_length=120, blank=True)
    #: The figure that justified the message, kept so the log still makes sense
    #: after the balance has changed.
    balance_at_send = models.BigIntegerField(default=0)

    status = models.CharField(
        max_length=12, choices=EmailStatus.choices, default=EmailStatus.SENT, db_index=True
    )
    error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["email_type", "-created_at"]),
            models.Index(fields=["ai_model", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.email_type} -> {self.recipient} ({self.status})"

    @property
    def tone(self) -> str:
        return {
            EmailStatus.SENT: "success",
            EmailStatus.FAILED: "danger",
            EmailStatus.SKIPPED: "neutral",
        }.get(self.status, "neutral")


class Referral(models.Model):
    """One person invited by another, and what became of it.

    The lifecycle is longer than "pending or rewarded" because the interesting
    question is usually where a referral stopped. A code that produces plenty of
    registrations and no purchases is a different problem from one nobody uses,
    and the two used to look identical.

    ``PENDING`` and ``REWARDED`` are kept as names so existing rows and existing
    code keep their meaning; the new states sit between and after them.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Registered"           # account made, nothing owed yet
        AWAITING_PURCHASE = "awaiting", "Awaiting purchase"
        CONVERTED = "converted", "Converted"        # bought, reward not yet paid
        REWARDED = "rewarded", "Rewarded"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"
        REJECTED = "rejected", "Rejected"

    #: Nullable, because a campaign code belongs to the organisation rather
    #: than to a person: a code on a poster has no referrer. The alternative —
    #: pointing the row at the new account — would make every student who used
    #: a campaign code appear to have referred themselves.
    referrer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name="referrals_made",
    )
    referred_user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="referral_record"
    )
    code_used = models.CharField(max_length=32)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    referrer_bonus = models.IntegerField(default=0)
    referred_bonus = models.IntegerField(default=0)
    signup_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    rewarded_at = models.DateTimeField(null=True, blank=True)

    #: The campaign whose rules applied. Null is the personal-code referral the
    #: platform has always had, which is why this is nullable rather than
    #: required — every existing row is one of those.
    campaign = models.ForeignKey(
        "AI.ReferralCampaign", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="referrals",
    )
    #: Which currency the reward was paid in, so per-model reporting can
    #: attribute it. Null for a reward that was a discount, which is money.
    ai_model = models.ForeignKey(
        "AI.AIModel", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="referrals",
    )

    #: The purchase that converted this referral, when one did.
    payment = models.ForeignKey(
        "AI.Payment", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="referrals",
    )
    plan = models.ForeignKey(
        "AI.Plan", null=True, blank=True, on_delete=models.SET_NULL, related_name="referrals",
    )
    purchase_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    converted_at = models.DateTimeField(null=True, blank=True)

    #: What each side actually got, recorded at the time. The campaign can be
    #: edited afterwards; what was paid out cannot change retrospectively.
    referrer_reward_type = models.CharField(max_length=12, blank=True)
    referred_reward_type = models.CharField(max_length=12, blank=True)
    discount_applied = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["referrer", "-created_at"]),
            models.Index(fields=["campaign", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.referrer_id} -> {self.referred_user_id}"

    @property
    def tone(self) -> str:
        return {
            self.Status.PENDING: "info",
            self.Status.AWAITING_PURCHASE: "warning",
            self.Status.CONVERTED: "brand",
            self.Status.REWARDED: "success",
            self.Status.EXPIRED: "neutral",
            self.Status.CANCELLED: "neutral",
            self.Status.REJECTED: "danger",
        }.get(self.status, "neutral")

    @property
    def is_settled(self) -> bool:
        return self.status in {self.Status.REWARDED, self.Status.CANCELLED,
                               self.Status.EXPIRED, self.Status.REJECTED}


class PushSubscription(models.Model):
    """One browser (on one device) that agreed to receive notifications.

    Created by the browser's Push API when a student turns notifications on;
    ``endpoint`` is the push service URL the browser gave us, and ``p256dh`` /
    ``auth`` are the keys every message is encrypted with. A subscription the
    push service reports as gone (404/410) is deleted.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.CharField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    user_agent = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    failure_count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        indexes = [models.Index(fields=["user"])]

    def __str__(self) -> str:
        return f"push:{self.user_id}:{self.endpoint[:40]}"

