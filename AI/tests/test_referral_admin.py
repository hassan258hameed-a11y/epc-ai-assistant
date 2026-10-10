"""The referral campaign form and its console pages, driven as a browser would.

A form that renders is not a form that works. These post real field names and
check what reaches the database, because the failures worth catching here are
the silent ones: a multiple-choice field that renders as a text box and drops
its value, or a datetime the form cannot parse back.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from AI.models import (
    AccountStatus,
    AIModel,
    AIProvider,
    Plan,
    ReferralCampaign,
    Role,
    StaffRole,
    RewardTrigger,
    RewardType,
    RewardWho,
)
from AI.services import settings_service

User = get_user_model()


class ReferralCampaignFormTests(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        vendor = AIProvider.objects.create(slug="rav", name="RAV")
        self.gpt = AIModel.objects.create(
            key="ra-gpt", name="RA GPT", provider="openai", provider_ref=vendor, sort_order=1
        )
        self.claude = AIModel.objects.create(
            key="ra-claude", name="RA Claude", provider="anthropic", provider_ref=vendor, sort_order=2
        )
        self.plan_a = Plan.objects.create(
            name="RA plan A", slug="ra-a", tokens=1000, price=Decimal("10.00"), ai_model=self.gpt
        )
        self.plan_b = Plan.objects.create(
            name="RA plan B", slug="ra-b", tokens=2000, price=Decimal("20.00"), ai_model=self.gpt
        )
        self.other_plan = Plan.objects.create(
            name="RA other", slug="ra-o", tokens=500, price=Decimal("5.00"), ai_model=self.claude
        )
        self.admin = User.objects.create_user(
            username="ra_admin", email="admin@ra.test", password="pw",
            role=Role.ADMIN, staff_role=StaffRole.OWNER, status=AccountStatus.ACTIVE, is_staff=True,
        )
        self.client.force_login(self.admin)

    def payload(self, **overrides):
        data = {
            "code": "SPRING26",
            "name": "Spring intake",
            "description": "",
            "is_active": "on",
            "starts_at": "2026-03-01T09:00",
            "expires_at": "2026-06-01T09:00",
            "usage_limit": "50",
            "ai_model": str(self.gpt.pk),
            "eligible_plans": [str(self.plan_a.pk), str(self.plan_b.pk)],
            "minimum_purchase": "0",
            "reward_who": RewardWho.REFERRED,
            "reward_trigger": RewardTrigger.ON_SIGNUP,
            "referrer_reward_type": RewardType.NONE,
            "referrer_reward_value": "0",
            "referred_reward_type": RewardType.TOKENS,
            "referred_reward_value": "5000",
        }
        data.update(overrides)
        return data

    def test_creating_a_campaign_saves_every_field(self):
        """Including the two the shared field partial used to render as text."""
        response = self.client.post(
            reverse("console:referral_campaign_create"), self.payload(), follow=True
        )
        self.assertEqual(response.status_code, 200)

        campaign = ReferralCampaign.objects.get(code="SPRING26")
        self.assertEqual(campaign.name, "Spring intake")
        self.assertEqual(campaign.usage_limit, 50)
        self.assertEqual(campaign.ai_model, self.gpt)
        self.assertEqual(campaign.created_by, self.admin)
        self.assertEqual(campaign.referred_reward_value, Decimal("5000"))

        # The multiple-choice field: it rendered as a plain text box before the
        # partial learned about SelectMultiple, so nothing was ever saved.
        self.assertEqual(
            {p.pk for p in campaign.eligible_plans.all()},
            {self.plan_a.pk, self.plan_b.pk},
        )
        # And the datetimes parsed back out of the browser's format.
        self.assertIsNotNone(campaign.expires_at)
        self.assertEqual(campaign.starts_at.year, 2026)
        self.assertEqual(campaign.starts_at.month, 3)

    def test_the_edit_form_preselects_the_chosen_plans(self):
        """A multi-select that forgets its selection loses data on every save."""
        self.client.post(reverse("console:referral_campaign_create"), self.payload())
        campaign = ReferralCampaign.objects.get(code="SPRING26")

        response = self.client.get(
            reverse("console:referral_campaign_edit", args=[campaign.pk])
        )
        html = response.content.decode()
        self.assertIn(f'value="{self.plan_a.pk}" selected', html)
        self.assertIn(f'value="{self.plan_b.pk}" selected', html)
        self.assertNotIn(f'value="{self.other_plan.pk}" selected', html)

    def test_a_code_is_stored_upper_case_whatever_was_typed(self):
        self.client.post(reverse("console:referral_campaign_create"),
                         self.payload(code="lowercase1"))
        self.assertTrue(ReferralCampaign.objects.filter(code="LOWERCASE1").exists())

    def test_a_duplicate_code_is_refused(self):
        self.client.post(reverse("console:referral_campaign_create"), self.payload())
        response = self.client.post(
            reverse("console:referral_campaign_create"), self.payload(name="Second")
        )
        self.assertEqual(response.status_code, 200)          # redisplayed, not saved
        self.assertEqual(ReferralCampaign.objects.filter(code="SPRING26").count(), 1)

    def test_a_discount_over_one_hundred_percent_is_refused(self):
        response = self.client.post(
            reverse("console:referral_campaign_create"),
            self.payload(
                referred_reward_type=RewardType.PERCENT, referred_reward_value="150",
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ReferralCampaign.objects.filter(code="SPRING26").exists())
        self.assertContains(response, "100 or less")

    def test_an_end_before_the_start_is_refused(self):
        response = self.client.post(
            reverse("console:referral_campaign_create"),
            self.payload(starts_at="2026-06-01T09:00", expires_at="2026-03-01T09:00"),
        )
        self.assertFalse(ReferralCampaign.objects.filter(code="SPRING26").exists())
        self.assertContains(response, "after the start")

    def test_plans_from_another_model_are_refused(self):
        response = self.client.post(
            reverse("console:referral_campaign_create"),
            self.payload(eligible_plans=[str(self.other_plan.pk)]),
        )
        self.assertFalse(ReferralCampaign.objects.filter(code="SPRING26").exists())
        self.assertContains(response, "do not belong to")

    def test_a_discount_campaign_is_moved_to_pay_on_purchase(self):
        """There is nothing to discount at signup, so the trigger is corrected."""
        self.client.post(
            reverse("console:referral_campaign_create"),
            self.payload(
                referred_reward_type=RewardType.PERCENT, referred_reward_value="25",
                reward_trigger=RewardTrigger.ON_SIGNUP,
            ),
        )
        # The form reports the correction rather than saving a nonsensical rule.
        self.assertFalse(ReferralCampaign.objects.filter(code="SPRING26").exists())

    def test_pausing_a_campaign_keeps_its_history(self):
        self.client.post(reverse("console:referral_campaign_create"), self.payload())
        campaign = ReferralCampaign.objects.get(code="SPRING26")
        campaign.registrations = 7
        campaign.save(update_fields=["registrations"])

        self.client.post(reverse("console:referral_campaign_toggle", args=[campaign.pk]))
        campaign.refresh_from_db()
        self.assertFalse(campaign.is_active)
        self.assertEqual(campaign.registrations, 7, "pausing must never be a delete")
        self.assertTrue(ReferralCampaign.objects.filter(pk=campaign.pk).exists())

    def test_the_console_pages_render(self):
        self.client.post(reverse("console:referral_campaign_create"), self.payload())
        campaign = ReferralCampaign.objects.get(code="SPRING26")
        for url in (
            reverse("console:referral_campaigns"),
            reverse("console:referral_campaign_detail", args=[campaign.pk]),
            reverse("console:referral_campaign_edit", args=[campaign.pk]),
        ):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_a_student_cannot_reach_the_campaign_pages(self):
        student = User.objects.create_user(
            username="ra_student", email="s@ra.test", password="pw",
            role=Role.STUDENT, status=AccountStatus.ACTIVE,
        )
        self.client.force_login(student)
        response = self.client.get(reverse("console:referral_campaigns"))
        self.assertNotEqual(response.status_code, 200)
