"""Model status page, the site-wide chat pause and the daily admin digest.

What matters here: only administrators can change anything, every change is
validated and audited, the student-facing notice is exactly what the service
says it is, the measured figures add up, and the digest goes out at most once
per administrator per Phnom Penh day — the test send only to the person who
asked for it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.messages import get_messages
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from AI.models import (
    AccountStatus,
    AIModel,
    AIUsageLog,
    AlertLog,
    AuditAction,
    AuditLog,
    Payment,
    PaymentStatus,
    Plan,
    Role,
    SupportTicket,
    User,
)
from AI.services import digest_service, model_status_service, settings_service
from AI.tests.helpers import GPT, SONNET, model_for

PASSWORD = "Sup3rSecret!x"
PP = ZoneInfo("Asia/Phnom_Penh")


def _admin(name="status.admin", email="status.admin@test.local"):
    admin = User.objects.create_superuser(username=name, email=email, password=PASSWORD)
    admin.role = Role.ADMIN
    admin.save()
    return admin


def _student(name="status.student"):
    return User.objects.create_user(
        username=name, email=f"{name}@test.local", password=PASSWORD,
        is_verified=True, status=AccountStatus.ACTIVE,
    )


def _log(user, key, *, success=True, latency=100, minutes_ago=1, tokens=0, error="", code=""):
    row = AIUsageLog.objects.create(
        user=user, model=key, model_key=key, success=success, latency_ms=latency,
        tokens_charged=tokens, total_tokens=tokens, error_message=error, error_code=code,
    )
    AIUsageLog.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(minutes=minutes_ago))
    return row


def _messages(response) -> list[str]:
    return [str(m) for m in get_messages(response.wsgi_request)]


class Base(TestCase):
    def setUp(self):
        cache.clear()
        settings_service.bootstrap()
        self.admin = _admin()
        self.student = _student()
        self.gpt = model_for(GPT)
        self.sonnet = model_for(SONNET)
        self.page = reverse("console:model_status")

    def tearDown(self):
        cache.clear()


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
class AccessTests(Base):
    def test_signed_out_visitors_go_to_the_admin_sign_in(self):
        for url in (self.page, reverse("console:model_status_set", args=[self.gpt.pk]),
                    reverse("console:model_status_pause"), reverse("console:model_status_digest")):
            response = self.client.post(url, {"status": "paused"}) if url != self.page else self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/admin/login/", response["Location"], url)
        self.gpt.refresh_from_db()
        self.assertEqual(self.gpt.service_status, AIModel.ServiceStatus.OPERATIONAL)

    def test_students_are_turned_away_and_change_nothing(self):
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(self.page).status_code, 302)
        response = self.client.post(reverse("console:model_status_set", args=[self.gpt.pk]), {"status": "paused"})
        self.assertEqual(response.status_code, 302)
        self.client.post(reverse("console:model_status_pause"), {"action": "pause", "confirm": "yes"})
        self.gpt.refresh_from_db()
        self.assertEqual(self.gpt.service_status, AIModel.ServiceStatus.OPERATIONAL)
        self.assertFalse(model_status_service.chat_pause()[0])

    def test_an_administrator_sees_every_model_and_every_control(self):
        AIModel.objects.filter(pk=self.sonnet.pk).update(is_active=False, best_for="Hard maths")
        self.client.force_login(self.admin)
        response = self.client.get(self.page)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for model in AIModel.objects.all():
            self.assertIn(f'id="model-{model.pk}"', html)
        self.assertIn("Hidden from students", html)
        self.assertIn("Best for Hard maths", html)
        self.assertIn("Pause all chat", html)
        self.assertIn("Send a test digest to me", html)
        self.assertIn(reverse("console:health"), html)
        # Active models are listed before inactive ones.
        rows = response.context["rows"]
        flags = [row["model"].is_active for row in rows]
        self.assertEqual(flags, sorted(flags, reverse=True))

    def test_actions_are_post_only_and_csrf_protected(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("console:model_status_pause")).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.admin)
        response = strict.post(reverse("console:model_status_set", args=[self.gpt.pk]), {"status": "paused"})
        self.assertEqual(response.status_code, 403)
        self.gpt.refresh_from_db()
        self.assertEqual(self.gpt.service_status, AIModel.ServiceStatus.OPERATIONAL)


# ---------------------------------------------------------------------------
# Declared status
# ---------------------------------------------------------------------------
class StatusChangeTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.url = reverse("console:model_status_set", args=[self.gpt.pk])

    def test_marking_a_model_degraded_saves_the_note_and_audits_it(self):
        response = self.client.post(self.url, {"status": "degraded", "note": "  Slow   replies\ntoday "})
        self.assertRedirects(response, self.page + f"#model-{self.gpt.pk}", fetch_redirect_response=False)
        self.gpt.refresh_from_db()
        self.assertEqual(self.gpt.service_status, AIModel.ServiceStatus.DEGRADED)
        self.assertEqual(self.gpt.status_note, "Slow replies today")
        self.assertIsNotNone(self.gpt.status_changed_at)
        entry = AuditLog.objects.filter(action=AuditAction.SETTINGS_UPDATE, target_id=str(self.gpt.pk)).latest("created_at")
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual((entry.old_value, entry.new_value), ("operational", "degraded"))
        self.assertTrue(any("Slow or unreliable" in m for m in _messages(response)))

    def test_an_unknown_status_is_refused(self):
        response = self.client.post(self.url, {"status": "exploded", "note": "x"})
        self.assertTrue(any("Choose" in m for m in _messages(response)))
        self.gpt.refresh_from_db()
        self.assertEqual(self.gpt.service_status, AIModel.ServiceStatus.OPERATIONAL)
        self.assertFalse(AuditLog.objects.filter(target_id=str(self.gpt.pk)).exists())

    def test_a_note_over_200_characters_is_refused_not_truncated(self):
        response = self.client.post(self.url, {"status": "paused", "note": "x" * 201})
        self.assertTrue(any("200 characters" in m for m in _messages(response)))
        self.gpt.refresh_from_db()
        self.assertEqual(self.gpt.service_status, AIModel.ServiceStatus.OPERATIONAL)
        # Exactly 200 is fine.
        self.client.post(self.url, {"status": "paused", "note": "y" * 200})
        self.gpt.refresh_from_db()
        self.assertEqual(self.gpt.status_note, "y" * 200)

    def test_back_to_normal_clears_the_note_and_a_repeat_is_a_no_op(self):
        self.client.post(self.url, {"status": "paused", "note": "Vendor outage"})
        self.client.post(self.url, {"status": "operational", "note": "ignored"})
        self.gpt.refresh_from_db()
        self.assertEqual((self.gpt.service_status, self.gpt.status_note), ("operational", ""))
        audits = AuditLog.objects.filter(target_id=str(self.gpt.pk)).count()
        response = self.client.post(self.url, {"status": "operational"})
        self.assertTrue(any("already" in m for m in _messages(response)))
        self.assertEqual(AuditLog.objects.filter(target_id=str(self.gpt.pk)).count(), audits)

    def test_an_unknown_model_is_a_404(self):
        response = self.client.post(reverse("console:model_status_set", args=[999999]), {"status": "paused"})
        self.assertEqual(response.status_code, 404)

    def test_the_page_previews_exactly_what_students_see(self):
        self.client.post(self.url, {"status": "paused", "note": "Back at 6pm"})
        html = self.client.get(self.page).content.decode()
        notice = model_status_service.notice_for(model_for(GPT))
        self.assertEqual(notice["text"], "Back at 6pm")
        self.assertIn(notice["title"], html)
        self.assertIn("Back at 6pm", html)


# ---------------------------------------------------------------------------
# Pause all chat
# ---------------------------------------------------------------------------
class PauseAllTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.url = reverse("console:model_status_pause")

    def test_pausing_needs_the_confirmation_step(self):
        response = self.client.post(self.url, {"action": "pause", "note": "Upgrading"})
        self.assertTrue(any("Confirm" in m for m in _messages(response)))
        self.assertFalse(model_status_service.chat_pause()[0])

    def test_pause_then_resume(self):
        response = self.client.post(self.url, {"action": "pause", "confirm": "yes", "note": "Back by 9:00 pm"})
        self.assertRedirects(response, self.page + "#chat-pause", fetch_redirect_response=False)
        self.assertEqual(model_status_service.chat_pause(), (True, "Back by 9:00 pm"))
        self.assertTrue(AuditLog.objects.filter(description__startswith="Paused all chat", actor=self.admin).exists())

        html = self.client.get(self.page).content.decode()
        self.assertIn('id="pauseBanner"', html)
        self.assertIn("All chat is paused", html)
        # Every model is blocked, with the maintenance notice.
        for model in AIModel.objects.all():
            blocked = model_status_service.blocks_sending(model)
            self.assertEqual(blocked["level"], "maintenance")
            self.assertEqual(blocked["text"], "Back by 9:00 pm")

        self.client.post(self.url, {"action": "resume"})
        self.assertFalse(model_status_service.chat_pause()[0])
        self.assertTrue(AuditLog.objects.filter(description="Resumed chat").exists())
        self.assertNotIn('id="pauseBanner"', self.client.get(self.page).content.decode())
        self.assertIsNone(model_status_service.blocks_sending(self.gpt))

    def test_the_message_can_be_changed_while_paused_and_is_validated(self):
        self.client.post(self.url, {"action": "pause", "confirm": "yes", "note": ""})
        self.assertEqual(model_status_service.notice_for(None)["text"], model_status_service.MAINTENANCE_TEXT)
        self.client.post(self.url, {"action": "note", "note": "Back soon"})
        self.assertEqual(model_status_service.chat_pause(), (True, "Back soon"))
        response = self.client.post(self.url, {"action": "note", "note": "z" * 201})
        self.assertTrue(any("200 characters" in m for m in _messages(response)))
        self.assertEqual(model_status_service.chat_pause()[1], "Back soon")
        response = self.client.post(self.url, {"action": "explode"})
        self.assertTrue(any("Unknown action" in m for m in _messages(response)))


# ---------------------------------------------------------------------------
# What students are told
# ---------------------------------------------------------------------------
class NoticeTests(Base):
    def test_a_working_model_has_no_notice(self):
        self.assertIsNone(model_status_service.notice_for(self.gpt))
        self.assertIsNone(model_status_service.blocks_sending(self.gpt))
        self.assertIsNone(model_status_service.notice_for(None))

    def test_degraded_warns_but_does_not_block(self):
        model_status_service.set_status(self.gpt, "degraded", "")
        notice = model_status_service.notice_for(self.gpt)
        self.assertEqual(notice["level"], "degraded")
        self.assertIn("slow", notice["title"])
        self.assertTrue(notice["text"])  # the standard wording
        self.assertIsNone(model_status_service.blocks_sending(self.gpt))

    def test_paused_blocks_that_model_only(self):
        model_status_service.set_status(self.gpt, "paused", "Vendor outage")
        blocked = model_status_service.blocks_sending(self.gpt)
        self.assertEqual((blocked["level"], blocked["text"]), ("paused", "Vendor outage"))
        self.assertIsNone(model_status_service.blocks_sending(self.sonnet))

    def test_model_notice_ignores_the_site_wide_pause(self):
        model_status_service.set_status(self.gpt, "degraded", "Slow")
        model_status_service.set_chat_pause(True, "Maintenance")
        self.assertEqual(model_status_service.notice_for(self.gpt)["level"], "maintenance")
        self.assertEqual(model_status_service.model_notice(self.gpt)["text"], "Slow")

    def test_set_status_rejects_unknown_values(self):
        with self.assertRaises(ValueError):
            model_status_service.set_status(self.gpt, "broken", "")


# ---------------------------------------------------------------------------
# Measured health
# ---------------------------------------------------------------------------
class MeasuredTests(Base):
    def test_counts_rates_latency_and_last_error(self):
        for latency in (100, 200, 300, 400, 500, 600, 700, 800):
            _log(self.student, GPT, latency=latency, minutes_ago=5)
        _log(self.student, GPT, success=False, latency=0, minutes_ago=10, error="old failure", code="timeout")
        _log(self.student, GPT, success=False, latency=0, minutes_ago=3, error="Vendor said 529", code="overloaded")
        _log(self.student, GPT, latency=9000, minutes_ago=120)          # outside 15 min and 1 h
        _log(self.student, SONNET, minutes_ago=30)

        short = model_status_service.measured(15)[GPT]
        self.assertEqual((short["requests"], short["failures"]), (10, 2))
        self.assertEqual(short["error_rate"], 20.0)
        self.assertEqual(short["p50_ms"], 500)
        self.assertEqual(short["p95_ms"], 800)
        self.assertEqual(short["last_error"], "Vendor said 529")
        self.assertEqual(short["last_error_code"], "overloaded")
        self.assertIsNotNone(short["last_error_at"])
        self.assertFalse(short["looks_unhealthy"])  # 20% is under the 25% line
        self.assertNotIn(SONNET, model_status_service.measured(15))

        day = model_status_service.measured(24 * 60)[GPT]
        self.assertEqual(day["requests"], 11)
        self.assertEqual(day["p95_ms"], 9000)

    def test_unhealthy_needs_enough_requests_and_a_high_error_rate(self):
        for _ in range(3):
            _log(self.student, GPT, success=False, minutes_ago=2)
        self.assertFalse(model_status_service.measured(15)[GPT]["looks_unhealthy"])  # only 3 requests
        for _ in range(2):
            _log(self.student, GPT, minutes_ago=2)
        self.assertTrue(model_status_service.measured(15)[GPT]["looks_unhealthy"])  # 3/5 = 60%

    def test_the_page_shows_the_hint_but_never_changes_the_status(self):
        for _ in range(6):
            _log(self.student, GPT, success=False, minutes_ago=2, error="upstream 500")
        self.client.force_login(self.admin)
        html = self.client.get(self.page).content.decode()
        self.assertIn("Looks unhealthy", html)
        self.assertIn("upstream 500", html)
        self.gpt.refresh_from_db()
        self.assertEqual(self.gpt.service_status, AIModel.ServiceStatus.OPERATIONAL)


# ---------------------------------------------------------------------------
# Navigation badge
# ---------------------------------------------------------------------------
class NavBadgeTests(Base):
    def test_badge_appears_for_paused_or_degraded_models_and_for_a_chat_pause(self):
        self.client.force_login(self.admin)
        other_page = reverse("console:audit_logs")
        response = self.client.get(other_page)
        self.assertEqual(response.context["model_status_alert"]["count"], 0)
        self.assertNotIn(" slow\">", response.content.decode())

        model_status_service.set_status(self.gpt, "paused", "")
        model_status_service.set_status(self.sonnet, "degraded", "")
        response = self.client.get(other_page)
        alert = response.context["model_status_alert"]
        self.assertEqual((alert["paused"], alert["degraded"], alert["count"], alert["tone"]), (1, 1, 2, "danger"))
        self.assertIn("1 paused, 1 slow", response.content.decode())

        model_status_service.set_chat_pause(True, "")
        response = self.client.get(other_page)
        self.assertTrue(response.context["model_status_alert"]["chat_paused"])
        self.assertIn("All chat is paused.", response.content.decode())

    def test_inactive_models_do_not_count(self):
        model_status_service.set_status(self.gpt, "paused", "")
        AIModel.objects.filter(pk=self.gpt.pk).update(is_active=False)
        self.assertEqual(model_status_service.attention(force=True)["count"], 0)


# ---------------------------------------------------------------------------
# Daily digest
# ---------------------------------------------------------------------------
class DigestTests(Base):
    def setUp(self):
        super().setUp()
        self.second_admin = _admin("status.admin2", "second.admin@test.local")
        plan = Plan.objects.filter(is_active=True).first()
        now = timezone.now()
        Payment.objects.create(user=self.student, plan=plan, amount=Decimal("12.50"), currency="USD",
                               method="aba_payway", status=PaymentStatus.PAID, paid_at=now, reference="DG-1")
        Payment.objects.create(user=self.student, plan=plan, amount=Decimal("7.50"), currency="USD",
                               method="binance", status=PaymentStatus.PAID, paid_at=now, reference="DG-2")
        Payment.objects.create(user=self.student, plan=plan, amount=Decimal("99.00"), currency="USD",
                               method="binance", status=PaymentStatus.PAID,
                               paid_at=now - timedelta(days=3), reference="DG-OLD")
        Payment.objects.create(user=self.student, plan=plan, amount=Decimal("5.00"), currency="USD",
                               method="binance", status=PaymentStatus.PENDING, reference="DG-3")
        SupportTicket.objects.create(user=self.student, full_name="S", email="s@x.y", category="other",
                                     subject="Help", description="x" * 30)
        for _ in range(3):
            _log(self.student, GPT, tokens=1000, minutes_ago=30)
        _log(self.student, GPT, success=False, minutes_ago=30)
        model_status_service.set_status(self.sonnet, "paused", "Vendor outage")

    def test_the_digest_reports_the_last_24_hours(self):
        digest = digest_service.build()
        self.assertEqual(digest["signups"], User.objects.filter(role=Role.STUDENT, is_superuser=False).count())
        self.assertEqual(digest["paid_count"], 2)
        self.assertEqual(digest["revenue_usd"], Decimal("20.00"))
        self.assertEqual(digest["pending_payments"], 1)
        self.assertEqual(digest["open_tickets"], 1)
        gpt = next(row for row in digest["usage"] if row["key"] == GPT)
        self.assertEqual((gpt["requests"], gpt["failures"], gpt["tokens"], gpt["error_rate"]), (4, 1, 3000, 25.0))
        self.assertEqual([row["name"] for row in digest["incidents"]], [self.sonnet.name])

        text = digest_service.as_text(digest)
        self.assertIn("US$20.00", text)
        self.assertIn("Paid orders: 2", text)
        self.assertIn("Open support tickets: 1", text)
        self.assertIn(f"{self.sonnet.name}: Paused — “Vendor outage”", text)
        self.assertIn(f"{self.gpt.name}: 3,000 tokens", text)
        telegram = digest_service.as_telegram(digest)
        self.assertIn("<b>Needs attention</b>", telegram)

    def test_send_if_due_is_off_by_default_and_waits_for_the_morning(self):
        self.assertEqual(digest_service.send_if_due()["skipped"], "disabled")
        settings_service.set_value("admin_digest_enabled", True)
        early = datetime(2026, 9, 27, 6, 30, tzinfo=PP)
        self.assertEqual(digest_service.send_if_due(early)["skipped"], "too early")
        self.assertEqual(len(mail.outbox), 0)

    @mock.patch("AI.services.digest_service.telegram_service.notify_user", return_value=True)
    def test_once_per_admin_per_phnom_penh_day(self, notify):
        settings_service.set_value("admin_digest_enabled", True)
        morning = datetime(2026, 9, 27, 8, 5, tzinfo=PP)
        admins = set(digest_service.recipients())
        self.assertEqual(digest_service.send_if_due(morning)["sent"], len(admins))
        self.assertEqual({m.to[0] for m in mail.outbox}, {a.email for a in admins if a.email})
        self.assertEqual(notify.call_count, len(admins))
        self.assertEqual(
            AlertLog.objects.filter(alert_type="admin_digest", key="2026-09-27").count(), len(admins))

        # Later the same Phnom Penh day: nothing more.
        late = datetime(2026, 9, 27, 23, 50, tzinfo=PP)
        self.assertEqual(digest_service.send_if_due(late)["sent"], 0)
        self.assertEqual(len(mail.outbox), len([a for a in admins if a.email]))

        # Next morning: once more.
        self.assertEqual(digest_service.send_if_due(morning + timedelta(days=1))["sent"], len(admins))

    def test_a_disabled_admin_gets_nothing(self):
        settings_service.set_value("admin_digest_enabled", True)
        User.objects.filter(pk=self.second_admin.pk).update(status=AccountStatus.DISABLED)
        self.assertNotIn(self.second_admin, list(digest_service.recipients()))

    def test_send_if_due_never_raises(self):
        settings_service.set_value("admin_digest_enabled", True)
        with mock.patch("AI.services.digest_service.build", side_effect=RuntimeError("boom")):
            outcome = digest_service.send_if_due(datetime(2026, 9, 27, 9, 0, tzinfo=PP))
        self.assertIsInstance(outcome, dict)
        with mock.patch("AI.services.digest_service.recipients", side_effect=RuntimeError("db down")):
            self.assertEqual(digest_service.send_if_due(datetime(2026, 9, 28, 9, 0, tzinfo=PP))["skipped"], "error")

    @mock.patch("AI.services.digest_service.telegram_service.notify_user", return_value=False)
    def test_the_test_send_reaches_only_the_requester(self, notify):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("console:model_status_digest"), {"action": "test"})
        self.assertRedirects(response, self.page + "#digest", fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.admin.email])
        self.assertIn("[Test]", mail.outbox[0].subject)
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(notify.call_args.args[0], self.admin)
        # A test is not the day's digest: nothing is claimed.
        self.assertFalse(AlertLog.objects.filter(alert_type="admin_digest").exists())
        self.assertTrue(any("Test digest sent" in m for m in _messages(response)))

    def test_the_digest_can_be_switched_on_and_off_from_the_page(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("console:model_status_digest"), {"action": "enable"})
        self.assertTrue(digest_service.is_enabled())
        self.client.post(reverse("console:model_status_digest"), {"action": "disable"})
        self.assertFalse(digest_service.is_enabled())
        self.assertEqual(AuditLog.objects.filter(description__contains="daily admin digest").count(), 2)

    def test_the_setting_is_registered_off_in_general(self):
        default, value_type, group = settings_service.DEFAULTS["admin_digest_enabled"][:3]
        self.assertEqual((default, value_type, group), (False, "bool", "general"))



class ReminderCommandTests(Base):
    def test_the_hourly_command_asks_for_the_digest(self):
        with mock.patch("AI.services.digest_service.send_if_due",
                        return_value={"sent": 2, "skipped": "", "day": None}) as due:
            call_command("send_reminders", "--skip-cleanup", stdout=mock.MagicMock())
        due.assert_called_once()
