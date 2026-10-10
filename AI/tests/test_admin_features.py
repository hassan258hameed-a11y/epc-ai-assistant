"""Admin console features: search, the revenue report, feedback triage, bulk
actions on students, and saved support replies."""
from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from AI.models import (
    AccountStatus,
    AIModel,
    CannedReply,
    FeedbackStatus,
    Message,
    MessageFeedback,
    MessageRole,
    Notification,
    Payment,
    PaymentStatus,
    Plan,
    User,
)
from AI.services import chat_service, library_service, report_service, settings_service, wallet_service
from AI.tests.helpers import GPT, fund_pool

PASSWORD = "Sup3rSecret!x"


def student(name, **extra):
    return User.objects.create_user(username=name, email=f"{name}@test.local", password=PASSWORD,
                                    is_verified=True, status=AccountStatus.ACTIVE, **extra)


class Base(TestCase):
    def setUp(self):
        settings_service.bootstrap()
        self.admin = User.objects.create_superuser(username="root", email="root@test.local", password=PASSWORD)
        self.ann = student("ann", full_name="Ann Lee")
        self.bob = student("bob")
        self.client.force_login(self.admin)


class SearchTests(Base):
    def test_finds_students_payments_and_more(self):
        plan = Plan.objects.filter(is_active=True).first()
        Payment.objects.create(user=self.ann, plan=plan, amount=Decimal("5.00"), currency="USD",
                               method="aba_payway", reference="PAY-ANN-1")
        data = self.client.get(reverse("console:search") + "?q=ann").json()
        labels = {group["label"] for group in data["groups"]}
        self.assertIn("Students", labels)
        self.assertIn("Payments", labels)
        student_urls = [item["url"] for group in data["groups"] if group["label"] == "Students" for item in group["items"]]
        self.assertIn(reverse("console:user_detail", args=[self.ann.pk]), student_urls)

    def test_a_short_query_returns_nothing_and_students_are_refused(self):
        self.assertEqual(self.client.get(reverse("console:search") + "?q=a").json()["groups"], [])
        self.client.force_login(self.ann)
        self.assertNotEqual(self.client.get(reverse("console:search") + "?q=ann").status_code, 200)


class ReportTests(Base):
    def test_net_revenue_takes_refunds_off_their_month(self):
        plan = Plan.objects.filter(is_active=True).first()
        now = timezone.now()
        Payment.objects.create(user=self.ann, plan=plan, amount=Decimal("20.00"), currency="USD", method="aba_payway",
                               status=PaymentStatus.PAID, paid_at=now, reference="P1")
        Payment.objects.create(user=self.bob, plan=plan, amount=Decimal("5.00"), currency="USD", method="binance",
                               status=PaymentStatus.REFUNDED, paid_at=now, reference="P2")
        report = report_service.revenue(3)
        this = report["months"][-1]
        self.assertEqual((this["gross"], this["refunds"], this["net"], this["payers"]),
                         (Decimal("25.00"), Decimal("5.00"), Decimal("20.00"), 1))
        self.assertEqual(report["totals"]["refund_rate"], 20.0)

    def test_the_page_and_csv_render(self):
        body = self.client.get(reverse("console:reports") + "?range=12").content.decode()
        self.assertIn("Revenue &amp; growth", body)
        self.assertIn("Do new students come back?", body)
        csv_body = self.client.get(reverse("console:reports_export") + "?range=3").content.decode()
        self.assertTrue(csv_body.startswith("Month,Gross,Refunds,Net"))
        self.assertEqual(len(csv_body.strip().splitlines()), 4)


class TriageTests(Base):
    def setUp(self):
        super().setUp()
        chat = chat_service.create_conversation(self.ann, "Maths")
        self.answer = Message.objects.create(conversation=chat, role=MessageRole.ASSISTANT, content="2+2=5",
                                             model_key=GPT)
        self.feedback = library_service.rate_message(self.ann, self.answer.pk, -1, reason="inaccurate")

    def test_review_is_recorded_without_re_dating_the_complaint(self):
        rated_at = self.feedback.updated_at
        self.client.post(reverse("console:feedback_review", args=[self.feedback.pk]),
                         {"status": "resolved", "note": "Prompt fixed"})
        self.feedback.refresh_from_db()
        self.assertEqual((self.feedback.status, self.feedback.admin_note, self.feedback.reviewed_by),
                         (FeedbackStatus.RESOLVED, "Prompt fixed", self.admin))
        self.assertEqual(self.feedback.updated_at, rated_at)

    def test_a_new_verdict_goes_back_into_the_queue(self):
        library_service.review_feedback(self.admin, self.feedback, status="resolved")
        library_service.rate_message(self.ann, self.answer.pk, -1, reason="incomplete")
        self.assertEqual(MessageFeedback.objects.get().status, FeedbackStatus.OPEN)

    def test_the_page_opens_on_what_needs_review(self):
        self.assertIn("2+2=5", self.client.get(reverse("console:quality")).content.decode())
        library_service.review_feedback(self.admin, self.feedback, status="reviewed")
        body = self.client.get(reverse("console:quality")).content.decode()
        self.assertIn("Nothing to review", body)
        self.assertIn("2+2=5", self.client.get(reverse("console:quality") + "?status=all").content.decode())


class BulkTests(Base):
    def bulk(self, action, ids, **extra):
        return self.client.post(reverse("console:users_bulk"), {"action": action, "ids": ids, **extra})

    def test_disable_and_enable_skip_administrators(self):
        self.bulk("disable", [self.ann.pk, self.bob.pk, self.admin.pk])
        self.assertEqual(set(User.objects.filter(status="disabled").values_list("username", flat=True)), {"ann", "bob"})
        self.bulk("enable", [self.ann.pk])
        self.assertEqual(User.objects.get(pk=self.ann.pk).status, "active")

    def test_export_is_a_csv_of_the_selection(self):
        response = self.bulk("export", [self.ann.pk])
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("Ann Lee", response.content.decode())
        self.assertNotIn("bob@test.local", response.content.decode())

    def test_tokens_are_reviewed_then_given(self):
        model = fund_pool(GPT)
        review = self.bulk("tokens", [self.ann.pk, self.bob.pk])
        self.assertContains(review, "Give tokens to 2")
        self.assertEqual(wallet_service.balance_of(self.ann, model), 0)
        self.bulk("tokens", [self.ann.pk, self.bob.pk], confirm="1", model=model.pk, amount="1500")
        self.assertEqual(wallet_service.balance_of(self.ann, model), 1500)
        self.assertEqual(wallet_service.balance_of(self.bob, model), 1500)

    def test_message_and_tag(self):
        self.bulk("message", [self.ann.pk, self.bob.pk], confirm="1", title="Exam week", body="Good luck!")
        self.assertEqual(Notification.objects.filter(title="Exam week").count(), 2)
        self.bulk("tag", [self.ann.pk], confirm="1", add_tag="Scholarship!")
        self.assertEqual(User.objects.get(pk=self.ann.pk).tags, ["scholarship"])
        self.assertContains(self.client.get(reverse("console:users")), "scholarship")

    def test_students_cannot_use_it(self):
        self.client.force_login(self.ann)
        self.bulk("disable", [self.bob.pk])
        self.assertEqual(User.objects.get(pk=self.bob.pk).status, "active")


class CannedReplyTests(Base):
    def test_create_edit_delete(self):
        self.client.post(reverse("console:canned_create"), {"title": "Paid?", "body": "Hi {name}, give it an hour."})
        reply = CannedReply.objects.get()
        self.client.post(reverse("console:canned_save", args=[reply.pk]), {"title": "Payment", "body": "Hi {name}."})
        reply.refresh_from_db()
        self.assertEqual(reply.title, "Payment")
        self.assertContains(self.client.get(reverse("console:canned_replies")), "Payment")
        self.client.post(reverse("console:canned_delete", args=[reply.pk]))
        self.assertFalse(CannedReply.objects.exists())

    def test_a_blank_reply_is_refused(self):
        self.client.post(reverse("console:canned_create"), {"title": "", "body": ""})
        self.assertFalse(CannedReply.objects.exists())


class AnnouncementPageTests(Base):
    def post(self, **data):
        base = {"title": "Hello", "body": "Body", "level": "info", "audience": "all"}
        base.update(data)
        return self.client.post(reverse("console:notifications"), base)

    def test_a_plan_audience_needs_a_plan(self):
        from AI.models import Announcement

        self.post(audience="plan")
        self.assertFalse(Announcement.objects.exists())

    def test_scheduling_saves_without_sending_and_send_now_sends(self):
        from datetime import timedelta

        from AI.models import Announcement

        when = (timezone.localtime() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        self.post(scheduled_for=when)
        announcement = Announcement.objects.get()
        self.assertFalse(announcement.is_published)
        self.assertEqual(Notification.objects.filter(announcement=announcement).count(), 0)
        self.client.post(reverse("console:announcement_action", args=[announcement.pk]), {"action": "send_now"})
        announcement.refresh_from_db()
        self.assertTrue(announcement.is_published)
        self.assertEqual(Notification.objects.filter(announcement=announcement).count(), 2)

    def test_a_button_must_link_inside_the_site_or_https(self):
        from AI.models import Announcement

        self.post(action_label="Go", action_url="javascript:alert(1)")
        self.assertFalse(Announcement.objects.exists())
        self.post(action_label="Plans", action_url="/app/pricing/")
        note = Notification.objects.filter(title="Hello").first()
        self.assertEqual((note.action_label, note.action_url), ("Plans", "/app/pricing/"))

    def test_retract_removes_it_from_every_inbox(self):
        from AI.models import Announcement

        self.post()
        announcement = Announcement.objects.get()
        self.client.post(reverse("console:announcement_action", args=[announcement.pk]), {"action": "retract"})
        announcement.refresh_from_db()
        self.assertIsNotNone(announcement.retracted_at)
        self.assertFalse(Notification.objects.filter(announcement=announcement).exists())


class StudentToolsTests(Base):
    def test_notes_timeline(self):
        url = reverse("console:student_note_add", args=[self.ann.pk])
        self.client.post(url, {"body": "Called about a refund."})
        from AI.models import StudentNote

        note = StudentNote.objects.get()
        self.assertEqual((note.author, note.student), (self.admin, self.ann))
        self.client.post(reverse("console:student_note_action", args=[self.ann.pk, note.pk]), {"action": "pin"})
        note.refresh_from_db()
        self.assertTrue(note.is_pinned)
        self.assertContains(self.client.get(reverse("console:user_detail", args=[self.ann.pk])), "Called about a refund.")
        self.client.post(reverse("console:student_note_action", args=[self.ann.pk, note.pk]), {"action": "delete"})
        self.assertFalse(StudentNote.objects.exists())

    def test_tags_are_cleaned_and_deduplicated(self):
        self.client.post(reverse("console:student_tags", args=[self.ann.pk]), {"tags": "Year 1, year 1, VIP!!, "})
        self.assertEqual(User.objects.get(pk=self.ann.pk).tags, ["year 1", "vip"])

    def test_message_and_grant_plan(self):
        self.client.post(reverse("console:student_message", args=[self.ann.pk]), {"title": "Hi", "body": "Welcome"})
        self.assertTrue(Notification.objects.filter(user=self.ann, title="Hi").exists())
        plan = Plan.objects.filter(is_active=True, ai_model__isnull=False).first()
        fund_pool(plan.ai_model.key)
        self.client.post(reverse("console:student_grant_plan", args=[self.ann.pk]), {"plan_id": plan.pk})
        self.assertEqual(self.ann.active_subscription.plan, plan)

    def test_sign_out_everywhere_keeps_the_account_active(self):
        from django.test import Client

        other = Client()
        other.force_login(self.ann)
        self.client.post(reverse("console:student_sign_out", args=[self.ann.pk]))
        self.assertEqual(User.objects.get(pk=self.ann.pk).status, "active")
        self.assertNotEqual(other.get(reverse("student:chat")).status_code, 200)

    def test_extend_and_remind_from_expiring(self):
        from datetime import timedelta

        from AI.models import Subscription

        plan = Plan.objects.filter(is_active=True).first()
        sub = Subscription.objects.create(user=self.ann, plan=plan, ends_at=timezone.now() + timedelta(days=3))
        before = sub.ends_at
        self.client.post(reverse("console:expiring_action", args=[sub.pk]), {"action": "extend"})
        sub.refresh_from_db()
        self.assertEqual((sub.ends_at - before).days, 30)
        self.client.post(reverse("console:expiring_action", args=[sub.pk]), {"action": "remind"})
        self.assertTrue(Notification.objects.filter(user=self.ann, title__contains="ends soon").exists())


class TicketToolsTests(Base):
    def test_a_saved_reply_counts_when_sent_and_staff_can_attach(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from AI.services import support_service

        ticket = support_service.create_ticket(self.ann, data={
            "category": "other", "subject": "Question", "description": "A question about something here."})
        canned = CannedReply.objects.create(title="Hi", body="Hi {name}!")
        upload = SimpleUploadedFile("steps.txt", b"Step one, step two.", content_type="text/plain")
        self.client.post(reverse("console:ticket_reply", args=[ticket.reference]),
                         {"body": "Hi Ann!", "canned_id": canned.pk, "attachment": upload})
        canned.refresh_from_db()
        self.assertEqual(canned.use_count, 1)
        self.assertTrue(ticket.replies.get().attachment)
        body = self.client.get(reverse("console:ticket_detail", args=[ticket.reference])).content.decode()
        self.assertIn('id="cannedPick"', body)

    def test_assignee_filter(self):
        from AI.models import SupportTicket

        SupportTicket.objects.create(user=self.ann, full_name="Ann", email="a@x.y", category="other",
                                     subject="Mine", description="x" * 30, assigned_to=self.admin)
        SupportTicket.objects.create(user=self.bob, full_name="Bob", email="b@x.y", category="other",
                                     subject="Nobody's", description="x" * 30)
        mine = self.client.get(reverse("console:tickets") + "?assignee=me").content.decode()
        self.assertIn("Mine", mine)
        self.assertNotIn("Nobody&#x27;s", mine)
