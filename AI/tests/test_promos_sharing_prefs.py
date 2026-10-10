"""Promo codes, shared conversations and custom instructions.

The properties a student or an administrator would notice if they broke:
a code that takes off the wrong amount, can be used more often than it says,
or survives a cancelled order; a shared link that shows more than was shared,
another student's conversation, or keeps working after it was revoked; and
preferences that do not reach the model, or can override the platform's rules.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from AI.models import (
    AccountStatus,
    AIModel,
    ChatPreference,
    ConversationShare,
    MessageRole,
    Payment,
    PaymentStatus,
    Plan,
    PromoCode,
    PromoRedemption,
    User,
)
from AI.services import (
    chat_service,
    payment_service,
    preference_service,
    promo_service,
    settings_service,
    share_service,
    wallet_service,
)
from AI.services.payments import PaymentError
from AI.services.providers.base import AIResponse
from AI.tests.helpers import fund

PASSWORD = "Sup3rSecret!x"
GPT = "gpt-5-mini"
COMPLETE = "AI.services.providers.openai_provider.OpenAICompatibleProvider.complete"


def student(name):
    return User.objects.create_user(
        username=name, email=f"{name}@test.local", password=PASSWORD,
        is_verified=True, status=AccountStatus.ACTIVE,
    )


def reply(content="Answer."):
    return AIResponse(content=content, input_tokens=100, output_tokens=200, total_tokens=300,
                      model=GPT, request_id="r", latency_ms=10)


# ---------------------------------------------------------------------------
# Promo codes
# ---------------------------------------------------------------------------
class PromoFixture(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.ann = student("ann")
        self.plan = Plan.objects.get(slug="gpt-5-mini-100k")          # $2.49
        self.astra = Plan.objects.get(slug="gpt-6-astra-100k")        # $14.99
        self.code = PromoCode.objects.create(code="save20", kind="percent", value=Decimal("20"))

    def buy(self, user=None, plan=None, code="SAVE20", method="aba_payway"):
        return payment_service.create_payment(user or self.ann, plan or self.astra, method, promo_code=code)


class PromoQuoteTests(PromoFixture):

    def test_codes_are_stored_and_matched_in_capitals(self):
        self.assertEqual(self.code.code, "SAVE20")
        self.assertEqual(promo_service.quote(self.ann, self.astra, " save20 ").discount, Decimal("3.00"))

    def test_percent_and_fixed_amounts(self):
        self.assertEqual(promo_service.quote(self.ann, self.astra, "SAVE20").total, Decimal("11.99"))
        PromoCode.objects.create(code="ONEOFF", kind="fixed", value=Decimal("1.50"))
        self.assertEqual(promo_service.quote(self.ann, self.astra, "ONEOFF").total, Decimal("13.49"))

    def test_a_discount_never_exceeds_the_price(self):
        PromoCode.objects.create(code="HUGE", kind="fixed", value=Decimal("500"))
        self.assertEqual(promo_service.quote(self.ann, self.plan, "HUGE").total, Decimal("0.00"))

    def test_every_refusal_says_why(self):
        now = timezone.now()
        cases = {
            "NOPE": ("unknown", {}),
            "PAUSED": ("unknown", {"is_active": False}),
            "LATER": ("scheduled", {"starts_at": now + timedelta(days=1)}),
            "GONE": ("expired", {"expires_at": now - timedelta(minutes=1)}),
            "BIGONLY": ("minimum", {"min_purchase": Decimal("100")}),
            "CLAUDE": ("wrong_model", {"ai_model": AIModel.objects.get(key="claude-opus-5")}),
        }
        for code, (expected, fields) in cases.items():
            if code != "NOPE":
                PromoCode.objects.create(code=code, kind="percent", value=Decimal("10"), **fields)
            with self.subTest(code=code):
                with self.assertRaises(promo_service.PromoError) as caught:
                    promo_service.quote(self.ann, self.astra, code)
                self.assertEqual(caught.exception.code, expected)

    def test_a_package_list_limits_the_code(self):
        self.code.eligible_plans.add(self.plan)
        with self.assertRaises(promo_service.PromoError) as caught:
            promo_service.quote(self.ann, self.astra, "SAVE20")
        self.assertEqual(caught.exception.code, "wrong_plan")
        self.assertTrue(promo_service.quote(self.ann, self.plan, "SAVE20").discount > 0)


class PromoPaymentTests(PromoFixture):

    def test_the_payment_is_charged_the_discounted_price(self):
        payment = self.buy()
        self.assertEqual(payment.amount, Decimal("11.99"))
        self.assertEqual(payment.discount_amount, Decimal("3.00"))
        self.assertEqual(payment.list_price, Decimal("14.99"))
        self.assertIn("SAVE20", payment.discount_reason)
        self.assertEqual(payment.tokens_to_grant, 100_000, "a discount changes the price, not the package")
        self.assertEqual(payment.promo_redemption.status, PromoRedemption.Status.RESERVED)

    def test_settling_uses_the_code_once(self):
        payment = self.buy()
        payment_service.settle(payment, source="test")
        payment_service.settle(payment, source="test")      # duplicate webhook
        self.assertEqual(PromoRedemption.objects.get().status, PromoRedemption.Status.USED)
        model = AIModel.objects.get(key="gpt-6-astra")
        self.assertEqual(wallet_service.balance_of(self.ann, model), 100_000)

    def test_a_student_cannot_reuse_a_one_per_student_code(self):
        payment_service.settle(self.buy(), source="test")
        with self.assertRaises(PaymentError) as caught:
            self.buy()
        self.assertEqual(caught.exception.code, "promo_already_used")

    def test_a_pending_checkout_holds_the_use(self):
        self.buy()
        with self.assertRaises(PaymentError):
            self.buy()

    def test_cancelling_gives_the_use_back(self):
        payment = self.buy()
        payment_service.cancel(payment)
        self.assertEqual(PromoRedemption.objects.get().status, PromoRedemption.Status.RELEASED)
        self.assertEqual(self.buy().amount, Decimal("11.99"))

    def test_failure_and_refund_release_it_too(self):
        failed = self.buy()
        payment_service.mark_failed(failed, "declined")
        self.assertEqual(PromoRedemption.objects.get(payment=failed).status, PromoRedemption.Status.RELEASED)
        paid = self.buy()
        payment_service.settle(paid, source="test")
        payment_service.refund(Payment.objects.get(pk=paid.pk), note="test")
        self.assertEqual(PromoRedemption.objects.get(payment=paid).status, PromoRedemption.Status.RELEASED)

    def test_an_abandoned_checkout_stops_holding_after_a_day(self):
        payment = self.buy()
        PromoRedemption.objects.filter(payment=payment).update(
            created_at=timezone.now() - timedelta(hours=25)
        )
        self.assertEqual(self.buy().discount_amount, Decimal("3.00"))

    def test_the_last_use_goes_to_exactly_one_student(self):
        self.code.max_uses = 1
        self.code.per_user_limit = 0
        self.code.save()
        self.buy()
        with self.assertRaises(PaymentError) as caught:
            self.buy(user=student("bob"))
        self.assertEqual(caught.exception.code, "promo_used_up")

    def test_an_invalid_code_creates_no_payment(self):
        with self.assertRaises(PaymentError):
            self.buy(code="NOPE")
        self.assertFalse(Payment.objects.exists())

    def test_no_code_behaves_exactly_as_before(self):
        payment = payment_service.create_payment(self.ann, self.astra, "aba_payway")
        self.assertEqual(payment.amount, Decimal("14.99"))
        self.assertFalse(payment.was_discounted)


class PromoCheckoutPageTests(PromoFixture):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.ann)

    def test_the_preview_endpoint(self):
        url = f"/checkout/{self.astra.slug}/promo/?code=save20"
        data = self.client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        self.assertEqual((data["total"], data["discount"], data["code"]), ("11.99", "3.00", "SAVE20"))
        bad = self.client.get(f"/checkout/{self.astra.slug}/promo/?code=nope", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(bad.status_code, 400)

    def test_the_preview_is_rate_limited(self):
        url = f"/checkout/{self.astra.slug}/promo/?code=guess"
        statuses = {self.client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code for _ in range(25)}
        self.assertIn(429, statuses)

    def test_the_checkout_page_offers_a_code_box(self):
        body = self.client.get(f"/checkout/{self.astra.slug}/").content.decode()
        self.assertIn('name="promo_code"', body)

    @patch("AI.services.payment_service.start_checkout")
    def test_checking_out_with_a_code(self, start):
        response = self.client.post(f"/checkout/{self.astra.slug}/", {"method": "aba_payway", "promo_code": "save20"})
        payment = Payment.objects.get()
        self.assertRedirects(response, f"/payments/{payment.reference}/", fetch_redirect_response=False)
        self.assertEqual(payment.amount, Decimal("11.99"))

    def test_a_bad_code_returns_to_checkout_with_the_reason(self):
        response = self.client.post(f"/checkout/{self.astra.slug}/", {"method": "aba_payway", "promo_code": "nope"})
        self.assertRedirects(response, f"/checkout/{self.astra.slug}/", fetch_redirect_response=False)
        self.assertFalse(Payment.objects.exists())

    def test_a_free_order_completes_without_a_gateway(self):
        PromoCode.objects.create(code="FREE", kind="percent", value=Decimal("100"))
        response = self.client.post(f"/checkout/{self.plan.slug}/", {"method": "aba_payway", "promo_code": "FREE"})
        payment = Payment.objects.get()
        self.assertEqual(payment.status, PaymentStatus.PAID)
        self.assertEqual(payment.amount, Decimal("0.00"))
        self.assertRedirects(response, f"/payments/{payment.reference}/receipt/", fetch_redirect_response=False)
        self.assertEqual(wallet_service.balance_of(self.ann, AIModel.objects.get(key=GPT)), 100_000)
        receipt = self.client.get(f"/payments/{payment.reference}/receipt/").content.decode()
        self.assertIn("Discount", receipt)


class PromoConsoleTests(PromoFixture):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(username="root", email="root@test.local", password=PASSWORD)
        self.client.force_login(self.admin)

    def test_create_edit_pause(self):
        response = self.client.post("/admin/promos/new/", {
            "code": "SAVE!!", "kind": "fixed", "value": "2.50", "max_uses": 100,
            "per_user_limit": 1, "min_purchase": "0", "is_active": "on",
        })
        self.assertEqual(response.status_code, 200, "punctuation is refused, and the form shown again")
        self.assertFalse(PromoCode.objects.filter(code="SAVE!!").exists())
        # Spaces are simply removed: "back to school" is a usable code.
        self.client.post("/admin/promos/new/", {
            "code": "back to school", "kind": "fixed", "value": "2.50", "max_uses": 100,
            "per_user_limit": 1, "min_purchase": "0", "is_active": "on",
        })
        self.assertTrue(PromoCode.objects.filter(code="BACKTOSCHOOL").exists())
        response = self.client.post("/admin/promos/new/", {
            "code": "school25", "kind": "percent", "value": "25", "max_uses": 100,
            "per_user_limit": 1, "min_purchase": "0", "is_active": "on",
        })
        promo = PromoCode.objects.get(code="SCHOOL25")
        self.assertRedirects(response, f"/admin/promos/{promo.pk}/", fetch_redirect_response=False)
        for url in ("/admin/promos/", f"/admin/promos/{promo.pk}/", f"/admin/promos/{promo.pk}/edit/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(f"/admin/promos/{promo.pk}/toggle/")
        promo.refresh_from_db()
        self.assertFalse(promo.is_active)

    def test_a_percentage_over_100_is_refused(self):
        self.client.post("/admin/promos/new/", {"code": "TOOMUCH", "kind": "percent", "value": "150",
                                                "max_uses": 0, "per_user_limit": 1, "min_purchase": "0"})
        self.assertFalse(PromoCode.objects.filter(code="TOOMUCH").exists())

    def test_the_figures_count_paid_orders_only(self):
        payment_service.settle(self.buy(), source="test")
        self.buy(user=student("bob"))                       # pending, not counted
        overview = promo_service.overview()
        self.assertEqual(overview["redemptions"], 1)
        self.assertEqual(overview["discount"], Decimal("3.00"))
        body = self.client.get(f"/admin/promos/{self.code.pk}/").content.decode()
        self.assertIn("Reserved", body)
        self.assertIn("Used", body)

    def test_students_cannot_reach_it(self):
        self.client.force_login(self.ann)
        self.assertEqual(self.client.get("/admin/promos/").status_code, 302)


# ---------------------------------------------------------------------------
# Shared conversations
# ---------------------------------------------------------------------------
class ShareFixture(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.ann = student("ann")
        fund(self.ann, 20_000)
        self.conversation = chat_service.create_conversation(self.ann)
        with patch(COMPLETE, return_value=reply("First answer")):
            chat_service.send_message(self.ann, self.conversation, "First question")
        self.client.force_login(self.ann)

    def share(self, action="share", conversation=None):
        pk = (conversation or self.conversation).pk
        return self.client.post(f"/api/chat/{pk}/share/", {"action": action}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")


class ShareTests(ShareFixture):

    def test_a_link_shows_the_conversation_to_anyone(self):
        url = self.share().json()["url"]
        self.client.logout()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("First question", body)
        self.assertIn("First answer", body)
        self.assertIn('name="robots" content="noindex"', body)

    def test_it_is_a_snapshot(self):
        url = self.share().json()["url"]
        with patch(COMPLETE, return_value=reply("Later answer")):
            chat_service.send_message(self.ann, self.conversation, "Later question")
        self.assertNotIn("Later question", self.client.get(url).content.decode())
        self.share()                                          # update the link
        self.assertIn("Later question", self.client.get(url).content.decode())

    def test_revoking_kills_the_link(self):
        url = self.share().json()["url"]
        self.share("revoke")
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_another_students_conversation_cannot_be_shared(self):
        self.client.force_login(student("bob"))
        self.assertEqual(self.share().status_code, 404)
        self.assertFalse(ConversationShare.objects.exists())

    def test_turning_sharing_off_hides_everything(self):
        url = self.share().json()["url"]
        settings_service.set_value("allow_chat_sharing", False)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.share().status_code, 400)
        self.assertNotIn('id="shareBtn"', self.client.get(f"/chat/{self.conversation.pk}/").content.decode())

    def test_views_are_counted(self):
        url = self.share().json()["url"]
        self.client.get(url)
        self.client.get(url)
        self.assertEqual(ConversationShare.objects.get().view_count, 2)

    def test_an_unknown_token_is_404(self):
        self.assertEqual(self.client.get("/share/not-a-real-token/").status_code, 404)

    def test_content_is_data_not_markup(self):
        with patch(COMPLETE, return_value=reply("<script>alert(1)</script>")):
            chat_service.send_message(self.ann, self.conversation, "hi")
        url = self.share().json()["url"]
        body = self.client.get(url).content.decode()
        self.assertNotIn("<script>alert(1)</script>", body)

    def test_an_admin_can_revoke_a_link(self):
        self.share()
        admin = User.objects.create_superuser(username="root", email="root@test.local", password=PASSWORD)
        self.client.force_login(admin)
        page = self.client.get(f"/admin/conversations/{self.conversation.pk}/").content.decode()
        self.assertIn("Shared publicly", page)
        self.client.post(f"/admin/conversations/{self.conversation.pk}/unshare/")
        self.assertFalse(ConversationShare.objects.get().is_active)

    def test_the_chat_header_has_share_and_download(self):
        body = self.client.get(f"/chat/{self.conversation.pk}/").content.decode()
        self.assertIn('id="shareBtn"', body)
        self.assertIn('id="downloadBtn"', body)
        self.assertIn('id="shareDialog"', body)
        self.assertIn('id="keysDialog"', body)


# ---------------------------------------------------------------------------
# Custom instructions
# ---------------------------------------------------------------------------
class PreferenceTests(ShareFixture):

    def save(self, **fields):
        data = {"action": "personalise", "enabled": "on", "style": "concise",
                "about_me": "", "instructions": "", "language": ""}
        data.update(fields)
        return self.client.post("/app/settings/", data)

    def test_preferences_reach_the_model(self):
        self.save(about_me="I study nursing.", instructions="Use simple words.", language="Khmer")
        with patch(COMPLETE, return_value=reply()) as vendor:
            chat_service.send_message(self.ann, self.conversation, "Explain osmosis")
        prompt = vendor.call_args.kwargs["system_prompt"]
        self.assertIn("I study nursing.", prompt)
        self.assertIn("Use simple words.", prompt)
        self.assertIn("Khmer", prompt)
        self.assertIn("Keep answers short", prompt)
        base = settings_service.get("ai_system_prompt", settings_service.DEFAULT_SYSTEM_PROMPT)
        self.assertTrue(prompt.startswith(base), "the platform's prompt always comes first")
        self.assertIn("always take priority", prompt)

    def test_switched_off_means_the_default_prompt(self):
        self.save(about_me="I study nursing.", enabled="")
        with patch(COMPLETE, return_value=reply()) as vendor:
            chat_service.send_message(self.ann, self.conversation, "hi")
        self.assertNotIn("nursing", vendor.call_args.kwargs["system_prompt"])

    def test_an_administrator_can_turn_the_feature_off(self):
        self.save(about_me="I study nursing.")
        settings_service.set_value("custom_instructions_enabled", False)
        with patch(COMPLETE, return_value=reply()) as vendor:
            chat_service.send_message(self.ann, self.conversation, "hi")
        self.assertNotIn("nursing", vendor.call_args.kwargs["system_prompt"])
        self.assertNotIn('value="personalise"', self.client.get("/app/settings/").content.decode())

    def test_one_students_preferences_never_reach_another(self):
        self.save(about_me="Ann's secret detail")
        bob = student("bob")
        fund(bob, 10_000)
        conversation = chat_service.create_conversation(bob)
        with patch(COMPLETE, return_value=reply()) as vendor:
            chat_service.send_message(bob, conversation, "hi")
        self.assertNotIn("Ann's secret", vendor.call_args.kwargs["system_prompt"])

    def test_limits_are_enforced(self):
        self.save(about_me="x" * 700)
        self.assertFalse(ChatPreference.objects.filter(user=self.ann).exists())

    def test_the_settings_panel_shows_what_was_saved(self):
        self.save(about_me="I study nursing.", style="steps")
        body = self.client.get("/app/settings/").content.decode()
        self.assertIn("I study nursing.", body)
        self.assertIn('value="steps" checked', body)
