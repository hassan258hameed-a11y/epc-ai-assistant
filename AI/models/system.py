"""Application settings, audit trail and the rate-limit ledger."""
from __future__ import annotations

from django.conf import settings
from django.db import models


class AppSetting(models.Model):
    """Typed key/value store for operator-configurable values.

    Mostly business settings an administrator should be able to change from the
    UI.  A few rows are ``SECRET`` — a vendor API key an operator would rather
    set from the console than from a shell.  Those are write-only: they are read
    by server code through :func:`AI.services.settings_service.secret` and are
    never placed in a template context, so nothing that reaches a browser can
    contain one.  Payment credentials stay in the environment regardless.
    """

    class ValueType(models.TextChoices):
        INT = "int", "Number"
        STR = "str", "Text"
        BOOL = "bool", "Toggle"
        JSON = "json", "List / JSON"
        #: Write-only. Stored server-side and never rendered back to a browser —
        #: the settings page reports whether one is set, never what it is.
        SECRET = "secret", "Secret"

    key = models.CharField(max_length=64, unique=True, db_index=True)
    value = models.TextField(blank=True)
    value_type = models.CharField(max_length=8, choices=ValueType.choices, default=ValueType.STR)
    group = models.CharField(max_length=32, default="general")
    label = models.CharField(max_length=120)
    help_text = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="settings_changed"
    )

    class Meta:
        ordering = ["group", "key"]

    def __str__(self) -> str:
        return f"{self.key}={self.value}"


class AuditAction(models.TextChoices):
    LOGIN = "login", "Login"
    LOGIN_FAILED = "login_failed", "Failed login"
    LOGOUT = "logout", "Logout"
    USER_CREATE = "user_create", "Created user"
    USER_UPDATE = "user_update", "Updated user"
    USER_DISABLE = "user_disable", "Disabled user"
    USER_ENABLE = "user_enable", "Enabled user"
    PASSWORD_RESET = "password_reset", "Reset password"
    TOKENS_ADD = "tokens_add", "Added tokens"
    TOKENS_REMOVE = "tokens_remove", "Removed tokens"
    PLAN_CREATE = "plan_create", "Created plan"
    PLAN_UPDATE = "plan_update", "Updated plan"
    PLAN_TOGGLE = "plan_toggle", "Toggled plan"
    PAYMENT_CONFIRM = "payment_confirm", "Confirmed payment"
    PAYMENT_REJECT = "payment_reject", "Rejected payment"
    PAYMENT_REFUND = "payment_refund", "Refunded payment"
    SETTINGS_UPDATE = "settings_update", "Updated settings"
    BULK_IMPORT = "bulk_import", "Bulk import"
    ANNOUNCEMENT = "announcement", "Published announcement"
    DATA_EXPORT = "data_export", "Exported data"


class AuditLog(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_entries"
    )
    action = models.CharField(max_length=32, choices=AuditAction.choices, db_index=True)
    target_type = models.CharField(max_length=48, blank=True)
    target_id = models.CharField(max_length=48, blank=True)
    target_label = models.CharField(max_length=140, blank=True)
    description = models.CharField(max_length=255, blank=True)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["action", "-created_at"]),
            models.Index(fields=["actor", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.action} by {self.actor_id}"

    @property
    def tone(self) -> str:
        if self.action in {AuditAction.USER_DISABLE, AuditAction.TOKENS_REMOVE, AuditAction.PAYMENT_REJECT,
                           AuditAction.LOGIN_FAILED, AuditAction.PAYMENT_REFUND}:
            return "danger"
        if self.action in {AuditAction.TOKENS_ADD, AuditAction.PAYMENT_CONFIRM, AuditAction.USER_ENABLE,
                           AuditAction.USER_CREATE}:
            return "success"
        if self.action == AuditAction.DATA_EXPORT:
            return "danger"
        return "neutral"


class RateLimitEvent(models.Model):
    """Durable sliding-window counter.

    Backed by the database rather than the cache so limits survive process
    restarts and hold across multiple workers even without Redis.
    """

    scope = models.CharField(max_length=32, db_index=True)
    identity = models.CharField(max_length=190, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["scope", "identity", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.scope}:{self.identity}"
