"""User accounts, roles and the token ledger account attached to each user."""
from __future__ import annotations

import secrets
import string

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone

REFERRAL_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous glyphs


def generate_referral_code(length: int = 8) -> str:
    return "".join(secrets.choice(REFERRAL_ALPHABET) for _ in range(length))


def generate_password(length: int = 12) -> str:
    pool = string.ascii_letters + string.digits
    return "".join(secrets.choice(pool) for _ in range(length))


class Role(models.TextChoices):
    STUDENT = "student", "Student"
    ADMIN = "admin", "Administrator"


class StaffRole(models.TextChoices):
    """What an administrator may do in the console.

    Only meaningful for ``Role.ADMIN`` accounts (and superusers, who are always
    owners). The areas each role reaches are defined in
    ``AI/services/staff_permissions.py``; this is only the label on the account.
    """
    OWNER = "owner", "Owner"
    SUPPORT = "support", "Support"
    FINANCE = "finance", "Finance"
    CONTENT = "content", "Content"
    OPERATIONS = "operations", "Operations"


class AccountStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    DISABLED = "disabled", "Disabled"
    PENDING = "pending", "Pending verification"


class AccountSource(models.TextChoices):
    WEB = "web", "Website signup"
    TELEGRAM = "telegram", "Telegram"
    MESSENGER = "messenger", "Messenger"
    INSTAGRAM = "instagram", "Instagram"
    TIKTOK = "tiktok", "TikTok"
    ADMIN = "admin", "Created by admin"
    IMPORT = "import", "Bulk import"
    # A student who came in on somebody's referral code, entered by hand.
    REFERRAL = "referral", "Referral"


class UserManager(BaseUserManager):
    """Manager that normalises e-mail and guarantees a token account exists."""

    use_in_migrations = True

    def _create(self, username, email, password, **extra):
        if not username:
            raise ValueError("A username is required.")
        email = self.normalize_email(email) if email else ""
        user = self.model(username=username, email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, username, email=None, password=None, **extra):
        extra.setdefault("role", Role.STUDENT)
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(username, email, password, **extra)

    def create_superuser(self, username, email=None, password=None, **extra):
        extra.setdefault("role", Role.ADMIN)
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("status", AccountStatus.ACTIVE)
        extra.setdefault("is_verified", True)
        if extra.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        return self._create(username, email, password, **extra)

    def students(self):
        return self.filter(role=Role.STUDENT)


class User(AbstractUser):
    """Single user model for both students and administrators.

    Role is authoritative for authorisation; ``is_staff`` is kept in sync so the
    break-glass Django admin stays usable for superusers when it is switched on.
    """

    first_name = None  # replaced by full_name
    last_name = None

    email = models.EmailField("email address", blank=True, db_index=True)
    full_name = models.CharField(max_length=150, blank=True)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.STUDENT, db_index=True)
    #: For administrators: which parts of the console they may use. Blank on
    #: student accounts. Existing administrators became owners.
    staff_role = models.CharField(max_length=16, choices=StaffRole.choices, blank=True, default="")
    status = models.CharField(
        max_length=16, choices=AccountStatus.choices, default=AccountStatus.ACTIVE, db_index=True
    )
    source = models.CharField(max_length=16, choices=AccountSource.choices, default=AccountSource.WEB)

    phone = models.CharField(max_length=32, blank=True)
    notes = models.TextField(blank=True, help_text="Internal notes, visible to admins only.")
    #: Short labels an administrator files students under ("scholarship",
    #: "school: RUPP"…). Admin-only, lower-cased, never shown to the student.
    tags = models.JSONField(default=list, blank=True)
    #: Ask for a one-time code after the password at every sign-in — sent to
    #: Telegram when linked, otherwise by e-mail.
    login_otp_enabled = models.BooleanField(default=False)

    is_verified = models.BooleanField(default=False, help_text="Completed e-mail or Telegram OTP verification.")
    must_change_password = models.BooleanField(default=False)

    referral_code = models.CharField(max_length=16, unique=True, db_index=True)
    referred_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="referred_users"
    )

    created_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="created_accounts"
    )

    # Single-active-session enforcement.
    active_session_key = models.CharField(max_length=40, blank=True, db_index=True)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    disabled_at = models.DateTimeField(null=True, blank=True)
    disabled_reason = models.CharField(max_length=255, blank=True)

    objects = UserManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["email"]

    class Meta:
        ordering = ["-date_joined"]
        indexes = [
            models.Index(fields=["role", "status"]),
            models.Index(fields=["-date_joined"]),
            models.Index(fields=["source"]),
        ]

    def __str__(self) -> str:
        return self.full_name or self.username

    def save(self, *args, **kwargs):
        if not self.referral_code:
            for _ in range(12):
                code = generate_referral_code()
                if not User.objects.filter(referral_code=code).exists():
                    self.referral_code = code
                    break
            else:  # pragma: no cover — astronomically unlikely
                self.referral_code = generate_referral_code(12)
        if self.role == Role.ADMIN:
            self.is_staff = True
        super().save(*args, **kwargs)

    # -- role helpers ------------------------------------------------------
    @property
    def is_admin_user(self) -> bool:
        return self.role == Role.ADMIN or self.is_superuser

    @property
    def is_student(self) -> bool:
        return not self.is_admin_user

    @property
    def is_active_account(self) -> bool:
        return self.is_active and self.status == AccountStatus.ACTIVE

    @property
    def display_name(self) -> str:
        return self.full_name or self.username

    @property
    def initials(self) -> str:
        source = (self.full_name or self.username or "?").strip()
        parts = [p for p in source.split() if p]
        if len(parts) >= 2:
            return (parts[0][0] + parts[1][0]).upper()
        return source[:2].upper()

    # -- convenience -------------------------------------------------------
    @property
    def token_balance(self) -> int:
        account = getattr(self, "token_account", None)
        return account.balance if account else 0

    @property
    def active_subscription(self):
        """The live subscription, or ``None``.

        A property that queries, so a list rendering it for every row is one
        query per row. A caller listing users should prefetch into
        ``live_subscriptions``; when that has been done this reads the prefetched
        rows instead of asking again, and a single-user page is unaffected.
        """
        from AI.models.billing import Subscription

        # Set by a list view that prefetched with to_attr="live_subscriptions".
        cached = getattr(self, "live_subscriptions", None)
        if cached is not None:
            return cached[0] if cached else None

        return (
            Subscription.objects.filter(user=self, status=Subscription.Status.ACTIVE, ends_at__gt=timezone.now())
            .select_related("plan")
            .order_by("-ends_at")
            .first()
        )

    def status_badge(self) -> tuple[str, str]:
        """Return (label, tone) used by the admin list badges."""
        if self.status == AccountStatus.DISABLED or not self.is_active:
            return "Disabled", "danger"
        if self.status == AccountStatus.PENDING:
            return "Pending", "warning"
        if self.token_balance <= 0:
            return "No tokens", "warning"
        subscription = self.active_subscription
        if subscription is None:
            return "Active", "success"
        if subscription.ends_at <= timezone.now():
            return "Expired", "danger"
        return "Active", "success"


class TokenAccount(models.Model):
    """Authoritative balance for a user.

    All mutations go through ``AI.services.token_service`` which locks this row
    inside a transaction, so concurrent AI requests cannot double-spend.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="token_account")
    balance = models.BigIntegerField(default=0)

    #: Lifetime figures — every token ever credited and every token ever spent.
    total_allocated = models.BigIntegerField(default=0)
    total_consumed = models.BigIntegerField(default=0)

    # The *current* allocation, which is what a student is shown as
    # "42,350 / 50,000". An administrator assigning a balance starts a new cycle;
    # top-ups extend the one in progress. The invariant
    # ``balance == cycle_allocated - cycle_consumed`` is maintained by
    # ``AI.services.token_service``.
    cycle_allocated = models.BigIntegerField(default=0)
    cycle_consumed = models.BigIntegerField(default=0)
    cycle_started_at = models.DateTimeField(null=True, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(balance__gte=0), name="token_balance_non_negative"),
        ]

    def __str__(self) -> str:
        return f"{self.user.username}: {self.balance:,} tokens"

    @property
    def allocation(self) -> int:
        """The denominator a student sees. Falls back to lifetime for old rows."""
        return self.cycle_allocated or self.total_allocated

    @property
    def used_in_cycle(self) -> int:
        return self.cycle_consumed if self.cycle_allocated else self.total_consumed

    @property
    def usage_percent(self) -> int:
        total = self.allocation
        if total <= 0:
            return 0
        return min(100, round(self.used_in_cycle / total * 100))

    @property
    def remaining_percent(self) -> int:
        total = self.allocation
        if total <= 0:
            return 0
        return max(0, min(100, round(self.balance / total * 100)))
