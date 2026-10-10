"""Django's stock admin — a break-glass tool, not part of the product.

The whole administrative job lives in the console at ``/admin/``: students,
tokens, the model catalogue, conversations, subscriptions, payments, audit logs
and settings. Nothing here is needed for normal operation.

These registrations exist only so that raw data is reachable if the console is
ever unusable. The URL is **not mounted** unless ``DJANGO_ADMIN_ENABLED=True``.
"""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from AI.models import (
    AIModel,
    AIUsageLog,
    Announcement,
    AppSetting,
    AuditLog,
    Conversation,
    Message,
    Notification,
    Payment,
    Plan,
    PoolTransaction,
    Referral,
    Subscription,
    TelegramAccount,
    TokenAccount,
    TokenPool,
    TokenTransaction,
    User,
)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "full_name", "email", "role", "status", "source", "date_joined")
    list_filter = ("role", "status", "source", "is_verified")
    search_fields = ("username", "full_name", "email", "phone", "referral_code")
    ordering = ("-date_joined",)
    readonly_fields = ("referral_code", "active_session_key", "last_seen_at", "last_login_ip")
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Profile", {"fields": ("full_name", "email", "phone", "notes")}),
        ("Access", {"fields": ("role", "status", "source", "is_verified", "must_change_password",
                               "is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Growth", {"fields": ("referral_code", "referred_by", "created_by")}),
        ("Session", {"fields": ("active_session_key", "last_login_ip", "last_seen_at", "last_login")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("username", "email", "full_name", "password1", "password2", "role")}),
    )


@admin.register(TokenAccount)
class TokenAccountAdmin(admin.ModelAdmin):
    list_display = ("user", "balance", "total_allocated", "total_consumed", "updated_at")
    search_fields = ("user__username", "user__email")


@admin.register(TokenTransaction)
class TokenTransactionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "amount", "transaction_type", "balance_after", "created_by")
    list_filter = ("transaction_type",)
    search_fields = ("user__username", "description", "idempotency_key")
    date_hierarchy = "created_at"


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "message_count", "total_tokens", "updated_at")
    search_fields = ("title", "user__username")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "conversation", "role", "total_tokens", "model", "status")
    list_filter = ("role", "status")


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("name", "tokens", "price", "currency", "duration_days", "is_active", "display_order")
    list_editable = ("is_active", "display_order")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("reference", "user", "plan", "amount", "method", "status", "tokens_credited", "created_at")
    list_filter = ("status", "method", "tokens_credited")
    search_fields = ("reference", "user__username", "provider_txn_id")
    date_hierarchy = "created_at"


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "plan", "status", "starts_at", "ends_at", "tokens_granted")
    list_filter = ("status",)


@admin.register(AIUsageLog)
class AIUsageLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "model", "total_tokens", "tokens_charged", "success", "latency_ms")
    list_filter = ("success", "model")
    date_hierarchy = "created_at"


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "target_label", "ip_address")
    list_filter = ("action",)
    search_fields = ("target_label", "description")
    date_hierarchy = "created_at"


admin.site.register([Notification, Announcement, Referral, TelegramAccount, AppSetting])

admin.site.site_header = "EPC AI Assistant — data admin"
admin.site.site_title = "EPC AI Assistant"
admin.site.index_title = "Raw data"


@admin.register(AIModel)
class AIModelAdmin(admin.ModelAdmin):
    """Raw access to the catalogue. Day to day this is managed at /admin/tokens/."""

    list_display = ("name", "key", "is_active", "is_default", "token_allocation",
                    "tokens_consumed", "request_count", "sort_order")
    list_filter = ("is_active", "is_default", "provider", "supports_vision")
    search_fields = ("key", "name", "description")
    ordering = ("sort_order", "name")
    readonly_fields = ("tokens_consumed", "input_tokens_total", "output_tokens_total",
                       "request_count", "failure_count", "last_used_at", "created_at", "updated_at")


@admin.register(TokenPool)
class TokenPoolAdmin(admin.ModelAdmin):
    """The platform's supply. Managed day to day at /admin/tokens/."""

    list_display = ("balance", "total_added", "total_distributed", "total_reclaimed", "updated_at")
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        # There is exactly one pool; a second row would silently split the supply.
        return not TokenPool.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PoolTransaction)
class PoolTransactionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "amount", "balance_after", "student", "created_by")
    list_filter = ("action",)
    search_fields = ("description", "student__username", "created_by__username")
    date_hierarchy = "created_at"
    readonly_fields = tuple(
        field.name for field in PoolTransaction._meta.fields
    )  # the pool ledger is append-only

    def has_add_permission(self, request):
        return False
