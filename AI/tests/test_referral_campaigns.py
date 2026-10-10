"""Referral campaigns, from clicking the link to being paid.

The expensive mistakes in a referral system are all about money and repetition:
paying a reward twice, applying a discount twice, charging a discounted price
and granting an undiscounted package, or paying out on a code that has expired.
Those are what this covers, plus the guarantee that the personal-code referrals
the platform already had keep working exactly as before.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from AI.models import (
    AccountStatus,
    AIModel,
    AIProvider,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Plan,
    Referral,
    ReferralCampaign,
    Role,
    StaffRole,
    RewardTrigger,
    RewardType,
    RewardWho,
)
from AI.services import (
    payment_service,
    referral_campaign_service,
    referral_service,
    settings_service,
    wallet_service,
)

User = get_user_model()


class ReferralCampaignTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.vendor = AIProvider.objects.create(slug="ref-v", name="Ref Vendor")
        self.gpt = AIModel.objects.create(
            key="ref-gpt", name="Ref GPT", provider="openai",
            provider_ref=self.vendor, sort_order=1, is_default=True,
        )
        self.claude = AIModel.objects.create(
            key="ref-claude", name="Ref Claude", provider="anthropic",
            provider_ref=self.vendor, sort_order=2,
        )
        self.plan = Plan.objects.create(
            name="Ref plan", slug="ref-plan", tokens=100_000,
            price=Decimal("20.00"), ai_model=self.gpt,
        )
        self.claude_plan = Plan.objects.create(
            name="Ref claude plan", slug="ref-claude-plan", tokens=50_000,
            price=Decimal("10.00"), ai_model=self.claude,
        )
        self.admin = User.objects.create_user(
            username="ref_admin", email="admin@ref.test", password="x", role=Role.ADMIN,
            staff_role=StaffRole.OWNER,
        )
        self.student = User.objects.create_user(
            username="ref_student", email="student@ref.test", password="x",
            role=Role.STUDENT, status=AccountStatus.ACTIVE,
        )

    def campaign(self, **kwargs):
        defaults = dict(
            code="AUTUMN25", name="Autumn intake", created_by=self.admin,
            referred_reward_type=RewardType.TOKENS, referred_reward_value=Decimal("5000"),
            referrer_reward_type=RewardType.NONE, referrer_reward_value=Decimal("0"),
            reward_who=RewardWho.REFERRED, reward_trigger=RewardTrigger.ON_SIGNUP,
        )
        defaults.update(kwargs)
        return ReferralCampaign.objects.create(**defaults)

    # -- validation --------------------------------------------------------
    def test_an_expired_code_is_refused_with_a_reason(self):
        self.campaign(expires_at=timezone.now() - timedelta(days=1))
        campaign, error = referral_campaign_service.validate("AUTUMN25")
        self.assertIsNone(campaign)
        self.assertIn("expired", error.lower())

    def test_a_code_that_hit_its_limit_is_refused(self):
        self.campaign(usage_limit=2, registrations=2)
        campaign, error = referral_campaign_service.validate("AUTUMN25")
        self.assertIsNone(campaign)
        self.assertIn("limit", error.lower())

    def test_a_paused_code_is_refused(self):
        self.campaign(is_active=False)
        self.assertIn("active", referral_campaign_service.validate("AUTUMN25")[1].lower())

    def test_codes_are_case_insensitive(self):
        self.campaign()
        self.assertIsNotNone(referral_campaign_service.validate("autumn25")[0])

    def test_an_unknown_code_is_not_treated_as_a_campaign_error(self):
        """It might still be somebody's personal code, so this is not an error."""
        campaign, error = referral_campaign_service.validate("NOTACODE")
        self.assertIsNone(campaign)
        self.assertEqual(error, "")

    def test_the_signup_form_resolves_both_kinds_of_code(self):
        self.campaign()
        referrer, campaign, error = referral_service.resolve_code("AUTUMN25")
        self.assertIsNone(referrer)
        self.assertIsNotNone(campaign)
        self.assertEqual(error, "")

        personal = User.objects.create_user(
            username="ref_sharer", email="sharer@ref.test", password="x", role=Role.STUDENT
        )
        referrer, campaign, error = referral_service.resolve_code(personal.referral_code)
        self.assertEqual(referrer, personal)
        self.assertIsNone(campaign)

    # -- signup ------------------------------------------------------------
    def test_a_campaign_signup_has_no_referrer(self):
        """A code on a poster was not shared by the student who used it."""
        campaign = self.campaign()
        referral = referral_campaign_service.attach(self.student, campaign)
        self.assertIsNone(referral.referrer)
        self.assertNotEqual(referral.referred_user, referral.referrer)
        campaign.refresh_from_db()
        self.assertEqual(campaign.registrations, 1)

    def test_one_account_is_referred_only_once(self):
        campaign = self.campaign()
        self.assertIsNotNone(referral_campaign_service.attach(self.student, campaign))
        self.assertIsNone(referral_campaign_service.attach(self.student, campaign))
        campaign.refresh_from_db()
        self.assertEqual(campaign.registrations, 1, "a resubmitted form is not a second signup")

    def test_a_signup_reward_pays_tokens_once(self):
        campaign = self.campaign()
        referral = referral_campaign_service.attach(self.student, campaign)
        referral_campaign_service.settle_on_signup(referral)

        self.assertEqual(wallet_service.balance_of(self.student, self.gpt), 5000)
        referral.refresh_from_db()
        self.assertEqual(referral.status, Referral.Status.REWARDED)

        # Running it again must not pay again.
        referral_campaign_service.settle_on_signup(referral)
        self.assertEqual(wallet_service.balance_of(self.student, self.gpt), 5000)

    def test_a_campaign_with_no_model_pays_in_every_currency(self):
        """Tokens are held per model, so "all models" means each of them."""
        campaign = self.campaign(ai_model=None)
        referral = referral_campaign_service.attach(self.student, campaign)
        referral_campaign_service.settle_on_signup(referral)
        self.assertEqual(wallet_service.balance_of(self.student, self.gpt), 5000)
        self.assertEqual(wallet_service.balance_of(self.student, self.claude), 5000)

    def test_a_model_specific_campaign_pays_only_that_model(self):
        campaign = self.campaign(ai_model=self.claude)
        referral = referral_campaign_service.attach(self.student, campaign)
        referral_campaign_service.settle_on_signup(referral)
        self.assertEqual(wallet_service.balance_of(self.student, self.claude), 5000)
        self.assertEqual(wallet_service.balance_of(self.student, self.gpt), 0)

    def test_the_global_bonus_is_not_paid_on_top_of_a_campaign(self):
        """A campaign overrides the platform default; it does not stack with it."""
        campaign = self.campaign()
        referral_campaign_service.attach(self.student, campaign)
        referral_service.reward_referral(self.student)
        # 5,000 from the campaign — not 5,000 plus the global referred bonus.
        self.assertEqual(wallet_service.balance_of(self.student, self.gpt), 5000)

    # -- discounts ---------------------------------------------------------
    def discount_campaign(self, **kwargs):
        defaults = dict(
            referred_reward_type=RewardType.PERCENT,
            referred_reward_value=Decimal("25"),
            reward_trigger=RewardTrigger.ON_PURCHASE,
        )
        defaults.update(kwargs)
        return self.campaign(**defaults)

    def test_a_percentage_discount_reduces_the_price_not_the_tokens(self):
        campaign = self.discount_campaign()
        referral_campaign_service.attach(self.student, campaign)

        payment = payment_service.create_payment(self.student, self.plan, PaymentMethod.ABA_PAYWAY)
        self.assertEqual(payment.amount, Decimal("15.00"))       # 25% off 20
        self.assertEqual(payment.discount_amount, Decimal("5.00"))
        self.assertEqual(payment.list_price, Decimal("20.00"))
        self.assertEqual(
            payment.tokens_to_grant, self.plan.tokens,
            "a discount changes the price, never the size of the package",
        )

    def test_a_fixed_discount_is_capped_at_the_price(self):
        """A discount bigger than the plan must not produce a negative payment."""
        campaign = self.discount_campaign(
            referred_reward_type=RewardType.FIXED, referred_reward_value=Decimal("500"),
        )
        referral_campaign_service.attach(self.student, campaign)
        payment = payment_service.create_payment(self.student, self.plan, PaymentMethod.ABA_PAYWAY)
        self.assertEqual(payment.amount, Decimal("0.00"))
        self.assertGreaterEqual(payment.amount, Decimal("0"))

    def test_a_discount_is_only_used_once(self):
        campaign = self.discount_campaign()
        referral_campaign_service.attach(self.student, campaign)

        first = payment_service.create_payment(self.student, self.plan, PaymentMethod.ABA_PAYWAY)
        self.assertEqual(first.discount_amount, Decimal("5.00"))

        second = payment_service.create_payment(self.student, self.plan, PaymentMethod.ABA_PAYWAY)
        self.assertEqual(
            second.discount_amount, Decimal("0.00"),
            "a welcome discount is an offer, not a standing price",
        )
        self.assertEqual(second.amount, self.plan.price)

    def test_a_discount_does_not_apply_to_another_model_s_plan(self):
        campaign = self.discount_campaign(ai_model=self.gpt)
        referral_campaign_service.attach(self.student, campaign)
        payment = payment_service.create_payment(
            self.student, self.claude_plan, PaymentMethod.ABA_PAYWAY
        )
        self.assertEqual(payment.discount_amount, Decimal("0.00"))
        self.assertEqual(payment.amount, self.claude_plan.price)

    def test_a_discount_respects_the_minimum_purchase(self):
        campaign = self.discount_campaign(minimum_purchase=Decimal("50.00"))
        referral_campaign_service.attach(self.student, campaign)
        payment = payment_service.create_payment(self.student, self.plan, PaymentMethod.ABA_PAYWAY)
        self.assertEqual(payment.discount_amount, Decimal("0.00"))

    def test_a_discount_respects_the_eligible_plan_list(self):
        campaign = self.discount_campaign()
        campaign.eligible_plans.add(self.claude_plan)
        referral_campaign_service.attach(self.student, campaign)
        payment = payment_service.create_payment(self.student, self.plan, PaymentMethod.ABA_PAYWAY)
        self.assertEqual(payment.discount_amount, Decimal("0.00"))

    # -- conversion --------------------------------------------------------
    def settle(self, payment):
        return payment_service.settle(payment, actor=self.admin, source="test")

    def test_a_purchase_triggered_reward_waits_for_the_payment(self):
        campaign = self.campaign(reward_trigger=RewardTrigger.ON_PURCHASE)
        referral = referral_campaign_service.attach(self.student, campaign)
        self.assertEqual(referral.status, Referral.Status.AWAITING_PURCHASE)
        self.assertEqual(wallet_service.balance_of(self.student, self.gpt), 0)

        payment = payment_service.create_payment(self.student, self.plan, PaymentMethod.ABA_PAYWAY)
        self.settle(payment)

        referral.refresh_from_db()
        self.assertEqual(referral.status, Referral.Status.REWARDED)
        self.assertEqual(referral.plan, self.plan)
        self.assertEqual(referral.purchase_amount, self.plan.price)
        # The plan's own tokens plus the referral reward.
        self.assertEqual(
            wallet_service.balance_of(self.student, self.gpt), self.plan.tokens + 5000
        )

    def test_settling_the_same_payment_twice_pays_once(self):
        """A webhook delivered twice must not pay a reward twice."""
        campaign = self.campaign(reward_trigger=RewardTrigger.ON_PURCHASE)
        referral_campaign_service.attach(self.student, campaign)
        payment = payment_service.create_payment(self.student, self.plan, PaymentMethod.ABA_PAYWAY)
        self.settle(payment)
        balance = wallet_service.balance_of(self.student, self.gpt)

        referral_campaign_service.on_payment_confirmed(payment)
        self.assertEqual(wallet_service.balance_of(self.student, self.gpt), balance)
        campaign.refresh_from_db()
        self.assertEqual(campaign.conversions, 1)

    def test_a_conversion_is_recorded_even_when_nothing_is_paid_out(self):
        """An administrator wants to know a code produced a sale regardless."""
        campaign = self.campaign(
            reward_trigger=RewardTrigger.ON_PURCHASE,
            referred_reward_type=RewardType.NONE, referred_reward_value=Decimal("0"),
            referrer_reward_type=RewardType.NONE,
        )
        referral_campaign_service.attach(self.student, campaign)
        payment = payment_service.create_payment(self.student, self.plan, PaymentMethod.ABA_PAYWAY)
        self.settle(payment)
        campaign.refresh_from_db()
        self.assertEqual(campaign.conversions, 1)
        self.assertEqual(campaign.revenue, self.plan.price)

    def test_a_purchase_below_the_minimum_converts_but_does_not_pay(self):
        campaign = self.campaign(
            reward_trigger=RewardTrigger.ON_PURCHASE, minimum_purchase=Decimal("100.00"),
        )
        referral_campaign_service.attach(self.student, campaign)
        payment = payment_service.create_payment(self.student, self.plan, PaymentMethod.ABA_PAYWAY)
        self.settle(payment)

        campaign.refresh_from_db()
        self.assertEqual(campaign.conversions, 1)
        self.assertEqual(
            wallet_service.balance_of(self.student, self.gpt), self.plan.tokens,
            "the plan's tokens, and no referral reward",
        )

    def test_stale_referrals_are_expired_rather_than_left_pending(self):
        campaign = self.campaign(
            reward_trigger=RewardTrigger.ON_PURCHASE,
            expires_at=timezone.now() - timedelta(days=1),
        )
        referral = referral_campaign_service.attach(self.student, campaign)
        self.assertEqual(referral.status, Referral.Status.AWAITING_PURCHASE)
        self.assertEqual(referral_campaign_service.expire_stale(), 1)
        referral.refresh_from_db()
        self.assertEqual(referral.status, Referral.Status.EXPIRED)

    # -- clicks and reporting ----------------------------------------------
    def test_clicks_are_counted_before_anyone_signs_up(self):
        campaign = self.campaign()
        referral_campaign_service.record_click(campaign, ip="203.0.113.4", agent="test")
        campaign.refresh_from_db()
        self.assertEqual(campaign.clicks, 1)
        self.assertEqual(campaign.registrations, 0)

    def test_the_landing_link_redirects_to_signup_with_the_code(self):
        campaign = self.campaign()
        response = self.client.get(f"/r/{campaign.code}/")
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"ref={campaign.code}", response["Location"])
        campaign.refresh_from_db()
        self.assertEqual(campaign.clicks, 1)

    def test_an_unknown_landing_link_still_reaches_a_working_signup_page(self):
        """Somebody holding an old flyer should not meet an error page."""
        response = self.client.get("/r/NOSUCHCODE/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/signup/", response["Location"])
        self.assertNotIn("ref=", response["Location"])

    def test_generated_codes_avoid_ambiguous_characters(self):
        """One of each confusable pair is dropped, so the survivor is unambiguous.

        The letters go and the digits stay: a code containing 5 is fine
        precisely because it can never contain S.
        """
        code = referral_campaign_service.generate_code()
        self.assertFalse(set(code) & set("OIS"), "codes get read off paper and retyped")

    def test_overview_reports_the_funnel(self):
        campaign = self.campaign(reward_trigger=RewardTrigger.ON_PURCHASE)
        referral_campaign_service.record_click(campaign)
        referral_campaign_service.attach(self.student, campaign)
        payment = payment_service.create_payment(self.student, self.plan, PaymentMethod.ABA_PAYWAY)
        payment_service.settle(payment, actor=self.admin, source="test")

        stats = referral_campaign_service.overview()
        self.assertEqual(stats["clicks"], 1)
        self.assertEqual(stats["registrations"], 1)
        self.assertEqual(stats["conversions"], 1)
        self.assertEqual(stats["conversion_rate"], 100)


class PersonalReferralsStillWorkTests(TestCase):
    """The referral system that existed before campaigns must be untouched."""

    def setUp(self):
        settings_service.bootstrap()
        vendor = AIProvider.objects.create(slug="pv", name="PV")
        self.model = AIModel.objects.create(
            key="p-gpt", name="P GPT", provider="openai",
            provider_ref=vendor, is_default=True, sort_order=1,
        )
        self.sharer = User.objects.create_user(
            username="p_sharer", email="sharer@p.test", password="x", role=Role.STUDENT
        )
        self.joiner = User.objects.create_user(
            username="p_joiner", email="joiner@p.test", password="x", role=Role.STUDENT
        )

    def test_a_personal_code_still_pays_both_sides(self):
        referral = referral_service.register_referral(self.joiner, self.sharer)
        self.assertIsNotNone(referral)
        self.assertIsNone(referral.campaign)

        referral_service.reward_referral(self.joiner)
        referral.refresh_from_db()
        self.assertEqual(referral.status, Referral.Status.REWARDED)

        # The bonus lands in whichever model the platform treats as default —
        # `default_model()` seeds the real catalogue, so ask it rather than
        # assuming the model this test created won the tie.
        from AI.services import model_service

        landing = model_service.default_model()
        self.assertEqual(
            wallet_service.balance_of(self.sharer, landing),
            int(settings_service.get("referral_bonus_referrer", 0)),
        )
        self.assertEqual(
            wallet_service.balance_of(self.joiner, landing),
            int(settings_service.get("referral_bonus_referred", 0)),
        )

    def test_you_still_cannot_refer_yourself(self):
        _referrer, _campaign, error = referral_service.resolve_code(
            self.sharer.referral_code, email=self.sharer.email
        )
        self.assertIn("yourself", error.lower())
