"""The console pages that replaced Django's stock admin.

Conversations, transcripts and subscriptions are administered inside the
product, so these check both that they work and that a student can never reach
them.
"""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from AI.models import (
    AccountStatus,
    AuditLog,
    Conversation,
    Message,
    MessageRole,
    Plan,
    Subscription,
    User,
)

PASSWORD = "Sup3rSecret!x"


class ConsoleConversationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="warden", email="warden@test.local", password=PASSWORD
        )
        self.student = User.objects.create_user(
            username="talker", email="talker@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.conversation = Conversation.objects.create(
            user=self.student, title="Photosynthesis help", message_count=2, total_tokens=900,
        )
        Message.objects.create(
            conversation=self.conversation, role=MessageRole.USER, content="Explain photosynthesis",
        )
        Message.objects.create(
            conversation=self.conversation, role=MessageRole.ASSISTANT, content="Plants convert light…",
            input_tokens=300, output_tokens=600, total_tokens=900, model_key="gpt-5-mini",
        )
        self.client.force_login(self.admin)

    def test_the_list_shows_every_conversation(self):
        response = self.client.get(reverse("console:conversations"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Photosynthesis help")

    def test_search_reaches_inside_messages(self):
        response = self.client.get(reverse("console:conversations"), {"q": "convert light"})
        self.assertContains(response, "Photosynthesis help")

        response = self.client.get(reverse("console:conversations"), {"q": "quantum chromodynamics"})
        self.assertNotContains(response, "Photosynthesis help")

    def test_filtering_by_student(self):
        other = User.objects.create_user(username="quiet", email="quiet@test.local", password=PASSWORD)
        Conversation.objects.create(user=other, title="Someone else's chat")

        response = self.client.get(reverse("console:conversations"), {"student": self.student.pk})

        self.assertContains(response, "Photosynthesis help")
        self.assertNotContains(response, "Someone else&#x27;s chat")

    def test_the_transcript_shows_both_sides_and_the_cost(self):
        response = self.client.get(
            reverse("console:conversation_detail", args=[self.conversation.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Explain photosynthesis")
        self.assertContains(response, "Plants convert light")
        self.assertContains(response, "gpt-5-mini")

    def test_reading_a_transcript_is_audited(self):
        """Opening a student's private messages is a recorded action."""
        self.client.get(reverse("console:conversation_detail", args=[self.conversation.pk]))

        entry = AuditLog.objects.filter(description__contains="Viewed conversation").first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.target_id, str(self.student.pk))

    def test_deleting_a_conversation_removes_its_messages(self):
        response = self.client.post(
            reverse("console:conversation_delete", args=[self.conversation.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Conversation.objects.filter(pk=self.conversation.pk).exists())
        self.assertFalse(Message.objects.filter(conversation_id=self.conversation.pk).exists())
        self.assertTrue(AuditLog.objects.filter(description__contains="Deleted conversation").exists())

    def test_a_student_cannot_reach_the_console(self):
        self.client.force_login(self.student)

        for url in (
            reverse("console:conversations"),
            reverse("console:conversation_detail", args=[self.conversation.pk]),
            reverse("console:subscriptions"),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertEqual(response.headers["Location"], "/chat/")


class ConsoleSubscriptionTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="biller", email="biller@test.local", password=PASSWORD
        )
        self.student = User.objects.create_user(
            username="subscriber", email="sub@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        self.plan = Plan.objects.create(
            name="Starter", slug="starter-test", tokens=50_000, price=10, duration_days=30,
        )
        now = timezone.now()
        self.live = Subscription.objects.create(
            user=self.student, plan=self.plan, tokens_granted=50_000,
            starts_at=now, ends_at=now + timedelta(days=3),
        )
        self.lapsed = Subscription.objects.create(
            user=self.student, plan=self.plan, tokens_granted=10_000,
            status=Subscription.Status.EXPIRED,
            starts_at=now - timedelta(days=60), ends_at=now - timedelta(days=30),
        )
        self.client.force_login(self.admin)

    def test_the_list_renders_with_counts(self):
        response = self.client.get(reverse("console:subscriptions"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["totals"]["live"], 1)
        self.assertEqual(response.context["totals"]["lapsed"], 1)

    def test_expiring_filter_catches_the_next_seven_days(self):
        response = self.client.get(reverse("console:subscriptions"), {"status": "expiring"})
        rows = list(response.context["page"].object_list)
        self.assertEqual(rows, [self.live])

    def test_lapsed_filter_excludes_the_live_one(self):
        response = self.client.get(reverse("console:subscriptions"), {"status": "lapsed"})
        self.assertNotIn(self.live, list(response.context["page"].object_list))

    def test_cancelling_leaves_credited_tokens_alone(self):
        response = self.client.post(reverse("console:subscription_cancel", args=[self.live.pk]))

        self.assertEqual(response.status_code, 302)
        self.live.refresh_from_db()
        self.assertEqual(self.live.status, Subscription.Status.CANCELLED)
        # The student paid for those tokens; cancelling a plan does not claw them back.
        self.assertEqual(self.live.tokens_granted, 50_000)
        self.assertTrue(AuditLog.objects.filter(description__contains="Cancelled the").exists())

    def test_cancelling_an_inactive_subscription_is_a_no_op(self):
        self.client.post(reverse("console:subscription_cancel", args=[self.lapsed.pk]))
        self.lapsed.refresh_from_db()
        self.assertEqual(self.lapsed.status, Subscription.Status.EXPIRED)


class DjangoAdminIsRetiredTests(TestCase):
    """The stock admin is off unless someone deliberately switches it on."""

    def test_the_stock_admin_is_not_mounted(self):
        self.assertEqual(self.client.get("/django-admin/").status_code, 404)

    def test_the_console_nav_does_not_link_to_it(self):
        admin = User.objects.create_superuser(
            username="navcheck", email="navcheck@test.local", password=PASSWORD
        )
        self.client.force_login(admin)

        response = self.client.get(reverse("console:dashboard"))

        self.assertNotContains(response, "/django-admin/")
        self.assertContains(response, reverse("console:conversations"))
        self.assertContains(response, reverse("console:subscriptions"))
