"""The daily, weekly and monthly meters move with every reply.

The bug this pins: a student who held an unmetered grant (an admin allocation
or the welcome bonus) *older* than a package they bought spent every token from
the grant, because draws were strictly oldest-first. The purchased package's
three counters therefore sat at zero however much they chatted — the limits
looked broken. Metered packages now pay first, while their windows allow.

The second half pins the chat page: every balance refresh describes the model
the student has selected, not the default one, so the meter shows the right
model's windows after a switch, a stop or an error.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from AI.models import AIModel, AllocationStatus, PackageAllocation, UserAIWallet
from AI.services import chat_service, usage_limit_service, wallet_service
from AI.tests.test_usage_limits import GPT, LimitFixture, reply

COMPLETE = "AI.services.providers.openai_provider.OpenAICompatibleProvider.complete"


class GrantThenPackageFixture(LimitFixture):
    """An older unmetered grant, then a purchased 100k package with ceilings."""

    grant = 20_000

    def setUp(self):
        super().setUp()
        # The scenario the original bug needed: an unmetered grant. Grants now
        # take the model's default ceilings, so switch those off for it.
        self.model.default_daily_limit = self.model.default_weekly_limit = 0
        self.model.default_monthly_limit = 0
        self.model.save()
        wallet_service.credit(self.student, self.model, self.grant, reason="admin grant")
        self.grant_row = PackageAllocation.objects.filter(user=self.student).latest("id")
        self.grant_row.activated_at = timezone.now() - timedelta(days=3)
        self.grant_row.save(update_fields=["activated_at"])
        self.buy()
        self.package = PackageAllocation.objects.filter(user=self.student).latest("id")
        self.assertTrue(self.package.has_limits)
        self.assertFalse(self.grant_row.has_limits)

    def refresh(self):
        self.grant_row.refresh_from_db()
        self.package.refresh_from_db()


class DrawOrderTests(GrantThenPackageFixture):

    def test_the_metered_package_pays_first_so_its_windows_move(self):
        usage_limit_service.consume(self.student, self.model, 3_000)
        self.refresh()
        self.assertEqual(self.package.daily_used, 3_000)
        self.assertEqual(self.package.weekly_used, 3_000)
        self.assertEqual(self.package.monthly_used, 3_000)
        self.assertEqual(self.grant_row.tokens_used, 0, "the grant is held back")

    def test_the_snapshot_reports_the_use(self):
        usage_limit_service.consume(self.student, self.model, 4_500)
        snap = usage_limit_service.snapshot(self.student, self.model)
        self.assertEqual(snap.windows["daily"]["used"], 4_500)
        self.assertEqual(snap.windows["daily"]["percent"], 50)

    def test_past_the_daily_ceiling_the_grant_carries_on(self):
        """The ceiling caps the package; the grant was always spendable."""
        usage_limit_service.consume(self.student, self.model, self.daily + 1_000)
        self.refresh()
        self.assertEqual(self.package.daily_used, self.daily)
        self.assertEqual(self.grant_row.tokens_used, 1_000)
        snap = usage_limit_service.snapshot(self.student, self.model)
        self.assertEqual(snap.windows["daily"]["percent"], 100)
        self.assertEqual(snap.spendable, self.grant - 1_000)
        usage_limit_service.check(self.student, self.model, required=500)

    def test_an_overshoot_still_adds_up(self):
        total = self.grant + self.tokens
        usage_limit_service.consume(self.student, self.model, total + 700)
        rows = PackageAllocation.objects.filter(user=self.student)
        self.assertEqual(sum(r.tokens_used for r in rows), total + 700)
        self.assertTrue(all(r.tokens_remaining >= 0 for r in rows))

    def test_an_exhausted_grant_is_marked_so(self):
        usage_limit_service.consume(self.student, self.model, self.daily + self.grant)
        self.refresh()
        self.assertEqual(self.grant_row.status, AllocationStatus.EXHAUSTED)
        self.assertEqual(self.package.status, AllocationStatus.ACTIVE)

    def test_a_reply_moves_the_meter_and_wallet_agrees(self):
        conversation = chat_service.create_conversation(self.student)
        with patch(COMPLETE, return_value=reply(GPT, input_tokens=1_000, output_tokens=2_000)):
            result = chat_service.send_message(self.student, conversation, "Hello")
        self.assertEqual(result["usage"]["windows"]["daily"]["used"], 3_000)
        self.refresh()
        self.assertEqual(self.package.daily_used, 3_000)
        wallet = UserAIWallet.objects.get(user=self.student, ai_model=self.model)
        held = sum(
            r.tokens_remaining for r in PackageAllocation.objects.filter(
                user=self.student, ai_model=self.model, status=AllocationStatus.ACTIVE,
            )
        )
        self.assertEqual(wallet.balance, held)


@override_settings(OPENAI_API_KEY="sk-test")
class SelectedModelPayloadTests(LimitFixture):
    """Every refresh describes the model the student is working in."""

    def setUp(self):
        super().setUp()
        self.buy()
        self.other = AIModel.objects.get(key="claude-haiku-4-5")
        wallet_service.credit(self.student, self.other, 5_000, reason="admin grant")
        self.client.force_login(self.student)

    def get_balance(self, key=""):
        url = "/api/chat/balance/" + (f"?model={key}" if key else "")
        return self.client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()

    def test_the_balance_endpoint_follows_the_selection(self):
        data = self.get_balance(self.other.key)
        self.assertEqual(data["model"], self.other.key)
        self.assertEqual(data["balance"], 5_000)
        # A grant carries the model's own default ceilings, not GPT's plan's.
        self.assertTrue(data["usage"]["metered"])
        self.assertEqual(data["usage"]["windows"]["daily"]["limit"], self.other.default_daily_limit)

        data = self.get_balance(GPT)
        self.assertEqual(data["model"], GPT)
        self.assertTrue(data["usage"]["metered"])
        self.assertEqual(data["usage"]["windows"]["daily"]["limit"], self.daily)

    def test_an_unknown_model_falls_back_to_the_default(self):
        data = self.get_balance("no-such-model")
        self.assertTrue(data["ok"])
        self.assertTrue(data["model"])

    def test_a_refusal_reports_the_model_that_was_asked_for(self):
        conversation = chat_service.create_conversation(self.student)
        allocation = PackageAllocation.objects.get(user=self.student, ai_model=self.model)
        allocation.daily_used = allocation.daily_limit
        allocation.save(update_fields=["daily_used"])
        from AI.services import settings_service

        settings_service.set_value("auto_switch_model", False)
        response = self.client.post(
            "/api/chat/send/",
            {"message": "Hi", "model": GPT, "conversation_id": conversation.pk},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 429)
        data = response.json()
        self.assertEqual(data["model"], GPT)
        self.assertIn("daily", data["usage"]["blocked"])


class ChatPageSwitchTests(LimitFixture):
    """The page carries what the switch animation needs."""

    def setUp(self):
        super().setUp()
        self.buy()
        self.client.force_login(self.student)

    def test_replies_are_tagged_with_the_model_that_wrote_them(self):
        conversation = chat_service.create_conversation(self.student)
        with patch(COMPLETE, return_value=reply(GPT)):
            chat_service.send_message(self.student, conversation, "Hello")
        body = self.client.get(f"/chat/{conversation.pk}/").content.decode()
        self.assertIn(f'data-model="{GPT}"', body)

    def test_the_switch_script_and_styles_are_present(self):
        body = self.client.get("/chat/").content.decode()
        for needle in ("function markModelSwitch", "function swapModelLabel",
                       "function refreshModelBalance", "is-swapping", "modelRing"):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)
