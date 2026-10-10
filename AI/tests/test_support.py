"""Help & Support: raising a request, working it, and who can see what."""
from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from AI.models import (
    AccountStatus,
    SupportTicket,
    TicketPriority,
    TicketReply,
    TicketStatus,
    User,
)
from AI.services import support_service
from AI.utils.uploads import InvalidUpload, validate_document

PASSWORD = "Sup3rSecret!x"

GOOD_REQUEST = {
    "full_name": "Sokha Ny",
    "email": "sokha@test.local",
    "phone": "012345678",
    "category": "payment",
    "subject": "Payment not credited",
    "description": "I paid by bank transfer two days ago and the tokens have not arrived yet.",
}


def make_student(username="asker") -> User:
    return User.objects.create_user(
        username=username, email=f"{username}@test.local", password=PASSWORD,
        is_verified=True, status=AccountStatus.ACTIVE,
    )


class SubmitRequestTests(TestCase):
    def setUp(self):
        self.student = make_student()
        self.client.force_login(self.student)

    def test_the_help_panel_renders_inside_the_app_shell(self):
        """Help is a region of the chat, so a cold load brings the shell with it."""
        response = self.client.get(reverse("student:help"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ask us anything")
        # The shell, not a page of its own.
        self.assertContains(response, 'id="panelHost"')
        self.assertContains(response, 'id="rail"')

    def test_the_help_panel_answers_a_soft_navigation_with_the_fragment_alone(self):
        response = self.client.get(reverse("student:help"), HTTP_X_APP_PANEL="1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ask us anything")
        self.assertNotContains(response, 'id="rail"')

    def test_a_request_creates_a_ticket_with_a_reference(self):
        response = self.client.post(reverse("student:submit_request"), GOOD_REQUEST, follow=True)
        self.assertEqual(response.status_code, 200)

        ticket = SupportTicket.objects.get()
        self.assertTrue(ticket.reference.startswith("TKT-"))
        self.assertEqual(ticket.user, self.student)
        self.assertEqual(ticket.status, TicketStatus.NEW)
        self.assertContains(response, ticket.reference)

    def test_money_categories_start_at_a_higher_priority(self):
        self.client.post(reverse("student:submit_request"), GOOD_REQUEST)
        self.assertEqual(SupportTicket.objects.get().priority, TicketPriority.HIGH)

    def test_an_ordinary_category_starts_normal(self):
        payload = dict(GOOD_REQUEST, category="chatbot")
        self.client.post(reverse("student:submit_request"), payload)
        self.assertEqual(SupportTicket.objects.get().priority, TicketPriority.NORMAL)

    def test_a_thin_description_is_rejected(self):
        payload = dict(GOOD_REQUEST, description="broken")
        response = self.client.post(reverse("student:submit_request"), payload)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SupportTicket.objects.exists())

    def test_an_attachment_is_stored_with_its_name_and_size(self):
        body = b"%PDF-1.7 receipt bytes"
        payload = dict(GOOD_REQUEST)
        payload["attachment"] = SimpleUploadedFile("receipt.pdf", body)

        self.client.post(reverse("student:submit_request"), payload)

        ticket = SupportTicket.objects.get()
        self.assertEqual(ticket.attachment_name, "receipt.pdf")
        self.assertEqual(ticket.attachment_size, len(body))
        # Stored under a random name, never the one the user supplied.
        self.assertNotIn("receipt.pdf", ticket.attachment.name)

    def test_an_executable_attachment_is_refused(self):
        payload = dict(GOOD_REQUEST)
        payload["attachment"] = SimpleUploadedFile("payload.exe", b"MZ\x90\x00")

        response = self.client.post(reverse("student:submit_request"), payload)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SupportTicket.objects.exists())

    def test_requests_are_rate_limited(self):
        statuses = []
        for index in range(9):
            payload = dict(GOOD_REQUEST, subject=f"Issue number {index}")
            statuses.append(self.client.post(reverse("student:submit_request"), payload).status_code)
        # The limit is 6 an hour; later attempts are turned away rather than filed.
        self.assertLessEqual(SupportTicket.objects.count(), 6)


class TicketThreadTests(TestCase):
    def setUp(self):
        self.student = make_student("threader")
        self.admin = User.objects.create_superuser(
            username="helper", email="helper@test.local", password=PASSWORD
        )
        self.ticket = support_service.create_ticket(self.student, data=GOOD_REQUEST)

    def test_a_staff_reply_moves_the_ticket_to_waiting_for_the_user(self):
        support_service.add_reply(self.ticket, self.admin, "Checking now.", from_staff=True)

        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatus.WAITING_USER)
        self.assertIsNotNone(self.ticket.first_response_at)

    def test_a_student_reply_brings_it_back_to_us(self):
        support_service.add_reply(self.ticket, self.admin, "Checking now.", from_staff=True)
        support_service.add_reply(self.ticket, self.student, "Still nothing.", from_staff=False)

        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatus.OPEN)

    def test_an_internal_note_changes_nothing_for_the_student(self):
        support_service.add_reply(
            self.ticket, self.admin, "Bank feed shows nothing.", internal=True, from_staff=True
        )

        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatus.NEW, "an internal note must not move the ticket")
        self.assertIsNone(self.ticket.first_response_at, "a note is not a reply to the student")

    def test_internal_notes_are_filtered_out_of_the_student_view(self):
        support_service.add_reply(self.ticket, self.admin, "SECRET NOTE", internal=True, from_staff=True)
        support_service.add_reply(self.ticket, self.admin, "Public answer", from_staff=True)

        student_side = support_service.visible_replies(self.ticket, for_staff=False)
        staff_side = support_service.visible_replies(self.ticket, for_staff=True)

        self.assertEqual([r.body for r in student_side], ["Public answer"])
        self.assertEqual(len(staff_side), 2)

    def test_the_students_page_never_renders_an_internal_note(self):
        support_service.add_reply(self.ticket, self.admin, "SECRET NOTE", internal=True, from_staff=True)
        self.client.force_login(self.student)

        response = self.client.get(reverse("student:ticket_detail", args=[self.ticket.reference]))

        self.assertNotContains(response, "SECRET NOTE")

    def test_resolving_stamps_the_time(self):
        support_service.set_status(self.ticket, TicketStatus.RESOLVED, actor=self.admin)
        self.ticket.refresh_from_db()
        self.assertIsNotNone(self.ticket.resolved_at)

    def test_reopening_clears_the_resolution_time(self):
        support_service.set_status(self.ticket, TicketStatus.RESOLVED)
        support_service.set_status(self.ticket, TicketStatus.OPEN)
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.resolved_at)

    def test_an_empty_reply_is_refused(self):
        with self.assertRaises(ValueError):
            support_service.add_reply(self.ticket, self.student, "   ")


class TicketAccessTests(TestCase):
    """A ticket belongs to one student. Nobody else can read or touch it."""

    def setUp(self):
        self.owner = make_student("owner")
        self.stranger = make_student("stranger")
        self.ticket = support_service.create_ticket(self.owner, data=GOOD_REQUEST)

    def test_another_student_cannot_open_it(self):
        self.client.force_login(self.stranger)
        response = self.client.get(reverse("student:ticket_detail", args=[self.ticket.reference]))
        self.assertEqual(response.status_code, 404)

    def test_another_student_cannot_reply_to_it(self):
        self.client.force_login(self.stranger)
        response = self.client.post(
            reverse("student:ticket_reply", args=[self.ticket.reference]), {"body": "hijack"}
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(TicketReply.objects.exists())

    def test_a_student_cannot_reach_the_admin_queue(self):
        self.client.force_login(self.owner)
        for url in (
            reverse("console:tickets"),
            reverse("console:ticket_detail", args=[self.ticket.reference]),
        ):
            self.assertEqual(self.client.get(url).status_code, 302, url)

    def test_a_student_cannot_change_a_ticket_status(self):
        self.client.force_login(self.owner)
        self.client.post(
            reverse("console:ticket_update", args=[self.ticket.reference]), {"status": "closed"}
        )
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatus.NEW)


class AdminQueueTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="queue", email="queue@test.local", password=PASSWORD
        )
        self.student = make_student("queued")
        self.ticket = support_service.create_ticket(self.student, data=GOOD_REQUEST)
        self.client.force_login(self.admin)

    def test_the_queue_renders_with_counts(self):
        response = self.client.get(reverse("console:tickets"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["needs_attention"], 1)
        self.assertEqual(response.context["stats"]["unanswered"], 1)

    def test_search_matches_the_reference(self):
        response = self.client.get(reverse("console:tickets"), {"q": self.ticket.reference})
        self.assertIn(self.ticket, list(response.context["page"].object_list))

    def test_filtering_by_category(self):
        response = self.client.get(reverse("console:tickets"), {"category": "chatbot"})
        self.assertNotIn(self.ticket, list(response.context["page"].object_list))

    def test_an_admin_can_reply_and_reassign(self):
        self.client.post(
            reverse("console:ticket_reply", args=[self.ticket.reference]), {"body": "On it."}
        )
        self.client.post(
            reverse("console:ticket_update", args=[self.ticket.reference]),
            {"status": "in_progress", "assign": "me"},
        )

        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatus.IN_PROGRESS)
        self.assertEqual(self.ticket.assigned_to, self.admin)


class DocumentValidationTests(TestCase):
    """Uploads are judged by content, never by filename."""

    def test_a_renamed_executable_is_refused(self):
        with self.assertRaises(InvalidUpload):
            validate_document(SimpleUploadedFile("invoice.pdf", b"MZ\x90\x00 not a pdf"))

    def test_script_bearing_types_are_refused_outright(self):
        for name in ("x.html", "x.svg", "x.js", "x.php"):
            with self.assertRaises(InvalidUpload, msg=name):
                validate_document(SimpleUploadedFile(name, b"<script>alert(1)</script>"))

    def test_an_oversized_file_is_refused(self):
        with self.assertRaises(InvalidUpload):
            validate_document(SimpleUploadedFile("big.txt", b"x" * 200), max_bytes=100)

    def test_valid_documents_pass(self):
        for name, data in (
            ("a.pdf", b"%PDF-1.7 body"),
            ("b.txt", "plain text".encode()),
            ("c.csv", b"a,b\n1,2"),
            ("d.docx", b"PK\x03\x04rest"),
        ):
            document = validate_document(SimpleUploadedFile(name, data))
            self.assertEqual(document.name, name)
