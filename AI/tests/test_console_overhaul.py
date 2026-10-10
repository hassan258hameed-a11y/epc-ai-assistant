"""The admin console overhaul: bugs found by two audits, and the features added
alongside them.

Grouped the way the audits were: things that returned a server error or lied to
the administrator, money and token movements that were incomplete, filters that
quietly stopped filtering, and the new tools on existing pages.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core import mail
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from AI.models import (
    AccountStatus,
    AIModel,
    AIUsageLog,
    Announcement,
    AnnouncementAudience,
    AuditAction,
    AuditLog,
    EmailLog,
    EmailStatus,
    EmailType,
    Notification,
    PackageAllocation,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Plan,
    PlanModelGrant,
    PromoCode,
    Role,
    Subscription,
    SupportTicket,
    TicketPriority,
    TicketStatus,
    User,
    UserAIWallet,
)
from AI.services import (
    account_service,
    analytics_service,
    distribution_service,
    model_pool_service,
    model_scope,
    notification_service,
    payment_service,
    settings_service,
    support_service,
    wallet_service,
)
from AI.tests.helpers import GPT, HAIKU, SONNET, balance, fund, fund_pool, model_for
from AI.utils.pagination import clean_date, clean_id, paginate

PASSWORD = "Sup3rSecret!x"
PHNOM_PENH = ZoneInfo("Asia/Phnom_Penh")


def make_student(name, **extra):
    return User.objects.create_user(
        username=name, email=f"{name}@test.local", password=PASSWORD,
        is_verified=True, status=AccountStatus.ACTIVE, **extra,
    )


class ConsoleCase(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.admin = User.objects.create_superuser(
            username="overhaul.admin", email="overhaul.admin@test.local", password=PASSWORD,
            full_name="Overhaul Admin",
        )
        self.student = make_student("overhaul.student", full_name="Sokha Chan")
        self.login(self.admin)

    def login(self, user):
        self.client.force_login(user)
        # The single-session middleware only honours the account's current session.
        User.objects.filter(pk=user.pk).update(active_session_key=self.client.session.session_key)

    def messages_of(self, response) -> list[str]:
        return [str(m) for m in response.context["messages"]] if response.context else []


# ---------------------------------------------------------------------------
# Pages that must render, and bad input that must never become a 500
# ---------------------------------------------------------------------------
class PagesRenderTests(ConsoleCase):
    PAGES = [
        "dashboard", "users", "accounts", "ai_tokens", "ai_distribute", "tokens", "token_history",
        "usage_limits", "quality", "prompt_library", "low_tokens", "expiring_tokens", "emails",
        "model_costs", "usage", "plans", "promos", "payments", "gateways", "conversations",
        "subscriptions", "tickets", "canned_replies", "referrals", "referral_campaigns",
        "notifications", "telegram", "audit_logs", "health", "exports", "settings", "reports",
    ]

    def test_every_console_page_renders(self):
        for name in self.PAGES:
            with self.subTest(page=name):
                response = self.client.get(reverse(f"console:{name}"))
                self.assertEqual(response.status_code, 200, name)

    def test_the_student_page_renders_for_a_student_and_for_yourself(self):
        fund(self.student, 5_000)
        for pk in (self.student.pk, self.admin.pk):
            with self.subTest(pk=pk):
                self.assertEqual(self.client.get(reverse("console:user_detail", args=[pk])).status_code, 200)

    def test_bad_query_values_are_ignored_not_a_server_error(self):
        huge = "9" * 30
        cases = [
            ("console:audit_logs", {"from": "abc", "to": "2026-99-99"}),
            ("console:ai_tokens", {"since": "abc", "until": "nope", "range": "x", "tokens": "zzz"}),
            ("console:conversations", {"student": huge}),
            ("console:conversations", {"student": "-4"}),
            ("console:subscriptions", {"plan": huge}),
            ("console:payments", {"from": "abc", "to": "31/12/2026", "status": "bogus", "method": "?"}),
            ("console:users", {"plan": huge, "days": "abc", "page": "abc"}),
            ("console:tickets", {"days": "abc", "assignee": huge}),
            ("console:emails", {"page": "-3"}),
        ]
        for name, params in cases:
            with self.subTest(page=name, params=params):
                self.assertEqual(self.client.get(reverse(name), params).status_code, 200)

    def test_bad_ids_in_posts_are_refused_politely(self):
        posts = [
            (reverse("console:ai_distribute_bulk"), {"model_id": "abc", "amount": "5"}),
            (reverse("console:ai_distribute_bulk"), {"model_id": "9" * 30, "amount": "5"}),
            (reverse("console:student_grant_plan", args=[self.student.pk]), {"plan_id": "abc"}),
            (reverse("console:student_grant_plan", args=[self.student.pk]), {"plan_id": "9" * 30}),
            (reverse("console:users_bulk"), {"action": "tokens", "ids": [self.student.pk, "9" * 30],
                                             "confirm": "1", "model": "abc", "amount": "5"}),
            (reverse("console:tickets_bulk"), {"action": "close", "ids": ["abc", "9" * 30]}),
        ]
        for url, data in posts:
            with self.subTest(url=url, data=data):
                response = self.client.post(url, data)
                self.assertIn(response.status_code, (200, 302))


class PaginationTests(TestCase):
    def page_for(self, value):
        request = RequestFactory().get("/", {"page": value} if value is not None else {})
        return paginate(request, list(range(95)), per_page=10)

    def test_zero_negative_and_junk_mean_the_first_page(self):
        for value in ("0", "-1", "-99", "abc", "", None, "1.5"):
            with self.subTest(value=value):
                self.assertEqual(self.page_for(value).number, 1)

    def test_past_the_end_means_the_last_page(self):
        self.assertEqual(self.page_for("500").number, 10)
        self.assertEqual(self.page_for("9" * 40).number, 10)

    def test_query_helpers(self):
        self.assertIsNone(clean_id("9" * 30))
        self.assertIsNone(clean_id("0"))
        self.assertEqual(clean_id(" 42 "), 42)
        self.assertIsNone(clean_date("2026-02-30"))
        self.assertEqual(str(clean_date("2026-02-03")), "2026-02-03")


class SharedPagerTests(ConsoleCase):
    def test_the_payments_pager_has_no_none_and_has_previous_and_next(self):
        plan = Plan.objects.filter(is_active=True).first()
        for index in range(26 * 9):
            Payment.objects.create(user=self.student, plan=plan, amount=Decimal("1.00"),
                                   method=PaymentMethod.ABA_PAYWAY, reference=f"PG-{index:04d}")
        body = self.client.get(reverse("console:payments"), {"page": 5}).content.decode()
        self.assertNotIn(">None<", body)
        self.assertIn('aria-label="Previous page"', body)
        self.assertIn('aria-label="Next page"', body)
        self.assertIn("…", body)


# ---------------------------------------------------------------------------
# Resetting your own password
# ---------------------------------------------------------------------------
class SelfResetTests(ConsoleCase):
    def test_resetting_your_own_password_is_refused_and_keeps_you_signed_in(self):
        before = User.objects.get(pk=self.admin.pk).password
        response = self.client.post(reverse("console:user_reset_password", args=[self.admin.pk]), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.get(pk=self.admin.pk).password, before)
        self.assertTrue(any("cannot reset your own password" in m for m in self.messages_of(response)))
        # Still signed in: the console keeps answering.
        self.assertEqual(self.client.get(reverse("console:dashboard")).status_code, 200)

    def test_your_own_page_does_not_offer_the_button(self):
        own = self.client.get(reverse("console:user_detail", args=[self.admin.pk])).content.decode()
        other = self.client.get(reverse("console:user_detail", args=[self.student.pk])).content.decode()
        target = reverse("console:user_reset_password", args=[self.admin.pk])
        self.assertNotIn(target, own)
        self.assertIn(reverse("console:user_reset_password", args=[self.student.pk]), other)

    def test_resetting_a_student_still_works(self):
        response = self.client.post(reverse("console:user_reset_password", args=[self.student.pk]), follow=True)
        self.assertTrue(any("Temporary password" in m for m in self.messages_of(response)))


# ---------------------------------------------------------------------------
# Payments: truthful confirm / reject, filters, bundles
# ---------------------------------------------------------------------------
class PaymentActionTruthTests(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.plan = Plan.objects.filter(is_active=True, ai_model__isnull=False).first()
        fund_pool(self.plan.ai_model.key)
        self.payment = payment_service.create_payment(self.student, self.plan, PaymentMethod.ABA_PAYWAY)

    def act(self, action):
        return self.client.post(
            reverse("console:payment_action", args=[self.payment.reference]), {"action": action}, follow=True,
        )

    def test_confirming_twice_credits_once_and_says_so(self):
        self.act("confirm")
        held = balance(self.student, self.plan.ai_model.key)
        audits = AuditLog.objects.filter(action=AuditAction.PAYMENT_CONFIRM).count()

        response = self.act("confirm")

        self.assertEqual(balance(self.student, self.plan.ai_model.key), held)
        self.assertEqual(AuditLog.objects.filter(action=AuditAction.PAYMENT_CONFIRM).count(), audits)
        text = " ".join(self.messages_of(response))
        self.assertIn("already confirmed", text)
        self.assertNotIn("tokens credited", text)

    def test_rejecting_a_paid_payment_leaves_it_paid_and_says_so(self):
        self.act("confirm")
        response = self.act("reject")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.PAID)
        text = " ".join(self.messages_of(response))
        self.assertIn("was not declined", text)
        self.assertNotIn("marked as failed", text)
        self.assertFalse(AuditLog.objects.filter(action=AuditAction.PAYMENT_REJECT).exists())

    def test_rejecting_a_pending_payment_still_works(self):
        response = self.act("reject")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.FAILED)
        self.assertTrue(any("marked as failed" in m for m in self.messages_of(response)))


class PaymentFilterTests(ConsoleCase):
    def setUp(self):
        super().setUp()
        plan = Plan.objects.filter(is_active=True).first()
        self.rows = {}
        for status in PaymentStatus.values:
            self.rows[status] = Payment.objects.create(
                user=self.student, plan=plan, amount=Decimal("2.00"), method=PaymentMethod.ABA_PAYWAY,
                status=status, reference=f"PF-{status}",
            )

    def refs(self, **params) -> set[str]:
        response = self.client.get(reverse("console:payments"), params)
        return {p.reference for p in response.context["page"].object_list}

    def test_each_closed_status_can_be_listed(self):
        for status in ("failed", "cancelled", "refunded", "expired"):
            with self.subTest(status=status):
                self.assertEqual(self.refs(status=status), {f"PF-{status}"})
        self.assertEqual(
            self.refs(status="declined"), {"PF-failed", "PF-cancelled", "PF-refunded", "PF-expired"},
        )

    def test_the_date_range_is_the_local_calendar_day(self):
        old = self.rows["paid"]
        Payment.objects.filter(pk=old.pk).update(
            created_at=datetime(2026, 3, 1, 18, 30, tzinfo=ZoneInfo("UTC"))   # 2 March, 01:30 in Phnom Penh
        )
        self.assertEqual(self.refs(**{"from": "2026-03-02", "to": "2026-03-02"}), {"PF-paid"})
        self.assertEqual(self.refs(**{"from": "2026-03-01", "to": "2026-03-01"}), set())

    def test_the_page_offers_every_closed_status(self):
        body = self.client.get(reverse("console:payments")).content.decode()
        for label in ("Failed", "Cancelled", "Refunded", "Expired"):
            self.assertIn(f">{label}</option>", body)


class BundleTests(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.main, self.extra = model_for(GPT), model_for(SONNET)
        self.plan = Plan.objects.create(
            name="Duo", slug="duo-test", tokens=60_000, price=Decimal("9.00"), ai_model=self.main,
            duration_days=30,
        )
        PlanModelGrant.objects.create(plan=self.plan, ai_model=self.extra, tokens=25_000)

    def test_granting_a_bundle_credits_every_model_and_says_what(self):
        before_main, before_extra = balance(self.student, GPT), balance(self.student, SONNET)
        self.client.post(reverse("console:student_grant_plan", args=[self.student.pk]), {"plan_id": self.plan.pk})
        self.assertEqual(balance(self.student, GPT) - before_main, 60_000)
        self.assertEqual(balance(self.student, SONNET) - before_extra, 25_000)
        note = Notification.objects.filter(user=self.student, title="Duo plan added").get()
        self.assertIn(self.main.name, note.body)
        self.assertIn(self.extra.name, note.body)

    def test_a_plan_with_no_tokens_does_not_claim_any(self):
        empty = Plan.objects.create(name="Access", slug="access-test", tokens=0, price=Decimal("1.00"),
                                    ai_model=self.main)
        self.client.post(reverse("console:student_grant_plan", args=[self.student.pk]), {"plan_id": empty.pk})
        note = Notification.objects.filter(user=self.student, title="Access plan added").get()
        self.assertNotIn("tokens", note.body)

    def test_refunding_a_bundle_takes_back_every_model(self):
        fund_pool(GPT)
        payment = payment_service.create_payment(self.student, self.plan, PaymentMethod.ABA_PAYWAY)
        payment_service.settle(payment, source="admin")
        self.assertEqual(balance(self.student, SONNET), 25_000 + 0)
        before_main = balance(self.student, GPT)

        refunded = payment_service.refund(payment, actor=self.admin, note="test")

        self.assertEqual(balance(self.student, SONNET), 0)
        self.assertEqual(before_main - balance(self.student, GPT), 60_000)
        self.assertEqual(refunded.tokens_removed, 85_000)
        # Refunding again moves nothing.
        with self.assertRaises(Exception):
            payment_service.refund(payment, actor=self.admin)


# ---------------------------------------------------------------------------
# Time zones: what a student reads is in the site's own calendar
# ---------------------------------------------------------------------------
@override_settings(TIME_ZONE="Asia/Phnom_Penh")
class LocalDateTests(ConsoleCase):
    def test_extending_from_expiring_tells_the_student_the_local_date(self):
        plan = Plan.objects.filter(is_active=True).first()
        # 20:00 UTC on 1 Jan is 03:00 on 2 Jan in Phnom Penh.
        ends = datetime(2031, 1, 1, 20, 0, tzinfo=ZoneInfo("UTC"))
        sub = Subscription.objects.create(user=self.student, plan=plan, ends_at=ends - timedelta(days=3))
        self.client.post(reverse("console:expiring_action", args=[sub.pk]), {"action": "extend", "days": 3})
        note = Notification.objects.filter(user=self.student).latest("id")
        self.assertIn("02 January 2031", note.body)

    def test_the_selected_students_csv_uses_the_local_join_date(self):
        User.objects.filter(pk=self.student.pk).update(
            date_joined=datetime(2026, 5, 4, 19, 0, tzinfo=ZoneInfo("UTC"))  # 5 May, 02:00 local
        )
        response = self.client.post(reverse("console:users_bulk"), {"action": "export", "ids": [self.student.pk]})
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(rows[1][6], "2026-05-05")
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.DATA_EXPORT, target_label="Students").exists())

    def test_chart_series_start_at_local_midnight(self):
        today = timezone.localdate()
        first = today - timedelta(days=6)
        just_after_midnight = timezone.make_aware(datetime.combine(first, datetime.min.time())) + timedelta(minutes=5)
        log = AIUsageLog.objects.create(user=self.student, model="m", model_key=GPT, total_tokens=77)
        AIUsageLog.objects.filter(pk=log.pk).update(created_at=just_after_midnight)
        series = analytics_service.usage_series(7)
        self.assertEqual(series["tokens"][0], 77)


# ---------------------------------------------------------------------------
# Filters keep the valid values when one is bad
# ---------------------------------------------------------------------------
class FilterIsolationTests(ConsoleCase):
    def test_a_stale_plan_does_not_switch_off_the_status_filter(self):
        disabled = make_student("gone.away")
        User.objects.filter(pk=disabled.pk).update(status=AccountStatus.DISABLED, is_active=False)
        response = self.client.get(reverse("console:users"), {"status": "disabled", "plan": "999999"})
        names = {u.username for u in response.context["page"].object_list}
        self.assertEqual(names, {"gone.away"})

    def test_a_bad_days_value_keeps_the_priority_filter(self):
        urgent = support_service.create_ticket(self.student, data={
            "category": "other", "subject": "Urgent one", "description": "Help with this now please."})
        SupportTicket.objects.filter(pk=urgent.pk).update(priority=TicketPriority.URGENT)
        support_service.create_ticket(self.student, data={
            "category": "other", "subject": "Calm one", "description": "Whenever you have a moment."})
        response = self.client.get(reverse("console:tickets"), {"days": "abc", "priority": "urgent"})
        self.assertEqual([t.subject for t in response.context["page"].object_list], ["Urgent one"])

    def test_the_ai_tokens_student_filter_does_not_change_the_console_scope(self):
        self.client.get(reverse("console:ai_tokens"), {"smodel": SONNET, "tokens": "funded"})
        self.assertNotEqual(self.client.session.get(model_scope.SESSION_KEY), SONNET)

    def test_has_tokens_works_with_the_remembered_scope(self):
        funded = make_student("funded.one")
        fund(funded, 10_000, SONNET)
        self.client.get(reverse("console:dashboard"), {"model": SONNET})       # remember Sonnet
        response = self.client.get(reverse("console:ai_tokens"), {"tokens": "funded"})
        names = {row["student"].username for row in response.context["rows"]}
        self.assertEqual(names, {"funded.one"})

    def test_the_two_ai_tokens_forms_carry_each_others_values(self):
        body = self.client.get(
            reverse("console:ai_tokens"), {"q": "sok", "hmodel": GPT, "since": "2026-01-01"},
        ).content.decode()
        self.assertIn('<input type="hidden" name="hmodel" value="gpt-5-mini">', body)
        self.assertIn('<input type="hidden" name="q" value="sok">', body)

    def test_the_usage_range_links_keep_other_filters(self):
        body = self.client.get(reverse("console:usage"), {"status": "failed", "q": "sok"}).content.decode()
        self.assertIn("status=failed", body.split("range=30")[0][-200:] + body.split("range=30")[1][:200])


# ---------------------------------------------------------------------------
# Subscriptions: extend and shorten
# ---------------------------------------------------------------------------
class SubscriptionEndDateTests(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.plan = Plan.objects.filter(is_active=True).first()
        self.sub = Subscription.objects.create(
            user=self.student, plan=self.plan, starts_at=timezone.now() - timedelta(days=5),
            ends_at=timezone.now() + timedelta(days=10),
        )

    def post(self, **data):
        return self.client.post(reverse("console:subscription_extend", args=[self.sub.pk]), data)

    def test_extending_moves_the_end_audits_and_tells_the_student(self):
        before = self.sub.ends_at
        self.post(days=14)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.ends_at - before, timedelta(days=14))
        self.assertTrue(AuditLog.objects.filter(description__startswith=f"Extended {self.plan.name} by 14").exists())
        note = Notification.objects.filter(user=self.student, title=f"{self.plan.name} extended").get()
        self.assertIn(f"{timezone.localtime(self.sub.ends_at):%d %B %Y}", note.body)

    def test_shortening_moves_it_earlier(self):
        before = self.sub.ends_at
        self.post(days=3, direction="shorten")
        self.sub.refresh_from_db()
        self.assertEqual(before - self.sub.ends_at, timedelta(days=3))
        self.assertTrue(AuditLog.objects.filter(description__startswith=f"Shortened {self.plan.name} by 3").exists())

    def test_it_cannot_end_before_it_started_or_move_an_inactive_one(self):
        before = self.sub.ends_at
        self.post(days=20, direction="shorten")
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.ends_at, before)
        Subscription.objects.filter(pk=self.sub.pk).update(status=Subscription.Status.CANCELLED)
        self.post(days=5)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.ends_at, before)

    def test_the_list_offers_it(self):
        body = self.client.get(reverse("console:subscriptions")).content.decode()
        self.assertIn(reverse("console:subscription_extend", args=[self.sub.pk]), body)


# ---------------------------------------------------------------------------
# Tickets: assign to anyone, and act in bulk
# ---------------------------------------------------------------------------
class TicketAssignTests(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.colleague = User.objects.create_superuser(
            username="second.admin", email="second@test.local", password=PASSWORD, full_name="Second Admin",
        )
        self.one = support_service.create_ticket(self.student, data={
            "category": "other", "subject": "First", "description": "The first request here."})
        self.two = support_service.create_ticket(self.student, data={
            "category": "other", "subject": "Second", "description": "The second request here."})

    def test_a_ticket_can_be_assigned_to_any_administrator(self):
        self.client.post(reverse("console:ticket_update", args=[self.one.reference]), {"assign": self.colleague.pk})
        self.one.refresh_from_db()
        self.assertEqual(self.one.assigned_to, self.colleague)
        self.assertTrue(AuditLog.objects.filter(description__contains="assigned to Second Admin").exists())

    def test_a_student_cannot_be_made_the_assignee(self):
        self.client.post(reverse("console:ticket_update", args=[self.one.reference]), {"assign": self.student.pk})
        self.one.refresh_from_db()
        self.assertIsNone(self.one.assigned_to)

    def test_bulk_close_and_bulk_assign(self):
        url = reverse("console:tickets_bulk")
        self.client.post(url, {"action": "assign", "assign": self.colleague.pk, "ids": [self.one.pk, self.two.pk]})
        self.assertEqual(set(SupportTicket.objects.values_list("assigned_to", flat=True)), {self.colleague.pk})
        self.client.post(url, {"action": "close", "ids": [self.one.pk, self.two.pk]})
        self.assertEqual(set(SupportTicket.objects.values_list("status", flat=True)), {TicketStatus.CLOSED})

    def test_the_queue_filters_by_a_named_administrator(self):
        SupportTicket.objects.filter(pk=self.two.pk).update(assigned_to=self.colleague)
        response = self.client.get(reverse("console:tickets"), {"assignee": self.colleague.pk})
        self.assertEqual([t.pk for t in response.context["page"].object_list], [self.two.pk])


# ---------------------------------------------------------------------------
# Model editor: "Best for"
# ---------------------------------------------------------------------------
class BestForTests(ConsoleCase):
    def payload(self, model, **extra):
        data = {
            "key": model.key, "name": model.name, "description": model.description,
            "provider_ref": model.provider_ref_id, "thinking_mode": model.thinking_mode,
            "max_output_tokens": model.max_output_tokens, "temperature": model.temperature,
            "token_allocation": model.token_allocation, "input_cost_per_1m": model.input_cost_per_1m,
            "output_cost_per_1m": model.output_cost_per_1m, "warning_thresholds": model.warning_thresholds,
            "sort_order": model.sort_order, "is_active": "on",
        }
        data.update(extra)
        return data

    def test_it_is_saved_trimmed(self):
        model = model_for(HAIKU)
        self.client.post(reverse("console:model_save", args=[model.pk]),
                         self.payload(model, best_for="  Quick   everyday questions "))
        model.refresh_from_db()
        self.assertEqual(model.best_for, "Quick everyday questions")

    def test_longer_than_120_is_refused(self):
        model = model_for(HAIKU)
        AIModel.objects.filter(pk=model.pk).update(best_for="Keep me")
        self.client.post(reverse("console:model_save", args=[model.pk]), self.payload(model, best_for="x" * 121))
        model.refresh_from_db()
        self.assertEqual(model.best_for, "Keep me")

    def test_a_post_without_it_leaves_it_alone(self):
        model = model_for(HAIKU)
        AIModel.objects.filter(pk=model.pk).update(best_for="Keep me")
        self.client.post(reverse("console:model_save", args=[model.pk]), self.payload(model))
        model.refresh_from_db()
        self.assertEqual(model.best_for, "Keep me")

    def test_the_editor_has_the_field(self):
        body = self.client.get(reverse("console:tokens")).content.decode()
        self.assertIn('name="best_for"', body)
        self.assertIn('maxlength="120"', body)


# ---------------------------------------------------------------------------
# "Can this student chat?"
# ---------------------------------------------------------------------------
CONFIGURED = SimpleNamespace(is_configured=True)


@patch("AI.services.providers.for_model", return_value=CONFIGURED)
class ChatExplainerTests(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.model = model_for(GPT)
        AIModel.objects.filter(pk=self.model.pk).update(
            default_daily_limit=0, default_weekly_limit=0, default_monthly_limit=0,
        )

    def verdict(self, key=GPT):
        response = self.client.get(reverse("console:user_detail", args=[self.student.pk]))
        rows = {row["model"].key: row for row in response.context["access"]["rows"]}
        return rows[key]["verdict"]

    def test_ready_when_everything_passes(self, _provider):
        fund(self.student, 20_000)
        self.assertEqual(self.verdict()["label"], "Ready")

    def test_out_of_tokens_offers_a_grant(self, _provider):
        wallet_service.get_wallet(self.student, self.model)
        verdict = self.verdict()
        self.assertEqual(verdict["label"], "Blocked: out of tokens")
        self.assertEqual(verdict["action"]["kind"], "grant")

    def test_access_turned_off_offers_the_switch(self, _provider):
        fund(self.student, 20_000)
        wallet_service.set_access(self.student, self.model, False)
        verdict = self.verdict()
        self.assertEqual(verdict["label"], "Blocked: access turned off")
        self.assertEqual(verdict["action"]["kind"], "toggle")

    def test_a_paused_model_blocks(self, _provider):
        fund(self.student, 20_000)
        AIModel.objects.filter(pk=self.model.pk).update(service_status=AIModel.ServiceStatus.PAUSED)
        self.assertEqual(self.verdict()["label"], "Blocked: model paused")

    def test_a_degraded_model_is_ready_with_a_warning(self, _provider):
        fund(self.student, 20_000)
        AIModel.objects.filter(pk=self.model.pk).update(service_status=AIModel.ServiceStatus.DEGRADED)
        verdict = self.verdict()
        self.assertEqual(verdict["state"], "warn")
        self.assertTrue(verdict["label"].startswith("Ready"))

    def test_a_disabled_account_blocks_every_model(self, _provider):
        fund(self.student, 20_000)
        User.objects.filter(pk=self.student.pk).update(status=AccountStatus.DISABLED, is_active=False)
        self.assertEqual(self.verdict()["label"], "Blocked: account disabled")

    def test_a_spent_daily_limit_blocks_with_its_reset(self, _provider):
        AIModel.objects.filter(pk=self.model.pk).update(default_daily_limit=1_000)
        fund(self.student, 20_000)
        PackageAllocation.objects.filter(user=self.student, ai_model=self.model).update(daily_used=1_000)
        verdict = self.verdict()
        self.assertEqual(verdict["label"], "Blocked: daily limit reached")
        self.assertIn("resets", verdict["detail"])

    def test_no_key_on_the_server_blocks(self, provider):
        provider.return_value = SimpleNamespace(is_configured=False)
        fund(self.student, 20_000)
        self.assertEqual(self.verdict()["label"], "Blocked: no API key on the server")

    def test_the_last_error_is_shown(self, _provider):
        fund(self.student, 20_000)
        AIUsageLog.objects.create(user=self.student, model="m", model_key=GPT, success=False,
                                  error_code="rate_limited", error_message="Vendor said slow down")
        body = self.client.get(reverse("console:user_detail", args=[self.student.pk])).content.decode()
        self.assertIn("Vendor said slow down", body)

    def test_toggle_and_rebuild_forms_are_wired(self, _provider):
        fund(self.student, 20_000)
        body = self.client.get(reverse("console:user_detail", args=[self.student.pk])).content.decode()
        self.assertIn(reverse("console:ai_toggle_access", args=[self.student.pk, self.model.pk]), body)
        self.assertIn(reverse("console:ai_recalculate", args=[self.student.pk, self.model.pk]), body)


# ---------------------------------------------------------------------------
# Distribution under a lock
# ---------------------------------------------------------------------------
class DistributionTests(ConsoleCase):
    def test_set_and_remove_move_exactly_the_difference(self):
        gpt = fund_pool(GPT)
        fund(self.student, 30_000)
        pool = model_pool_service.balance(gpt)

        result = distribution_service.distribute(self.student, gpt, 50_000, action="set", performed_by=self.admin)
        self.assertEqual(balance(self.student), 50_000)
        self.assertEqual(model_pool_service.balance(gpt), pool - 20_000)
        self.assertEqual(result.balance_before, 30_000)

        distribution_service.distribute(self.student, gpt, 80_000, action="remove", performed_by=self.admin)
        self.assertEqual(balance(self.student), 0)
        # Only what the wallet actually held goes back.
        self.assertEqual(model_pool_service.balance(gpt), pool + 30_000)


# ---------------------------------------------------------------------------
# Announcements
# ---------------------------------------------------------------------------
class AnnouncementTests(ConsoleCase):
    def test_send_now_sends_once(self):
        announcement = Announcement.objects.create(
            title="Later", body="Body", created_by=self.admin, scheduled_for=timezone.now() + timedelta(days=1),
        )
        first = notification_service.publish_now(announcement)
        second = notification_service.publish_now(Announcement.objects.get(pk=announcement.pk))
        self.assertGreaterEqual(first, 1)
        self.assertIsNone(second)
        self.assertEqual(Notification.objects.filter(announcement=announcement).count(), first)

    def test_the_page_counts_reach_in_a_fixed_number_of_queries(self):
        for index in range(3):
            Plan.objects.create(name=f"P{index}", slug=f"p-count-{index}", tokens=1, price=Decimal("1"))
        breakdown = notification_service.audience_breakdown()
        for plan in Plan.objects.all():
            self.assertEqual(
                breakdown["plan"][plan.pk],
                notification_service.audience_students(AnnouncementAudience.PLAN, plan=plan).count(),
            )
        with CaptureQueriesContext(connection) as small:
            notification_service.audience_breakdown()
        for index in range(6):
            Plan.objects.create(name=f"Q{index}", slug=f"q-count-{index}", tokens=1, price=Decimal("1"))
        with CaptureQueriesContext(connection) as large:
            notification_service.audience_breakdown()
        self.assertEqual(len(small), len(large))


# ---------------------------------------------------------------------------
# N+1s that were found
# ---------------------------------------------------------------------------
class QueryShapeTests(ConsoleCase):
    def count(self, url) -> int:
        self.client.get(url)
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(url).status_code, 200)
        return len(captured)

    def test_plans_page_does_not_grow_with_bundles(self):
        before = self.count(reverse("console:plans"))
        for index in range(5):
            plan = Plan.objects.create(name=f"B{index}", slug=f"b-shape-{index}", tokens=10,
                                       price=Decimal("2"), ai_model=model_for(GPT))
            PlanModelGrant.objects.create(plan=plan, ai_model=model_for(SONNET), tokens=5)
        self.assertLessEqual(self.count(reverse("console:plans")), before + 2)

    def test_promo_list_does_not_grow_with_codes(self):
        PromoCode.objects.create(code="ONE", value=Decimal("10"))
        before = self.count(reverse("console:promos"))
        for index in range(6):
            PromoCode.objects.create(code=f"MORE{index}", value=Decimal("5"), max_uses=3)
        self.assertLessEqual(self.count(reverse("console:promos")), before + 2)

    def test_the_usage_limits_page_writes_nothing(self):
        gpt = model_for(GPT)
        AIModel.objects.filter(pk=gpt.pk).update(default_daily_limit=5_000)
        fund(self.student, 20_000)
        allocation = PackageAllocation.objects.filter(user=self.student, ai_model=gpt).first()
        PackageAllocation.objects.filter(pk=allocation.pk).update(
            daily_used=4_000, daily_window_end=timezone.now() - timedelta(minutes=5),
        )
        self.client.get(reverse("console:usage_limits"))
        allocation.refresh_from_db()
        self.assertEqual(allocation.daily_used, 4_000)


# ---------------------------------------------------------------------------
# E-mail log: retry a failed low-token warning, and only that
# ---------------------------------------------------------------------------
@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailRetryTests(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.gpt = model_for(GPT)
        fund(self.student, 10_000)
        UserAIWallet.objects.filter(user=self.student, ai_model=self.gpt).update(balance=300, used_tokens=9_700)
        settings_service.set_value("low_token_email_enabled", True)
        self.log = EmailLog.objects.create(
            user=self.student, recipient=self.student.email, email_type=EmailType.LOW_TOKENS,
            subject="Low", ai_model=self.gpt, trigger="balance <= 10% of allocation",
            status=EmailStatus.FAILED, error="SMTP down",
        )

    def test_a_failed_warning_is_offered_and_re_sent(self):
        response = self.client.get(reverse("console:emails"))
        row = next(log for log in response.context["page"].object_list if log.pk == self.log.pk)
        self.assertTrue(row.can_retry)
        self.client.post(reverse("console:email_retry", args=[self.log.pk]))
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(EmailLog.objects.filter(status=EmailStatus.SENT, email_type=EmailType.LOW_TOKENS).exists())

    def test_not_once_the_balance_has_recovered(self):
        UserAIWallet.objects.filter(user=self.student, ai_model=self.gpt).update(balance=9_000, used_tokens=1_000)
        self.client.post(reverse("console:email_retry", args=[self.log.pk]))
        self.assertEqual(len(mail.outbox), 0)

    def test_never_for_credentials(self):
        creds = EmailLog.objects.create(
            user=self.student, recipient=self.student.email, email_type=EmailType.CREDENTIALS,
            subject="Your login", status=EmailStatus.FAILED,
        )
        self.client.post(reverse("console:email_retry", args=[creds.pk]))
        self.assertEqual(len(mail.outbox), 0)


# ---------------------------------------------------------------------------
# Wiring: things that were unreachable
# ---------------------------------------------------------------------------
class WiringTests(ConsoleCase):
    def test_referral_campaigns_are_in_the_navigation(self):
        body = self.client.get(reverse("console:dashboard")).content.decode()
        self.assertIn(f'href="{reverse("console:referral_campaigns")}"', body)

    def test_the_bell_lists_what_needs_attention(self):
        support_service.create_ticket(self.student, data={
            "category": "other", "subject": "Hello", "description": "A question about the service."})
        body = self.client.get(reverse("console:users")).content.decode()
        self.assertIn('id="attentionMenu"', body)
        self.assertIn(f'{reverse("console:tickets")}?status=open', body)

    def test_bulk_allocation_is_on_the_ai_tokens_page(self):
        body = self.client.get(reverse("console:ai_tokens")).content.decode()
        self.assertIn(f'action="{reverse("console:ai_distribute_bulk")}"', body)

    def test_the_pool_top_up_note_is_kept(self):
        gpt = model_for(GPT)
        self.client.post(reverse("console:ai_pool_topup"), {"model_id": gpt.pk, "amount": 1_000, "note": "Invoice 42"})
        self.assertTrue(gpt.pool_movements.filter(description__contains="Invoice 42").exists())

    def test_the_dashboard_revenue_question_goes_to_revenue(self):
        body = self.client.get(reverse("console:dashboard")).content.decode()
        start = body.index("How is revenue trending?")
        self.assertIn(reverse("console:reports"), body[start - 200:start])
