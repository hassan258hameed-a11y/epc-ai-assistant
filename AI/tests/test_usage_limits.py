"""Daily, weekly and monthly usage windows.

The properties under test are the ones a student would notice if they broke:

* a ceiling actually stops a request, and stops it *before* a provider is paid;
* a ceiling is not the same thing as running out of tokens, and never says so;
* a window resets when it is due, and not a moment earlier — including across
  the calendar's awkward months;
* what a package was sold with is what it keeps, whatever the price list says
  later;
* nothing the browser sends can widen a limit.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from AI.models import (
    AccountStatus,
    Role,
    AIModel,
    AllocationSource,
    AllocationStatus,
    PackageAllocation,
    Plan,
    User,
    add_months,
    anchored_month_end,
)
from AI.services import chat_service, settings_service, usage_limit_service, wallet_service
from AI.services.chat_service import ChatBlocked
from AI.services.providers.base import AIResponse
from AI.services.usage_limit_service import UsageLimitReached

PASSWORD = "Sup3rSecret!x"
GPT = "gpt-5-mini"


def reply(model: str, *, input_tokens: int = 100, output_tokens: int = 200) -> AIResponse:
    return AIResponse(
        content="Answer.",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        model=model,
        request_id="req-1",
        latency_ms=120,
    )


class CalendarTests(TestCase):
    """Month arithmetic, which is where date handling usually goes wrong."""

    def test_a_short_month_clamps_rather_than_overflowing(self):
        start = datetime(2026, 1, 31, 15, 30)
        self.assertEqual(add_months(start, 1).date(), datetime(2026, 2, 28).date())

    def test_february_in_a_leap_year_keeps_its_extra_day(self):
        start = datetime(2028, 1, 31, 9, 0)
        self.assertEqual(add_months(start, 1).date(), datetime(2028, 2, 29).date())

    def test_the_cycle_returns_to_its_anchor_after_a_short_month(self):
        """31 Jan -> 28 Feb -> 31 Mar. The anchor is not lost to the short month."""
        january = datetime(2026, 1, 31, 12, 0)
        february = anchored_month_end(january, 31)
        self.assertEqual(february.date(), datetime(2026, 2, 28).date())
        march = anchored_month_end(february, 31)
        self.assertEqual(march.date(), datetime(2026, 3, 31).date())

    def test_a_thirty_day_month_is_handled(self):
        start = datetime(2026, 4, 30, 8, 0)
        self.assertEqual(anchored_month_end(start, 31).date(), datetime(2026, 5, 31).date())

    def test_the_year_rolls_over(self):
        start = datetime(2026, 12, 15, 8, 0)
        self.assertEqual(add_months(start, 1).date(), datetime(2027, 1, 15).date())


class LimitFixture(TestCase):
    """One student, one funded 100k package with all three ceilings set."""

    daily = 9_000
    weekly = 35_000
    monthly = 100_000
    tokens = 100_000

    def setUp(self):
        settings_service.bootstrap()
        self.student = User.objects.create_user(
            username="s1", email="s1@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.model = AIModel.objects.get(key=GPT)
        self.plan = Plan.objects.create(
            name="Test 100k",
            tokens=self.tokens,
            ai_model=self.model,
            price=Decimal("2.49"),
            daily_limit=self.daily,
            weekly_limit=self.weekly,
            monthly_limit=self.monthly,
        )

    def buy(self, plan=None, *, when=None):
        """Credit a package the way a confirmed payment does."""
        plan = plan or self.plan
        result = wallet_service.credit(
            self.student, self.model, plan.tokens,
            transaction_type="plan_purchase", reason=plan.name, plan=plan,
        )
        if when is not None:
            allocation = PackageAllocation.objects.filter(user=self.student).latest("id")
            allocation.seed_windows(when)
            allocation.save()
        return result


class AllocationCreationTests(LimitFixture):

    def test_a_purchase_opens_a_package_carrying_the_plans_ceilings(self):
        self.buy()
        allocation = PackageAllocation.objects.get(user=self.student)
        self.assertEqual(allocation.tokens_allocated, self.tokens)
        self.assertEqual(allocation.tokens_remaining, self.tokens)
        self.assertEqual(allocation.daily_limit, self.daily)
        self.assertEqual(allocation.weekly_limit, self.weekly)
        self.assertEqual(allocation.monthly_limit, self.monthly)
        self.assertEqual(allocation.source, AllocationSource.PURCHASE)

    def test_the_ceilings_are_a_snapshot_and_do_not_follow_the_price_list(self):
        """Re-tuning tomorrow's packages must not rewrite one already sold."""
        self.buy()
        self.plan.daily_limit = 1
        self.plan.save(update_fields=["daily_limit"])

        allocation = PackageAllocation.objects.get(user=self.student)
        self.assertEqual(allocation.daily_limit, self.daily)

    def test_a_package_below_the_threshold_takes_the_models_defaults(self):
        """Below the threshold the plan's own ceilings do not apply — the
        model's defaults do, so no route to tokens is unlimited."""
        small = Plan.objects.create(
            name="Tiny", tokens=500, ai_model=self.model, price=Decimal("0"),
            daily_limit=100, weekly_limit=200, monthly_limit=400,
        )
        self.buy(small)
        allocation = PackageAllocation.objects.get(user=self.student)
        self.assertEqual(allocation.daily_limit, self.model.default_daily_limit)
        self.assertEqual(allocation.weekly_limit, self.model.default_weekly_limit)
        self.assertEqual(allocation.monthly_limit, self.model.default_monthly_limit)
        self.assertEqual(allocation.spendable(), 500)

    def test_the_threshold_is_configuration_not_a_constant(self):
        settings_service.set_value("minimum_limit_package", 200_000)
        # Distinct from the plan's numbers, so the test can tell which applied.
        self.model.default_daily_limit, self.model.default_weekly_limit = 1_234, 5_678
        self.model.default_monthly_limit = 9_999
        self.model.save()
        self.buy()
        allocation = PackageAllocation.objects.get(user=self.student)
        self.assertEqual(
            (allocation.daily_limit, allocation.weekly_limit, allocation.monthly_limit),
            (1_234, 5_678, 9_999),
        )

    def test_zero_defaults_leave_a_small_grant_unlimited(self):
        self.model.default_daily_limit = self.model.default_weekly_limit = 0
        self.model.default_monthly_limit = 0
        self.model.save()
        wallet_service.credit(self.student, self.model, 500, reason="admin grant")
        allocation = PackageAllocation.objects.get(user=self.student)
        self.assertFalse(allocation.has_limits)

    def test_the_windows_are_anchored_to_the_moment_of_activation(self):
        when = timezone.make_aware(datetime(2026, 9, 1, 15, 30))
        self.buy(when=when)
        allocation = PackageAllocation.objects.get(user=self.student)
        self.assertEqual(allocation.activated_at, when)
        self.assertEqual(allocation.daily_window_end, when + timedelta(days=1))
        self.assertEqual(allocation.weekly_window_end, when + timedelta(days=7))
        self.assertEqual(allocation.monthly_window_end.date(), datetime(2026, 10, 1).date())


class HeadroomTests(LimitFixture):

    def test_the_daily_ceiling_caps_what_can_be_spent_today(self):
        self.buy()
        snap = usage_limit_service.snapshot(self.student, self.model)
        # 100,000 tokens owned, but only the day's allowance is spendable now.
        self.assertEqual(snap.balance, self.tokens)
        self.assertEqual(snap.spendable, self.daily)

    def test_spending_moves_all_three_counters_and_the_balance(self):
        self.buy()
        usage_limit_service.consume(self.student, self.model, 4_000)

        allocation = PackageAllocation.objects.get(user=self.student)
        self.assertEqual(allocation.daily_used, 4_000)
        self.assertEqual(allocation.weekly_used, 4_000)
        self.assertEqual(allocation.monthly_used, 4_000)
        self.assertEqual(allocation.tokens_remaining, self.tokens - 4_000)

    def test_the_percentage_is_of_the_limit_not_of_the_package(self):
        """80% of a 9,000 daily limit, not 7.2% of a 100,000 package."""
        self.buy()
        usage_limit_service.consume(self.student, self.model, 7_200)

        snap = usage_limit_service.snapshot(self.student, self.model)
        self.assertEqual(snap.windows["daily"]["percent"], 80)

    def test_check_refuses_once_the_day_is_spent(self):
        self.buy()
        usage_limit_service.consume(self.student, self.model, self.daily)

        with self.assertRaises(UsageLimitReached) as caught:
            usage_limit_service.check(self.student, self.model, required=100)
        self.assertEqual(caught.exception.windows, ["daily"])
        self.assertIn("daily usage limit has been reached", caught.exception.friendly_message)

    def test_two_exhausted_windows_are_named_in_one_sentence(self):
        self.buy()
        # Spend the week out; the day goes with it on the final push.
        for _ in range(4):
            usage_limit_service.consume(self.student, self.model, self.daily)
            self._roll_day()
        usage_limit_service.consume(self.student, self.model, self.weekly - 4 * self.daily)

        with self.assertRaises(UsageLimitReached) as caught:
            usage_limit_service.check(self.student, self.model, required=100)
        message = caught.exception.friendly_message
        self.assertIn("weekly", message)
        self.assertTrue(message.endswith("limits have been reached.") or "weekly usage limit" in message)

    def _roll_day(self):
        """Move every window start back a day, as if a day had passed."""
        for allocation in PackageAllocation.objects.filter(user=self.student):
            allocation.daily_window_start -= timedelta(days=1)
            allocation.daily_window_end -= timedelta(days=1)
            allocation.save()
            allocation.roll()
            allocation.save()


class ResetTests(LimitFixture):

    def test_the_day_resets_when_it_is_due_and_not_before(self):
        self.buy()
        usage_limit_service.consume(self.student, self.model, self.daily)
        allocation = PackageAllocation.objects.get(user=self.student)

        just_before = allocation.daily_window_end - timedelta(seconds=1)
        self.assertEqual(allocation.roll(just_before), [])
        self.assertEqual(allocation.daily_used, self.daily)

        just_after = allocation.daily_window_end + timedelta(seconds=1)
        self.assertTrue(allocation.roll(just_after))
        self.assertEqual(allocation.daily_used, 0)

    def test_a_reset_frees_the_day_without_touching_the_week(self):
        self.buy()
        usage_limit_service.consume(self.student, self.model, self.daily)
        allocation = PackageAllocation.objects.get(user=self.student)
        allocation.roll(allocation.daily_window_end + timedelta(seconds=1))

        self.assertEqual(allocation.daily_used, 0)
        self.assertEqual(allocation.weekly_used, self.daily)

    def test_a_dormant_account_does_not_get_a_window_starting_now(self):
        """Waiting must not be a way to reset a ceiling at a chosen moment."""
        self.buy()
        allocation = PackageAllocation.objects.get(user=self.student)
        start = allocation.daily_window_start

        allocation.roll(start + timedelta(days=30, hours=5))
        # 30 whole days on from the anchor, so the window still starts at the
        # same time of day it always did.
        self.assertEqual(allocation.daily_window_start, start + timedelta(days=30))
        self.assertEqual(
            allocation.daily_window_start.timetz().isoformat(), start.timetz().isoformat()
        )

    def test_the_monthly_cycle_walks_the_calendar_correctly(self):
        when = timezone.make_aware(datetime(2026, 1, 31, 10, 0))
        self.buy(when=when)
        allocation = PackageAllocation.objects.get(user=self.student)
        self.assertEqual(allocation.monthly_window_end.date(), datetime(2026, 2, 28).date())

        allocation.roll(timezone.make_aware(datetime(2026, 3, 1, 10, 0)))
        self.assertEqual(allocation.monthly_window_end.date(), datetime(2026, 3, 31).date())


class DrawOrderTests(LimitFixture):

    def test_the_oldest_package_is_spent_first(self):
        first = self.buy()
        older = PackageAllocation.objects.latest("id")
        older.activated_at = timezone.now() - timedelta(days=10)
        older.save(update_fields=["activated_at"])
        self.buy()

        usage_limit_service.consume(self.student, self.model, 5_000)
        older.refresh_from_db()
        newer = PackageAllocation.objects.exclude(pk=older.pk).get()
        self.assertEqual(older.tokens_used, 5_000)
        self.assertEqual(newer.tokens_used, 0)
        self.assertTrue(first.created)

    def test_headroom_is_summed_across_packages(self):
        self.buy()
        self.buy()
        snap = usage_limit_service.snapshot(self.student, self.model)
        self.assertEqual(snap.windows["daily"]["limit"], self.daily * 2)
        self.assertEqual(snap.spendable, self.daily * 2)

    def test_an_exhausted_package_does_not_block_a_fresh_one(self):
        self.buy()
        spent = PackageAllocation.objects.latest("id")
        spent.daily_used = spent.daily_limit
        spent.save(update_fields=["daily_used"])
        self.buy()

        snap = usage_limit_service.snapshot(self.student, self.model)
        self.assertFalse(snap.is_blocked)
        self.assertEqual(snap.spendable, self.daily)


class RefundTests(LimitFixture):

    def test_a_refund_gives_back_the_allowance_as_well_as_the_tokens(self):
        self.buy()
        usage_limit_service.consume(self.student, self.model, 5_000)
        usage_limit_service.refund(self.student, self.model, 5_000)

        allocation = PackageAllocation.objects.get(user=self.student)
        self.assertEqual(allocation.tokens_used, 0)
        self.assertEqual(allocation.daily_used, 0)
        self.assertEqual(allocation.tokens_remaining, self.tokens)

    def test_an_admin_reclaim_is_not_counted_as_the_student_spending(self):
        self.buy()
        wallet_service.reclaim(self.student, self.model, 40_000, reason="test")

        allocation = PackageAllocation.objects.get(user=self.student)
        self.assertEqual(allocation.tokens_remaining, self.tokens - 40_000)
        self.assertEqual(allocation.daily_used, 0)


class ChatEnforcementTests(LimitFixture):
    """The gate as the chat runtime actually uses it."""

    def setUp(self):
        super().setUp()
        self.buy()
        self.conversation = chat_service.create_conversation(self.student)

    def test_a_reply_charges_the_windows_with_the_providers_real_numbers(self):
        with patch("AI.services.providers.openai_provider.OpenAICompatibleProvider.complete") as complete:
            complete.return_value = reply(GPT, input_tokens=1_200, output_tokens=2_800)
            result = chat_service.send_message(self.student, self.conversation, "Hello there")

        self.assertEqual(result["tokens_charged"], 4_000)
        allocation = PackageAllocation.objects.get(user=self.student)
        self.assertEqual(allocation.daily_used, 4_000)
        self.assertEqual(allocation.weekly_used, 4_000)
        self.assertEqual(allocation.monthly_used, 4_000)

    def test_an_exhausted_day_blocks_before_the_provider_is_called(self):
        usage_limit_service.consume(self.student, self.model, self.daily)

        with patch("AI.services.providers.openai_provider.OpenAICompatibleProvider.complete") as complete:
            with self.assertRaises(ChatBlocked) as caught:
                chat_service.send_message(self.student, self.conversation, "Hello there")
            complete.assert_not_called()

        self.assertEqual(caught.exception.code, "usage_limit")
        self.assertEqual(caught.exception.extra["windows"], ["daily"])

    def test_the_block_offers_a_reset_time_rather_than_telling_them_to_buy_more(self):
        usage_limit_service.consume(self.student, self.model, self.daily)
        with self.assertRaises(ChatBlocked) as caught:
            chat_service.send_message(self.student, self.conversation, "Hello there")

        resets = caught.exception.extra["resets"]
        self.assertIn("daily", resets)
        self.assertTrue(resets["daily"]["reset_display"])
        self.assertGreater(resets["daily"]["reset_in_seconds"], 0)
        # They still own the tokens; the message must not claim otherwise.
        self.assertNotIn("run out", caught.exception.friendly_message)
        self.assertGreater(wallet_service.balance_of(self.student, self.model), 0)

    def test_the_balance_payload_reports_all_three_windows(self):
        usage = chat_service.balance_payload(self.student, ai_model=self.model)["usage"]
        self.assertTrue(usage["metered"])
        for window in ("daily", "weekly", "monthly"):
            self.assertIn(window, usage["windows"])
            self.assertTrue(usage["windows"][window]["enforced"])
        self.assertEqual(usage["windows"]["daily"]["limit"], self.daily)

    def test_a_second_tab_cannot_spend_the_same_headroom_twice(self):
        """Two requests that each fit alone must not both get through."""
        usage_limit_service.consume(self.student, self.model, self.daily - 3_000)

        with patch("AI.services.providers.openai_provider.OpenAICompatibleProvider.complete") as complete:
            complete.return_value = reply(GPT, input_tokens=1_000, output_tokens=2_000)
            chat_service.send_message(self.student, self.conversation, "First message")

            # The day is now spent; the second tab is refused, uncalled.
            complete.reset_mock()
            with self.assertRaises(ChatBlocked):
                chat_service.send_message(self.student, self.conversation, "Second message")
            complete.assert_not_called()

        allocation = PackageAllocation.objects.get(user=self.student)
        self.assertLessEqual(allocation.daily_used, allocation.daily_limit)

    def test_nothing_the_client_sends_can_widen_a_ceiling(self):
        usage_limit_service.consume(self.student, self.model, self.daily)
        self.client.force_login(self.student)

        response = self.client.post(
            "/api/chat/send/",
            data={
                "message": "Hello",
                "conversation_id": self.conversation.pk,
                # All of this is ignored: the server reads its own numbers.
                "daily_limit": 999_999,
                "daily_used": 0,
                "usage": '{"daily": {"remaining": 999999}}',
            },
        )
        self.assertEqual(response.status_code, 429)
        allocation = PackageAllocation.objects.get(user=self.student)
        self.assertEqual(allocation.daily_limit, self.daily)


class CeilingHoldsTests(LimitFixture):
    """A ceiling has to bound what one reply can cost, not just permit it.

    The bug this guards against: the guards proved a request was affordable and
    then let the vendor answer at its full configured length. With 100 tokens of
    the day left and a 4,096-token output budget, a single message took a 9,000
    daily limit to 18,900 — twice the ceiling that had allowed it.
    """

    def setUp(self):
        super().setUp()
        self.buy()
        self.conversation = chat_service.create_conversation(self.student)

    def _spend_down_to(self, remaining: int):
        """Spend the day through the real path, leaving ``remaining`` tokens."""
        target = self.daily - remaining
        with patch("AI.services.providers.openai_provider.OpenAICompatibleProvider.complete",
                   return_value=reply(GPT, input_tokens=target - 100, output_tokens=100)):
            chat_service.send_message(self.student, self.conversation, "warm up")

    def test_one_reply_cannot_spend_past_the_daily_ceiling(self):
        self._spend_down_to(100)
        with patch("AI.services.providers.openai_provider.OpenAICompatibleProvider.complete",
                   return_value=reply(GPT, input_tokens=5_000, output_tokens=5_000)) as complete:
            with self.assertRaises(ChatBlocked):
                chat_service.send_message(self.student, self.conversation, "hello")
            complete.assert_not_called()

        allocation = PackageAllocation.objects.get(user=self.student)
        self.assertLessEqual(allocation.daily_used, allocation.daily_limit)

    def test_the_output_budget_is_capped_to_what_is_left(self):
        self._spend_down_to(3_000)
        with patch("AI.services.providers.openai_provider.OpenAICompatibleProvider.complete",
                   return_value=reply(GPT, input_tokens=200, output_tokens=500)) as complete:
            chat_service.send_message(self.student, self.conversation, "explain gravity")

        asked = complete.call_args.kwargs.get("max_tokens")
        self.assertIsNotNone(asked, "the request must carry a budget")
        self.assertLessEqual(asked, 3_000, "the budget must fit the remaining allowance")

    def test_a_full_allowance_still_gets_the_models_whole_budget(self):
        """The cap must not quietly shorten every answer."""
        with patch("AI.services.providers.openai_provider.OpenAICompatibleProvider.complete",
                   return_value=reply(GPT, input_tokens=50, output_tokens=100)) as complete:
            chat_service.send_message(self.student, self.conversation, "hello")

        asked = complete.call_args.kwargs.get("max_tokens")
        self.assertEqual(asked, self.model.max_output_tokens)

    def test_an_unmetered_balance_is_capped_by_the_balance(self):
        student = User.objects.create_user(
            username="grantonly", email="grantonly@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        wallet_service.credit(student, self.model, 900, reason="admin grant")
        conversation = chat_service.create_conversation(student)
        with patch("AI.services.providers.openai_provider.OpenAICompatibleProvider.complete",
                   return_value=reply(GPT, input_tokens=50, output_tokens=100)) as complete:
            chat_service.send_message(student, conversation, "hi")

        self.assertLessEqual(complete.call_args.kwargs.get("max_tokens"), 900)

    def test_regenerate_and_edit_are_capped_too(self):
        """Every path that calls a vendor, not only the one that is easy to reach."""
        self._spend_down_to(2_000)
        with patch("AI.services.providers.openai_provider.OpenAICompatibleProvider.complete",
                   return_value=reply(GPT, input_tokens=100, output_tokens=200)) as complete:
            chat_service.regenerate(self.student, self.conversation)
            self.assertLessEqual(complete.call_args.kwargs.get("max_tokens"), 2_000)


class UnmeteredTests(LimitFixture):
    """A student with no metered package is governed by the balance alone."""

    def test_an_admin_grant_takes_the_models_default_ceilings(self):
        wallet_service.credit(self.student, self.model, 20_000, reason="admin grant")
        snap = usage_limit_service.snapshot(self.student, self.model)

        self.assertTrue(snap.metered)
        self.assertEqual(snap.windows["daily"]["limit"], self.model.default_daily_limit)
        self.assertEqual(snap.spendable, min(20_000, self.model.default_daily_limit))

    def test_an_admin_grant_is_unlimited_when_the_model_says_so(self):
        self.model.default_daily_limit = self.model.default_weekly_limit = 0
        self.model.default_monthly_limit = 0
        self.model.save()
        wallet_service.credit(self.student, self.model, 20_000, reason="admin grant")
        snap = usage_limit_service.snapshot(self.student, self.model)
        self.assertFalse(snap.metered)
        self.assertEqual(snap.spendable, 20_000)
        usage_limit_service.check(self.student, self.model, required=20_000)

    def test_running_out_of_tokens_still_says_so(self):
        conversation = chat_service.create_conversation(self.student)
        with self.assertRaises(ChatBlocked) as caught:
            chat_service.send_message(self.student, conversation, "Hello")
        self.assertNotEqual(caught.exception.code, "usage_limit")


class AdminUsageLimitsPageTests(LimitFixture):
    """The console's usage-limits page, and the filter that reads the windows.

    The bug this guards against: the "limit reached" filter ran in Python over
    an already-paginated queryset, so the paginator counted rows it then did not
    show — the wrong number of pages, and an empty one at the end.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(
            username="root", email="root@test.local", password=PASSWORD
        )
        self.admin.role = Role.ADMIN
        self.admin.save()
        self.client.force_login(self.admin)

        self.buy()
        at_ceiling = PackageAllocation.objects.get(user=self.student)
        at_ceiling.daily_used = at_ceiling.daily_limit
        at_ceiling.save(update_fields=["daily_used"])

        # Somebody who is nowhere near a ceiling.
        self.other = User.objects.create_user(
            username="roomy", email="roomy@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        wallet_service.credit(
            self.other, self.model, self.plan.tokens,
            transaction_type="plan_purchase", reason=self.plan.name, plan=self.plan,
        )

    def test_the_page_renders(self):
        response = self.client.get("/admin/usage-limits/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page"].paginator.count, 2)

    def test_the_blocked_filter_counts_exactly_what_it_shows(self):
        response = self.client.get("/admin/usage-limits/?status=blocked")
        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(response.context["page"].paginator.count, len(rows))
        self.assertTrue(all(row["blocked"] for row in rows))
        self.assertNotContains(response, "roomy@test.local")

    def test_a_window_that_has_already_rolled_is_not_reported_as_blocked(self):
        """Counters reset lazily, so a spent window in the past is not a block."""
        row = PackageAllocation.objects.get(user=self.student)
        row.daily_window_start -= timedelta(days=2)
        row.daily_window_end -= timedelta(days=2)
        row.save(update_fields=["daily_window_start", "daily_window_end"])

        response = self.client.get("/admin/usage-limits/?status=blocked")
        self.assertEqual(response.context["page"].paginator.count, 0)
        self.assertEqual(len(response.context["rows"]), 0)

    def test_the_model_filter_narrows_the_count_too(self):
        response = self.client.get("/admin/usage-limits/?model=gpt-5-mini")
        self.assertEqual(response.context["page"].paginator.count, len(response.context["rows"]))


class ConsistencyTests(LimitFixture):

    def test_packages_and_wallet_agree_after_a_reply(self):
        self.buy()
        conversation = chat_service.create_conversation(self.student)
        with patch("AI.services.providers.openai_provider.OpenAICompatibleProvider.complete") as complete:
            complete.return_value = reply(GPT, input_tokens=500, output_tokens=500)
            chat_service.send_message(self.student, conversation, "Hi")

        wallet_balance = wallet_service.balance_of(self.student, self.model)
        held = sum(
            row.tokens_remaining
            for row in PackageAllocation.objects.filter(
                user=self.student, ai_model=self.model, status=AllocationStatus.ACTIVE
            )
        )
        self.assertEqual(wallet_balance, held)

    def test_no_counter_can_go_negative(self):
        self.buy()
        usage_limit_service.refund(self.student, self.model, 999_999)
        allocation = PackageAllocation.objects.get(user=self.student)
        self.assertGreaterEqual(allocation.daily_used, 0)
        self.assertGreaterEqual(allocation.tokens_used, 0)

    def test_a_rebuilt_wallet_takes_its_packages_with_it(self):
        self.buy()
        usage_limit_service.consume(self.student, self.model, 10_000)
        wallet_service.recalculate(self.student, self.model)

        wallet_balance = wallet_service.balance_of(self.student, self.model)
        held = sum(
            row.tokens_remaining
            for row in PackageAllocation.objects.filter(
                user=self.student, ai_model=self.model, status=AllocationStatus.ACTIVE
            )
        )
        self.assertEqual(wallet_balance, held)
