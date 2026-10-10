"""The Announcements page: who a broadcast reaches, and what the page shows.

An audience is a question about balances asked at publish time, so each of
the three is tested against students placed on either side of its line. The
pin has to reach the student's inbox in the right order, and the page has to
report how many recipients opened what was sent — otherwise the figures on
it are decoration.
"""
from __future__ import annotations

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from AI.models import (
    AccountStatus,
    Announcement,
    AnnouncementAudience,
    Notification,
    NotificationCategory,
    NotificationLevel,
    Role,
    User,
)
from AI.services import notification_service
from AI.tests.test_multi_provider import GPT, MultiProviderTestCase, PASSWORD


def student(username: str, **extra) -> User:
    defaults = {
        "email": f"{username}@test.local", "is_verified": True,
        "status": AccountStatus.ACTIVE, "role": Role.STUDENT,
    }
    defaults.update(extra)
    return User.objects.create_user(username=username, password=PASSWORD, **defaults)


class AudienceTests(MultiProviderTestCase):
    """Three students: one comfortable, one below the warning line, one at zero."""

    def setUp(self):
        super().setUp()
        self.full = student("full")
        self.low = student("low")
        self.empty = student("empty")
        # self.student (from the fixture) has no wallet at all.
        self.give(GPT, 10_000, self.full)
        self.give(GPT, 10_000, self.low)
        wallet = self.low.ai_wallets.get(ai_model=self.models[GPT])
        wallet.balance = 500  # 5% of what was allocated
        wallet.save(update_fields=["balance"])
        self.give(GPT, 1_000, self.empty)
        wallet = self.empty.ai_wallets.get(ai_model=self.models[GPT])
        wallet.balance = 0
        wallet.save(update_fields=["balance"])

    def names(self, audience) -> set[str]:
        return set(notification_service.audience_students(audience).values_list("username", flat=True))

    def test_everyone_active_is_reachable(self):
        self.assertEqual(self.names(AnnouncementAudience.ALL), {"student1", "full", "low", "empty"})

    def test_running_low_is_the_same_line_the_console_badge_uses(self):
        self.assertEqual(self.names(AnnouncementAudience.LOW), {"low", "empty"})

    def test_no_tokens_includes_students_who_were_never_given_a_wallet(self):
        self.assertEqual(self.names(AnnouncementAudience.EMPTY), {"empty", "student1"})

    def test_disabled_students_and_administrators_are_never_reached(self):
        student("gone", status=AccountStatus.DISABLED, is_active=False)
        for audience in (AnnouncementAudience.ALL, AnnouncementAudience.LOW, AnnouncementAudience.EMPTY):
            self.assertNotIn("gone", self.names(audience))
            self.assertNotIn("root", self.names(audience))

    def test_counts_agree_with_the_querysets(self):
        counts = notification_service.audience_counts()
        self.assertEqual(counts, {"all": 4, "low": 2, "empty": 2})

    def test_publishing_to_an_audience_reaches_only_that_audience(self):
        announcement = Announcement.objects.create(
            title="Top up before Monday", body="Allocate tokens.", created_by=self.admin,
            audience=AnnouncementAudience.EMPTY, is_pinned=True,
        )
        count = notification_service.publish_announcement(announcement)
        self.assertEqual(count, 2)
        delivered = Notification.objects.filter(announcement=announcement)
        self.assertEqual({n.user.username for n in delivered}, {"empty", "student1"})
        self.assertTrue(all(n.is_pinned for n in delivered), "the pin travels with each copy")
        self.assertTrue(all(n.category == NotificationCategory.ANNOUNCEMENT for n in delivered))


class PinnedInboxTests(MultiProviderTestCase):
    def test_a_pinned_broadcast_stays_first_until_it_is_opened(self):
        older = Notification.objects.create(user=self.student, title="Older", is_pinned=True)
        # Two rows created in one tick can share a timestamp on Windows, so
        # the age difference this test is about is made explicit.
        Notification.objects.filter(pk=older.pk).update(created_at=timezone.now() - timedelta(hours=1))
        newer = Notification.objects.create(user=self.student, title="Newer")
        self.assertEqual([n.title for n in notification_service.inbox(self.student)], ["Older", "Newer"])

        older.is_read = True
        older.read_at = timezone.now()
        older.save(update_fields=["is_read", "read_at"])
        self.assertEqual([n.title for n in notification_service.inbox(self.student)], ["Newer", "Older"])
        self.assertEqual(newer.pk, notification_service.inbox(self.student)[0].pk)

    def test_the_student_feed_uses_that_order_and_names_the_pin(self):
        Notification.objects.create(user=self.student, title="Pinned one", is_pinned=True)
        Notification.objects.create(user=self.student, title="Plain one")
        self.client.force_login(self.student)
        payload = self.client.get(reverse("student:notifications_feed")).json()
        titles = [row["title"] for row in payload["notifications"]]
        self.assertEqual(titles, ["Pinned one", "Plain one"])
        self.assertTrue(payload["notifications"][0]["is_pinned"])
        self.assertFalse(payload["notifications"][1]["is_pinned"])


class AnnouncementsPageTests(MultiProviderTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.url = reverse("console:notifications")

    def test_the_page_renders_the_design_with_real_counts(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        for text in (
            "Write once, and it lands in every chosen student",
            "New announcement", "STUDENT PREVIEW", "Nothing published yet",
            "Recent notifications sent", "Pin to the top of their notification centre",
            "reachable",
        ):
            self.assertContains(response, text)
        self.assertEqual(response.context["hero"]["reachable"], 1)
        self.assertEqual(response.context["hero"]["published_here"], 0)
        by_value = {aud["value"]: aud for aud in response.context["audiences"]}
        # Plan and model audiences are offered with a picker, counted per choice.
        self.assertEqual(set(by_value), {"all", "low", "empty", "plan", "model"})
        self.assertEqual(by_value["all"]["count"], 1)
        # The last level is "Critical" on this page, whatever the model calls it.
        self.assertEqual([level["label"] for level in response.context["levels"]],
                         ["Info", "Success", "Warning", "Critical"])

    def test_publishing_from_the_page_lands_and_is_listed_with_opens(self):
        response = self.client.post(self.url, {
            "title": "Scheduled maintenance this Sunday",
            "body": "We will be offline from 02:00 to 03:00.",
            "level": NotificationLevel.WARNING,
            "audience": AnnouncementAudience.ALL,
            "is_pinned": "on",
        })
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        announcement = Announcement.objects.get()
        self.assertTrue(announcement.is_published)
        self.assertTrue(announcement.is_pinned)
        self.assertEqual(announcement.audience, "all")
        self.assertEqual(announcement.recipients, 1)

        page = self.client.get(self.url)
        self.assertEqual(page.context["hero"]["published_here"], 1)
        self.assertEqual(page.context["toast"], "Published to 1 student — all active students.")
        row = page.context["published"][0]
        self.assertEqual(row.opened, 0)
        self.assertEqual(row.read_pct, 0)
        self.assertTrue(row.is_new_here)
        self.assertContains(page, "0 of 1 opened")
        self.assertContains(page, "1 published just now")

        # The student opens it: the page's open count follows.
        copy = Notification.objects.get(announcement=announcement)
        copy.is_read = True
        copy.save(update_fields=["is_read"])
        page = self.client.get(self.url)
        self.assertEqual(page.context["published"][0].opened, 1)
        self.assertEqual(page.context["published"][0].read_pct, 100)
        self.assertContains(page, "1 of 1 opened")

    def test_a_body_is_required(self):
        response = self.client.post(self.url, {"title": "No body", "body": "", "level": "info", "audience": "all"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Announcement.objects.exists())

    def test_the_toast_shows_once(self):
        self.client.post(self.url, {"title": "T", "body": "B", "level": "info", "audience": "empty"})
        self.assertIsNotNone(self.client.get(self.url).context["toast"])
        self.assertIsNone(self.client.get(self.url).context["toast"])

    def test_students_cannot_open_it(self):
        self.client.force_login(self.student)
        self.assertNotEqual(self.client.get(self.url).status_code, 200)
