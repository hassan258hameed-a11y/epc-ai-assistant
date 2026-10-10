"""Rate limiting, alerts, expiry reminders, settings and the audit trail."""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from AI.models import (
    AccountStatus,
    AlertLog,
    Announcement,
    AuditAction,
    AuditLog,
    Notification,
    NotificationCategory,
    Plan,
    Role,
    Subscription,
    User,
)
from AI.services import (
    audit_service,
    model_pool_service,
    notification_service,
    pool_service,
    settings_service,
    token_service,
    wallet_service,
)
from AI.tests.helpers import GPT, SONNET, fund, fund_pool, model_for
from AI.utils import ratelimit
from AI.utils.ratelimit import RateLimited

PASSWORD = "Sup3rSecret!x"


def make_user(username: str, **extra) -> User:
    defaults = {
        "email": f"{username}@test.local",
        "is_verified": True,
        "status": AccountStatus.ACTIVE,
        "role": Role.STUDENT,
    }
    defaults.update(extra)
    return User.objects.create_user(username=username, password=PASSWORD, **defaults)


class RateLimitTests(TestCase):
    def test_limit_is_enforced_then_released(self):
        for index in range(3):
            status = ratelimit.check("login", "1.2.3.4", limit=3, window=60)
            self.assertTrue(status.allowed, f"hit {index} should be allowed")

        blocked = ratelimit.check("login", "1.2.3.4", limit=3, window=60)
        self.assertFalse(blocked.allowed)
        self.assertGreater(blocked.retry_after, 0)

    def test_enforce_raises_with_a_friendly_message(self):
        ratelimit.check("otp_send", "user:1", limit=1, window=60)
        with self.assertRaises(RateLimited) as ctx:
            ratelimit.enforce("otp_send", "user:1", limit=1, window=60)
        self.assertIn("Too many attempts", ctx.exception.friendly_message)

    def test_identities_are_tracked_separately(self):
        ratelimit.check("login", "user-a", limit=1, window=60)
        self.assertFalse(ratelimit.check("login", "user-a", limit=1, window=60).allowed)
        self.assertTrue(ratelimit.check("login", "user-b", limit=1, window=60).allowed)

    def test_reset_clears_the_window(self):
        ratelimit.check("login", "resettable", limit=1, window=60)
        self.assertFalse(ratelimit.check("login", "resettable", limit=1, window=60).allowed)

        ratelimit.reset("login", "resettable")
        self.assertTrue(ratelimit.check("login", "resettable", limit=1, window=60).allowed)


class LowBalanceAlertTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        settings_service.set_value("low_token_threshold", 5_000)
        settings_service.set_value("low_token_threshold_critical", 1_000)
        self.user = make_user("alerted")

    def test_healthy_balance_produces_no_alert(self):
        self.assertIsNone(notification_service.check_low_balance(self.user, 50_000))
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 0)

    def test_low_balance_alerts_once_only(self):
        first = notification_service.check_low_balance(self.user, 4_000)
        second = notification_service.check_low_balance(self.user, 3_500)

        self.assertIsNotNone(first)
        self.assertIsNone(second, "The same threshold must not alert twice")
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 1)

    def test_crossing_into_critical_raises_a_second_distinct_alert(self):
        notification_service.check_low_balance(self.user, 4_000)
        critical = notification_service.check_low_balance(self.user, 500)
        empty = notification_service.check_low_balance(self.user, 0)

        self.assertIsNotNone(critical)
        self.assertIsNotNone(empty)
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 3)

    def test_topping_up_re_arms_the_alerts(self):
        notification_service.check_low_balance(self.user, 4_000)
        notification_service.reset_low_balance_alerts(self.user)

        self.assertFalse(AlertLog.objects.filter(user=self.user, alert_type="low_tokens").exists())
        self.assertIsNotNone(notification_service.check_low_balance(self.user, 4_000))

    def test_admins_are_never_alerted(self):
        admin = User.objects.create_superuser(username="chief", email="chief@test.local", password=PASSWORD)
        self.assertIsNone(notification_service.check_low_balance(admin, 0))


class ExpiryReminderTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        settings_service.set_value("expiry_reminder_days", [7, 3, 1])
        self.user = make_user("expiring")
        self.plan = Plan.objects.create(name="Term", tokens=10_000, price=5, duration_days=30)

    def _subscription(self, days: int) -> Subscription:
        return Subscription.objects.create(
            user=self.user, plan=self.plan, tokens_granted=10_000,
            starts_at=timezone.now() - timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=days, hours=1),
        )

    def test_reminder_fires_on_a_configured_day(self):
        self._subscription(3)
        result = notification_service.send_expiry_reminders()

        self.assertEqual(result["sent"], 1)
        note = Notification.objects.get(user=self.user)
        self.assertEqual(note.category, NotificationCategory.EXPIRY)
        self.assertIn("3 days", note.title)

    def test_reminder_is_not_repeated(self):
        self._subscription(3)
        notification_service.send_expiry_reminders()
        second = notification_service.send_expiry_reminders()

        self.assertEqual(second["sent"], 0)
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 1)

    def test_no_reminder_outside_the_configured_days(self):
        self._subscription(5)
        self.assertEqual(notification_service.send_expiry_reminders()["sent"], 0)

    def test_finished_subscriptions_are_expired_and_announced(self):
        subscription = Subscription.objects.create(
            user=self.user, plan=self.plan, tokens_granted=10_000,
            starts_at=timezone.now() - timedelta(days=40),
            ends_at=timezone.now() - timedelta(hours=1),
        )
        count = notification_service.expire_subscriptions()

        subscription.refresh_from_db()
        self.assertEqual(count, 1)
        self.assertEqual(subscription.status, Subscription.Status.EXPIRED)
        self.assertTrue(Notification.objects.filter(user=self.user, category=NotificationCategory.EXPIRY).exists())

    def test_expiring_twice_notifies_once(self):
        Subscription.objects.create(
            user=self.user, plan=self.plan, tokens_granted=10_000,
            starts_at=timezone.now() - timedelta(days=40), ends_at=timezone.now() - timedelta(hours=1),
        )
        notification_service.expire_subscriptions()
        notification_service.expire_subscriptions()
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 1)


class AnnouncementTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.admin = User.objects.create_superuser(username="herald", email="herald@test.local", password=PASSWORD)
        self.active = make_user("listener")
        self.disabled = make_user("ignored", status=AccountStatus.DISABLED, is_active=False)

    def test_announcement_reaches_active_students_only(self):
        announcement = Announcement.objects.create(
            title="Scheduled maintenance", body="We will be offline briefly on Sunday.", created_by=self.admin
        )
        count = notification_service.publish_announcement(announcement)

        self.assertEqual(count, 1)
        self.assertTrue(Notification.objects.filter(user=self.active).exists())
        self.assertFalse(Notification.objects.filter(user=self.disabled).exists())
        self.assertFalse(Notification.objects.filter(user=self.admin).exists())

        announcement.refresh_from_db()
        self.assertTrue(announcement.is_published)
        self.assertEqual(announcement.recipients, 1)


class SettingsTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()

    def test_defaults_are_available_before_anything_is_saved(self):
        self.assertEqual(settings_service.get("free_trial_tokens"), 3000)
        self.assertTrue(settings_service.get("referral_enabled"))

    def test_the_low_balance_warning_fires_while_a_message_is_still_affordable(self):
        """The warning threshold has to sit at or above the per-request floor.

        Below it, the first thing a student would learn about running low is a
        refused message — the warning would never get a chance to fire.
        """
        self.assertGreaterEqual(
            settings_service.get("low_token_threshold"), settings.AI_MIN_REQUEST_TOKENS
        )

    def test_values_persist_and_are_typed(self):
        settings_service.set_value("free_trial_tokens", 42_000)
        self.assertEqual(settings_service.get("free_trial_tokens"), 42_000)
        self.assertIsInstance(settings_service.get("free_trial_tokens"), int)

        settings_service.set_value("referral_enabled", False)
        self.assertIs(settings_service.get("referral_enabled"), False)

        settings_service.set_value("expiry_reminder_days", [10, 2])
        self.assertEqual(settings_service.get("expiry_reminder_days"), [10, 2])

    def test_grouped_output_covers_every_default(self):
        groups = settings_service.grouped()
        keys = {item["key"] for items in groups.values() for item in items}
        self.assertEqual(keys, set(settings_service.DEFAULTS.keys()))

    def test_admin_can_change_settings_through_the_console(self):
        admin = User.objects.create_superuser(username="tuner", email="tuner@test.local", password=PASSWORD)
        self.client.force_login(admin)

        payload = {"setting_free_trial_tokens": "33000", "setting_low_token_threshold": "2500"}
        response = self.client.post("/admin/settings/", payload)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(settings_service.get("free_trial_tokens"), 33_000)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.SETTINGS_UPDATE).exists())

    def test_students_cannot_change_settings(self):
        student = make_user("meddler")
        self.client.force_login(student)
        self.client.post("/admin/settings/", {"setting_free_trial_tokens": "999999"})
        self.assertEqual(settings_service.get("free_trial_tokens"), 3000)


class AuditTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.admin = User.objects.create_superuser(username="scribe", email="scribe@test.local", password=PASSWORD)
        self.student = make_user("subject")

    def test_record_captures_actor_target_and_values(self):
        entry = audit_service.record(
            AuditAction.TOKENS_ADD, actor=self.admin, target=self.student,
            description="Manual grant", old_value="0", new_value="5000",
        )
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.target_type, "User")
        self.assertEqual(entry.target_id, str(self.student.pk))
        self.assertEqual(entry.new_value, "5000")

    def _distribute(self, action: str, amount: int, reason: str, model=None):
        model = model or model_for(GPT)
        return self.client.post(
            "/admin/ai-tokens/distribute/apply/",
            {
                "user_id": self.student.pk,
                "model_id": model.pk,
                "action": action,
                "amount": amount,
                "reason": reason,
            },
        )

    def test_token_grant_through_the_console_is_audited(self):
        model = fund_pool(GPT, 100_000)   # the admin gives from that model's pool
        self.client.force_login(self.admin)
        self._distribute("add", 7_500, "Support credit")

        self.assertEqual(wallet_service.balance_of(self.student, model), 7_500)
        entry = AuditLog.objects.get(action=AuditAction.TOKENS_ADD)
        self.assertEqual(entry.actor, self.admin)
        self.assertIn("Support credit", entry.description)
        # The audit trail names the model, because the amount alone is ambiguous
        # once every model has its own balance.
        self.assertEqual(entry.new_value, f"{GPT}:7500")
        self.assertEqual(entry.old_value, f"{GPT}:0")

    def test_token_removal_through_the_console_is_audited(self):
        model = fund(self.student, 10_000, GPT)
        self.client.force_login(self.admin)
        self._distribute("remove", 4_000, "Correction")

        self.assertEqual(wallet_service.balance_of(self.student, model), 6_000)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.TOKENS_REMOVE).exists())

    def test_assigning_an_exact_balance_is_audited_and_drawn_from_the_pool(self):
        """The "assign 50,000 Sonnet tokens to Student A" case."""
        model = fund(self.student, 3_000, SONNET)
        before = model_pool_service.balance(model)

        self.client.force_login(self.admin)
        self._distribute("set", 50_000, "Term 2", model=model)

        wallet = wallet_service.get_wallet(self.student, model)
        self.assertEqual(wallet.balance, 50_000)
        self.assertEqual(wallet.allocated_tokens, 50_000)
        self.assertEqual(wallet.used_tokens, 0)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.TOKENS_ADD).exists())
        # The 47,000 shortfall came out of that model's pool, not out of nowhere.
        self.assertEqual(model_pool_service.balance(model), before - 47_000)
        # And no other model was touched.
        self.assertEqual(wallet_service.balance_of(self.student, model_for(GPT)), 0)

    def test_a_failed_login_is_recorded(self):
        self.client.post("/login/", {"identifier": "subject", "password": "wrong-password"})
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.LOGIN_FAILED).exists())

    def test_audit_failure_never_breaks_the_caller(self):
        # A target with no primary key must not raise.
        entry = audit_service.record(AuditAction.LOGIN, actor=User(), description="unsaved actor")
        self.assertIsNotNone(entry)
        self.assertIsNone(entry.actor)
