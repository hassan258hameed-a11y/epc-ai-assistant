"""Low-token e-mail warnings: right student, right model, exactly once.

The failure modes worth guarding are all about *who* and *how often*. A warning
that names the wrong model is useless; one that goes to the wrong address is a
privacy incident; one that repeats every hour is why people filter your mail.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from AI.models import (
    AccountStatus,
    AIModel,
    AIProvider,
    AlertLog,
    EmailLog,
    EmailStatus,
    EmailType,
    NotificationPreference,
    Role,
    StaffRole,
    UserAIWallet,
)
from AI.services import settings_service, token_alert_service, wallet_service

User = get_user_model()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class TokenAlertTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.vendor = AIProvider.objects.create(slug="alerts-v", name="Alerts Vendor")
        self.gpt = AIModel.objects.create(
            key="alert-gpt", name="Alert GPT", provider="openai",
            provider_ref=self.vendor, sort_order=1,
        )
        self.claude = AIModel.objects.create(
            key="alert-claude", name="Alert Claude", provider="anthropic",
            provider_ref=self.vendor, sort_order=2,
        )
        self.student = User.objects.create_user(
            username="alert_student", email="student@alerts.test", password="x",
            role=Role.STUDENT, status=AccountStatus.ACTIVE,
        )
        mail.outbox = []

    def wallet(self, model, balance, allocated, user=None):
        return UserAIWallet.objects.create(
            user=user or self.student, ai_model=model, balance=balance,
            allocated_tokens=allocated, used_tokens=allocated - balance,
            lifetime_allocated=allocated, lifetime_used=allocated - balance,
        )

    # -- thresholds --------------------------------------------------------
    def test_a_percentage_means_the_same_on_any_package_size(self):
        """The point of percentages: 5% is 5% on 1,000 tokens and on 200,000."""
        small = self.wallet(self.gpt, 40, 1_000)          # 4%
        big = UserAIWallet.objects.create(
            user=self.student, ai_model=self.claude, balance=8_000,
            allocated_tokens=200_000, used_tokens=192_000,
        )                                                  # 4%
        self.assertEqual(token_alert_service.tier_for(small), 5)
        self.assertEqual(token_alert_service.tier_for(big), 5)

    def test_a_comfortable_wallet_is_not_warned(self):
        self.assertIsNone(token_alert_service.tier_for(self.wallet(self.gpt, 900, 1_000)))

    def test_an_unfunded_wallet_is_not_warned(self):
        """Never allocated is not the same as spent — and is not an emergency."""
        self.assertIsNone(token_alert_service.tier_for(self.wallet(self.gpt, 0, 0)))

    def test_the_most_urgent_crossed_threshold_is_used(self):
        self.assertEqual(token_alert_service.tier_for(self.wallet(self.gpt, 30, 1_000)), 5)

    def test_a_model_may_override_the_platform_warning_points(self):
        """An expensive model can be warned about earlier than a cheap one."""
        self.gpt.warning_thresholds = "80,40"
        self.gpt.save(update_fields=["warning_thresholds"])

        # 60% left: past the platform default of 50, but inside this model's 80.
        wallet = self.wallet(self.gpt, 600, 1_000)
        self.assertEqual(token_alert_service.tier_for(wallet), 80)

        # Another model is unaffected and still uses the default.
        other = self.wallet(self.claude, 600, 1_000)
        self.assertIsNone(token_alert_service.tier_for(other))

    def test_a_malformed_override_falls_back_rather_than_failing(self):
        """One bad character must not stop warnings being sent at all."""
        self.gpt.warning_thresholds = "not, a, number"
        self.gpt.save(update_fields=["warning_thresholds"])
        self.assertEqual(
            token_alert_service.thresholds_for(self.gpt),
            token_alert_service.thresholds(),
        )

    def test_an_override_is_parsed_leniently(self):
        self.gpt.warning_thresholds = " 60 , 30% ,15 "
        self.gpt.save(update_fields=["warning_thresholds"])
        self.assertEqual(token_alert_service.thresholds_for(self.gpt), [60, 30, 15])

    def test_the_sweep_reaches_a_model_that_warns_early(self):
        """A model warning at 80% must be swept even though the default is 50."""
        self.gpt.warning_thresholds = "80"
        self.gpt.save(update_fields=["warning_thresholds"])
        self.wallet(self.gpt, 700, 1_000)          # 70% left
        result = token_alert_service.sweep()
        self.assertEqual(result["sent"], 1)

    # -- delivery ----------------------------------------------------------
    def test_the_warning_names_the_model_that_is_low(self):
        self.wallet(self.claude, 900, 1_000)          # healthy
        low = self.wallet(self.gpt, 60, 1_000)        # 6%

        log = token_alert_service.check_wallet(low)
        self.assertIsNotNone(log)
        self.assertEqual(len(mail.outbox), 1)

        message = mail.outbox[0]
        self.assertEqual(message.to, [self.student.email])
        self.assertIn("Alert GPT", message.subject)
        body = message.body + "".join(part for part, _ in message.alternatives)
        self.assertIn("Alert GPT", body)
        self.assertNotIn("Alert Claude", body)
        self.assertEqual(log.ai_model, self.gpt)
        self.assertEqual(log.balance_at_send, 60)

    def test_a_delivered_warning_is_also_pushed_to_the_students_devices(self):
        from unittest.mock import patch

        low = self.wallet(self.gpt, 60, 1_000)
        with patch("AI.services.push_service.queue") as queue:
            token_alert_service.check_wallet(low)
        queue.assert_called_once()
        args, kwargs = queue.call_args
        self.assertEqual(args[0], self.student)
        self.assertIn("Alert GPT", args[1])
        self.assertEqual(kwargs["category"], "tokens")
        self.assertEqual(kwargs["url"], token_alert_service.PRICING_PATH)

    def test_no_push_when_the_email_was_not_delivered(self):
        from unittest.mock import patch

        low = self.wallet(self.gpt, 60, 1_000)
        with override_settings(EMAIL_BACKEND="AI.tests.test_token_alerts.ExplodingBackend"), \
                patch("AI.services.push_service.queue") as queue:
            token_alert_service.check_wallet(low)
        queue.assert_not_called()

    def test_the_renewal_link_carries_no_identity(self):
        """A forwarded warning must not open the sender's account.

        The button is a plain in-site path. Whoever opens it gets their own
        session, or the login page — never an account chosen by the URL.
        """
        token_alert_service.check_wallet(self.wallet(self.gpt, 20, 1_000))
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn(token_alert_service.PRICING_PATH, html)
        for leak in ("token=", "uid=", "user_id=", "password", "session", "/admin/"):
            self.assertNotIn(leak, html, f"the renewal link must not contain {leak!r}")

    def test_the_same_warning_is_not_sent_twice(self):
        low = self.wallet(self.gpt, 60, 1_000)
        self.assertIsNotNone(token_alert_service.check_wallet(low))
        self.assertIsNone(token_alert_service.check_wallet(low))
        self.assertEqual(len(mail.outbox), 1)

    def test_falling_further_earns_a_new_warning(self):
        """10% and 5% are different news, and both are worth sending."""
        wallet = self.wallet(self.gpt, 240, 1_000)          # 24% -> tier 25
        token_alert_service.check_wallet(wallet)
        wallet.balance = 40                                  # 4% -> tier 5
        wallet.save(update_fields=["balance"])
        token_alert_service.check_wallet(wallet)
        self.assertEqual(len(mail.outbox), 2)

    def test_topping_up_re_arms_the_warning(self):
        wallet = self.wallet(self.gpt, 60, 1_000)
        token_alert_service.check_wallet(wallet)
        self.assertEqual(len(mail.outbox), 1)

        wallet_service.credit(self.student, self.gpt, 5_000, reason="top-up")
        self.assertFalse(
            AlertLog.objects.filter(
                user=self.student, alert_type="low_tokens_email", key__startswith="alert-gpt:"
            ).exists(),
            "a top-up must clear this model's warning claims, or the student is "
            "warned once and never again",
        )

    def test_topping_up_one_model_does_not_silence_another(self):
        gpt = self.wallet(self.gpt, 60, 1_000)
        claude = self.wallet(self.claude, 30, 1_000)
        token_alert_service.check_wallet(gpt)
        token_alert_service.check_wallet(claude)

        wallet_service.credit(self.student, self.gpt, 5_000, reason="top-up")
        self.assertTrue(
            AlertLog.objects.filter(
                user=self.student, alert_type="low_tokens_email",
                key__startswith="alert-claude:",
            ).exists(),
            "refilling one model must not clear another model's warning history",
        )

    # -- who is eligible ---------------------------------------------------
    def test_a_student_who_opted_out_is_not_e_mailed(self):
        prefs, _ = NotificationPreference.objects.get_or_create(user=self.student)
        prefs.low_token_alerts = False
        prefs.save()
        self.assertIsNone(token_alert_service.check_wallet(self.wallet(self.gpt, 20, 1_000)))
        self.assertEqual(len(mail.outbox), 0)

    def test_a_disabled_account_is_not_e_mailed(self):
        self.student.status = AccountStatus.DISABLED
        self.student.save(update_fields=["status"])
        self.assertIsNone(token_alert_service.check_wallet(self.wallet(self.gpt, 20, 1_000)))
        self.assertEqual(len(mail.outbox), 0)

    def test_an_administrator_is_never_e_mailed_about_tokens(self):
        admin = User.objects.create_user(
            username="alert_admin", email="admin@alerts.test", password="x", role=Role.ADMIN,
            staff_role=StaffRole.OWNER,
        )
        wallet = self.wallet(self.gpt, 20, 1_000, user=admin)
        self.assertIsNone(token_alert_service.check_wallet(wallet))
        self.assertEqual(len(mail.outbox), 0)

    def test_the_master_switch_stops_every_warning(self):
        settings_service.set_value("low_token_email_enabled", False)
        self.assertIsNone(token_alert_service.check_wallet(self.wallet(self.gpt, 20, 1_000)))
        self.assertEqual(len(mail.outbox), 0)
        settings_service.set_value("low_token_email_enabled", True)

    # -- the log -----------------------------------------------------------
    def test_a_failed_send_is_logged_and_retried_next_time(self):
        """"No e-mail arrived" and "we never tried" are different problems."""
        wallet = self.wallet(self.gpt, 20, 1_000)
        with override_settings(
            EMAIL_BACKEND="AI.tests.test_token_alerts.ExplodingBackend"
        ):
            log = token_alert_service.check_wallet(wallet)
        self.assertIsNotNone(log)
        self.assertEqual(log.status, EmailStatus.FAILED)
        self.assertIn("smtp is down", log.error)
        # The claim was released, so the next run is allowed to try again.
        self.assertIsNotNone(token_alert_service.check_wallet(wallet))
        self.assertEqual(len(mail.outbox), 1)

    def test_the_log_records_recipient_model_and_trigger(self):
        token_alert_service.check_wallet(self.wallet(self.gpt, 60, 1_000))
        log = EmailLog.objects.get(email_type=EmailType.LOW_TOKENS)
        self.assertEqual(log.recipient, "student@alerts.test")
        self.assertEqual(log.ai_model, self.gpt)
        self.assertIn("%", log.trigger)
        self.assertEqual(log.status, EmailStatus.SENT)

    # -- the sweep ---------------------------------------------------------
    def test_the_sweep_is_safe_to_run_repeatedly(self):
        self.wallet(self.gpt, 60, 1_000)
        self.wallet(self.claude, 30, 1_000)
        first = token_alert_service.sweep()
        second = token_alert_service.sweep()
        self.assertEqual(first["sent"], 2)
        self.assertEqual(second["sent"], 0, "an hourly cron must not mean hourly mail")
        self.assertEqual(len(mail.outbox), 2)

    def test_at_risk_orders_by_proportion_not_by_count(self):
        """The student closest to being cut off comes first, whatever the size."""
        self.wallet(self.gpt, 8_000, 200_000)      # 4%
        self.wallet(self.claude, 300, 1_000)       # 30%
        rows = token_alert_service.at_risk()
        self.assertEqual(rows[0]["ai_model"], self.gpt)
        self.assertEqual(rows[0]["percent"], 4)


class ModelWarningSettingTests(TestCase):
    """The per-model override, saved through the console's own form."""

    def setUp(self):
        settings_service.bootstrap()
        # The catalogue is seeded, so this vendor already exists in most runs.
        vendor, _ = AIProvider.objects.get_or_create(
            slug="openai", defaults={"name": "OpenAI"}
        )
        self.model = AIModel.objects.create(
            key="warn-model", name="Warn Model", provider="openai", provider_ref=vendor
        )
        self.admin = User.objects.create_user(
            username="warn_admin", email="a@warn.test", password="pw",
            role=Role.ADMIN, staff_role=StaffRole.OWNER, status=AccountStatus.ACTIVE, is_staff=True,
        )
        self.client.force_login(self.admin)

    def payload(self, **overrides):
        data = {
            "key": self.model.key, "name": self.model.name, "description": "",
            "provider_ref": str(self.model.provider_ref_id),
            "thinking_mode": "off", "max_output_tokens": "0", "temperature": "0.70",
            "token_allocation": "0", "input_cost_per_1m": "0", "output_cost_per_1m": "0",
            "sort_order": "100", "is_active": "on",
            "warning_thresholds": "50,25,10",
        }
        data.update(overrides)
        return data

    def test_the_console_saves_a_normalised_override(self):
        self.client.post(
            reverse("console:model_save", args=[self.model.pk]),
            self.payload(warning_thresholds=" 60 , 30% ,15 "),
        )
        self.model.refresh_from_db()
        self.assertEqual(self.model.warning_thresholds, "60,30,15")
        self.assertEqual(self.model.warning_points, [60, 30, 15])

    def test_a_typo_is_reported_rather_than_silently_dropped(self):
        self.client.post(
            reverse("console:model_save", args=[self.model.pk]),
            self.payload(warning_thresholds="fifty"),
        )
        self.model.refresh_from_db()
        self.assertEqual(
            self.model.warning_thresholds, "",
            "an unparseable value must not be saved as if it worked",
        )

    def test_blank_means_use_the_platform_default(self):
        self.client.post(
            reverse("console:model_save", args=[self.model.pk]),
            self.payload(warning_thresholds=""),
        )
        self.model.refresh_from_db()
        self.assertEqual(self.model.warning_thresholds, "")
        self.assertIsNone(self.model.warning_points)
        self.assertEqual(
            token_alert_service.thresholds_for(self.model),
            token_alert_service.thresholds(),
        )


class ExplodingBackend:
    """A mail backend that always fails, to exercise the failure path."""

    def __init__(self, *args, **kwargs):
        pass

    def send_messages(self, messages):
        raise OSError("smtp is down")
